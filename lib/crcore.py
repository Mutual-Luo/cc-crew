"""cc-crew 核心：发现本机的 Claude Code / Codex 会话、提取回复正文、遥控（tmux / ccm，见 crterm.py）、命令解析、监控。

Claude 的状态来源是 Claude Code 自己维护的 ~/.claude/sessions/<pid>.json（sessionId / cwd / tmux / busy|idle），
回复正文来自对话记录 ~/.claude/projects/*/<sessionId>.jsonl，不靠识别屏幕。
Codex 员工的发现、对话记录、屏幕上的框在 crcodex.py 里，整理成跟 Claude 会话一样的字段（多一个 agent: "codex"），
这里按 agent 分派；两种员工的差别（能做什么）见 cragents.py。
"""
import glob
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime
from urllib.parse import urlparse

import psutil

import crcodex
import crterm
from crterm import tmux

# 路径都可以用环境变量改（安装时写进 cc-crew 的 env 文件）；不设就用各系统的默认位置
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
# 另外几个配置目录里的 claude 也算员工（比如走第三方 API 的，配置和正版账号分开放），冒号分隔
CLAUDE_DIRS = [CLAUDE_DIR] + [d for d in os.environ.get("CLAUDE_MONITOR_EXTRA_CLAUDE_DIRS", "").split(":")
                              if d and os.path.realpath(d) != os.path.realpath(CLAUDE_DIR)]
FILES_ROOT = os.path.normpath(os.environ.get("CLAUDE_MONITOR_FILES_ROOT") or os.path.expanduser("~"))   # 员工只在这下面干活，也只把这下面的文件发到手机上
PROXY = os.environ.get("CLAUDE_MONITOR_PROXY", "")   # 「127.0.0.1:10000」：claude 必须经这个代理上网，不通就不启动；不设就不查
DATA = os.environ.get("CLAUDE_MONITOR_DATA") or os.path.join(ROOT, "data")
QUIET_PREFIXES = tuple(p for p in os.environ.get("CLAUDE_MONITOR_QUIET_PREFIXES", "").split(",") if p)   # 自动任务会话的名字前缀，逗号分隔
REGISTRY = f"{DATA}/registry.json"
STATE = f"{DATA}/state.json"
OUTBOX = f"{DATA}/outbox.jsonl"
EVENTS = f"{DATA}/events.jsonl"
PENDING = f"{DATA}/pending_cards.json"          # 会话名 -> “思考中”卡片的消息 id（飞书守护进程维护）
UPLOADS = f"{DATA}/uploads"                     # 你从飞书发来的图片 / 文件
STATUS_DIR = f"{DATA}/status"
BADGES = f"{DATA}/badges.json"            # 工号：{名字: 号}，入职时分配，开除时收回                   # 状态栏命令 cr-statusline 记下的：每个会话的模型、思考程度、上下文和额度
SLASH_CACHE = f"{DATA}/slash_cache.json"        # 每个目录能用的斜杠命令（翻补全列表得到的）
ASKS = f"{DATA}/asks"                           # PermissionRequest hook 记下的：每个会话最近一次弹框问的是什么
CLOSING = f"{DATA}/closing.json"                # 主动关掉的员工 pid -> 时间：监控看到它没了不报「退出了」
ALIASES = f"{DATA}/aliases.json"                # claude 进程 pid -> 你给它起的名字
HELD = f"{DATA}/held.jsonl"                     # 夜里你没在用时攒下、没推送的卡片（点看板上的〔🌙 看攒下的〕再发）
SENT = f"{DATA}/sent.jsonl"                     # 机器人替人打进员工终端的话：对不上这里的，就是你在电脑上亲手打的
HEARD = f"{DATA}/heard.jsonl"                   # 员工听到的每一句话（谁说的、从哪来的），守护进程边看对话记录边记，给卡片和手机 App 用
VIA = {"via": "", "by": ""}                     # 这个进程替谁打字：守护进程设成飞书（crbot）；命令行 cr 是 cli（by＝在哪个员工的终端里跑的，见 crcli）；
                                                 # 没设的（员工自己写的脚本直接调这里）也算 cli，by 现查
SETTINGS = f"{CLAUDE_DIR}/settings.json"
LAUNCH = os.environ.get("CLAUDE_MONITOR_LAUNCH_SCRIPT", "")   # 自己的安全启动脚本（参数：会话名 目录 claude参数…）；不设就用内置的
ASSISTANT_PROMPT = f"{ROOT}/assistant-prompt.md"
ASSISTANT_DIR = os.environ.get("CLAUDE_MONITOR_ASSISTANT_DIR") or FILES_ROOT

IDLE_DELAY_SECS = 0         # 一停下就通知（你要求的：不等）
STUCK_MINS = 30             # 一直 busy 但对话记录这么久没更新，提示可能卡住
SUMMARY_CHARS = 200         # 额度提醒里放屏幕上这么多字
CARD_CHARS = 6000           # 一张飞书卡片放这么多字（卡片上限约 30KB）；更长的接着发「续」卡片，不截断
MERGE_SECS = 10 * 60        # 同一个员工 10 分钟内的汇报 / 进展合成一张卡片：原地更新，手机只响第一次
QUIET_HOURS = os.environ.get("CLAUDE_MONITOR_QUIET_HOURS", "auto").strip()   # 夜里免打扰：auto（按你平时几点用 Claude 算）/ off / 1-8
AWAKE_SECS = 30 * 60        # 你这么久以内动过（发消息、点按钮、在电脑上跟 Claude 说话）：夜里也照常推
CARD_GRACE = 8              # 你刚发了话 / 点了按钮：飞书那边还在登记“思考中”卡片，监控先别抢着另发一张回复
HOLD_KINDS = ("idle", "stuck", "hire", "peer", "limit", "dead", "slash", "proxy", "upgrade")   # 夜里可以先攒着的通知（弹框等你点的除外）
DEFAULT_NEW_ARGS = ["--dangerously-skip-permissions"]

EFFORTS = [("low", "低"), ("medium", "中"), ("high", "高"), ("xhigh", "很高"), ("max", "最高")]
EFFORT_CN = dict(EFFORTS)
EFFORT_ASK = "Change effort level?"   # 对话进行到一半改思考程度，Claude Code 先弹框确认（改了以后下一条要整段重读）
ASSISTANT_EFFORT = "medium"  # 新助手的默认思考程度：手机上聊天，回得快一点；可在卡片上改
STATUS_RE = re.compile(r"((?:Opus|Sonnet|Haiku|Fable)[^·]*?)\s*·\s*(low|medium|high|xhigh|max|auto)\b")
SPINNER_RE = re.compile(r"^\S\s+\S+…\s*(?:\((.*)\))?$")   # ✻ Cascading… (5m 5s · ↓ 10.1k tokens · thinking)
OPTION_RE = re.compile(r"^(?:[❯↓↑]\s*)*(\d+)\.\s+(.+)$")     # 选择框里的「❯ 1. Yes」「↓ 10. Opus 4.8」
SUGGEST_RE = re.compile(r"^(?:❯\s*)?(/[\w:.\-]+)\s{2,}(.*)$")   # 打「/」弹出的补全：「❯ /compact      Free up context…」
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
SEND_EXTS = IMAGE_EXTS + (".pdf",)                          # 回复里提到这些文件，卡片上给〔📎〕按钮发到手机上
KEY_NAMES = {"enter": ("Enter", "回车"), "esc": ("Escape", "Esc"), "tab": ("Tab", "Tab"), "space": ("Space", "空格")}
KEY_DESC = {"cancel": "取消", "select": "选中", "confirm": "确认", "exit": "退出", "go back": "返回", "submit": "提交",
            "close": "关闭", "toggle": "勾选", "set as default": "设为默认", "use this session only": "只用于这个会话",
            "for this session only": "只用于这个会话", "adjust": "调整", "add notes": "加备注"}

LIMIT_RE = re.compile(r"(?i)(usage limit|limit reached|hit your .{0,20}limit|limit will reset|resets? at \d)")

KEYMAP = {
    "上": "Up", "下": "Down", "左": "Left", "右": "Right",
    "回车": "Enter", "确定": "Enter", "enter": "Enter",
    "esc": "Escape", "取消": "Escape", "tab": "Tab",
    "shift-tab": "BTab", "shifttab": "BTab", "ctrl-c": "C-c", "ctrlc": "C-c",
    "空格": "Space", "退格": "BSpace",
}


# ---------------------------------------------------------------- 小工具

def now():
    return time.time()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"   # 守护进程的几个线程、命令行可能同时写同一个文件
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def assistant_prompt():
    """助手的说明书：assistant-prompt.template.md 填上本机的 cr 命令、目录、代理，再接上本机自己的附加说明
    （assistant-prompt.local.md，可选），写到 assistant-prompt.md。助手每次启动前重新生成。"""
    tpl = os.path.join(ROOT, "assistant-prompt.template.md")
    if not os.path.exists(tpl):
        return ASSISTANT_PROMPT
    cr = os.environ.get("CLAUDE_MONITOR_CR_CMD")
    if not cr:
        cr = (os.path.join(ROOT, "bin", "cr.cmd").replace("\\", "/") if os.name == "nt"
              else shlex.quote(os.path.join(ROOT, "bin", "cr")))
    text = (open(tpl, encoding="utf-8").read().replace("{CR}", cr).replace("{FILES_ROOT}", FILES_ROOT)
            .replace("{PROXY_NOTE}", f"，确认代理 {PROXY} 通了才启动，并自动带上代理环境变量" if PROXY else ""))
    local = os.path.join(ROOT, "assistant-prompt.local.md")
    if os.path.exists(local):
        text = text.rstrip() + "\n\n" + open(local, encoding="utf-8").read()
    save_text(ASSISTANT_PROMPT, text)
    return ASSISTANT_PROMPT


def save_text(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def split_args(text):
    """「名字 目录 参数…」拆开；Windows 的路径里有反斜杠，不能当转义符。"""
    if os.name != "nt":
        return shlex.split(text)
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in shlex.split(text, posix=False)]


_proxy_seen = {"t": 0.0, "ok": True}


def proxy_ok(max_age=60):
    """配置了代理时：代理端口通不通。没配置就当通。
    结果记 1 分钟：每次探测都会在代理的日志里留一条「连上就断开」的报错，不能每 3 秒探一次。"""
    if not PROXY:
        return True
    if time.time() - _proxy_seen["t"] < max_age:
        return _proxy_seen["ok"]
    host, _, port = PROXY.rpartition(":")
    try:
        socket.create_connection((host or "127.0.0.1", int(port)), timeout=3).close()
        ok = True
    except (OSError, ValueError):
        ok = False
    _proxy_seen.update(t=time.time(), ok=ok)
    return ok


def under_root(path):
    """path 在 FILES_ROOT 下面（含它自己）。"""
    try:
        real = os.path.realpath(path)
        return os.path.commonpath([real, os.path.realpath(FILES_ROOT)]) == os.path.realpath(FILES_ROOT)
    except ValueError:                                   # Windows 上不同盘符
        return False


def first_paragraph(text, limit):
    """要点：标题当前缀，加上第一段正文；去掉 markdown 符号，最多 limit 字。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    heads, body = [], ""
    for p in paras:
        if re.fullmatch(r"#{1,6}\s+.*", p):
            heads.append(re.sub(r"^#{1,6}\s+", "", p))
            continue
        body = p
        break
    para = "：".join(heads[:1] + ([body] if body else []))
    para = re.sub(r"\*\*|__|`", "", para).replace("\n", " ")
    more = len(paras) > len(heads) + 1
    if len(para) > limit:
        para, more = para[:limit].rstrip(), True
    return para + ("……" if more else "")


def clip(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + f"……（共 {len(text)} 字，点〔看全文〕）"


def split_text(text, size=CARD_CHARS):
    """按行切成几块，每块不超过 size 字（单行太长就硬切）。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:size])
            line = line[size:]
        if cur and len(cur) + len(line) + 1 > size:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


def _table_md(rows):
    """markdown 表格 → 手机上好读的样子（飞书卡片没有表格）：
    两列的每行一条「**左**：右」；三列以上每行一小段，加粗的行名下面一行一个「列名：内容」。"""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    head, body = cells[0], cells[1:]
    if not body:
        return ["　".join(head)]
    out = []
    if len(head) == 2:
        if any(head):
            out.append(f"<font color='grey'>{head[0]} → {head[1]}</font>")
        out += [f"**{r[0]}**：{r[1] if len(r) > 1 else ''}" for r in body]
        return out
    for r in body:
        out.append(f"**{r[0]}**")
        for i, c in enumerate(r[1:], 1):
            label = head[i] if i < len(head) else ""
            out.append(f"　{label}：{c}" if label else f"　{c}")
        out.append("")
    return out[:-1]


def _md_line(line):
    """一行 markdown 里飞书卡片不认的写法：`代码` 去掉反引号（不再套「」，读着干净）；无序列表的「- 」换成「• 」，
    缩进的子项用全角空格缩进。有序列表「1. 」本来就读得懂，不动。"""
    line = re.sub(r"`([^`\n]+)`", r"\1", line)
    return re.sub(r"^(\s*)[-*+]\s+", lambda m: "　" * (len(m.group(1)) // 2) + "• ", line)


def phone_md(text):
    """把 Claude 的 markdown 改成飞书卡片能显示的样子：标题变粗体，表格拆成一行一条，``` 代码块的围栏去掉（飞书卡片都不认），
    行内代码和列表见 _md_line。"""
    out, table, fence = [], [], False

    def flush():
        if table:
            out.extend(_table_md(table))
            table.clear()

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            flush()
            fence = not fence
            continue
        if fence:
            out.append(line)
            continue
        line = _md_line(line)
        s = line.strip()
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", s):
            continue                                            # 表格的 |---|---| 分隔行
        if s.startswith("|") and s.endswith("|"):
            table.append(s)
            continue
        flush()
        out.append(re.sub(r"^#{1,6}\s+(.*)$", r"**\1**", line))
    flush()
    return "\n".join(out)


def _table_el(rows):
    """markdown 表格 → 飞书卡片的原生表格（有表头、网格线，列多了能左右滑）。第一列加粗，当作行名。"""
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    width = max(len(r) for r in cells)
    head = cells[0] + [""] * (width - len(cells[0]))
    cols = [{"name": f"c{i}", "display_name": h or " ", "data_type": "lark_md", "width": "auto"} for i, h in enumerate(head)]
    body = [{f"c{i}": (f"**{c}**" if i == 0 and c else c) for i, c in enumerate(r + [""] * (width - len(r)))}
            for r in cells[1:]]
    return {"tag": "table", "page_size": 10, "row_height": "low",
            "header_style": {"text_align": "left", "text_size": "normal", "background_style": "grey", "bold": True, "lines": 1},
            "columns": cols, "rows": body}


def rich(text, size=CARD_CHARS):
    """把 Claude 写的 markdown 变成卡片元素：普通文字照 phone_md 转（标题变粗体、列表、行内代码见 _md_line），
    表格变成飞书原生表格。太长就分成几块（每块一张卡片，表格不会被拆开）。返回 [[元素…], …]。"""
    segs, buf, table, fence = [], [], [], False

    def flush_text():
        if buf:
            segs.append(("md", "\n".join(buf)))
            buf.clear()

    def flush_table():
        if len(table) > 1:
            flush_text()
            segs.append(("table", list(table)))
        elif table:
            buf.extend(table)
        table.clear()

    for line in (text or "").splitlines():
        st = line.strip()
        if st.startswith("```"):
            flush_table()
            fence = not fence
            continue
        if fence:
            buf.append(line)
            continue
        line = _md_line(line)
        st = line.strip()
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", st):
            continue                                            # 表格的 |---|---| 分隔行
        if st.startswith("|") and st.endswith("|"):
            table.append(st)
            continue
        flush_table()
        buf.append(re.sub(r"^#{1,6}\s+(.*)$", r"**\1**", line))
    flush_table()
    flush_text()
    chunks, cur, used = [], [], 0
    for kind, val in segs:
        if kind == "table":
            n = sum(len(r) for r in val)
            if cur and used + n > size:
                chunks.append(cur)
                cur, used = [], 0
            cur.append(_table_el(val))
            used += n
            continue
        for piece in split_text(val.strip("\n"), size):
            if not piece.strip():
                continue
            if cur and used + len(piece) > size:
                chunks.append(cur)
                cur, used = [], 0
            cur.append(_md(piece))
            used += len(piece)
    if cur:
        chunks.append(cur)
    return chunks or [[]]


LINE_EM = 24        # 手机窄屏上卡片里一行大约放得下多少个汉字宽（灰底块、按钮行都按它算，宁可短一点也不折行）
NOTE_EM = 27        # 灰色小字（note）字号小一号，一行多放几个字


def text_em(text):
    """一段字大概多宽，以一个汉字为 1：汉字、全角符号算 1，英文字母、数字、半角符号约 0.55。"""
    return sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.55 for c in str(text))


def fit(text, em):
    """压成一行、截到 em 宽以内（截掉了就加「…」），保证手机上正好一行。"""
    text = one_line(text, 10 ** 6)
    if text_em(text) <= em:
        return text
    out, w = [], 1                                       # 留一格给「…」
    for c in text:
        w += text_em(c)
        if w > em:
            break
        out.append(c)
    return "".join(out).rstrip() + "…"


def one_line(text, limit):
    """压成一行、去掉会被飞书当成格式的符号，最多 limit 字。"""
    text = re.sub(r"[*`<>\[\]]", "", " ".join(str(text).split()))
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def first_sentence(text):
    """第一行有字的话（跳过表格线），去掉加粗、标题、列表这些符号。"""
    first = next((r for r in (text or "").splitlines() if r.strip() and not set(r.strip()) <= set("|-: ")), "")
    return re.sub(r"[*#`>]|^\s*[-•·]\s*", "", first).strip()


def fmt_secs(secs):
    secs = int(max(secs, 0))
    if secs < 60:
        return f"{secs}秒"
    if secs < 3600:
        return f"{secs // 60}分{secs % 60}秒"
    return f"{secs // 3600}小时{secs % 3600 // 60}分"


# ---------------------------------------------------------------- 会话发现

def _proc_alive(pid):
    try:
        return psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, ValueError, TypeError):
        return False


def _proc_args(pid):
    try:
        return psutil.Process(int(pid)).cmdline()
    except (psutil.Error, ValueError, TypeError):
        return []


def strip_resume_args(args):
    """去掉 --resume/-r/--continue/-c（及其值），保留其余启动参数。"""
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("--resume", "-r"):
            nxt = args[i + 1] if i + 1 < len(args) else ""
            skip = bool(nxt) and not nxt.startswith("-")
            continue
        if a in ("--continue", "-c") or a.startswith("--resume="):
            continue
        out.append(a)
    return out


def _shown_job(d, jobs, title):
    """tmux 里这个前台眼下接着的是哪个后台任务。会话文件里的 parkedJobId 只记它最早挂上的那个，在前台里切到别的后台会话后
    Claude Code 不改它，只有终端标题变成那个会话的名字——有名字对得上的后台任务就认它（同名的先认原来那个、再认同目录最近动过的）。"""
    parked = jobs.get(d.get("parkedJobId"))
    if not parked:
        return None
    named = [j for j in jobs.values() if title and re.sub(r"^\W+", "", j.get("name") or "").strip() == title]
    if not named or parked in named:
        return parked
    return max(named, key=lambda j: (j.get("cwd") == d.get("cwd"), j.get("updatedAt") or 0))


def discover():
    """返回 {显示名: 会话信息}：本机所有交互式 claude。
    在 tmux / ccm 里跑的能遥控；直接在终端里打 claude 开的只能看（term == "none"），见 crterm.py。"""
    found = []
    records = [(p, load_json(p, None)) for cdir in CLAUDE_DIRS for p in glob.glob(os.path.join(cdir, "sessions", "*.json"))]
    jobs = {d["jobId"]: d for _, d in records if d and d.get("kind") == "bg" and d.get("jobId") and d.get("pid") and _proc_alive(d["pid"])}
    for path, d in records:
        if not d or d.get("kind") != "interactive":
            continue
        pid = d.get("pid")
        if not pid or not _proc_alive(pid):
            continue
        t = crterm.attach_info(pid, d)
        if not t:
            continue
        title = re.sub(r"^\W+", "", t["title"]).strip()      # Claude Code 把对话标题写在终端标题里，前面是状态符号
        job = _shown_job(d, jobs, title)
        if job:                                      # 对话被挪成了后台任务（Claude Code 的 parked 会话）：tmux 里这个只是它的前台，
            d = {**d, **{k: job[k] for k in ("sessionId", "status", "statusUpdatedAt", "waitingFor", "name") if k in job},
                 "jobPid": job.get("pid")}           # 真正干活的、写对话记录的是后台那个进程；打字、读屏照旧走 tmux 里这个
            if "waitingFor" not in job:
                d.pop("waitingFor", None)
        args = _proc_args(pid)[1:]
        found.append({
            "pid": pid, "agent": "claude", "sessionId": d.get("sessionId"), "cwd": d.get("cwd"),
            "cfg": os.path.dirname(os.path.dirname(path)),    # 它用的配置目录
            "status": d.get("status", "idle"), "statusAt": (d.get("statusUpdatedAt") or 0) / 1000,
            "startedAt": (d.get("startedAt") or 0) / 1000,
            "waiting": (d.get("waitingFor") or "?") if d.get("status") == "waiting" else "",   # input needed / permission prompt
            "peer": d.get("name") or "",                 # 同一台机器上的会话之间互相发消息时用的名字
            **({"jobPid": d["jobPid"]} if d.get("jobPid") else {}),
            **{k: v for k, v in t.items() if k != "title"},
            "tmux_session": t["group"],
            "args": strip_resume_args(args),
            "title": "" if title in ("Claude Code", "claude") else title,
        })
    per_sess = {}
    for s in found:
        per_sess[s["group"]] = per_sess.get(s["group"], 0) + 1
    aliases = load_json(ALIASES, {})
    result = {}
    for s in found:
        name = s.get("tag") or s["group"]            # 机器人开的：窗格上记着员工名（会话名前面可能多了 me- 之类的前缀）
        if not s.get("tag") and per_sess[name] > 1:
            name = f"{name}:{s['window']}"          # 同一个 tmux 里有好几个 claude：exp-a:0 / exp-a:1
        result[aliases.get(str(s["pid"]), name)] = s
    # codex 员工：起名的规矩一样，但不改 Claude 员工原来的名字——跟 claude 在同一个 tmux 会话里的 codex 带上窗口号
    try:
        codex = crcodex.discover()
    except Exception:                                # codex 那边出什么错都不能耽误 Claude 员工
        codex = []
    claude_groups = set(per_sess)
    per_cx = {}
    for s in codex:
        per_cx[s["group"]] = per_cx.get(s["group"], 0) + 1
    for s in codex:
        name = s.get("tag") or s["group"]
        if not s.get("tag") and (per_cx[name] > 1 or name in claude_groups):
            name = f"{name}:{s['window']}"
        if name in result:
            name = f"{name}-codex"
        result[aliases.get(str(s["pid"]), name)] = s
    return result


def is_codex(e):
    return (e or {}).get("agent") == "codex"


def claude_label(cfg):
    """另一个 Claude 配置目录的简称：claude-work → WORK。"""
    path = os.path.normpath(cfg)
    base = os.path.basename(path)
    if base in (".claude", "claude"):
        base = os.path.basename(os.path.dirname(path))
    base = re.sub(r"^\.?claude[-_.]?", "", base, flags=re.I) or base
    return base.upper() if len(base) <= 4 and base.isascii() else base


def mark(e):
    """员工是什么：CC（Claude Code）/ CC·WORK（别的 Claude 配置目录）/ Codex·官方 / Codex·备用。
    看发现它时认出来的程序和它实际用的配置目录（CLAUDE_CONFIG_DIR / CODEX_HOME），不看名字。"""
    e = e or {}
    if is_codex(e):
        return crcodex.tag(e)
    cfg = e.get("cfg")
    if cfg and os.path.realpath(cfg) != os.path.realpath(CLAUDE_DIR):
        return f"CC·{claude_label(cfg)}"
    return "CC"


_reg_cache = {"t": 0.0, "reg": {}}


def _registry(max_age=3):
    """注册表，记 3 秒（每张卡片的标题都要查员工是什么，网络盘上读文件慢）。"""
    if time.time() - _reg_cache["t"] > max_age:
        _reg_cache.update(t=time.time(), reg=load_json(REGISTRY, {}))
    return _reg_cache["reg"]


def mark_of(name):
    e = _registry().get(name)
    return mark(e) if e is not None else ""


def label_name(name):
    """「8号 Codex·备用 cxtest」：列表、按钮、名单里写员工的样子。"""
    return " ".join(x for x in (badge(name), mark_of(name), name) if x)


def tag_names(text):
    """文字里每个「【名字】」（前面可能已经有「3号」）前面补上工号和类型：「3号 CC【名字】」「8号 Codex·备用【cxtest】」。"""
    reg = _registry()

    def sub(m):
        name = m.group(2)
        if name not in reg:
            return m.group(0)
        head = " ".join(x for x in (m.group(1) or badge(name), mark(reg[name])) if x)
        return f"{head}【{name}】"
    return re.sub(r"(\d+号)?【([^】]+)】", sub, text)


def agent_word(e):
    """说话时怎么称呼它的程序：claude / codex。"""
    return "codex" if is_codex(e) else "claude"


def transcript_path(session_id):
    hits = [h for cdir in CLAUDE_DIRS for h in glob.glob(f"{cdir}/projects/*/{session_id}.jsonl")]
    return hits[0] if hits else crcodex.rollout_path(session_id)      # 不是 Claude 的：看是不是 codex 的会话


def last_summary(session_id, limit=None):
    """最后一轮的结论：它最后一条消息，默认整条。"""
    full = phone_md(last_reply(session_id, final_only=True))
    return clip(full, limit) if limit else full


def last_reply(session_id, final_only=False):
    """取最后一轮里 Claude 写给你的正文（不含工具调用），读文件末尾即可。
    final_only=True 时只要这一轮的最后一条正文（通常就是结论）。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return ""
    if crcodex.is_rollout(path):
        return crcodex.last_reply(path, final_only)
    size = os.path.getsize(path)
    chunk = 2 << 20
    while True:
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk))
            lines = f.read().decode("utf-8", "replace").splitlines()
        if size > chunk:
            lines = lines[1:]                            # 第一行可能被截断
        texts = []
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            msg = e.get("message") or {}
            content = msg.get("content")
            if e.get("type") == "assistant" and isinstance(content, list):
                for b in reversed(content):
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                        texts.append(b["text"])
                        if final_only:
                            return b["text"]
            elif e.get("type") == "user" and not e.get("isMeta"):
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                if not is_tool_result and texts:
                    break
        if texts or size <= chunk or chunk >= 32 << 20:
            return "\n\n".join(reversed(texts))
        chunk *= 4


def first_user_text(session_id, limit=16):
    """对话标题：这段对话里用户说的第一句话（像 App 的对话列表）。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return ""
    if crcodex.is_rollout(path):
        return crcodex.first_user_text(path, limit)
    with open(path, "rb") as f:
        head = f.read(512 << 10).decode("utf-8", "replace").splitlines()
    for line in head:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        c = (e.get("message") or {}).get("content")
        text = c if isinstance(c, str) else "".join(
            b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
        text = text.strip()
        if text and not text.startswith(("<", "[Request interrupted")):
            text = text.replace("\n", " ")
            return text if len(text) <= limit else text[:limit] + "…"
    return ""


def is_assistant(name):
    return name.startswith("助手")


def is_quiet(name):
    """自动跑的会话（名字以 CLAUDE_MONITOR_QUIET_PREFIXES 里的前缀开头，比如每日资讯）：停下、退出都不报警，结果由它们自己发布。"""
    return bool(QUIET_PREFIXES) and name.startswith(QUIET_PREFIXES)


def _ts(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0


def describe_tool(name, inp):
    """把一次工具调用说成人话，比如「⚙️ 运行命令 查看实验日志」。"""
    base = os.path.basename(str(inp.get("file_path") or inp.get("notebook_path") or "").rstrip("/"))
    if name == "Bash":
        return "⚙️ 运行命令", inp.get("description") or inp.get("command", "")
    if name == "Read":
        return "📖 读取", base
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "✏️ 修改", base
    if name == "Write":
        return "📝 写入", base
    if name in ("Grep", "Glob"):
        return "🔍 查找", inp.get("pattern", "")
    if name == "WebSearch":
        return "🌐 上网搜", inp.get("query", "")
    if name == "WebFetch":
        return "🌐 打开网页", urlparse(inp.get("url", "")).netloc
    if name in ("Agent", "Task"):
        return "🤖 派出子任务", inp.get("description", "")
    if name == "TodoWrite":
        return "📋 更新待办", ""
    if name == "SendMessage":
        return "💬 发消息给同事", "→ " + str(inp.get("to") or "")
    if name == "CodexDialog":                          # codex 屏幕上的批准框（没装 hook 时）
        return inp.get("label") or "🔐 要你批准", ""
    if name == "apply_patch":
        return "✏️ 修改文件", ""
    return "🔧 " + name.split("__")[-1], ""


def turn_steps(session_id, since):
    """since 之后这一轮的过程，像 App 里那样一步一步列出来。
    返回 (你发的那句话, [(类型, 文字, 是否做完)], 是否被打断)；类型是 think / tool / text，
    还有 said：这期间有人跟它说的话（文字是 said_of 的结果，标好了来源，见 label_said）。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return "", [], False
    asked, steps, stopped = crcodex.turn_steps(path, since) if crcodex.is_rollout(path) else _claude_steps(path, since)
    label_said(session_id, [t for k, t, _ in steps if k == "said"])
    return asked, steps, stopped


def _claude_steps(path, since):
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - (4 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    asked, steps, finished, stopped = "", [], set(), False
    prev, began, ended = None, {}, {}                    # 用来算每一步花了多久：上一条记录的时间、命令开始 / 结束的时间
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        at = _ts(e.get("timestamp"))
        if at < since - 2:
            continue
        content = (e.get("message") or {}).get("content")
        said = said_of(e)
        if said:                                         # 有人跟它说话（含它干活时你插的话）：来源由 label_said 对出来
            steps.append(("said", said, None))
            if said["kind"] == "human" and not asked:
                asked = said["text"] or f"（{said['images']} 张图）"
        if e.get("type") == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                ids = [b.get("tool_use_id") for b in content if isinstance(b, dict)]
                finished.update(ids)
                ended.update((i, at) for i in ids)
                prev = at or prev
                continue
            if "[Request interrupted by user" in _blocks_text(content):
                stopped = True
        elif e.get("type") == "assistant" and isinstance(content, list):
            stopped = False                              # 打断以后它又接着干了：这一轮不算停下
            for b in content:
                kind = b.get("type") if isinstance(b, dict) else None
                if kind == "thinking":                   # 想了多久：这条记录写下的时间减上一条
                    steps.append(("think", (b.get("thinking", "").strip(), at - prev if prev and at else None), None))
                elif kind == "tool_use":
                    label, detail = describe_tool(b.get("name", ""), b.get("input") or {})
                    began[b.get("id")] = at
                    steps.append(("tool", (label, one_line(detail, 50)), b.get("id")))
                elif kind == "text" and b.get("text", "").strip():
                    steps.append(("text", b["text"].strip(), None))
        prev = at or prev
    took = lambda i: (ended[i] if i in ended else now()) - began[i] if began.get(i) else None   # 命令跑了多久（还在跑的算到现在）
    return asked, [(k, t + (took(i),) if k == "tool" else t, i is None or i in finished) for k, t, i in steps], stopped


BACKGROUND_ORIGINS = ("task_notification", "scheduled", "auto_continuation", "peer")   # 不是你叫醒的：后台任务、定时唤醒…
SUBSTANTIAL_CHARS = 200     # 后台醒来写了这么多字（不算空白）才单独发卡片，否则只更新〔后台进度〕



def last_turn(session_id):
    """最后一轮：谁叫醒它的（你 / 后台）、它在这一轮里写了什么。
    返回 {"id": 这一轮开头那条记录, "t": 开始时间, "by": "you" / "background", "texts": [它写的话], "tools": 做了几步,
    "said": [这一轮里有人跟它说的话，标好了来源，见 label_said]}；读不到返回 None。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return None
    turn = crcodex.last_turn(path) if crcodex.is_rollout(path) else _claude_last_turn(path)
    if turn:
        label_said(session_id, turn["said"])
    return turn


def _claude_last_turn(path, size=4 << 20):
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - size))
        lines = f.read().decode("utf-8", "replace").splitlines()
    if size < os.path.getsize(path):
        lines = lines[1:]                                # 第一行可能被截断
    turn = None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("isSidechain"):
            continue
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text")
            origin = e.get("turnOrigin")
            if e.get("isCompactSummary") or not origin and (e.get("isMeta") or text.startswith("[Request interrupted")):
                continue                                 # 打断、系统附带的说明、压缩摘要：不算新的一轮
            bg = origin in BACKGROUND_ORIGINS or text.lstrip().startswith("<task-notification>")
            said = said_of(e)
            turn = {"id": e.get("uuid") or e.get("timestamp"), "t": _ts(e.get("timestamp")), "by": "background" if bg else "you",
                    "texts": [], "tools": 0, "said": [said] if said else []}
        elif e.get("type") == "attachment" and turn and said_of(e):
            turn["said"].append(said_of(e))              # 它干活时你插的话、同事发来的话
        elif e.get("type") == "assistant" and turn and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    turn["texts"].append(b["text"].strip())
                elif b.get("type") == "tool_use":
                    turn["tools"] += 1
    if turn is None and size < min(os.path.getsize(path), 32 << 20):
        return _claude_last_turn(path, size * 4)        # 这一轮的开头不在读到的这截里（贴了很多截图）：往前多读
    return turn


def turn_answer(texts):
    """卡片正文：这一轮最后那段话（它的回答）。干活时写的「先看一下……」这类过程说明留在〔🔎 看过程〕里。
    例外：最后一句很短（比如「等待中。」）、前面却写过一大段结果时，整轮都放上，免得结果被藏起来。"""
    if not texts:
        return ""
    chars = lambda t: len(re.sub(r"\s", "", t))
    if chars(texts[-1]) < 100 and sum(chars(t) for t in texts[:-1]) >= SUBSTANTIAL_CHARS:
        return "\n\n".join(texts)
    return texts[-1]


def turn_answers(steps):
    """since 之后不一定只有一轮：它忙的时候你又发了一句，排在后面、上一轮答完才轮到，就是两轮（它干活时插进去的话不算新的一轮）。
    每轮各取它的回答（turn_answer），返回 [(这一轮问的话, 回答)]，没写话的轮不列。"""
    turns = [([], [])]                                   # (这一轮问的话, 它写的话)
    for k, t, _ in steps:
        if k == "said" and not t.get("mid"):
            if turns[-1][1]:
                turns.append(([], []))
            turns[-1][0].append(t)
        elif k == "text":
            turns[-1][1].append(t)
    return [(qs, turn_answer(texts)) for qs, texts in turns if texts]


def turn_label(qs):
    """多轮回答里每轮上面那行灰字：答的是你哪句。后台任务的通知不是你问的话，不写成「答「Monitor …」」。"""
    you = [q for q in qs if q.get("kind") != "background"]
    if not you:
        return "<font color='grey'>后台任务有结果后：</font>\n" if qs else ""
    ask = one_line(you[0]["text"] or f"{you[0]['images']} 张图", 16) + ("…" if len(you) > 1 else "")
    return f"<font color='grey'>答「{ask}」</font>\n"


def is_human(e):
    """对话记录里这一条是不是人说的话（电脑上打的，或机器人替你发过去的）：
    工具结果、系统附带的说明、后台唤醒、本地命令、打断都不算。codex 的记录交给 crcodex 认。"""
    if e.get("type") == "event_msg":
        return crcodex.is_human(e)
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain") or e.get("isCompactSummary"):
        return False
    content = (e.get("message") or {}).get("content")
    if e.get("turnOrigin") in BACKGROUND_ORIGINS or isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = content if isinstance(content, str) else "".join(
        b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text")
    return bool(text.strip()) and not text.lstrip().startswith(("<", "[Request interrupted"))


def last_human_text(session_id):
    """这段对话里你最后说的那句话（电脑上打的或机器人替你发的）；找不到返回 ""。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return ""
    if crcodex.is_rollout(path):
        return crcodex.last_human_text(path)
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - (2 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"user"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if is_human(e):
            c = e["message"]["content"]
            return (c if isinstance(c, str) else "".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")).strip()
    return ""


# ------------------------------------------------------------ 谁跟员工说了话：飞书 / cr 命令 / 你在终端亲手打的 / 同事 / 斜杠命令

PASTED_RE = re.compile(r"</?pasted_content\b[^>]*>")
PASTE_TAG_RE = re.compile(r"\[(?:Image|Pasted text|Pasted Content) #?\d+[^\]]*\]")
QUEUE_WAIT_SECS = 12 * 3600      # 它在忙时打的话排着队，可能隔很久才轮到（对话记录里记的是轮到的时间）


def clean_said(text):
    """人说的话去掉终端加的包装：粘贴时套的 <pasted_content>、贴图留下的 [Image #3]。"""
    return PASTE_TAG_RE.sub("", PASTED_RE.sub("", text or "")).strip()


def _blocks_text(content):
    return content if isinstance(content, str) else "".join(
        b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text")


def said_of(e):
    """对话记录里的这一条是不是有人在跟它说话。是的话返回 {"t", "kind", "text", "from", "images"}：
    kind = human（输入框里提交的话：你亲手打的，或机器人替谁打的——记录里分不出，要和 sent.jsonl 对）/
    slash（斜杠命令、! 开头的命令）/ peer（别的员工发来的，from 是它的名字）/ background（后台任务做完、定时唤醒）。
    它在干活时你插的话记成 attachment（queued_command），也算。codex 的交给 crcodex.said_of。"""
    if e.get("type") in ("event_msg", "response_item"):
        return crcodex.said_of(e)
    out = {"t": _ts(e.get("timestamp")), "from": "", "images": 0}
    if e.get("type") == "attachment":
        a = e.get("attachment") or {}
        if a.get("type") != "queued_command":
            return None
        origin, text, out["images"] = a.get("origin") or {}, _blocks_text(a.get("prompt")), len(a.get("imagePasteIds") or [])
        out["mid"] = True                                  # 它干活时插进来的话
    elif e.get("type") == "user" and not (e.get("isMeta") or e.get("isSidechain") or e.get("isCompactSummary")):
        content = (e.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        origin, text = e.get("origin") or {}, _blocks_text(content)
        out["images"] = len(e.get("imagePasteIds") or []) or (
            sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image") if isinstance(content, list) else 0)
        out["queued"] = e.get("promptSource") == "queued"
    else:
        return None
    kind, head = origin.get("kind"), text.lstrip()
    if head.startswith("[Request interrupted"):
        return None
    if kind == "peer" or e.get("turnOrigin") == "peer" or "<cross-session-message" in head[:300]:
        m = re.search(r"<cross-session-message\b([^>]*)>\n?(.*?)</cross-session-message>", text, re.S)
        attrs = m.group(1) if m else ""
        name = origin.get("name") or (re.search(r'from-name="([^"]*)"', attrs) or [None, ""])[1]
        pid = origin.get("verifiedPeerPid") or (re.search(r'from="[^"]*?(\d+)\.sock"', attrs) or [None, None])[1]
        return {**out, "kind": "peer", "from": name, "pid": int(pid) if pid else None,
                "text": (origin.get("body") or (m.group(2) if m else text)).strip()}
    if kind and kind not in ("human", "peer") or e.get("turnOrigin") in BACKGROUND_ORIGINS or head.startswith("<task-notification>"):
        summary = re.search(r"<summary>(.*?)</summary>", text, re.S)
        return {**out, "kind": "background", "text": (summary.group(1) if summary else "").strip()}
    if head.startswith(("<command-name>", "<command-message>")):
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        if not name:
            return None
        return {**out, "kind": "slash", "text": f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()}
    if head.startswith("<bash-input>"):                    # 在 claude 里打「! 命令」直接跑的
        return {**out, "kind": "slash", "text": "!" + re.sub(r"</?bash-input>", "", head).strip()}
    if head.startswith("<") and not head.startswith("<pasted_content"):
        return None                                        # 本地命令的输出、系统附带的说明
    said = clean_said(text)
    if not said and not out["images"]:
        return None
    return {**out, "kind": "human", "text": said}


def _sent_tail(size=512 << 10):
    try:
        with open(SENT, "rb") as f:
            f.seek(max(0, os.path.getsize(SENT) - size))
            return [r for r in map(_sent_rec, f.read().decode("utf-8", "replace").splitlines()) if r]
    except OSError:
        return []


def _norm_said(text):
    return re.sub(r"\s+", "", clean_said(text))


def _same_said(rec, x):
    """sent.jsonl 里的这条是不是对话记录里的这句话。"""
    sent, said, files = _norm_said(rec.get("text") or ""), _norm_said(x.get("text") or ""), rec.get("files")
    if (x["kind"] == "slash") != sent.startswith(("/", "!")):
        return False                                       # 斜杠命令只对得上机器人发的斜杠命令
    if sent == said or sent and said and sent[:30] == said[:30]:
        return True
    if files and sent and sent in said:                    # 发文件时先把路径贴进去，再打字
        return True
    return bool(files) and not sent and (x.get("images") or not said or said.startswith("/")) and x["t"] - rec["t"] < 120   # 只发了图片 / 文件


def _sent_start():
    """sent.jsonl 从什么时候开始记的：更早的话分不清是飞书发的还是你亲手打的。"""
    try:
        with open(SENT, encoding="utf-8") as f:
            return _sent_rec(f.readline()).get("t", 0)
    except OSError:
        return now()


def label_said(sid, items):
    """给 said_of 找出来的话标上来源 src：feishu / cli / app（机器人替谁打的，对得上 sent.jsonl）、
    terminal（对不上：你在电脑上亲手打的）、peer（别的员工发来的）、background（后台叫醒的）、
    unknown（sent.jsonl 开始记之前的，分不清）；by 是哪个员工（发消息的同事、用 cr 转话的秘书）。原地改 items 并返回。"""
    mine = any(x["kind"] in ("human", "slash") for x in items)
    reg = load_json(REGISTRY, {}) if mine or any(x["kind"] == "peer" for x in items) else {}
    pids = {e.get("pid") for e in reg.values() if e.get("pid") and e.get("sessionId") == sid}
    # 按会话对；也认同一个进程（codex 发第一句话前还没有会话 id，claude /clear 以后换了会话）
    sent = [r for r in _sent_tail() if r.get("sid") == sid or r.get("pid") in pids] if mine else []
    start = _sent_start() if mine else 0
    used = set()
    for x in items:
        if x["kind"] == "peer":
            by = next((n for n, e in reg.items() if x.get("pid") and x["pid"] in (e.get("pid"), e.get("jobPid"))), None) \
                or next((n for n, e in reg.items() if e.get("peer") and e.get("peer") == x["from"]), x["from"])
            mine = any(e.get("sessionId") == sid and n == by for n, e in reg.items())
            x.update(src="self" if mine else "peer", by=by)   # 它自己发给同事的话（对话记录里也会留一份）：不算别人跟它说的
        elif x["kind"] == "background":
            x.update(src="background", by="")
        else:
            wait = QUEUE_WAIT_SECS if x.get("queued") or x.get("queued_now") else 120
            hit = next((i for i in range(len(sent) - 1, -1, -1)
                        if i not in used and -5 <= x["t"] - sent[i]["t"] < wait and _same_said(sent[i], x)), None)
            if hit is None:
                x.update(src="terminal" if x["t"] >= start - 5 else "unknown", by="")
            else:
                used.add(hit)
                x.update(src=sent[hit].get("via") or "feishu", by=sent[hit].get("by") or "")
    return items


GOAL_REASONS = (("timed out", "检查 goal 有没有达成时超时了"), ("kept finding it unmet", "这一轮反复检查，goal 都还没达成"),
                ("usage limit", "额度用完了，额度恢复后会自动接着干"), ("unavailable or the connection dropped", "连不上 API，自动重试都失败了"),
                ("rejected", "API 拒绝了上一个请求"), ("budget", "goal 的预算用完了"))


def goal_event(e):
    """对话记录里的这一条是不是 goal 自己停下了（不是你按 Esc 停的）。是的话返回 {"t", "what", "why", "resume"}，不是返回 None。
    Claude：系统提示「Goal paused · the goal check timed out · send a message to continue」；
    codex：thread_goal_updated 变成了 active / paused 以外的状态（完成了、预算用完…；paused 是你打断它时变的）。"""
    t = _ts(e.get("timestamp"))
    if e.get("type") == "system" and e.get("subtype") == "informational" and str(e.get("content", "")).startswith("Goal paused"):
        text = e["content"]
        why = next((cn for en, cn in GOAL_REASONS if en in text), text.split("·")[1].strip() if "·" in text else text)
        return {"t": t, "what": "paused", "why": why, "resume": "send a message" in text}
    p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    if e.get("type") == "event_msg" and p.get("type") == "thread_goal_updated":
        status = str((p.get("goal") or {}).get("status") or "")
        if status and status not in ("active", "paused"):
            done = status in ("complete", "completed", "achieved")
            return {"t": t, "what": "done" if done else "paused", "why": "" if done else next(
                (cn for en, cn in GOAL_REASONS if en in status.lower()), status), "resume": not done}
    return None


def note_heard(name, sid, items):
    """守护进程看到员工听到了话（peer_tick 一行行看对话记录）：记进 heard.jsonl（只留 3 天）。"""
    try:
        if os.path.exists(HEARD) and os.path.getsize(HEARD) > 4 << 20:
            with open(HEARD, encoding="utf-8") as f:
                keep = [line for line in f if _sent_rec(line).get("t", 0) > now() - 3 * 86400]
            save_text(HEARD, "".join(keep))
        with open(HEARD, "a", encoding="utf-8") as f:
            for x in items:
                f.write(json.dumps({"name": name, "sid": sid, **{k: x.get(k) for k in SAID_KEYS}}, ensure_ascii=False) + "\n")
    except OSError:
        pass


SAID_KEYS = ("t", "kind", "src", "by", "text", "images", "mid")


def heard_log(sid=None, since=0, until=None, name=None, size=2 << 20):
    """heard.jsonl 里这个会话（或这个员工）since 到 until 之间听到的话，按时间排。"""
    try:
        with open(HEARD, "rb") as f:
            f.seek(max(0, os.path.getsize(HEARD) - size))
            rows = [_sent_rec(line) for line in f.read().decode("utf-8", "replace").splitlines()]
    except OSError:
        return []
    return [r for r in rows if r and (sid is None or r.get("sid") == sid) and (name is None or r.get("name") == name)
            and r.get("t", 0) >= since - 2 and (until is None or r["t"] <= until + 2)]


def said_between(sid, since, until=None, extra=()):
    """since 到 until 之间有人跟它说的话：heard.jsonl 记下的（守护进程一直在看，不怕一轮太长）
    加上对话记录末尾读到的 extra（守护进程开始看之前的），去重、按时间排。"""
    out = {}
    for x in heard_log(sid, since, until) + [x for x in extra if x["t"] >= since - 2 and (until is None or x["t"] <= until + 2)]:
        out.setdefault((int(x["t"]), _norm_said(x.get("text") or "")[:30]), x)
    return [out[k] for k in sorted(out)]


SAID_SHOW = 3       # 卡片上最多列几句别处来的话（多了只留最近的，全文在〔🔎 看过程〕里）
SAID_LINES = 4      # 卡片上说给它的话最多占几行（多了只留最近的，前面写「还有 N 条」）
PREVIEW_LINES = 6   # 选择题选项的预览（ASCII 草图、代码）直接放在选项下面的最多几行，多了、画框的点〔预览 N〕单独看
BOX_RE = re.compile(r"[─━│┃┌┐└┘├┤┬┴┼╭╮╯╰═║╔╗╚╝]")   # 画框的字符：跟汉字混排时不等宽，手机上会歪
SAID_CHARS = 80


def said_shown(x):
    """卡片上要不要列出这句：飞书发的（飞书里本来就有）、后台叫醒的、分不清来源的都不列。"""
    return x.get("src") in ("terminal", "cli", "app", "peer") and x.get("kind") in ("human", "slash", "peer")


def said_tag(x):
    """这句话是谁、从哪说的（卡片上的来源标签，数据层的 label 也用它）：
    你 · 飞书 / 你 · 终端（直接在员工会话里打的，不管人在电脑、远程桌面、ssh 还是手机远程）/ N号 名字（同事）/ 助手（秘书代传）/
    系统（定时任务、goal 自动续跑、后台任务叫醒）。你自己在命令行用 cr 发的也算「你 · 终端」。"""
    src, by = x.get("src"), x.get("by") or ""
    someone = "助手" if is_assistant(by) else " ".join(y for y in (badge(by), by) if y)
    if src == "peer":
        return someone or x.get("from") or "同事"
    if src == "cli":
        return someone or "你 · 终端"
    return {"feishu": "你 · 飞书", "terminal": "你 · 终端", "app": "你 · App", "background": "系统"}.get(src, "")


def said_line(x, width=SAID_CHARS):
    """卡片上的一行：「**🧑 你在电脑上说：**字体再大一点（带 1 张图） 14:41」。返回 (这一行, 有没有截断)。
    width=None 不截断（〔🔎 看过程〕里看全文）。"""
    text = " ".join((x.get("text") or "").split())
    body = (one_line(text, width) if width else phone_md(x.get("text") or "")) if text else ""
    pics = f"（带 {x['images']} 张图）" if x.get("images") else ""
    sep = " · 命令 " if x.get("kind") == "slash" else "："
    when = time.strftime("%H:%M", time.localtime(x["t"]))
    return f"**{said_tag(x)}**{sep}{body}{pics} <font color='grey'>{when}</font>", bool(width) and len(text) > width


SAID_COLORS = {"feishu": "blue", "terminal": "blue", "app": "blue", "peer": "turquoise", "cli": "violet", "background": "neutral"}


MSG_BG = {"color": {"feishu": "blue-50", "terminal": "blue-50", "app": "blue-50", "peer": "green-50", "cli": "violet-50"},
          "grey": {}}                               # 消息块底色：你（浅蓝）/ 同事（浅绿）/ 助手（浅紫），其余浅灰；样式选「grey」就都用浅灰


def said_els(said, limit=SAID_LINES, feishu=False, pending=()):
    """有人跟它说的话：同一个来源接连说的几句放一块，一块一个底色（你浅蓝、同事浅绿，样式里可以换成都浅灰），块和块之间留一点空。
    一条一行：第一行「[你 · 终端] 测试机器又卡了吗」，同一块里后面几句前面一个很淡的「·」。它干活时插的话加「插话」，
    还在排队的加「排队中」，斜杠命令加命令本身。同事的长消息截成一行左右，你的两行左右；最多 limit 行，更早的写成「还有 N 条更早的」。
    feishu=True 时飞书发的也列（实时卡片上那句起因）；pending 是屏幕上还在排队的话。返回 (卡片元素, 有没有省略 / 截断)。"""
    show = [x for x in said if said_shown(x) or feishu and x.get("src") == "feishu"] + list(pending)
    keep = show if len(show) <= limit else show[-(limit - 1):]
    cut = len(keep) < len(show)
    palette = MSG_BG.get((load_json(STATE, {}).get("style") or {}).get("msgbg", "color"), {})
    groups = []                                          # [(来源, 底色, [行])]
    for x in keep:
        src = said_tag(x) or "你"
        tags = [] if groups and groups[-1][0] == src else [_pill(src).replace("neutral", SAID_COLORS.get(x.get("src"), "neutral"))]
        if x.get("queued_now"):
            tags.append(_pill("排队中", 1))
        elif x.get("mid"):
            tags.append(_pill("插话"))
        text = " ".join((x.get("text") or "").split())
        if x.get("kind") == "slash":
            word, _, text = text.partition(" ")
            tags.append(_pill(one_line(word, 20)))
        pics = f" <font color='grey'>· {x['images']} 张图</font>" if x.get("images") else ""
        room = LINE_EM - sum(text_em(re.sub(r"<[^>]+>", "", t)) * 0.85 + 1.3 for t in tags) - (4.5 if pics else 0) \
            - (1.2 if groups and groups[-1][0] == src else 0)          # 标签、「· 2 张图」、行首的「·」占的宽度
        short = fit(text, max(room, 6))
        cut = cut or short != one_line(text, 10 ** 6)
        line = " ".join(tags + [short]) + pics
        if groups and groups[-1][0] == src:
            groups[-1][2].append("<font color='grey'>·</font> " + line)
        else:
            groups.append((src, palette.get(x.get("src"), "grey"), [line]))
    els = [_mk(f"<font color='grey'>还有 {len(show) - len(keep)} 条更早的</font>")] if cut and len(keep) < len(show) else []
    els += [{"tag": "column_set", "flex_mode": "none", "background_style": bg,
             "columns": [{"tag": "column", "width": "weighted", "weight": 1, "elements": [_mk("\n".join(lines))]}]}
            for _, bg, lines in groups]
    return els, cut


def said_md(said, limit=SAID_SHOW):
    """卡片顶上几行：这一轮里别处来的话（你在电脑上亲手打的、同事发来的、cr 转来的、斜杠命令），飞书发的不重复列。
    返回 (markdown, 有没有省略 / 截断——有的话卡片上给〔🔎 看过程〕看全文)。"""
    show = [x for x in said if said_shown(x)]
    lines = [said_line(x) for x in show[-limit:]]
    md = "\n".join(([f"<font color='grey'>…前面还有 {len(show) - limit} 句</font>"] if len(show) > limit else [])
                    + [line for line, _ in lines])
    return md, len(show) > limit or any(cut for _, cut in lines)


def learn_night(days=21):
    """你一般几点睡：最近 days 天里，每个钟点有几天你跟 Claude 说过话。说话的日子不到五分之一的钟点算「睡了」，
    取连着凌晨 3 点（或 4、2、5 点）的那一段，最长 10 小时。返回 [开始, 结束) 的整点；夜里也常用的返回 None；
    记录不到 5 天时先按 1:00–8:00。"""
    cut = now() - days * 86400
    hits = {h: set() for h in range(24)}
    for path in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl")):
        try:
            if os.path.getmtime(path) < cut:
                continue
            with open(path, "rb") as f:
                for line in f:
                    if b'"user"' not in line or b"tool_result" in line:   # 先粗筛，省得每行都解析
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    t = _ts(e.get("timestamp"))
                    if t > cut and is_human(e):
                        lt = time.localtime(t)
                        hits[lt.tm_hour].add(lt.tm_yday)
        except OSError:
            continue
    days_on = len(set().union(*hits.values()))
    if days_on < 5:
        return [1, 8]
    asleep = [len(hits[h]) * 5 < days_on for h in range(24)]
    core = next((h for h in (3, 4, 2, 5) if asleep[h]), None)
    if core is None:
        return None
    start = core
    while asleep[(start - 1) % 24] and (core - start) < 9:
        start -= 1
    end = core + 1
    while asleep[end % 24] and end - start < 10:
        end += 1
    return [start % 24, end % 24]


def pending_tool(session_id):
    """它停在哪一步等你：(那次工具调用, 它在这之前写给你的话)；没有就是 None。选择题、要权限都靠它显示内容。"""
    path = transcript_path(session_id) if session_id else None
    if not path or crcodex.is_rollout(path):          # codex 的批准框不写进对话记录，见 waiting_on
        return None
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - (4 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    texts, waiting = [], {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "user" and not e.get("isMeta"):
            done = [b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            for tid in done:
                waiting.pop(tid, None)
            if not done:
                waiting = {}                         # 你又说了一句：之前没结果的调用不算了
            texts = []
        elif e.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    texts.append(b["text"].strip())
                elif b.get("type") == "tool_use":
                    waiting[b.get("id")] = (b, list(texts))
    return next(iter(waiting.values()), None)


def waiting_on(s, pt=None):
    """它停在选择框上等的是哪一步：(工具调用, 它在这之前写的话)，没有就是 None。
    对话记录要等你答完才写进去，所以先看 PermissionRequest hook 刚记下的；对话记录里对得上就用它（带着前面的话）。
    codex：hook 记下的（装了并信任过才有），没有就看屏幕上的批准框。"""
    if is_codex(s):
        hook = load_json(f"{ASKS}/{s.get('sessionId')}.json", None) if s.get("sessionId") else None
        return crcodex.waiting_on(s, pane_rows(s), hook)
    pt = pt or pending_tool(s.get("sessionId"))
    d = load_json(f"{ASKS}/{s.get('sessionId')}.json", None)
    if not d or d.get("t", 0) < s.get("statusAt", 0) - 10:
        return pt
    hid = d.get("tool_use_id") or f"h{int(d['t'] * 1000)}"     # 这次弹框的编号：防止点了旧卡片的按钮去选新问题
    if pt and pt[0].get("name") == d.get("tool_name") and pt[0].get("input") == d.get("tool_input"):
        return dict(pt[0], alt_id=hid), pt[1]
    return {"id": hid, "name": d.get("tool_name"), "input": d.get("tool_input") or {}}, []


def above_dialog(rows):
    """选择框上面、你上一句话下面：它刚写给你的话（屏幕上能看到的部分）。"""
    ones = [i for i, r in enumerate(rows) if (m := OPTION_RE.match(r)) and m.group(1) == "1"]
    if not ones:
        return ""
    lines = [i for i in range(ones[-1]) if len(rows[i]) > 20 and set(rows[i]) <= set("─━")]
    if not lines:
        return ""
    start = max([i for i in range(lines[-1]) if rows[i].startswith("❯ ")] + [-1]) + 1
    keep = [r for r in rows[start:lines[-1]] if r and not set(r) <= set("─━")]
    return "\n".join(keep[-40:])


def dialog_options(rows):
    """屏幕底部正在显示的选择框（选择题、权限确认）：[(编号, 文字)]；没有选择框就是 []。"""
    tail = [r for r in rows if r][-40:]
    ends = [i for i, r in enumerate(tail) if "Esc to cancel" in r]
    if not ends or ends[-1] < len(tail) - 3:
        return crcodex.dialog_options(rows)          # codex 的批准框长得不一样
    opts = []
    for r in tail[:ends[-1]]:
        m = OPTION_RE.match(r)
        if m and int(m.group(1)) == 1:
            opts = []                                # 只要最后一组从 1 开始的编号
        if m and int(m.group(1)) == len(opts) + 1:
            opts.append((len(opts) + 1, m.group(2).strip()))
    return opts


SAFEGUARD_WHAT = (("non-cybersecurity", "有时会把正常的问题误当成网络攻击类的内容拦下"),
                  ("biology", "跟生物研究沾边的正常问题有时也会被拦下"))


def builtin_question(q):
    """Claude Code 自己弹的英文框（不是 Claude 在问你）：安全检查拦下了回答，问要不要换个模型接着答。
    翻成中文：{"title", "intro", "ask", "model", "options": [(选项, 选了会怎样, "switch" / "stay" / None)]}；别的框返回 None。"""
    text = q.get("question", "")
    m = re.search(r"([\w .\-]+?)'s safeguards", text)
    if not m:
        return None
    opts = q.get("options") or []
    fb = re.search(r"Switch to (.+?) and keep going", text) or next(
        (x for o in opts if (x := re.search(r"Continue on (.+?) now", o.get("description", "")))), None)
    fb = fb.group(1).strip() if fb else "别的模型"
    what = next((cn for en, cn in SAFEGUARD_WHAT if en in text), "有时会误拦正常的问题")
    return {"title": "回答被安全检查拦下了", "model": m.group(1).strip(), "ask": f"要不要换 {fb} 接着答？",
            "options": [_safeguard_choice(o.get("label", ""), o.get("description", ""), fb) for o in opts],
            "intro": f"{m.group(1).strip()} 的安全检查把刚才那次回答停下了。这个检查管得比较严，{what}。"}


def _safeguard_choice(label, desc, fb):
    """安全检查那几种框里的一个选项 → (中文, 选了会怎样, "switch" / "stay" / None)。"""
    later = ("以后再被拦也自动换，不再问你（可以在 /config 改回来）" if "without asking" in desc
             else "以后再被拦还会问你" if "each time" in desc else "")
    if label.startswith(("Switch", "Continue")):
        return f"换 {fb} 接着答", "；".join(x for x in (f"现在就用 {fb} 接着答", later) if x), "switch"
    if label.startswith(("Stay", "Edit prompt")) or desc.startswith("Stop"):
        cn, now_ = ("改一下再问", "你那段话退回它的输入框，改好了再用原来的模型问") if label.startswith("Edit") \
            else ("不换，先停下", "这次就不答了，你那段话会退回它的输入框")
        return cn, "；".join(x for x in (now_, later) if x), "stay"
    return label, desc, None


def paused_question(rows):
    """「Session paused」：安全检查又拦下了，对话停住，问你换模型还是改了再问。
    这块底下没有「Esc to cancel」那行，dialog_options 认不出；认出来就跟 builtin_question 一样翻成中文，外加 "keys": [编号]。"""
    tail = [r for r in rows if r][-30:]
    top = next((i for i in range(len(tail) - 1, -1, -1) if tail[i] == "Session paused"), None)
    if top is None:
        return None
    text, opts = [], []
    for r in tail[top + 1:]:
        m = OPTION_RE.match(r)
        if m and int(m.group(1)) == len(opts) + 1:
            opts.append(m.group(2).strip())
        elif not opts:
            text.append(r)
    text = " ".join(text)
    m = re.search(r"([\w .\-]+?)'s safeguards", text)
    if not opts or not m:
        return None
    fb = next((x.group(1).strip() for o in opts if (x := re.match(r"Switch to (.+)", o))), "别的模型")
    what = next((cn for en, cn in SAFEGUARD_WHAT if en in text), "有时会误拦正常的问题")
    return {"title": "又被安全检查拦下了，对话暂停", "model": m.group(1).strip(), "keys": list(range(1, len(opts) + 1)),
            "ask": f"换 {fb} 接着答，还是改一下再问？", "options": [_safeguard_choice(o, "", fb) for o in opts],
            "intro": f"{m.group(1).strip()} 的安全检查又把回答停下了，这段对话先暂停。这个检查管得比较严，{what}。"}


def screen_menu(rows):
    """屏幕底部的菜单（/model、/resume、权限框…）：编号项、光标在哪一项、底部提示的按键；没有菜单返回 None。"""
    tail = [r for r in rows if r][-40:]
    foot = next((i for i in range(len(tail) - 1, max(len(tail) - 5, -1), -1)
                 if re.search(r"\b(Esc|Enter) to\b", tail[i])), None)
    footer = tail[foot] if foot is not None else ""
    if foot is None:                                     # 没有底部提示的框（比如改思考程度的确认框）：最后几行是带光标的编号项
        foot = len(tail)
        while foot and OPTION_RE.match(tail[foot - 1]):
            foot -= 1
        group = tail[foot:]
        if len(group) < 2 or not any(r.startswith("❯") for r in group):
            return crcodex.menu(rows)                    # codex 的菜单：「enter select · esc back」
        foot, footer = len(tail), "Enter to select"
    items, cursor, seen = [], None, []
    for r in tail[max(0, foot - 30):foot]:
        m = OPTION_RE.match(r)
        if m:
            if int(m.group(1)) == 1:
                items, cursor, seen = [], None, []      # 只要最后一组从 1 开始的编号（上面它写的列表不算）
            items.append((int(m.group(1)), re.split(r"\s{2,}", m.group(2).strip())[0]))   # 只要名字，不要后面的说明
            seen.append(r)
            if r.startswith("❯"):
                cursor = int(m.group(1))
    keys = []
    for seg in footer.split("·"):
        m = re.match(r"\s*(.+?)\s+to\s+(.+?)\s*$", seg)
        if not m or re.search(r"(?i)vim|editor", m.group(2)):
            continue                                     # 「ctrl+g to edit in Vim」在手机上没法用
        k, desc = m.group(1).strip(), m.group(2).strip()
        desc = KEY_DESC.get(desc.lower(), desc)
        if k.lower() in KEY_NAMES:
            keys.append((KEY_NAMES[k.lower()][0], f"{KEY_NAMES[k.lower()][1]}：{desc}"))
        elif re.fullmatch(r"[a-z]", k):
            keys.append((k, f"{k}：{desc}"))
        elif re.fullmatch(r"(?i)ctrl\+[a-z]", k):
            keys.append((f"C-{k[-1].lower()}", f"Ctrl+{k[-1].upper()}：{desc}"))
        elif k == "←/→":
            keys += [("Left", f"←：{desc}"), ("Right", f"→：{desc}")]
    sig = hashlib.md5("\n".join(seen + [footer]).encode()).hexdigest()[:8]   # 光标一动就变：防止点了旧卡片
    return {"items": items, "cursor": cursor, "keys": keys, "sig": sig}


MORE_RE = re.compile(r"^(?:…|\.\.\.)\s*\+\d+|^[↑↓]\s*\d+\.")   # 菜单太长只显示一截：「… +5 models」「↑ 7. Sonnet 5」


def pick_action(menu):
    """点了某个选项以后按哪个键：多选框是「勾选」（空格）；/model 这类有「只用于这个会话」的，用它（不会顺手改掉
    所有新会话的默认）；其他都是回车。返回 (键, 给你看的说明)。"""
    descs = {k: label.split("：", 1)[-1] for k, label in menu["keys"]}
    for k, d in descs.items():
        if d == "勾选":
            return k, "勾选它（全部选好再按确认）"
    for k, d in descs.items():
        if d == "只用于这个会话":
            return k, "换成它，只改这个会话，不影响别的会话"
    return "Enter", "选中它"


def menu_scrolls(rows):
    """屏幕上的菜单是不是只显示了一截（上面或下面还有被折起来的选项）。"""
    return any(MORE_RE.match(r) for r in [r for r in rows if r][-40:])


def scan_menu(s, menu, limit=40):
    """菜单只显示了一截时：把光标从头到尾走一遍，记下所有选项，再把光标放回原处。
    只按 ↑ ↓（不会选中任何东西）；返回 [(编号, 名字)…]，按编号排好。"""
    got, c0 = dict(menu["items"]), menu["cursor"]
    if c0 is None:
        return menu["items"]

    def step(direction, prev):
        crterm.key(s, direction)
        for _ in range(10):                              # 等屏幕跟上（光标动了，或者到头了不动）
            time.sleep(0.05)
            m = screen_menu(pane_rows(s))
            if m and m["cursor"] is not None and m["cursor"] != prev:
                break
        if m:
            got.update(m["items"])
        return m["cursor"] if m else None

    cur, wrapped = c0, False
    for _ in range(limit):                               # 一路往下：绕回原处（循环菜单）或停在最底下
        nxt = step("Down", cur)
        if nxt is None or nxt == cur:
            break
        if nxt == c0:
            wrapped = True
            break
        cur = nxt
    if not wrapped:
        for _ in range(limit):                           # 不循环的菜单：再一路往上，拿到上面折起来的
            nxt = step("Up", cur)
            if nxt is None or nxt == cur:
                break
            cur = nxt
        for _ in range(abs(cur - c0)):                   # 放回原来那一项
            crterm.key(s, "Down" if c0 > cur else "Up")
            time.sleep(0.05)
    return sorted(got.items())


def held_peer(rows):
    """屏幕上是不是「别的会话发来的消息，要你批准才送达」的框（两边权限模式不一样时 Claude Code 会拦一下）。
    是就返回 {"pid": 发送方进程号, "name": 发送方自称的名字, "body": 消息内容}，不是返回 None。"""
    text = "\n".join(rows)
    if "Held message from another session" not in text or "Deliver this message to Claude" not in text:
        return None
    pid = re.search(r"verified pid (\d+)", text)
    name = re.search(r"peer claims name: ([^)\s]+)", text)
    body = [r[1:].strip() for r in rows if r.startswith("│")]
    return {"pid": int(pid.group(1)) if pid else None, "name": name.group(1) if name else "?",
            "body": "\n".join(b for b in body if not b.startswith("…["))}


def strip_metadata(data):
    """去掉照片里附带的信息（EXIF 里的拍摄地点、时间、手机型号等），只留图像本身。"""
    if data.startswith(b"\xff\xd8"):
        out, i = bytearray(data[:2]), 2
        while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] != 0xDA:   # 0xDA 之后是图像数据，原样保留
            marker, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
            if marker not in (0xE1, 0xFE) and not 0xE3 <= marker <= 0xED and marker != 0xEF:
                out += data[i:i + 2 + size]                  # 留 JFIF、色彩配置等；丢 EXIF/XMP、IPTC、注释
            i += 2 + size
        return bytes(out + data[i:])
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        out, i = bytearray(data[:8]), 8
        while i + 8 <= len(data):
            size = int.from_bytes(data[i:i + 4], "big")
            if data[i + 4:i + 8] not in (b"eXIf", b"tEXt", b"iTXt", b"zTXt"):
                out += data[i:i + 12 + size]
            i += 12 + size
        return bytes(out)
    return data


def _input_box(rows):
    """输入框上下两条横线的位置；找不到返回 None。"""
    seps = [i for i, r in enumerate(rows) if len(r) > 20 and set(r) <= set("─━")]
    return (seps[-2], seps[-1]) if len(seps) >= 2 else None


def box_text(rows):
    """输入框里还没发出去的字；空的、找不到输入框返回 ""。停在选择框上时别用（那时最后两条横线是框的边）。"""
    box = _input_box(rows)
    text = "\n".join(r for r in rows[box[0] + 1:box[1]] if r) if box else ""
    return text[1:].strip() if text.startswith("❯") else ""


def input_text(s, rows=None):
    """输入框里还没发出去的字：Claude 看两条横线中间，codex 看粗体的「›」那一行（灰色的提示语不算）。"""
    if is_codex(s):
        return crcodex.box_text(s)
    return box_text(pane_rows(s) if rows is None else rows)


def input_ready(s):
    """输入框出来了、能接话了（刚启动 / 刚上班时等它）。"""
    return crcodex.ready(s) if is_codex(s) else bool(_input_box(pane_rows(s)))


def same_start(a, b, n=30):
    """两段话开头一样（不管换行、空格）：屏幕上折了行的输入框也能跟原话对上。"""
    a, b = re.sub(r"\s+", "", a or ""), re.sub(r"\s+", "", b or "")
    n = min(n, len(a), len(b))
    return n >= 4 and a[:n] == b[:n]


def clear_box(s):
    """清掉输入框里没发出去的字，像在电脑上按一下 Ctrl+C；清干净了返回 True。
    本来就空的不按：空输入框连按两下 Ctrl+C 会退出 claude（codex 也一样）。"""
    if not input_text(s):
        return True
    crterm.key(s, "C-c")
    for _ in range(8):
        time.sleep(0.25)
        if not input_text(s):
            return True
    return False


def slash_catalog(s):
    """这个会话能用的全部斜杠命令（内置的、技能、自定义命令）：[(命令, 说明)]。
    像在电脑上一样：在它的输入框里打一个「/」，把弹出的补全列表往下翻一遍，再删掉那个「/」。
    按目录缓存 12 小时。输入框里有字（你在电脑上打了一半）或正停在选择框上时不动它，返回缓存或 None。"""
    if is_codex(s):
        return codex_slash_catalog(s)
    cache = load_json(SLASH_CACHE, {})
    hit = cache.get(s.get("cwd") or "")
    if hit and now() - hit["t"] < 12 * 3600:
        return hit["items"]
    rows = pane_rows(s)
    box = _input_box(rows)
    if not box or [r for r in rows[box[0] + 1:box[1]] if r] != ["❯"] or screen_menu(rows):
        return hit["items"] if hit else None
    items, quiet = [], 0
    crterm.literal(s, "/")
    try:
        time.sleep(0.8)
        for _ in range(500):
            rows = pane_rows(s)
            box = _input_box(rows)
            new = 0
            for r in rows[max(0, box[0] - 15):box[0]] if box else []:
                m = SUGGEST_RE.match(r)
                if m and m.group(1) not in dict(items):
                    items.append((m.group(1), m.group(2).strip()))
                    new += 1
            quiet = 0 if new else quiet + 1
            if quiet > 12:                               # 翻到头又绕回来了
                break
            crterm.key(s, "Down")
            time.sleep(0.05)
    finally:
        crterm.key(s, "BSpace")                          # 删掉那个「/」，输入框恢复原样
    if items:
        cache[s.get("cwd") or ""] = {"t": now(), "items": items}
        save_json(SLASH_CACHE, cache)
    return items or (hit["items"] if hit else None)


def codex_slash_catalog(s):
    """codex 的斜杠命令：一样是打「/」把弹出的列表往下翻一遍再删掉「/」，按「codex:目录」缓存。"""
    key = f"codex:{s.get('cwd') or ''}"
    cache = load_json(SLASH_CACHE, {})
    hit = cache.get(key)
    if hit and now() - hit["t"] < 12 * 3600:
        return hit["items"]
    if s.get("status") == "waiting" or not crcodex.ready(s) or crcodex.box_text(s):
        return hit["items"] if hit else None
    items, quiet = [], 0
    crterm.literal(s, "/")
    try:
        time.sleep(0.8)
        for _ in range(200):
            new = 0
            for c, d in crcodex.slash_items(pane_rows(s)[-20:]):
                if c not in dict(items):
                    items.append((c, d))
                    new += 1
            quiet = 0 if new else quiet + 1
            if quiet > 12:
                break
            crterm.key(s, "Down")
            time.sleep(0.05)
    finally:
        crterm.key(s, "BSpace")
    if items:
        cache[key] = {"t": now(), "items": items}
        save_json(SLASH_CACHE, cache)
    return items or (hit["items"] if hit else None)


def slash_menu(s):
    """常用斜杠命令：[(命令, 按钮, 说明)]，Claude 和 codex 各一套。"""
    return crcodex.SLASH if is_codex(s) else SLASH


def slash_label(word):
    """斜杠命令的中文说法和说明（两套里找）。"""
    return next(((lb.split(" ", 1)[-1], d) for c, lb, d in SLASH + crcodex.SLASH if c == word), (word, ""))


def save_upload(data, name, kind):
    """你从飞书发来的图片 / 文件存到 data/uploads/日期/ 下，返回路径。图片先去掉拍摄地点等附带信息。"""
    if kind == "image" or (name or "").lower().endswith(IMAGE_EXTS):
        data = strip_metadata(data)
    if kind == "image" or not name:
        magic = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"GIF8": ".gif", b"RIFF": ".webp"}
        name = ("图片" if kind == "image" else "文件") + next((e for m, e in magic.items() if data.startswith(m)), "")
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", os.path.basename(name))[-80:] or "file"
    folder = f"{UPLOADS}/{time.strftime('%Y%m%d')}"
    os.makedirs(folder, exist_ok=True)
    path, i = f"{folder}/{time.strftime('%H%M%S')}-{name}", 1
    while os.path.exists(path):
        path, i = f"{folder}/{time.strftime('%H%M%S')}-{i}-{name}", i + 1
    with open(path, "wb") as f:
        f.write(data)
    return path


def sendable(path):
    """能发到你手机上的文件：真实存在、在 FILES_ROOT 下面、不超过 30MB。"""
    path = os.path.realpath(path)
    ok = under_root(path) and os.path.isfile(path) and os.path.getsize(path) <= 30 << 20
    return path if ok else None


def mentioned_files(text, cwd):
    """回复里提到的图片 / PDF：卡片上给〔📎〕按钮，像在电脑上点开看一样发到手机上。"""
    found = []
    for m in re.finditer(r"(?:[A-Za-z]:)?[\w\-./\\\u4e00-\u9fff]+\.(?:png|jpe?g|gif|webp|pdf)\b", text or "", re.I):
        p = m.group(0)                                   # Windows 的 C:\\x\\y.png 也认
        path = sendable(p if os.path.isabs(p) else os.path.join(cwd or FILES_ROOT, p))
        if path and path not in found:
            found.append(path)
    return found[:6]


def file_buttons(paths):
    return [_button(f"📎 {one_line(os.path.basename(p), 18)}", f"#sendfile {p}") for p in paths]


def perm_label(text):
    """权限框的英文选项翻成中文按钮。"""
    t = text.lower()
    if t.startswith("yes, and switch to auto"):
        return "允许并切自动"
    if t.startswith(("yes, and", "yes, allow", "yes, don")):
        return "总是允许"
    if t.startswith("yes"):
        return "允许"
    if t.startswith("no"):
        return "拒绝"
    return one_line(text, 24)


def tool_detail(name, inp):
    """要你批准的那一步具体是什么：命令原文、改哪个文件、改成什么。"""
    if name == "Bash":
        return f"```\n{inp.get('command', '')}\n```"
    if name in ("Edit", "MultiEdit"):
        parts = [f"文件：{inp.get('file_path', '')}"]
        for ed in inp.get("edits") or [inp]:
            parts.append(f"原来：\n```\n{ed.get('old_string', '')}\n```\n改成：\n```\n{ed.get('new_string', '')}\n```")
        return "\n".join(parts)
    if name == "Write":
        return f"文件：{inp.get('file_path', '')}\n```\n{inp.get('content', '')}\n```"
    if name == "ExitPlanMode":
        return phone_md(inp.get("plan", ""))
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "CodexDialog":
        parts = [f"原因：{inp['reason']}" if inp.get("reason") else ""]
        parts.append(f"```\n{inp['command']}\n```" if inp.get("command") else
                     f"```\n{inp.get('body', '')}\n```" if inp.get("body") else "")
        return "\n".join(x for x in parts if x)
    if name == "apply_patch":
        return f"```\n{inp.get('command') or inp.get('patch') or inp.get('input') or json.dumps(inp, ensure_ascii=False)}\n```"
    return f"```\n{json.dumps(inp, ensure_ascii=False, indent=1)}\n```"


SCROLLED_RE = re.compile(r"new messages? \(ctrl\+End\)")    # 有人在电脑上把画面往上翻了，底下的框看不到


def pane_rows(s):
    rows = [r.strip() for r in crterm.rows(s)]
    if (s.get("waiting") or s.get("status") == "waiting") and any(SCROLLED_RE.search(r) for r in rows[-6:]):
        crterm.key(s, "C-End")                           # 停在框上时画面却翻上去了：回到底部，才看得到选项
        time.sleep(0.4)
        rows = [r.strip() for r in crterm.rows(s)]
    return rows


def spinner_info(rows):
    """屏幕上「✻ Cascading… (1m 5s · ↓ 3.2k tokens · thinking)」那一行：还在不在动、生成了多少、是不是在想。"""
    for r in reversed(rows):
        m = SPINNER_RE.match(r)
        if m:
            inside = m.group(1) or ""
            tokens = re.search(r"([\d.]+k?) tokens", inside)
            return {"tokens": tokens.group(1) if tokens else "", "thinking": bool(re.search(r"\bthinking\b|deep in thought", inside))}
    return {}


def status_bar(rows):
    """底部状态栏「Opus 5.5 · xhigh · ctx 85% · 5h 2% · 7d 5%」：模型、思考程度、上下文和额度用了多少（百分数）。"""
    for r in reversed(rows):
        m = STATUS_RE.search(r)
        if m:
            def pct(key):
                v = re.search(rf"\b{key} (\d+)%", r)
                return int(v.group(1)) if v else None
            return {"model": m.group(1).strip(), "effort": m.group(2), "ctx": pct("ctx"), "5h": pct("5h"), "7d": pct("7d")}
    return {}


def session_status(s):
    """模型、思考程度、上下文和额度用了多少（百分数）。
    先用 Claude Code 交给状态栏的原始数据（cr-statusline 记下的，不管状态栏显示成什么样都准）；
    缺的再从屏幕底部的状态栏读。codex 的从它的对话记录里读（额度是它自己账号的）。"""
    if is_codex(s):
        return crcodex.session_status(s)
    d = load_json(f"{STATUS_DIR}/{s.get('sessionId')}.json", None) if s.get("sessionId") else None
    d = d or {}
    pct = lambda v: round(v) if isinstance(v, (int, float)) else None
    out = {"model": d.get("model") or "", "effort": d.get("effort") or "", "ctx": pct(d.get("ctx")),
           "5h": pct(d.get("5h")), "7d": pct(d.get("7d"))}
    at = d.get("t", 0)
    if s.get("alive", True) and any(out[k] in (None, "") for k in out):
        for k, v in status_bar(pane_rows(s)).items():
            if out.get(k) in (None, ""):
                out[k] = v
    out["at"] = at                                       # 数据是什么时候刷新的（额度是整个账号的，取最新的那份）
    return out


def ctx_text(bar):
    """「上下文 85%」；快满了加个 ⚠️。"""
    ctx = bar.get("ctx")
    return "" if ctx is None else (f"<font color='red'>上下文 {ctx}%</font>" if ctx >= CTX_WARN else f"上下文 {ctx}%")


def latest_quota():
    """5 小时 / 7 天额度是整个账号共用的：取所有会话里最新的那份记录（某个会话一阵子没调用，它记的就是旧数）。"""
    best = {}
    for path in glob.glob(f"{STATUS_DIR}/*.json"):
        d = load_json(path, None) or {}
        if (d.get("5h") is not None or d.get("7d") is not None) and d.get("t", 0) > best.get("t", 0):
            best = d
    return {k: round(best[k]) for k in ("5h", "7d") if isinstance(best.get(k), (int, float))}


def quota_text():
    """「5 小时额度 12% · 周额度 31%」（已用的百分比）；没有数据的那项不写。"""
    q = latest_quota()
    return " · ".join(f"{label} {q[k]}%" for k, label in (("5h", "5 小时额度"), ("7d", "周额度")) if k in q)


def usage_text(bar):
    """卡片上一行：上下文 + 额度。codex 的额度是它自己那个账号的，不用 Claude 的。"""
    if bar.get("agent") == "codex":
        quota = " · ".join(f"{label} {bar[k]}%" for k, label in (("5h", "5 小时额度"), ("7d", "周额度"), ("30d", "月额度"))
                           if bar.get(k) is not None)
        return " · ".join(x for x in (ctx_text(bar), quota) if x)
    return " · ".join(x for x in (ctx_text(bar), quota_text()) if x)


def model_effort(rows):
    """底部状态栏「Opus 5.5 · xhigh · ctx 9% …」：当前模型和思考程度。"""
    for r in reversed(rows):
        m = STATUS_RE.search(r)
        if m:
            return m.group(1).strip(), m.group(2)
    return "", ""


def effort_asking(rows):
    """屏幕上是不是「Change effort level?」确认框；是就返回它要改成的思考程度，不是返回 None。"""
    tail = [r for r in rows if r.strip()][-12:]
    if not any(r.strip() == EFFORT_ASK for r in tail):
        return None
    m = re.search(r"Yes, switch to (\w+)", "\n".join(tail))
    return m.group(1) if m else "?"


def set_effort(s, level, confirm=False):
    """/effort 会顺手把它存成以后新会话的默认值；这里只想改这一个会话，改完把默认值还原。
    对话进行到一半时 Claude Code 会先弹框确认（这段对话按原来的思考程度缓存，改了以后下一条要整段重读）：
    这时返回 False，框留着等你在卡片上确认；confirm=True 表示你点了确认，按「1」。改好了返回 True。"""
    before = load_json(SETTINGS, None)
    if confirm:
        send_keys(s, ["1"])
    else:
        send_text(s, f"/effort {level}")
    t0 = time.time()
    while time.time() - t0 < 10:                 # 等它真改好（状态栏变了、设置也存了），不然紧接着发的话会丢
        time.sleep(0.5)
        if not confirm and effort_asking(pane_rows(s)):
            return False                         # 弹了确认框：这时打的字会被框吃掉，要先确认
        if level in (session_status(s)["effort"], model_effort(pane_rows(s))[1]) and (load_json(SETTINGS, None) != before or time.time() - t0 > 3):
            break
    time.sleep(0.5)
    after = load_json(SETTINGS, None)
    if not before or not after:
        return True
    changed = False
    for key in ("effortLevel", "modelSettings"):
        if after.get(key) != before.get(key):
            if key in before:
                after[key] = before[key]
            else:
                after.pop(key, None)
            changed = True
    if changed:
        tmp = SETTINGS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(after, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, SETTINGS)
    return True


def with_effort(args, level):
    """启动参数里换上 --effort level（去掉原来的）。"""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == "--effort":
            skip = True
        elif not a.startswith("--effort="):
            out.append(a)
    return out + ["--effort", level] if level else out


def slash_output(rows, word):
    """斜杠命令发出后它的输出：屏幕上「❯ /compact」那行下面、输入框上面的几行，去掉 ⎿ 和「装好更新了」之类的提示。"""
    box = _input_box(rows)
    end = box[0] if box else len(rows)
    start = max((i for i in range(end) if rows[i].split()[:2] == ["❯", word]), default=None)
    if start is None:
        return ""
    out = [re.sub(r"^[⎿└]\s*", "", r).strip() for r in rows[start + 1:end]]
    out = [re.sub(r"^Set model to (.+?)( for this session only)?$", lambda m: f"已切到 {m.group(1)}" + ("（只对这次对话）" if m.group(2) else ""),
                  re.sub(r"^Kept model as (.+)$", r"没换，还是 \1", r)) for r in out]
    return "\n".join(r for r in out if r and not set(r) <= set("─━") and not re.search(r"Update installed|/restart to apply", r))


def screen(s, lines=40):
    rows = pane_rows(s)
    rows = ["────" if r and set(r) <= set("─━-") else r for r in rows]
    rows = [r for i, r in enumerate(rows) if r or (i and rows[i - 1])]      # 连续空行只留一行
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[-lines:])


CHROME_RE = re.compile(r"^\s*(?:[─━—-]{3,}.*|❯\s*|›\s*|>\s*|\? for shortcuts.*|.*bypass permissions on.*|⏵⏵.*|.*shift\+tab to cycle.*"
                       r"|.*Update installed.*|.*/restart to apply.*|.*tmux detected.*|.*Press up to edit queued messages.*"
                       r"|.*ctrl\+x ctrl\+s to send now.*|.*\(ctrl\+b ctrl\+b.*|.*Tip: .*|.*esc to interrupt.*|.*Ask Codex to do anything.*"
                       r"|.*(?:Enter|Esc) to \w+.*|.*to navigate.*|.*Notes: press.*|.*to add notes.*|\s*\d*\.?\s*Chat about this.*"
                       r"|\s*\d*\.?\s*\[?\s*\]?\s*Type something.*|.*[Pp]ress up to edit.*|.*Tab to amend.*)$")
DONE_RE = re.compile(r"^\s*\S\s+\w+ for \d.*·\s*done\b")    # 「✻ Crunched for 23s · done 3:34 PM」：上一轮结束的分隔
THOUGHT_RE = re.compile(r"^(?:\S\s+)?Thought for \d[\dhms ]*$")   # 「Thought for 11s」：想了多久，屏幕卡片上是噪音


def clean_screen(rows, lines=25):
    """屏幕去掉终端外壳，只留内容：分隔线、空输入框、权限模式那行、底部状态行（模型、上下文、额度在底栏标签里）、
    更新提示、小贴士、各种操作提示（Enter to select、Esc to cancel、Notes…）、排队提示、转圈那行（顶上的状态标签）都不要；
    「Thought for 11s」也不要；空行都去掉；每行写全、在手机上自然折行（以前截成一屏宽，满屏「…」看不懂），
    开头的缩进换成全角空格留着；「●」换成「•」（「● - 苹果」只留「- 苹果」），原文的「- 」不动；
    你说的话（「❯ 」/ codex 的「› 」开头，菜单光标「❯ 1.」不算）写成「你：…」，前面空一行，一问一答分得开。"""
    out = []
    for r in rows:
        r = r.rstrip()
        if not r.strip() or CHROME_RE.match(r) or DONE_RE.match(r) or SPINNER_RE.match(r.strip()) or THOUGHT_RE.match(r.strip()) \
                or STATUS_RE.search(r) and re.search(r"ctx \d+%", r):
            continue
        r = re.sub(r"(?:[⛀-⛿]\s?)+", "", r)             # /context 的方格图手机上没用
        r = re.sub(r"^(\s*)●\s+(?=[-*]\s)", r"\1", r)
        r = re.sub(r"^(\s*)●\s", r"\1• ", r)
        r = re.sub(r"^(\s*)⎿\s*", r"\1└ ", r)
        said = re.match(r"^[❯›]\s+(?!\d+\.\s)(.+)", r.strip())
        if said:
            out += ([""] if out else []) + ["你：" + said.group(1)]
            continue
        indent = len(r) - len(r.lstrip())
        out.append("　" * (indent // 2) + r.strip())
    return "\n".join(out[-lines:])


def menu_title(rows, menu):
    """菜单上面那一两行（问的是什么、选的是什么）。"""
    first = next((i for i, r in enumerate(rows) if OPTION_RE.match(r) and int(OPTION_RE.match(r).group(1)) == 1
                  and re.split(r"\s{2,}", OPTION_RE.match(r).group(2).strip())[0] == (menu["items"] or [(1, "")])[0][1]), None)
    if first is None:
        return ""
    above = [r.strip() for r in rows[max(0, first - 6):first] if r.strip() and not CHROME_RE.match(r) and not set(r.strip()) <= set("─━-")]
    return "\n".join(fit(r, LINE_EM) for r in above[-2:])


def screen_state(s, rows):
    """它这会儿在干什么，做成屏幕卡片顶上的一个小标签：(文字, 颜色)。"""
    if s.get("waiting") or dialog_options(rows) or screen_menu(rows):
        return "等你选择", "orange"
    spin = next((SPINNER_RE.match(r.strip()) for r in reversed(rows) if SPINNER_RE.match(r.strip())), None)
    if spin:
        inside = (spin.group(1) or "").split("·")
        took = inside[0].strip() if inside and re.match(r"\d", inside[0].strip()) else ""
        doing = "运行命令" if any("Running" in r for r in rows[-12:]) else "思考中"
        return " · ".join(x for x in (doing, took) if x), "blue"
    if s.get("status") == "busy":
        return "干活中", "blue"
    if s.get("status") == "shell":
        return "等后台任务", "neutral"
    return "空闲", "green"


# ---------------------------------------------------------------- 遥控动作

def note_sent(s, text, files=0):
    """机器人替人往员工终端里打了一句话：记进 sent.jsonl（只留 3 天）。对话记录里分不出输入框里的字是谁打的，
    对得上这里的是飞书 / cr 发的，对不上的就是你在电脑上亲手打的（见 label_said）。"""
    via = VIA["via"] or os.environ.get("CR_VIA") or "cli"
    by = VIA["by"] if VIA["via"] else caller_name()
    rec = {"t": now(), "sid": s.get("sessionId") or "", "pid": s.get("pid"), "text": text, "files": files, "via": via, "by": by}
    try:
        if os.path.exists(SENT) and os.path.getsize(SENT) > 1 << 20:
            with open(SENT, encoding="utf-8") as f:
                keep = [line for line in f if _sent_rec(line).get("t", 0) > now() - 3 * 86400]
            save_text(SENT, "".join(keep))
        with open(SENT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def mark_sent_start(t=None):
    """sent.jsonl 还没有（第一次跑）：先记一条开头。从这以后对不上它的话，才能确定是你在电脑上亲手打的。"""
    if not os.path.exists(SENT):
        try:
            with open(SENT, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": t or now(), "sid": "", "text": "", "files": 0, "via": "start", "by": ""}) + "\n")
        except OSError:
            pass


def _sent_rec(line):
    try:
        rec = json.loads(line)
        return rec if isinstance(rec, dict) else {}
    except ValueError:
        return {}


def caller_name():
    """命令行 cr 是在哪个员工的终端里跑的（秘书派活、员工之间用 cr 转话）；不是员工的终端返回 ""。"""
    sock, pane = os.environ.get("TMUX", "").split(",")[0], os.environ.get("TMUX_PANE", "")
    if not (sock and pane):
        return ""
    for name, e in load_json(REGISTRY, {}).items():
        if e.get("alive") and e.get("pane") == pane and e.get("socket") and os.path.realpath(e["socket"]) == os.path.realpath(sock):
            return name
    return ""


def send_text(s, text, files=()):
    note_sent(s, text, len(files))
    for path in files:                                  # 像把文件拖进终端：粘贴路径，图片会变成 [Image #1]
        crterm.paste(s, path)
        time.sleep(0.3)
        crterm.literal(s, " ")
    if "\n" in text:
        crterm.paste(s, text)
    elif text:
        crterm.literal(s, text)
    time.sleep(0.4)
    crterm.key(s, "Enter")


SEND_NOW = "ctrl+x ctrl+s to send now"


def queued(rows):
    """它在忙时发过去的话在排队，要等它这一步做完才看得到：屏幕上「❯ 那句话 / ctrl+x ctrl+s to send now」。
    返回排着的话（可能有几句）；没有返回 []。codex 用回车发的话会直接插进这一轮，只有按 Tab 才排队（「Queued follow-up」），也认。"""
    out = []
    for i, r in enumerate(rows):
        if SEND_NOW in r:
            j = i - 1
            while j > 0 and rows[j].strip() and not rows[j].lstrip().startswith("❯"):
                j -= 1
            out.append(" ".join(x.strip() for x in rows[j:i]).lstrip("❯").strip())
    if not out and any("edit queued messages" in r or "Queued follow-up" in r or "submitted at end of turn" in r for r in rows):
        out.append("")
    return out


def send_now(s):
    """排着的话立即发出去（Claude Code 的 ctrl+x ctrl+s：不等这一步做完）。"""
    crterm.key(s, "C-x")
    time.sleep(0.15)
    crterm.key(s, "C-s")


def key_name(t):
    """遥控键盘上的一个键 → tmux 的键名；「ctrl-x」「ctrl+s」「C-x」这类 ctrl+字母都认。不是键（要原样打的字）返回 None。"""
    m = re.fullmatch(r"(?i)(?:ctrl|c)[-+]([a-z])", t)
    return f"C-{m.group(1).lower()}" if m else KEYMAP.get(t.lower(), KEYMAP.get(t))


def send_keys(s, tokens):
    typed = [t for t in tokens if not key_name(t)]
    if typed:                                           # 用遥控键盘打的字：也是从飞书来的，不是你在终端里打的
        note_sent(s, " ".join(typed))
    for t in tokens:
        key = key_name(t)
        if key:
            crterm.key(s, key)
        else:
            crterm.literal(s, t)
        time.sleep(0.25)


# 从 Claude 会话里（比如助手跑 cr 新建）开出来的 claude 会带上这些变量，被当成「子会话」，之后 --resume 找不到它
CHILD_ENV = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
             "CLAUDE_CODE_MESSAGING_TOKEN")


def launch_env():
    env = {k: v for k, v in os.environ.items()
           if k.lower() != "no_proxy" and k not in CHILD_ENV}   # 机器人自己直连飞书的设置不带给 claude
    if PROXY:
        url = f"http://{PROXY}"
        for k in ("https_proxy", "http_proxy", "all_proxy", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            env[k] = url
    return env


def preflight(cwd, agent="claude"):
    """启动前的检查，任何一项不过就不启动（返回原因）。"""
    if not os.path.isdir(cwd):
        return f"工作目录不存在：{cwd}"
    if not under_root(cwd):
        return f"工作目录不在 {FILES_ROOT} 下：{cwd}"
    if agent == "claude" and not shutil.which("claude"):
        return "找不到 claude 命令"
    if PROXY and not proxy_ok(max_age=0):              # 启动前一定现查
        return f"代理 {PROXY} 不通，不启动（不经代理直接连很危险）"
    return ""


def launch_codex(name, cwd, args, home, where=None):
    """开一个交互式 codex（tmux 里的普通 TUI，跟你自己在终端里打 codex 一样）：先加载那一套 codex 自己的环境脚本，
    检查目录、代理和 codex 命令，再在你平常用的 tmux 里跑 bin/codex-inner.sh。"""
    why = preflight(cwd, "codex") or crcodex.check(home)
    if why:
        return False, "启动中止：" + why
    if crterm.launch_mode() != "tmux":
        return False, "启动中止：codex 员工目前只支持开在 tmux 里（这台机器上没有 tmux）"
    label = crcodex.home_info(home)["label"]
    ok, out = crterm.start_tmux(name, cwd, crcodex.launch_argv(home, cwd, args, PROXY), launch_env(), "codex", where)
    return ok, (f"已启动 codex（{label or home}）：codex {' '.join(args)}".rstrip() + f"\n{out}" if ok else out)


def last_spot(e):
    """它上次在你平常用的 tmux 里的哪个会话、哪个窗口：(会话, 窗口号)，重开时放回那里；不在那个 tmux 里返回 None。"""
    return (e.get("group"), e.get("window")) if e.get("socket") == crterm.USER_SOCK and e.get("term") == "tmux" else None


def launch(name, cwd, args, home=None, where=None):
    """开一个新的交互式 claude：服务器上开在你平常用的 tmux 里，Windows / macOS 在桌面上弹一个终端窗口（用 ccm 跑）。
    给了 home（某一套 codex 的 CODEX_HOME）就开 codex。name 是员工名（可以带「:窗口号」），开好后窗格上记着它。"""
    if home is not None:
        return launch_codex(name, cwd, args, home, where)
    env = launch_env()
    if LAUNCH:                                           # 自己的安全启动脚本（这台服务器：claude-launch.sh）：在给它的 tmux 里开个新会话，再挪到该在的地方
        if crterm.running(name):
            return False, f"【{name}】的 claude 还在运行"
        tmp = crterm.free_session(name)
        r = subprocess.run([LAUNCH, tmp, cwd] + args, capture_output=True, text=True, timeout=60,
                           env={**env, **({"CLAUDE_MONITOR_LAUNCH_SOCK": crterm.USER_SOCK} if crterm.USER_SOCK else {})})
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, (crterm.place(tmp, name, "claude", where) or out) if r.returncode == 0 else out
    why = preflight(cwd)
    if why:
        return False, "启动中止：" + why
    if crterm.launch_mode() == "tmux":
        return crterm.start_tmux(name, cwd, [shutil.which("claude")] + args, env, "claude", where)
    env.update({"CCM_NAME": name, "CCM_PRIVATE": "1"})
    if PROXY:
        env["CCM_REQUIRE_PROXY"] = PROXY                 # ccm 自己再查一遍：代理不通就不启动 claude
    py = sys.executable
    if py.lower().endswith("pythonw.exe"):               # Windows 后台运行的守护进程是 pythonw，新窗口要用有控制台的 python
        py = py[:-len("pythonw.exe")] + "python.exe"
    return crterm.start_window(name, cwd, [py, os.path.join(ROOT, "lib", "ccterm.py")] + args, env)


# ---------------------------------------------------------------- 通知

def _item(msg, kind):
    item = {"t": now(), "kind": kind, "text": str(msg)}
    if isinstance(msg, Card):
        item["card"] = msg.card
        if msg.more:
            item["more"] = [m.card for m in msg.more]
        if msg.reply_for:
            item["for"] = msg.reply_for
        if msg.pending_for:
            item["pending_for"] = msg.pending_for
        if msg.slot:
            item["slot"], item["minor"] = msg.slot, msg.minor
        if msg.merge:
            item["merge"] = msg.merge
    return json.dumps(item, ensure_ascii=False) + "\n"


def notify(msg, kind="info"):
    with open(OUTBOX, "a", encoding="utf-8") as f:
        f.write(_item(msg, kind))


def hold(msg, kind):
    """夜里先不推：攒进 HELD，等你点〔🌙 看攒下的〕。"""
    with open(HELD, "a", encoding="utf-8") as f:
        f.write(_item(msg, kind))


def take_held():
    """取出攒下的（三天以内的），清空。"""
    try:
        with open(HELD, encoding="utf-8") as f:
            lines = f.readlines()
        os.remove(HELD)
    except OSError:
        return []
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except ValueError:
            continue
    return [i for i in items if now() - i.get("t", 0) < 3 * 86400]


def as_card(item):
    """outbox / HELD 里的一条变回 Card（纯文字的包成一张灰卡片）。"""
    if not item.get("card"):
        return alert(item["text"].split("\n")[0][:60], "\n".join(item["text"].split("\n")[1:]), "grey")
    card = Card(item["card"], item["text"])
    card.more = [Card(c, "") for c in item.get("more", [])]
    card.merge = item.get("merge")
    return card


def welcome_card(group_name):
    """员工群建好后的第一张卡片：怎么在群里使唤大家。"""
    body = (f"你是老板（群主），这台机器（{socket.gethostname()}）上的每个 Claude 会话都是一个员工，【助手】是你的秘书。\n\n"
            "**直接打字**：交给秘书（助手），派活、上下班、问进展都跟它说。助手的回复在你和机器人的单聊里，不会被员工们的消息冲掉\n"
            "**「@3 内容」或引用某个员工的卡片回复**：直接对那个员工说\n"
            "**置顶的〔📋 员工看板〕**：谁在干活、谁在等你、谁下班了，原地更新\n"
            "**招人、下班、上班、开除**：也跟秘书说；开除要你自己点确认\n\n"
            "<font color='grey'>机器人只听你一个人的；群里被拉进别人，我会提醒你。发「帮助」看全部用法。</font>")
    return Card(_card(f"👋 欢迎来到「{group_name}」", "turquoise", [_md(body)]), f"👋 欢迎来到「{group_name}」\n{body}")


# ---------------------------------------------------------------- 命令

class Files:
    """要发到你飞书上的文件（回复里提到的图、PDF）。"""
    def __init__(self, paths):
        self.paths = paths

    def __str__(self):
        return "📎 " + "、".join(self.paths)


class Card:
    """飞书交互卡片；命令行里用 text 显示。"""
    def __init__(self, card, text):
        self.card, self.text = card, text
        self.pending_for = None      # 这是“思考中”卡片：回复到了就原地替换
        self.reply_for = None        # 这是某个会话的回复：替换它的“思考中”卡片
        self.in_place = False        # 点按钮得到的结果：直接替换被点的那张卡片
        self.close_old = None        # 上一张实时卡片其实已经答完：它原地换成这张回复（不撤回）
        self.more = []               # 正文太长时，接着发的「续」卡片
        self.slot = None             # 固定的一张卡片（〔📋 员工看板〕）：原地更新，被刷上去了才挪到最下面
        self.minor = False           # 固定卡片只是状态变了（谁开始干活了）：只原地改，不挪位置
        self.summon = False          # 你发「看看」叫出来的：挪到最下面
        self.merge = None            # 汇报 / 进展：10 分钟内同一个员工的下一张并进这张里。{"name", "label", "body", "t"}

    def __str__(self):
        head, sep, rest = self.text.partition("\n")
        return tag_names(head) + sep + rest              # 命令行里看也带上工号和类型


BUTTON_EMOJI = re.compile("^(?:[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF][\uFE0F\u200D]?\\s*)+")
BUTTON_KEEP = "✓☑☐❯↑↓←→↩"                            # 这几个不是装饰：勾选、光标、方向键


def _button(label, cmd, kind="default"):
    """小号按钮，文字前面的装饰表情去掉（勾选框、方向键这类有意思的符号留着）。"""
    if label[:1] not in BUTTON_KEEP:
        label = BUTTON_EMOJI.sub("", label) or label
    return {"tag": "button", "text": {"tag": "plain_text", "content": label}, "type": kind, "size": "small", "value": {"cmd": cmd}}


def _note(text):
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _md(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def short_name(name):
    """底栏里提到员工：「11号」；没工号的写名字，秘书写「助手」。"""
    return "助手" if is_assistant(name) else badge(name) or name


def talking_to(state=None, about=None):
    """底栏上面那行浅灰提示：只在你正跟某个员工单聊时提醒一句（平时打的字都发给秘书，不用每张卡片都说）。"""
    state = load_json(STATE, {}) if state is None else state
    cur = state.get("current")
    return f"你正在和 {short_name(cur)} 单聊，打的字都发给它  ·  发“回到助手”结束" if cur else ""


def _pill(text, level=0):
    """底栏的小标签：平时中性灰，快满了橙色，再满红色。"""
    return f"<text_tag color='{('neutral', 'orange', 'red')[level]}'>{text}</text_tag>"


def usage_tags(bar, model=True, extra=()):
    """底栏的一排浅灰小标签。先是这一轮的（extra：用时、做了几步、生成了多少 token），再是模型、思考程度（「思考 中」）、上下文、额度（5 小时 / 本周合成一个）；
    上下文、额度快满了变橙、变红（不在文字里堆 ⚠️）。codex 的额度是它自己那个账号的；Claude 的取整个账号最新的那份。"""
    level = lambda pct, warn, high: 2 if pct >= high else 1 if pct >= warn else 0
    tags = [_pill(x) for x in extra if x]
    tags += [_pill(bar["model"])] if model and bar.get("model") else []
    tags += [_pill(f"思考 {EFFORT_CN.get(bar['effort'], bar['effort'])}")] if model and bar.get("effort") else []
    if bar.get("ctx") is not None:
        tags.append(_pill(f"上下文 {bar['ctx']}%", level(bar["ctx"], 60, CTX_WARN)))
    quota = {k: bar[k] for k in ("5h", "7d", "30d") if bar.get(k) is not None} if bar.get("agent") == "codex" else latest_quota()
    used = [quota[k] for k in ("5h", "7d", "30d") if quota.get(k) is not None]
    if used:                                            # 「额度 10% / 26%」：5 小时、本周（codex 可能还有本月）
        tags.append(_pill("额度 " + " / ".join(f"{v}%" for v in used), level(max(used), 70, 90)))
    return " ".join(tags)


def footer_els(hint, tags="", live=False):
    """所有卡片统一的底栏：最后一行是一排小标签（usage_tags：用时、模型、上下文、额度），时间用灰字跟在最后
    （自动刷新的卡片写「自动刷新 16:10:05」）；有提示（这张卡片怎么操作、你正跟谁单聊）就在它上面一行浅灰小字。
    时间只在最后一个元素里：实时卡片比较内容变没变时去掉它（见 crbot.live_loop）。"""
    stamp = "自动刷新 " + time.strftime("%H:%M:%S") if live else time.strftime("%H:%M")
    if not tags:                                        # 没有标签：提示和时间合成一行灰字
        return [_note("  ·  ".join(x for x in (hint, stamp) if x))]
    return ([_note(hint)] if hint else []) + [_mk(f"{tags} {_pill(stamp)}")]


def reg_entry(name):
    """注册表里的这个员工（活着的、下班的都在）。"""
    return load_json(REGISTRY, {}).get(name) or {}


def badge(name):
    """「2号」；助手（秘书）和没分到工号的返回空。"""
    n = load_json(BADGES, {}).get(name)
    return f"{n}号" if n else ""


BADGE_GRACE = 600    # 进程没了多久才收回工号：重启、断一下、一时没认出来都不算下班
RENUM_SECS = 86400   # 工号重排后，旧号再认一天


def sync_badges(reg):
    """工号只给在上班的员工：下班就收回（「下班」关掉的马上收回，进程自己没了的过 BADGE_GRACE 秒）；
    新来的、上班回来的取最小的空号；在班的人号码不动，不因为别人下班而挪。助手、资讯这类不占号。返回 {名字: 号}。"""
    badges = load_json(BADGES, {})
    staff = {n: e for n, e in reg.items() if not is_assistant(n) and not is_quiet(n)}
    keep = {n: v for n, v in badges.items() if n in staff and (staff[n].get("alive") or now() - staff[n].get("died", 0) < BADGE_GRACE)}
    for name in sorted((n for n, e in staff.items() if e.get("alive") and n not in keep and _proc_alive(e.get("pid"))),
                       key=lambda n: (staff[n].get("startedAt") or 0, n)):
        keep[name] = min(set(range(1, len(keep) + 2)) - set(keep.values()))
    if keep != badges:
        save_json(BADGES, keep)
    return keep


def renum_hint(k, name, at):
    """用了重排前的旧号：「11号现在是6号（exp-a:1）」；它已经下班（没有工号了）就说用名字叫回。at：什么时候重排的。"""
    until = time.strftime("%H:%M", time.localtime(at + RENUM_SECS))
    return (f"ℹ️ {k}号现在是{badge(name)}（{name}）。工号重排过，旧号认到明天 {until}。" if badge(name) else
            f"ℹ️ 原来的 {k}号 {name} 下班了，已经没有工号；叫回用名字：「上班 {name}」。")


UPGRADE_IDLE = 600   # 停下这么久没动静才算闲着，可以重开换新版本


def _version(text):
    return tuple(int(x) for x in text.split(".")) if re.fullmatch(r"\d+(\.\d+)+", text or "") else None


def outdated(e):
    """它跑的还是旧版本（客户端自己更新过了）：返回 (旧, 新)，不是就 None。
    Claude 看进程实际跑的那个版本文件和现在 claude 命令指向的比；codex 看进程的程序文件是不是已经被新版换掉了。
    别的配置目录（LMU 之类）不管。"""
    try:
        exe = os.readlink(f"/proc/{e['pid']}/exe")
    except (OSError, KeyError):
        return None
    if is_codex(e):
        return ("旧版", "新版") if exe.endswith(" (deleted)") else None
    if not e.get("cfg") or os.path.realpath(e["cfg"]) != os.path.realpath(CLAUDE_DIR):
        return None
    old = os.path.basename(exe.replace(" (deleted)", ""))
    new = os.path.basename(os.path.realpath(shutil.which("claude") or ""))
    return (old, new) if _version(old) and _version(new) and _version(old) < _version(new) else None


def free_to_restart(e):
    """闲着、重开不会丢东西：停下 UPGRADE_IDLE 秒以上，没弹框，没在等后台任务，底下没有它开的命令还在跑
    （Claude 跑命令是一个 bash 子进程），codex 没挂着 /goal（重开会弹「要不要恢复目标」的框）。"""
    if not _proc_alive(e.get("pid")) or e.get("status") != "idle" or e.get("waiting") or e.get("jobPid") or not crterm.controllable(e):
        return False
    if now() - (e.get("statusAt") or now()) < UPGRADE_IDLE:
        return False
    try:
        if any(k.name() in crterm.SHELLS for k in psutil.Process(e["pid"]).children()):
            return False
    except psutil.Error:
        return False
    return not (is_codex(e) and any(re.search(r"Goal (paused|active)", r) for r in pane_rows(e)))


def release_badge(name):
    """「下班」关掉了：工号马上收回。"""
    badges = load_json(BADGES, {})
    if badges.pop(name, None) is not None:
        save_json(BADGES, badges)


def renumber_badges(state, order, reg):
    """整理工号：在班的按 order 排成连续的 1～N（order 里没写到的在班员工按原来的号接在后面），助手不占号。
    旧号记在 state["renum"]（{旧号: 名字}，只记换了人的），再认 RENUM_SECS 秒。返回 (旧, 新)。"""
    old = load_json(BADGES, {})
    on = [n for n in order if n in reg and reg[n].get("alive") and not is_assistant(n) and not is_quiet(n)]
    on += sorted((n for n, e in reg.items() if e.get("alive") and n not in on and not is_assistant(n) and not is_quiet(n)),
                 key=lambda n: (old.get(n) or 999, n))
    new = {n: i for i, n in enumerate(on, 1)}
    save_json(BADGES, new)
    holder = {v: k for k, v in new.items()}
    state["renum"] = {"t": now(), "old": {str(v): k for k, v in old.items() if holder.get(v) != k}}
    return old, new


def topic_of(e):
    """它在做什么：Claude Code 自己起的对话标题；没有就用你说的第一句话。"""
    return e.get("title") or first_user_text(e.get("sessionId"), 24) or ""


def meta_of(name, e=None):
    """卡片的副标题：它在做什么（会话标题），目录跟在旁边，隔开两格。秘书的不写。"""
    e = e or _registry().get(name)
    if not e or is_assistant(name):
        return ""
    where = _short_dir(e.get("cwd"))
    if len(where) > 30 and where.count("/") > 2:      # 目录太深：中间省掉，留开头和最后一层
        where = where.split("/")[0] + "/…/" + where.rsplit("/", 1)[-1]
    return "　　".join(x for x in (one_line(topic_of(e), 40), where) if x)


def _header(title, color, sub=None):
    """卡片标题：「⏳【名字】正在思考」写成「⏳ 6号 名字 · 正在思考」（不要【】）；它是什么（CC / Codex·官方）做成标题右边的小标签，
    一眼分清；标题下面一行浅灰小字是它在做什么、在哪个目录，sub 是这张卡片自己补充的（用时、做了几步…）。"""
    reg = _registry()
    m = re.search(r"(\d+号)?【([^】]+)】", title)
    name = m.group(2) if m else None
    if m:
        before, after = title[:m.start()], title[m.end():]
        subject = not re.search(r"[\w\u4e00-\u9fff]", before)        # 名字前面只有表情：卡片说的就是它
        after = re.sub(r"^\s*[：:]\s*", "", after)
        if subject and after and "【" not in after and not after.startswith((" ", "→", "（", "(")):
            after = " · " + after
        who = " ".join(x for x in (m.group(1) or badge(name), name) if x)
        title = f"{before.rstrip()} {who} {after}"
        title = re.sub(r"(\d+号)?【([^】]+)】", lambda x: f" {' '.join(y for y in (x.group(1) or badge(x.group(2)), x.group(2)) if y)} ", title)
        title = re.sub(r"\s+([，,。：:）)])", r"\1", re.sub(r"\s{2,}", " ", title)).strip()
    title = BUTTON_EMOJI.sub("", title).strip() or title     # 标题不要表情：状态写在字里，颜色也在说
    head = {"title": {"tag": "plain_text", "content": title}, "template": color}
    meta = "　　".join(x for x in (meta_of(name) if name else "", sub) if x)
    if meta:
        head["subtitle"] = {"tag": "plain_text", "content": meta}
    if name in reg:
        kind = mark(reg[name])
        head["text_tag_list"] = [{"tag": "text_tag", "text": {"tag": "plain_text", "content": kind},
                                  "color": "violet" if kind.startswith("Codex") else "neutral"}]
    return head


def _card(title, color, elements, footer=None, tags="", live=False, state=None, sub=None):
    """统一卡片样式：标题（_header：工号、名字、类型标签，下面一行话题 · 目录 · sub）+ 内容 + 统一的底栏（footer_els）。
    footer：底栏提示换成这句（默认是「消息发往谁 · 怎么回这张卡片」）；tags：底栏的小标签（usage_tags 做的模型、上下文、额度）；
    live：自动刷新的卡片，时间带秒。"""
    about = re.search(r"【([^】]+)】", title)
    hint = footer if footer is not None else talking_to(state, about and about.group(1))
    for el in elements:                                 # 按钮行没指定排法时，手机上飞书一个按钮占一整行：一律按自身宽度横排
        if el.get("tag") == "action" and "layout" not in el:
            el["layout"] = "flow"
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": _header(title, color, sub),
            "elements": elements + footer_els(hint, tags, live)}


def _rows(buttons):
    """一排按钮按宽度分成几行，每行都放得下（员工名单、命令面板、遥控键盘）。"""
    rows, cur = [], []
    for b in buttons:
        if cur and sum(_btn_em(x) + 0.6 for x in cur + [b]) > LINE_EM:
            rows.append(cur)
            cur = []
        cur.append(b)
    return [{"tag": "action", "actions": r} for r in rows + ([cur] if cur else [])]


def _pairs(buttons):
    """选项按钮：两个一行、一样宽，字截到半行放得下（选项全文在卡片正文里）。"""
    for b in buttons:
        b["text"]["content"] = fit(b["text"]["content"], LINE_EM / 2 - 2.6)
    return [{"tag": "action", "layout": "bisected", "actions": buttons[i:i + 2]} for i in range(0, len(buttons), 2)]


def code_block(text):
    """等宽灰底的代码块（选项预览、迷你终端）。里面的 ``` 和 ~~~ 换成长得像的字，免得被当成代码块的开头结尾把块弄断。"""
    return "```\n" + re.sub(r"~{3,}", lambda m: "∼" * len(m.group(0)), (text or "").replace("```", "ˋˋˋ")) + "\n```"


def _mk(text):
    """markdown 组件（能放小标签 text_tag，div 里的 lark_md 不行）。"""
    return {"tag": "markdown", "content": text}


def _btn_em(b):
    return text_em(b["text"]["content"]) + 2.4            # 字 + 按钮两边的留白


def _actions(main, more=()):
    """一行按钮：常用的直接摆出来，其余收进最后的「···」菜单（点了跟按钮一样，菜单项的 value 就是命令）。
    一行放不下（手机窄屏也算上）就把后面的挪进「···」，任何时候都不折行。"""
    main, more = list(main), list(more)
    while len(main) > 1 and sum(_btn_em(b) + 0.6 for b in main) + (3 if more else 0) > LINE_EM:
        more.insert(0, main.pop())
    acts = list(main)
    if more:
        acts.append({"tag": "overflow", "options": [{"text": {"tag": "plain_text", "content": b["text"]["content"]},
                                                     "value": b["value"]["cmd"]} for b in more]})
    return {"tag": "action", "actions": acts}


def continued(card, title, color, chunks):
    """正文太长：第一块留在这张卡片里，其余做成「续」卡片挂在 card.more，一个字都不截。"""
    n = len(chunks)
    card.more = [Card(_card(f"{title}（续 {i}/{n}）", color, c if isinstance(c, list) else [_md(c)]), f"{title}（续 {i}/{n}）")
                 for i, c in enumerate(chunks[1:], 2)]
    return card


def alert(title, body, color, buttons=(), footer=None, live=False, state=None, sub=None):
    """带按钮的提醒卡片；命令行里显示成「标题 + 正文」。"""
    body = re.sub(r"〔([^〕]*)〕", r"「\1」", (body or "").strip("\n"))
    body = re.sub(r"(\d+号)?【([^】]+)】", lambda m: " ".join(x for x in (m.group(1) or badge(m.group(2)), m.group(2)) if x), body)
    chunks = rich(body) if body else []
    els = list(chunks[0]) if chunks else []
    if buttons:
        buttons = list(buttons)
        els.append(_actions(buttons[:3], buttons[3:]) if len(buttons) > 3 else _actions(buttons))
    card = Card(_card(title, color, els, footer=footer, live=live, state=state, sub=sub), f"{title}\n{body}" if body else title)
    return continued(card, title, color, chunks)


def merged_card(entries, latest):
    """同一个员工接连几条汇报合成一张：前面几条在上（放不下的只留第一句），最新一条完整放在最下面，按钮跟着最新的。
    entries 是前面几条的 Card.merge，latest 是最新的那张 Card。"""
    stamp = lambda e: f"**{time.strftime('%H:%M', time.localtime(e['t']))}　{e['label']}**"
    room = CARD_CHARS - len(latest.text)
    blocks = []
    for e in reversed(entries):                         # 越新的越优先放全文
        chunks = rich(e["body"])
        said = (e.get("said") if isinstance(e.get("said"), list) else [_md(e["said"])] if e.get("said") else [])   # 这一条的起因
        if len(e["body"]) <= room and len(chunks) == 1:
            room -= len(e["body"])
            blocks.append([_md(stamp(e))] + said + chunks[0])
        else:
            blocks.append([_md(f"{stamp(e)}　<font color='grey'>{one_line(first_sentence(e['body']), 60)}</font>")] + said)
    card = json.loads(json.dumps(latest.card))
    card["header"]["title"]["content"] += f"（{len(entries) + 1} 条）"
    card["elements"] = ([x for b in reversed(blocks) for x in b + [{"tag": "hr"}]]
                        + [_md(stamp(latest.merge) + "（最新）")] + card["elements"])
    out = Card(card, latest.text)
    out.merge = latest.merge
    return out


def _state_label(e):
    if not e.get("alive"):
        return "🌙 下班了 " + time.strftime("%m-%d %H:%M", time.localtime(e.get("died", 0)))
    if e["status"] == "busy":
        return "🟢 在干活"
    if e.get("waiting"):
        return ("🔐 等你批准 " if "permission" in e["waiting"] else "❓ 等你回答 ") + time.strftime("%H:%M", time.localtime(e["statusAt"]))
    return "🟡 停下 " + time.strftime("%H:%M", time.localtime(e["statusAt"]))


def _state_color(e):
    if not e.get("alive"):
        return "red"
    if e.get("waiting"):
        return "yellow"
    return "green" if e["status"] == "busy" else "orange"


def _short_dir(path):
    """卡片上显示的目录：FILES_ROOT 写成「~」（不是家目录时写成「~目录名」）。"""
    home = os.path.expanduser("~")
    label = "~" if os.path.normcase(FILES_ROOT) == os.path.normcase(home) else "~" + os.path.basename(FILES_ROOT)
    path = path or ""
    if os.path.normcase(path).startswith(os.path.normcase(FILES_ROOT)):
        return label + path[len(FILES_ROOT):].replace("\\", "/")
    return path


CHAT_TIMEOUT_SECS = 30 * 60
EXIT_WORDS = ("退出", "回到助手", "回助手", "结束对话")
TYPED_WORDS = ("帮助", "help", "看看", "看板", "列表", "名单", "新对话", "新助手", "对话记录", "思考程度")   # 手打时机器人自己认的几个词（整句一模一样才算）
CTX_WARN = 80               # 上下文用到这么多就提醒，并给〔🗜 压缩上下文〕按钮
SLASH = [("/compact", "🗜 压缩上下文", "把前面的对话压成要点，腾出上下文空间（要点会保留）"),
         ("/context", "📊 上下文明细", "看上下文被什么占了"),
         ("/usage", "📈 额度用量", "这个会话花了多少、5 小时 / 7 天额度用了多少"),
         ("/model", "🤖 换模型", "弹出菜单，用遥控键盘选"),
         ("/resume", "📂 接回以前的对话", "弹出列表，用遥控键盘选"),
         ("/clear", "🧹 清空重来", "开一段新的空对话（旧的还在，可以 /resume 接回）")]

ACTION_WORDS = ("下班", "上班", "开除", "重启", "停", "停下", "停止", "回来", "滚", "退出")   # 「@3 下班」是叫秘书办事，不是对 3 号说话
HELP = """🤖 我是监控机器人，帮你管这台机器上的 Claude Code 员工（tmux 里的 Codex 也算员工，卡片标题旁边标着「CC」「Codex」）。
**怎么使唤**
· 直接打字：交给秘书（助手）。派活、谁上班下班、问进展、让员工之间传话，都跟它说，它会去办
· 助手有自己的对话框：你和机器人的单聊。它的进度、回复、提问都发在那里，不会被员工们的消息冲掉；在群里直接打字也交给它，回复同样在单聊
· 「@3 内容」或引用某个员工的卡片回复：直接对那个员工说（最快）；「@所有人 内容」：开会
· 「@3! 内容」：它正在干活时也不排队，立即插进去（平时它要做完手上这一步才看得到你的话）
  点了好几个人（「@1 2 6 下班」）或只说「@3 下班」这类动作，还是交给秘书去办
· 点卡片上的按钮：〔回复它〕〔下班〕〔上班〕〔看屏幕〕、A/B/C 选项……点了马上执行，不经过秘书；不常用的在「···」里
**我自己认的几个词**（整句一模一样才算，其他话都交给秘书）
· 看看：员工看板叫到最底下
· 列表：详细的员工列表（工号、目录、上下文）
· 新对话 / 对话记录：跟秘书开新对话 / 切回以前的对话
· 思考程度：改秘书的思考程度
· 回到助手：你在跟某个员工单聊时，回到秘书
**卡片上的东西**
· 标题：「3号 名字 · 状态」（在问你 / 要你批准 / 汇报 / 有新进展 / 正在执行…），右边是类型：CC＝Claude Code，Codex＝OpenAI 的 codex；
  装了几套 codex（或几个 Claude 配置目录）时带上套的标签，比如「Codex·官方」（按它实际跑的程序认，不看名字）
· 说给它的话前面有来源标签：你 · 飞书 / 你 · 终端（你直接在它的会话里打的）/ 3号 名字（员工之间）/ 助手 / 系统
· 招 Codex 员工：跟秘书说「招个 codex 在 X 目录干 Y」，名字自动带 cx- 前缀；上班时自动用它原来那套环境接着原对话
· 工号只给在上班的人：下班就收回，叫回用名字（「上班 my-app」）；上班回来的、新来的取最小的空号，在班的人号码不变
· 「2号 → 3号」：员工之间在互发消息；同事的消息要你批准才送达
· 底栏小标签是用时、模型、上下文、额度，快满了变橙、变红；上下文快满时会出现〔压缩上下文〕
· 跑长实验时只说「还在等」的，只更新看板，不打扰你
**其他**
· 发图片 / 文件：先发图，再打字说要它做什么，会一起发出去
· / 开头的斜杠命令（/compact、/model…）照常发给对面"""


class Remote:
    def __init__(self):
        self.state = load_json(STATE, {})
        self.state.setdefault("current", None)
        self.state.setdefault("remote_pending", {})   # 名字 -> 远程发话时间

    def save(self):
        save_json(STATE, self.state)

    # ------------------------------------------------------------ 夜里免打扰

    def night(self):
        """夜里是几点到几点：(1, 9) 是 1:00–9:00，None 是不分。CLAUDE_MONITOR_QUIET_HOURS 写了「1-8」就按它，
        写 off 就不分；默认按你最近三周几点跟 Claude 说话算（learn_night），一天算一次。"""
        if QUIET_HOURS.lower() in ("off", "no", "none", "0", ""):
            return None
        m = re.fullmatch(r"(\d{1,2})\s*[-~到]\s*(\d{1,2})", QUIET_HOURS)
        if m:
            return int(m.group(1)) % 24, int(m.group(2)) % 24
        day = time.strftime("%Y-%m-%d")
        if (self.state.get("night") or {}).get("day") != day:
            self.state["night"] = {"day": day, "hours": learn_night()}
        hours = self.state["night"]["hours"]
        return tuple(hours) if hours else None

    def quiet_now(self):
        """现在是夜里、你这半小时也没动过：不用你点的先攒着不推。"""
        hours = self.night()
        if not hours or now() - self.state.get("seen_you", 0) < AWAKE_SECS:
            return False
        h, (a, b) = time.localtime().tm_hour, hours
        return a <= h < b if a < b else h >= a or h < b

    def saw_you(self):
        """你在飞书上发了消息、点了按钮：你醒着，照常推。"""
        self.state = load_json(STATE, self.state)
        self.state["seen_you"] = now()
        self.save()

    def push(self, msg, kind, must=False):
        """发一条通知。夜里你没在用时，不用你点的（汇报、进展、退出、卡住…）先攒着，看板上显示攒了几条；
        弹框在问你、要你批准的照常推。"""
        if must or kind not in HOLD_KINDS or not self.quiet_now():
            return notify(msg, kind)
        hold(msg, kind)
        self.state["held"] = self.state.get("held", 0) + 1

    def release_held(self):
        """〔🌙 看攒下的〕：夜里没推的按时间顺序发出来，同一个员工的汇报合成一张。"""
        items = take_held()
        self.state["held"] = 0
        self.save()
        if not items:
            return "没有攒下的消息了。"
        cards, chain = [], {}                            # chain：员工 -> 它的汇报（合成一张，放在它最后一条的位置）
        for item in items:
            card = as_card(item)
            name = (card.merge or {}).get("name")
            if name:
                if name in chain:
                    cards.remove(chain[name][-1])
                chain.setdefault(name, []).append(card)
                cards.append(card)
            else:
                cards.append(card)
        out = []
        for card in cards:
            name = (card.merge or {}).get("name")
            same = chain.get(name, [])
            if name and len(same) > 1 and card is same[-1]:
                card = merged_card([c.merge for c in same[:-1]], card)
            out.append(card)
        first = time.strftime("%H:%M", time.localtime(items[0]["t"]))
        head = alert(f"🌙 你不在时攒下的 {len(items)} 条", f"{first} 以来没推送的消息，按时间顺序在下面（在问你、要你批准的当时已经推了）。", "grey")
        head.more = out + head.more
        return head

    # 注册表：活着的会话随时更新；死掉的保留，供重启
    def refresh(self):
        live = discover()
        reg = load_json(REGISTRY, {})
        for name, s in live.items():
            entry = dict(s, alive=True, seen=now())
            reg[name] = entry
        for name, e in reg.items():
            if e.get("alive") and name not in live:
                e["alive"], e["died"] = False, now()
        def keep(k, v):
            days = 30 if is_assistant(k) else 7
            return v.get("alive") or v.get("died", now()) > now() - days * 86400
        reg = {k: v for k, v in reg.items() if keep(k, v)}
        save_json(REGISTRY, reg)
        _reg_cache.update(t=time.time(), reg=reg)
        return live, reg

    def resolve(self, word, reg, want_alive=True):
        m = re.fullmatch(r"(\d+)号?", word or "")
        if m:                                            # 工号：@2 / 2号
            k = int(m.group(1))
            cur = {n: x for x, n in load_json(BADGES, {}).items()}.get(k)
            old = self.renum_old(k, reg)                 # 工号重排后一天内：这个号原来是谁
            if old and cur and reg[old].get("alive") and not (VIA["via"] == "feishu" and self.state["renum"].get("new_ok")):
                return None, None, self.renum_clash(k, cur, old)   # 新旧两个人都在上班：不猜，免得弄错人
            if old and not cur:
                self.renum_note = renum_hint(k, old, self.state["renum"]["t"])   # 这个号现在没人用：照旧找原来那个人，提醒它的新号
            word = cur or old or word
        pool = {k: v for k, v in reg.items() if v.get("alive") or not want_alive}
        if word not in pool and self.state.get("renamed", {}).get(word) in pool:
            word = self.state["renamed"][word]
        if word in pool:
            return word, pool[word], None
        hits = [k for k in pool if word.lower() in k.lower()]
        if len(hits) == 1:
            return hits[0], pool[hits[0]], None
        if not hits:
            return None, None, f"没有找到会话「{word}」。发「状态」看看有哪些。"
        return None, None, f"「{word}」匹配到多个：{'、'.join(sorted(hits))}，写完整一点。"

    def handle(self, text, typed=False, to=None, dm=False):
        """处理一条命令或一句话。typed=True 是你在手机上手打的：除了 TYPED_WORDS 这几个词，机器人不猜你的意思，
        一律交给助手（或你正在对话的员工）去理解；to 是你引用了哪个员工的卡片，就直接对它说。
        dm=True 是在你和机器人的单聊（助手自己的对话框）里打的：不管群里正跟哪个员工对话，都交给助手。
        命令行（助手用的 cr）和卡片按钮走下面的命令。用了重排前的旧工号：回复前面加一句「11号现在是6号（名字）」。"""
        self.renum_note = ""
        out = self._handle(text, typed, to, dm)
        note = self.renum_note
        if note and isinstance(out, Card):
            head, sep, rest = out.text.partition("\n")     # 命令行里：标题下面第一行（标题行要补工号和类型）
            out.text = f"{head}\n{note}{sep}{rest}"
            if VIA["via"] == "feishu":
                out.more.append(note)                    # 飞书上：卡片下面跟一行字
        elif note and isinstance(out, str):
            out = f"{note}\n{out}"
        return out

    def renum_old(self, k, reg):
        """工号重排后 RENUM_SECS 秒内：k 号原来是谁（跟现在的 k 号不是同一个人、还在名单里才算）；不是就返回 None。"""
        r = self.state.get("renum") or {}
        old = r.get("old", {}).get(str(k)) if now() - r.get("t", 0) < RENUM_SECS else None
        return old if old in reg else None

    def renum_clash(self, k, cur, old):
        return (f"⚠️ 工号{time.strftime('%H:%M', time.localtime(self.state['renum']['t']))}重排过，{k}号换人了：现在的 {k}号是 {cur}，"
                f"原来的 {k}号 {old} 现在是 {badge(old) or '下班了'}。这次没有执行，免得弄错人；请写名字再发一次（比如「@{old} …」）。")

    def renum_ask(self, k, cur, old, body, urgent):
        """飞书里「@k 内容」，k 号新旧两个人都在上班：先问发给谁，不猜。点了新号就当你已经换用新号，之后不再问。"""
        self.state["renum"]["ask"] = {"names": [cur, old], "body": body, "urgent": urgent, "t": now()}
        self.save()
        said = f"\n<font color='grey'>{one_line(body, 60)}</font>" if body else ""
        card = alert(f"🔢 {k}号换人了，这句发给谁？", f"工号今天重排过：现在的 {k}号是 **{cur}**，原来的 {k}号 **{old}** 现在是 {badge(old)}。"
                     f"这句还没发。{said}", "orange")
        card.card["elements"][-1:-1] = _rows([_button(f"{k}号 {cur}（新号）", "#renum 0", "primary"),
                                              _button(f"{badge(old)} {old}（原 {k}号）", "#renum 1")])
        return card

    def _handle(self, text, typed=False, to=None, dm=False):
        text = text.strip()
        if not text:
            return "（空消息）"
        self.state = load_json(STATE, self.state)
        live, reg = self.refresh()
        head, _, rest = text.partition(" ")
        rest = rest.strip()

        chat = None if dm else self.chat_target()      # 单聊是助手的对话框：群里正跟谁对话不算数
        if typed and to and not text.startswith("@"):   # 引用了谁的卡片就对它说；开头 @了人（引用 A 却「@B …」）以 @ 为准，走下面
            return self.deliver(to, text, reg)
        if typed and text not in TYPED_WORDS and text not in EXIT_WORDS:
            direct = self.direct_at(text, reg)
            if direct:
                return direct
            if text.startswith("@"):                     # @了人但不能直接发（好几个人 / 动作 / @助手）：交给秘书，不发给正在单聊的员工
                return self.say(re.sub(r"^@(助手|秘书)[\s，,:：]*", "", text) or text, None, reg)
            return self.say(text, chat, reg)
        if text.startswith("#"):                        # 只有卡片按钮会发 # 开头的命令
            return self.button_cmd(text[1:].split(), reg)
        # 只有 3 个单独的关键词归机器人；带名字的命令必须是「命令 空格 名字」。其他话都发给 Claude。
        if text in ("帮助", "help"):
            return HELP
        if text in ("看看", "看板"):                     # 把看板叫到聊天最底下（旧的那张撤回），不用点置顶跳上去
            card = self.status_card()
            card.summon = True
            self.save()
            return card
        if text in ("列表", "名单"):
            return self.menu(reg)
        if text in EXIT_WORDS:
            self.state["current"] = None
            self.save()
            if chat:
                default = self.state.get("default")
                body = (f"现在你打的字又发给 ⭐【{default}】了。" if default and reg.get(default, {}).get("alive")
                        else "现在你打的字不会发给任何 Claude。")
                if chat in self.state["remote_pending"]:
                    body += f"\n【{chat}】还在干活，不会被打断；做完它的回复照样发给你（那张“思考中”卡片会自己变成回复）。"
                return alert(f"↩ 不再发给【{chat}】", body, "grey",
                             [_button("返回列表", "看看"), _button("✍️ 再回复它", f"对话 {chat}")])
            return self.menu(reg)
        if head == "思考程度" or text == "思考程度":
            level = {cn: lv for lv, cn in EFFORTS}.get(rest, rest if rest in EFFORT_CN else None)
            target = (None if level else rest) or chat or self.state.get("default")
            name, s, err = self.resolve(target, reg) if target else (None, None, "用法：思考程度 名字")
            if err:
                return err
            return self.button_cmd(["effort", name, level], reg) if level else self.effort_card(name, s)
        if not rest:
            head = ""                                   # 单个词（不是上面 3 个关键词）→ 当普通话处理
        if text in ("新助手", "新对话"):
            return self.cmd_new_assistant()
        if text == "对话记录":
            return self.history(reg)
        if head == "切换" and rest:
            return self.cmd_switch(rest, reg)
        if head == "改名":
            old, _, new = rest.partition(" ")
            if not new.strip():
                return "用法：改名 名字 新名字（比如「改名 forcing:0 SGP实验」）"
            return self.cmd_rename(old, new.strip(), reg)
        if head == "开除":                                # 只会从命令行来（助手）：给你发确认卡，你点了才开除
            if not rest:
                return "用法：开除 名字"
            name, s, err = self.resolve(rest, reg, want_alive=False)
            if err:
                return err
            card = self.fire_card(name, s)
            if not isinstance(card, Card):
                return card
            notify(card, "fire")
            return f"已经把「确定开除【{name}】？」的确认卡片发给用户了，用户点〔确定开除〕才会执行。"
        if head in ("关闭", "下班") and len(re.split(r"[\s,，、]+", rest.strip())) > 1:
            return self.cmd_off_many(re.split(r"[\s,，、]+", rest.strip()), reg)
        if head in ("设为默认", "关闭", "下班"):
            if not rest:
                return f"用法：{head} 名字"
            name, s, err = self.resolve(rest, reg)
            if err:
                return err
            if head in ("关闭", "下班"):
                return self.cmd_close(name, s)
            self.state["default"] = name
            self.save()
            return alert(f"⭐ 已把【{name}】设为默认", "之后随手打的字都会发给它。想临时找别的 Claude，点它卡片上的〔✍️ 回复它〕，或引用它的消息回复。",
                         "blue", [_button("返回列表", "看看")])
        if head in ("上班", "叫回"):                      # 下班的员工回来：在原目录接着原来的对话
            name, s, err = self.resolve(rest, reg, want_alive=False) if rest else (None, None, "用法：上班 名字")
            if err or s.get("alive"):
                return err or f"【{name}】在上班呢。"
            s, why = self.wake(name, s)
            if not s:
                return why if isinstance(why, Card) else f"【{name}】没能上班：\n{why}"
            return alert(f"☀️【{name}】上班了", "\n".join(x for x in (self.who(name, s), "接着原来的对话，给它派活：「@" +
                         (badge(name)[:-1] or name) + " 内容」。") if x), "green",
                         [_button("✍️ 回复它", f"对话 {name}", "primary"), _button("看屏幕", f"屏 {name}")])
        if head in ("看", "屏", "进入", "对话", "重启"):
            if not rest:
                return f"用法：{head} 名字"
            word, _, opt = rest.partition(" ")
            name, s, err = self.resolve(word, reg, want_alive=head not in ("重启", "进入"))
            if err:
                return err
            if head == "看":
                if opt.strip() == "全部":
                    return f"💬【{name}】最后一轮全部内容：\n" + (last_reply(s.get("sessionId")) or "（还没有回复）")
                body = last_reply(s.get("sessionId"), final_only=True) or "（还没有回复）"
                return alert(f"💬【{name}】最新结论", body, "green", self.chat_buttons(name))
            if head == "屏":
                return self.screen_card(name, s)
            if head == "进入":
                return self.session_card(name, s)
            if head == "对话":
                prev = chat if chat and chat != name else None
                self.state["current"], self.state["current_at"] = name, now()
                self.save()
                back = self.state.get("default") or "默认对象"
                title = f"✍️ 现在打字是发给【{name}】"
                body = (self.who(name, s) + "\n" + (f"（不再发给【{prev}】。）" if prev else "") +
                        f"你接下来打的每句话都发给【{name}】，不再发给【{back}】。它的回复会以「💬【{name}】」出现。\n"
                        f"说完点〔↩ 回到{back}〕或发「回到助手」；它还没回复时也可以回去，它会接着干完、回复照样发给你。"
                        f"30 分钟不说话也会自动回到{back}。")
                return alert(title, body, "green", self.chat_buttons(name) + [self.effort_button(name, s), _button("返回列表", "看看")])
            return self.cmd_restart(name, s)
        if head == "新建":
            parts = split_args(rest)
            if len(parts) < 2:
                return "用法：新建 名字 目录 [claude 参数…]，或者 新建 名字 目录 codex[-套] [codex 参数…]"
            extra = parts[2:]
            if extra and extra[0].lower().startswith("codex"):   # 招 codex 员工：「codex」是第一套，「codex-标签」按标签找
                h = crcodex.pick_home(extra[0])
                if not h:
                    return (f"不认识「{extra[0]}」。这台机器上的 codex 有：" +
                            "、".join(f"codex-{x['label'].lower()}（{x['home']}）" if x["label"] else f"codex（{x['home']}）"
                                     for x in crcodex.homes()) + "；只写 codex 就是第一套。")
                if not parts[0].lower().startswith("cx-"):
                    parts[0] = "cx-" + parts[0]                # 名字上也看得出是 codex（类型标记不靠名字，按实际跑的程序认）
                ok, out = launch(parts[0], parts[1], extra[1:], home=h["home"])
            else:
                ok, out = launch(parts[0], parts[1], extra or DEFAULT_NEW_ARGS)
            return out if not ok else out + "\n" + self.after_launch(parts[0])
        if head == "键":
            target, tokens = self.state.get("current"), rest.split()
            if tokens and tokens[0].startswith("@"):
                target, tokens = tokens[0][1:], tokens[1:]
            if not target or not tokens:
                return "用法：键 [@名字] 上 下 回车 …"
            name, s, err = self.resolve(target, reg)
            if err:
                return err
            send_keys(s, tokens)
            time.sleep(1.5)
            return self.screen_card(name, s, note=f"⌨️ 已按：{' '.join(tokens)}")

        # 命令行里的「@名字 内容」：发给它；「@所有人 内容」：开会
        if text.startswith("@"):
            target, _, body = text[1:].partition(" ")
            if target in ("_all", "所有人", "全体", "all"):
                return self.broadcast(body.strip(), reg)
            urgent = target.endswith(("!", "！"))           # 「@3! 内容」：它在忙也不排队，立即发
            return self.deliver(target.rstrip("!！"), body.strip(), reg, urgent=urgent)
        return self.say(text, chat, reg)

    def direct_at(self, text, reg):
        """手打的「@3 内容」：只点了一个人、对得上、内容是正常的话，就直接发给它（不经过秘书，最快）。
        点了好几个人（「@1 2 6 下班」）、只说了「下班 / 上班」这类动作、对不上是谁，都返回 None 交给秘书去理解。
        带了图片 / 文件的（飞书里「@11 + 截图」，不管图在前在后，文字只剩「@11」）：只有 @ 没有话也直接发，图跟着过去。"""
        m = re.match(r"@\s*(\d+号?)(?![\d号])([!！])?[\s，,:：、]*(.*)", text, re.S) or \
            re.match(r"@\s*([^\s，,:：、!！]+(?::\d+)?)([!！])?(?:[\s，,:：、]+(.*))?$", text, re.S)  # 工号后面不加空格也认（「@3帮我看看」）；名字可以带「:0」
        if not m:
            return None
        who, urgent, body = m.group(1), bool(m.group(2)), (m.group(3) or "").strip()   # 「@3! 内容」：它在忙也不排队，立即发
        if not body and not self.held_files():
            return None
        if re.match(r"@|\d+号?(?:[\s，,、]|$)", body) or body in ACTION_WORDS:
            return None
        if who in ("所有人", "全体", "all"):
            if not body:
                return None                              # 「@所有人 + 图」：开会只发文字，交给秘书
            return self.broadcast(body, reg)
        default = self.state.get("default")
        if who in ("助手", "秘书") or (default and who == default):
            return None                                  # 本来就交给秘书
        n = re.fullmatch(r"(\d+)号?", who)
        if n:
            k = int(n.group(1))
            cur = {x: m for m, x in load_json(BADGES, {}).items()}.get(k)
            old = self.renum_old(k, reg)
            if old and cur and reg[old].get("alive") and not self.state["renum"].get("new_ok"):
                return self.renum_ask(k, cur, old, body, urgent)   # 工号刚重排过，新旧两个人都在上班：问你发给谁
            if not cur and not old:
                return None                              # 没有这个工号：别拿数字去模糊匹配名字
        name, _, err = self.resolve(who, reg, want_alive=False)
        if err or not name or name.startswith("助手"):
            return None
        return self.deliver(name, body, reg, urgent=urgent)

    def say(self, text, chat, reg):
        """一句普通的话：发给你正在对话的员工；没有就发给默认对象（秘书）。"""
        target = chat
        if chat:
            self.state["current_at"] = now()
        else:
            default = self.state.get("default")
            if default and reg.get(default, {}).get("alive"):
                target = default
            elif default:
                return alert(f"⭐ 默认的【{default}】已退出", "这句话没有发出去。", "red",
                             [_button("重启它", f"重启 {default}", "danger"), _button("🆕 新对话", "新对话"),
                              _button("返回列表", "看看")])
        held = self.held_files()
        if held and held.get("for"):
            target = held["for"]                        # 图片是引用某张卡片发的：这句话也发给它
        if not target:
            expired = self.state.pop("expired_chat", None)
            note = f"（和【{expired}】的对话 30 分钟没说话，已自动结束。）\n" if expired else ""
            self.save()
            return (f"🤖 我是监控机器人，这句话没有转给任何 Claude。\n{note}"
                    "要给某个 Claude 发消息：在它的卡片上点〔✍️ 回复它〕，或引用它的消息回复。发「帮助」看全部命令。")
        return self.deliver(target, text, reg)

    def deliver(self, target, body, reg, urgent=False):
        """把你的话（连同你先发来的图片 / 文件）发给它，回一张实时进度卡片。
        它正在干活时这句话会排队，等它这一步做完才看得到；urgent（「@3! 内容」）就立即发出去（ctrl+x ctrl+s）。"""
        held = self.held_files()
        files = held["paths"] if held and not body.startswith("/") else []
        if not body and not files:
            return "内容是空的。"
        name, s, err = self.resolve(target, reg)
        if err:                                           # 下班的员工：先让它上班，再把这句话发给它
            off, e, err2 = self.resolve(target, reg, want_alive=False)
            if err2 or e.get("alive") or is_assistant(off):
                return err
            s, why = self.wake(off, e)
            if not s:
                return why if isinstance(why, Card) else f"【{off}】下班了，想叫它上班但没成功：\n{why}"
            name = off
        m = re.fullmatch(r"/effort(?:\s+(\w+))?", body.strip())
        if m and not files:                               # 「@4 /effort」：不让终端弹那个只能左右拨的滑块，给一张选档位的卡片
            level = (m.group(1) or "").lower()
            level = {cn: en for en, cn in EFFORTS}.get(level, level)    # 「/effort 很高」也认
            return self.button_cmd(["effort", name, level], reg) if level in EFFORT_CN else self.effort_card(name, s)
        rows = pane_rows(s)
        qbox = crcodex.question_box(rows) if is_codex(s) and not dialog_options(rows) else None
        if qbox and VIA["via"] == "feishu":              # codex 在问你几道题、你在飞书上打的字：当第一道题的回答（填进 Other）
            if not body:
                return self.ask_card(name, s, note="图片 / 文件先留着，答完题再发。") or "它在问你问题，图片 / 文件先留着，答完再发。"
            return self.codex_pick(name, s, None, None, None, text=body)
        if qbox and qbox["open"]:                        # 秘书 / 同事转来的话，提问框又正开着（有人在电脑上答题）：打进去会落在框里
            return self.ask_card(name, s, note="⚠️ 它的提问框正开着，这句话没有发，免得打进框里。") \
                or f"【{name}】的提问框正开着，这句话没有发。"
        if s.get("waiting") and not dialog_options(rows) and not qbox:   # 停在一个不是问题的框上：打的字会被框当成按键吃掉，不能发
                                                         # （提问框收起时主输入框照常能用：转来的话照常发，题还开着）
            held = held_peer(rows)
            if held:                                          # 同事发来的消息等批准：先存着你这句，点完〔送达〕/〔拒绝〕自动发
                if body:
                    self.state["held_msg"] = {"name": name, "body": body, "t": now()}
                    self.save()
                return self.held_card(name, s, held, reg)
            asking = effort_asking(rows)
            if asking and body:
                self.state["held_msg"] = {"name": name, "body": body, "t": now()}
                self.save()
                return self.effort_confirm_card(name, s, asking)
            return self.screen_card(name, s, note="⚠️ 它停在下面这个框上，你这句话没有发（打进去会被框当成按键）。"
                                                  "先用下面的键处理掉这个框，再发一次。")
        if is_codex(s) and not s.get("waiting") and crcodex.menu(rows):   # codex 停在菜单上（/model、启动时的框…）：打的字会被菜单吃掉
            return self.screen_card(name, s, note="⚠️ 它停在下面这个菜单上，你这句话没有发（打进去会被当成按键）。先用下面的键处理掉，再发一次。")
        if body.startswith("/") and not (s.get("waiting") and dialog_options(rows)):
            return self.run_slash(name, s, body)
        sent_at = now()
        if s.get("waiting") and dialog_options(rows):
            if not body:
                return self.ask_card(name, s, note="⚠️ 它停在一个问题上，图片 / 文件先留着，回答完再发。") or "它停在一个选择框上，先点〔看屏幕〕看看。"
            other = self.answer_with_text(name, s, body)    # 它停在选择框上：你打的字当成回答，不会被当成新消息
            if other:
                return other
            self.state.setdefault("asked_note", {})[name] = [sent_at, f"回答：{body}"]
        else:
            left = input_text(s, rows) if not s.get("waiting") else ""
            if left and same_start(left, last_human_text(s.get("sessionId"))) and not clear_box(s):
                return self.screen_card(name, s, note="⚠️ 它的输入框里还有上一句退回来的话，没清掉，你这句没有发（不然会接在后面一起发出去）。")
            send_text(s, body, files)                         # ↑ 被拦下、被打断后退回输入框的上一句：先清掉，不跟你这句粘在一起
            if urgent and not is_codex(s):                   # 它在忙：这句排上队了就立即发（codex 用回车发的本来就直接插进去）
                for _ in range(10):
                    time.sleep(0.3)
                    if queued(pane_rows(s)):
                        send_now(s)
                        break
            if files:
                self.state.pop("attach", None)
                if not body:
                    self.state.setdefault("asked_note", {})[name] = [sent_at, f"（发了 {len(files)} 个图片 / 文件）"]
        close = self.pend_since(name, sent_at, s)
        self.state.setdefault("typed_at", {})[name] = sent_at   # 机器人替人打的字：不算你在电脑前
        self.save()
        card = self.live_card(name, s, sent_at)
        card.pending_for, card.close_old = name, close
        return card

    # ------------------------------------------------------------ 斜杠命令：像在电脑上打「/」

    def run_slash(self, name, s, body):
        """发斜杠命令。只打了「/」或命令名的一部分：像电脑上一样列出候选命令，不直接执行。
        发出后回一张屏幕卡片（弹出菜单的用遥控键盘操作）；要跑一会儿的（/compact）做完再通知你。"""
        word = body.split()[0]
        if is_codex(s) and not crcodex.ready(s):         # codex 停在框上（比如「Resume paused goal?」）：字会被框吃掉，回车还会替你选第一项
            return self.screen_card(name, s, note="它停在一个框上，先用下面的按键处理，再发命令（这次没有按任何键）。")
        if body == word:
            cat = slash_catalog(s) or []
            if word not in dict(cat or [(c, d) for c, _, d in slash_menu(s)]):
                return self.cmds_card(name, s, query=word, cat=cat)   # 只打了「/」或半截：不能回车，回车会执行列表里第一个
        was_busy = s["status"] == "busy"
        before = session_status(s).get("ctx")
        sent_at = now()
        send_text(s, body)
        time.sleep(2.5)
        running = not was_busy and (discover().get(name) or s)["status"] == "busy"
        if running:
            self.state.setdefault("slash", {})[name] = [sent_at, word, before]   # 比如 /compact：做完发「✅ 做完了」
            self.save()
        rows = pane_rows(s)
        if screen_menu(rows):                           # 弹了菜单（/model、/resume…）：要遥控键盘
            return self.screen_card(name, s, note=f"➡️ 已把 {body} 发给【{name}】，用下面的按钮选：")
        label, desc = slash_label(word)
        if running:                                     # 像回复一样：卡片自己动（计时、转圈），做完原地换成结果
            card = self.slash_card(name, s, self.state["slash"][name])
            card.pending_for = name
            return card
        else:
            out = slash_output(rows, word)
            title, color = f"⌨️【{name}】{label}", "blue"
            text = f"<font color='grey'>{body}</font>\n" + (out if out else "已发出，没有输出。")
        card = alert(title, "\n".join(x for x in (self.who(name, s), text) if x), color, [_button("看屏幕", f"屏 {name}")])
        return card

    def broadcast(self, body, reg):
        """@所有人：发给每个在上班的员工，像开会。停在选择框上的先跳过；正在干活的，这句话会排在它手上的活后面。
        每个人做完的回复照常发过来。"""
        if not body:
            return "用法：@所有人 内容（发给每个在上班的员工）"
        sent, skipped = [], []
        for name, e in sorted(reg.items(), key=lambda kv: (load_json(BADGES, {}).get(kv[0]) or 999, kv[0])):
            if not e.get("alive") or is_assistant(name) or is_quiet(name):
                continue
            if e.get("waiting"):
                skipped.append(name)
                continue
            send_text(e, body)
            self.pend_since(name, now())
            sent.append(name)
        self.save()
        who = lambda names: "、".join(label_name(n) for n in names)
        lines = [f"已发给 {len(sent)} 位员工：{who(sent)}" if sent else "没有在上班的员工。"]
        if skipped:
            lines.append(f"{who(skipped)} 正停在选择框上等你，没发；先处理它的问题再说。")
        lines.append("它们做完会各自回复你。")
        return alert(f"📢 开会：{one_line(body, 30)}", "\n".join(lines), "blue", [_button("看板", "看看")])

    def held_card(self, name, e, held, reg):
        """「📨 2号收到 4号 的消息，要你批准」：对得上是你的员工就写清楚是谁；对不上（可能是同一台机器上别人的 Claude）就提醒你。"""
        sender = next((n for n, x in reg.items() if x.get("alive") and x.get("pid") == held["pid"]), None)
        if sender:
            who = f"{badge(sender)}【{sender}】"
            warn = ""
        else:
            who = f"一个不在你员工名单里的会话（自称 {held['name']}）"
            warn = "\n⚠️ **这不是你的员工发的**，可能是这台机器上别人的 Claude。不认识就点〔拒绝〕。"
        body = (f"{who}给它发了一条消息。两边的权限设置不一样，Claude Code 先拦下来，等你决定要不要送达。{warn}\n\n"
                f"**消息内容：**\n{held['body'] or '（看不到内容，点〔看屏幕〕）'}")
        mine = self.state.get("held_msg")
        if mine and mine["name"] == name:
            body += f"\n\n你刚才那句还没发：「{one_line(mine['body'], 60)}」。点哪个按钮都会接着把它发出去。"
        return alert(f"📨【{name}】收到同事消息，要你批准", body, "yellow",
                     [_button("送达", f"#heldmsg {name} deliver", "primary"), _button("拒绝", f"#heldmsg {name} deny", "danger"),
                      _button("看屏幕", f"屏 {name}")])

    def held_answer(self, name, s, how):
        """卡片上点了〔送达〕/〔拒绝〕：框还在才按（光标默认在「拒绝」上：送达＝下移一格再回车）。"""
        if not held_peer(pane_rows(s)):
            card = alert(f"✅【{name}】这条消息已经处理过了", "可能你已经在电脑上处理了，这次没有按任何键。", "grey",
                         [_button("看屏幕", f"屏 {name}")])
        else:
            send_keys(s, ["下", "回车"] if how == "deliver" else ["回车"])
            time.sleep(1)
            mine = self.state.get("held_msg")
            if mine and mine["name"] == name and now() - mine["t"] < 1800 and not held_peer(pane_rows(s)):
                self.state.pop("held_msg")
                self.save()
                card = self.deliver(name, mine["body"], self.refresh()[1])   # 框处理掉了：接着把你刚才那句发出去
                if isinstance(card, Card):
                    card.in_place = True
                return card
            card = alert(f"{'📬 已送达' if how == 'deliver' else '🚫 已拒绝'}【{name}】的同事消息",
                         "它会读到这条消息并接着处理。" if how == "deliver" else "这条消息丢掉了，发送方会收到「被拒绝」的通知。", "grey",
                         [_button("看屏幕", f"屏 {name}")])
        card.in_place = True
        return card

    def compact_button(self, name):
        b = _button("🗜 压缩上下文", f"#slash {name} /compact", "primary")
        b["confirm"] = {"title": {"tag": "plain_text", "content": f"压缩【{name}】的上下文？"},
                        "text": {"tag": "plain_text", "content": "就是 /compact：把前面的对话压成要点，腾出空间，之后接着聊。"}}
        return b

    def cmds_card(self, name, s, query="", cat=None):
        """斜杠命令面板：像在电脑上打「/」弹出的列表。常用的直接点；发「/关键词」搜索全部命令。"""
        cat = slash_catalog(s) if cat is None else cat
        full = bool(cat)
        common = slash_menu(s)
        cat = cat or [(c, d) for c, _, d in common]         # 列不出全部时，只在常用的里面找
        q = query.lstrip("/").lower()
        bar = session_status(s)
        head = ""
        els = []
        if q:
            hits = [c for c in cat or [] if c[0][1:].lower().startswith(q)]
            hits += [c for c in cat or [] if c not in hits and (q in c[0].lower() or q in c[1].lower())]
            title = f"⌨️【{name}】匹配「/{q}」的命令" if hits else f"⌨️【{name}】没有「/{q}」开头的命令"
            lines = [f"**{c}**　<font color='grey'>{one_line(d, 50)}</font>" for c, d in hits[:16]]
            els.append(_md(head + ("\n".join(lines) or "换个词试试，或者发「/」看全部。")
                           + ("" if full else "\n<font color='grey'>它的输入框现在有字，或正停在选择框上，只在常用命令里找了；没有发出任何命令。</font>")))
            buttons = [_button(c, f"#slash {name} {c}") for c, _ in hits[:16]]
        else:
            title = f"⌨️【{name}】斜杠命令"
            lines = [f"**{c}** {label}　<font color='grey'>{desc}</font>" for c, label, desc in common]
            els.append(_md(head + "**常用**\n" + "\n".join(lines)))
            buttons = [_button(label, f"#slash {name} {c}") for c, label, _ in common]
            if full:
                others = [c for c, _ in cat if c not in {x[0] for x in common}]
                els += [{"tag": "hr"}, _md(f"**全部 {len(cat)} 个**（含技能、自定义命令）\n<font color='grey'>{'　'.join(others)}</font>")]
            else:
                els.append(_md("<font color='grey'>它的输入框现在有字，或正停在选择框上，暂时列不出全部命令。</font>"))
        els += _rows(buttons)
        els.append(_md("<font color='grey'>发「/关键词」搜索，比如 /co；带内容的命令直接发，比如「/goal 把论文改完」。"
                       "点了会弹菜单的（/model、/resume），屏幕卡片上用遥控键盘选。</font>"))
        return Card(_card(title, "blue", els, tags=usage_tags(bar)), f"{title}\n" + "\n".join(c for c, _ in cat))

    # ------------------------------------------------------------ 图片 / 文件：像在电脑上贴截图、点开图片

    def held_files(self):
        """你先发来、还没发出去的图片 / 文件（30 分钟内有效）。"""
        held = self.state.get("attach")
        if held and now() - held.get("at", 0) > 30 * 60:
            self.state.pop("attach", None)
            return None
        return held

    def handle_files(self, paths, text, name=None, dm=False):
        """你从飞书发来图片 / 文件：像在电脑上先贴截图再打字，附在你下一句话前面一起发。
        图文一起发的（带了文字）马上发出去。在单聊（助手的对话框）里发的给助手；文字开头 @了人（「截图 + @6 …」）
        点名优先：不管在单聊还是引用了谁的卡片，都发给被 @ 的人，图跟着过去。"""
        self.state = load_json(STATE, self.state)
        if text.strip().startswith("@"):
            name = None                                  # 交给 direct_at；也别把图记成给助手 / 引用的那个
        else:
            name = name or (self.state.get("default") if dm else None)
        held = self.held_files() or {"paths": []}
        held.update(paths=held["paths"] + paths, at=now(), **{"for": name or held.get("for")})
        self.state["attach"] = held
        self.save()
        if text.strip():
            return self.handle(text, typed=True, to=name, dm=dm)
        target = held["for"] or self.chat_target() or self.state.get("default") or "（还没有对象）"
        what = "图片" if all(p.lower().endswith(IMAGE_EXTS) for p in held["paths"]) else "文件"
        return alert(f"📎 收到 {len(held['paths'])} 个{what}，还没发出去",
                     f"接着打字说要它做什么，会和{what}一起发给【{target}】，就像在电脑上先贴截图再打字。\n"
                     f"不想说话也可以点〔直接发给它〕。", "blue",
                     [_button("直接发给它", "#attach send", "primary"), _button("不发了", "#attach drop")])

    def attach_cmd(self, op, reg):
        held = self.held_files()
        if op == "send" and held:
            target = held.get("for") or self.chat_target() or self.state.get("default")
            return self.deliver(target, "", reg) if target else "还没有默认对象，先在某个 Claude 的卡片上点〔✍️ 回复它〕。"
        self.state.pop("attach", None)
        self.save()
        card = alert("📎 已取消，图片 / 文件没有发出去" if held else "📎 没有待发的图片 / 文件", "", "grey")
        card.in_place = True
        return card

    # ------------------------------------------------------------ 看屏幕 + 遥控键盘：像坐在电脑前按键

    def screen_card(self, name, s, note=""):
        """屏幕内容 + 遥控键盘。屏幕上有菜单（/model、/resume…）时每一项一个按钮：点了光标移过去，
        再点底部提示的键（回车 / s / Esc…）确认，跟在电脑前用方向键一样。每按一下卡片原地刷新。"""
        rows = pane_rows(s)
        menu = screen_menu(rows)
        if menu and menu["items"] and menu_scrolls(rows):    # 只显示了一截：所有选项都给按钮，不用一格一格往上翻
            menu["items"] = self.full_menu(name, s, menu)
            time.sleep(0.2)
            rows = pane_rows(s)
            menu = dict(screen_menu(rows) or menu, items=menu["items"])
        sig = menu["sig"] if menu else "-"
        if s.get("waiting") and not note:               # 停在它问你 / 要你批准的框上：直接给结构化的那张（选项、预览、按钮）
            card = self.ask_card(name, s)
            if card:
                return card
        text = clean_screen(rows)

        def key(label, k, kind="default"):
            return _button(label, f"#key {name} {sig} {k}", kind)

        state, color = screen_state(s, rows)            # 顶上一行：它在干什么（思考中 · 27s / 等你选择 / 空闲…），排着队的话也标上
        waiting = queued(rows)
        tags = _pill(state).replace("neutral", color, 1) + (" " + _pill("有话在排队", 1) if waiting else "")
        els = [_mk(tags)] + ([_mk(note)] if note else [])
        if menu and menu["items"]:                      # 菜单：不贴原画面，写成「标题 + 选项（当前那项标出来）」，按钮一一对应
            title = menu_title(rows, menu)
            opts = "\n".join(f"{n}. {fit(label, LINE_EM - 6)}" + (" " + _pill("当前", 0).replace("neutral", "blue") if n == menu["cursor"] else "")
                             for n, label in menu["items"][:24])
            els.append({"tag": "column_set", "flex_mode": "none", "background_style": "grey", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": ([{"tag": "div", "text": {"tag": "plain_text", "content": title}}] if title else []) + [_mk(opts)]}]})
        else:
            els.append({"tag": "column_set", "flex_mode": "none", "background_style": "grey", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1,
                 "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text or "（空白）"}}]}]})
        if menu:
            items = [key(f"{n}. {label}", f"item:{n}", "primary" if n == menu["cursor"] else "default") for n, label in menu["items"][:24]]
            pick_key, pick_desc = pick_action(menu)
            pad = ([] if items else [key("↑", "Up"), key("↓", "Down")]) + [key(label, k) for k, label in menu["keys"]
                                                                        if not (items and k in ("Up", "Down"))]
            if not any(k == "Escape" for k, _ in menu["keys"]):
                pad.append(key("Esc：取消", "Escape"))
            els += _pairs(items) + _rows(pad)
            note_line = (f"点一下选项就{pick_desc}" if items else "用上面的键操作，屏幕会跟着刷新")
        else:
            note_line = None
        main = [key("刷新", "refresh")]
        if waiting and not is_codex(s):
            main.insert(0, _button("立即发送", f"#sendnow {name}", "primary"))
        if s.get("status") == "busy" and not menu:
            main.append(_button("停止", f"#stop {name}", "danger"))
        if self.state.get("current") != name:
            main.append(_button("回复它", f"对话 {name}"))
        els.append(_actions(main[:3], main[3:]))          # 露出 3 个，其余在「···」里
        return Card(_card(f"🖥【{name}】屏幕", "grey", els, footer=note_line, tags=usage_tags(session_status(s)), state=self.state),
                    f"🤖【{name}】当前屏幕：\n{text}")

    def full_menu(self, name, s, menu):
        """长菜单的全部选项。走一遍要几秒，记 10 分钟；屏幕上看得到的选项和记下的对不上（换了个菜单）就重新走。"""
        cache = getattr(self, "_menus", {})
        self._menus = cache
        hit = cache.get(name)
        if hit and now() - hit["t"] < 600 and all(hit["items"].get(n) == label for n, label in menu["items"]):
            return sorted(hit["items"].items())
        items = scan_menu(s, menu)
        cache[name] = {"t": now(), "items": dict(items)}
        return items

    def press(self, name, s, sig, k):
        """遥控键盘按了一个键：屏幕还是你看到的那个才按，按完原地刷新屏幕。"""
        ok = k == "refresh" or k.startswith("item:") or k in ("Enter", "Escape", "Tab", "Space", "Up", "Down", "Left", "Right") \
            or re.fullmatch(r"[a-z]|C-[a-z]", k)
        if not ok:
            return None
        note = ""
        if k != "refresh":
            menu = screen_menu(pane_rows(s))
            if (menu["sig"] if menu else "-") != sig:
                note = "⚠️ 屏幕已经变了，这次没有按键。看一眼现在的屏幕再按。"
            elif k.startswith("item:"):                  # 点选项＝光标移过去并选中，一下搞定（跟在手机 App 里点一样）
                want, cur = int(k[5:]), menu["cursor"]
                if cur is None:
                    note = "⚠️ 没找到光标在哪，这次没有按键。可以用 ↑ ↓ 移动。"
                for _ in range(abs(want - cur) if cur else 0):
                    crterm.key(s, "Down" if want > cur else "Up")
                    time.sleep(0.08)
                if cur is not None:
                    time.sleep(0.2)
                    crterm.key(s, pick_action(menu)[0])
            else:
                crterm.key(s, k)
            time.sleep(0.8)
        card = (k != "refresh" and not note and self.menu_done_card(name, s)) or self.screen_card(name, s, note=note)
        card.in_place = True
        return card

    def menu_done_card(self, name, s):
        """在菜单里选完了、菜单关了：/model 这类直接说结果（「已切到 Opus 5.5」），不贴整屏。
        菜单还开着、最近一句不是斜杠命令、或没认出输出的，返回 None（照常给屏幕卡片）。"""
        rows = pane_rows(s)
        if screen_menu(rows) or dialog_options(rows):
            return None
        box = _input_box(rows)
        asked = [r for r in rows[:box[0] if box else len(rows)] if r.startswith("❯ ") and r[2:].strip()]
        word = asked[-1].split()[1] if asked and asked[-1].startswith("❯ /") else ""
        out = slash_output(rows, word) if word else ""
        if not out or len(out.splitlines()) > 6:
            return None
        label = slash_label(word)[0]
        return alert(f"✅【{name}】{BUTTON_EMOJI.sub('', label).strip()}", out, "green", [_button("看屏幕", f"屏 {name}")])

    # ------------------------------------------------------------ 像 App 一样：实时过程、停止、思考程度

    def button_cmd(self, words, reg):
        """卡片按钮专用命令（# 开头，你打字不会碰到）：stop / effort / steps。"""
        if len(words) < 2:
            return None
        if words[0] == "attach":
            return self.attach_cmd(words[1], reg)
        if words[0] == "sendfile":
            path = sendable(" ".join(words[1:]))
            return Files([path]) if path else f"这个文件现在不在了（或不在 {_short_dir(FILES_ROOT)} 下面），没有发。"
        if words[0] == "wakeall":                       # 〔全部上班〕：开机后把断掉的员工都叫回来
            return self.wake_all(words[1:], reg)
        if words[0] == "night":                          # 看板上的〔🌙 看攒下的 N 条〕
            return self.release_held()
        if words[0] == "renum":                          # 「@k 内容」问发给谁时点的：0 是现在的 k 号，1 是原来的 k 号
            ask = (self.state.get("renum") or {}).pop("ask", None)
            if words[1] == "0":
                self.state["renum"]["new_ok"] = True     # 你已经换用新号了：之后 @ 新号直接发，不再问
            self.save()
            if not ask or words[1] not in ("0", "1"):
                return "这句已经处理过了。"
            card = self.deliver(ask["names"][int(words[1])], ask["body"], reg, urgent=ask["urgent"])
            if isinstance(card, Card):
                card.in_place = True                     # 这张问题卡片原地变成它的实时进度
            return card
        if words[0] == "board":                          # 看板上的〔刷新〕：原地换成最新的
            card = self.status_card()
            card.in_place = True
            self.save()
            return card
        if words[0] == "style" and len(words) > 2:      # 样式选择卡片上点的：记下你选的样式（state["style"]），之后的卡片都按它来
            self.state.setdefault("style", {})[words[1]] = words[2]
            self.save()
            card = alert("已记下你选的样式", f"之后的卡片按「{' '.join(words[3:]) or words[2]}」来。想换随时再点上面的选项。", "green")
            card.in_place = True
            return card
        if words[0] == "enter":                          # 看板 / 列表上点了员工名字：看它的详情，并且接下来打的字直接发给它
            name, e, err = self.resolve(words[1], reg, want_alive=False)
            if err:
                return err
            if is_assistant(name) or name == self.state.get("default"):
                self.state["current"] = None
            else:
                self.state["current"], self.state["current_at"] = name, now()
            self.save()
            return self.session_card(name, e)
        op, word = words[0], words[1]
        name, s, err = self.resolve(word, reg, want_alive=op != "fire")
        if err:
            return err
        if op == "heldmsg" and len(words) > 2:
            return self.held_answer(name, s, words[2])
        if op == "box" and len(words) > 2:              # 〔重新发〕〔清掉〕：退回输入框的那段话
            return self.box_cmd(name, s, words[2])
        if op == "fire":
            return self.cmd_fire(name, s, reg) if words[2:3] == ["yes"] else self.fire_card(name, s)
        if op == "stop":
            send_keys(s, ["esc"])                       # 卡片会自己变成「⏹ 已停止」，不用再发消息
            return None
        if op == "live":                                # 实时卡片上的〔刷新〕：原地换成最新的样子（平时也会每几秒自己刷新）
            since = self.state.get("remote_pending", {}).get(name)
            card = self.live_card(name, s, since) if since else self.screen_card(name, s)
            card.in_place = True
            return card
        if op == "sendnow":                             # 排着队的话立即发出去；实时卡片会自己刷新
            if is_codex(s) or not queued(pane_rows(s)):
                return alert(f"✅【{name}】没有在排队的话了", "可能它这一步刚好做完，已经看到了。这次没有按任何键。", "grey",
                             [_button("看屏幕", f"屏 {name}")])
            send_now(s)
            return None
        if op == "effort" and len(words) > 2 and words[2] in EFFORT_CN and is_codex(s):
            return self.codex_effort(name, s, words[2])
        if op == "effort" and len(words) > 2 and words[2] in EFFORT_CN:
            level, how = words[2], (words[3] if len(words) > 3 else "")
            asking = effort_asking(pane_rows(s))
            if how in ("yes", "no"):                    # 确认框上点了〔改成…〕或〔不改了〕
                if asking != level:
                    card = alert(f"✅【{name}】这一步已经过去了", "确认框已经不在了（可能你在电脑上处理过了），这次没有按任何键。", "grey",
                                 [self.effort_button(name, s), _button("看屏幕", f"屏 {name}")])
                    card.in_place = True
                    return card
                if how == "no":
                    send_keys(s, ["2"])                 # 2. No, go back
                else:
                    set_effort(s, level, confirm=True)
                for _ in range(15):                     # 状态文件比屏幕慢半秒左右：等它也不再显示停在框上，再接着发你那句
                    time.sleep(0.2)
                    if not (discover().get(name) or {}).get("waiting"):
                        break
            elif asking or s.get("waiting"):            # 已经停在一个框上：这时打「/effort」会被框吃掉
                card = self.effort_confirm_card(name, s, asking) if asking else self.screen_card(
                    name, s, note="⚠️ 它停在下面这个框上，先处理完再改思考程度。这次没有改。")
                card.in_place = True
                return card
            elif not set_effort(s, level):
                card = self.effort_confirm_card(name, s, level)
                card.in_place = True
                return card
            if how != "no" and is_assistant(name):
                self.state["assistant_effort"] = level   # 以后的新对话也用这个
                self.save()
            held = self.state.get("held_msg")
            if held and held["name"] == name:
                self.state.pop("held_msg")
                self.save()
            if held and held["name"] == name and now() - held["t"] < 1800:
                card = self.deliver(name, held["body"], self.refresh()[1])   # 框处理掉了：接着把你刚才那句发出去（用最新状态）
            else:
                card = self.effort_card(name, s, done=level if how != "no" else None)
            if isinstance(card, Card):
                card.in_place = True
            return card
        if op == "effort":
            return self.effort_card(name, s)
        if op == "rename":
            return alert(f"✏️ 给【{name}】起个好认的名字", f"{self.who(name, s)}\n直接发：**改名 {name} 新名字**\n比如：改名 {name} SGP实验",
                         "blue", [_button("返回列表", "看看")])
        if op == "steps":
            since = float(words[2]) if len(words) > 2 else s["statusAt"]
            return self.steps_card(name, s, since)
        if op == "ask":
            return self.ask_card(name, s) or f"【{name}】现在没有在等你回答或批准。"
        if op in ("mpick", "msubmit") and len(words) > 2:
            return self.multi_pick(name, s, words[2], words[3] if len(words) > 3 else "0", reg, submit=op == "msubmit",
                                   qi=words[4] if len(words) > 4 else 0)
        if op == "preview" and len(words) > 4:
            return self.preview_card(name, s, words[2], words[3], words[4])
        if op == "pick" and len(words) > 4:
            return self.pick(name, s, words[2], words[3], words[4])
        if op == "cxq" and len(words) > 4:               # codex 提问框：第几道题选第几项
            return self.codex_pick(name, s, words[2], words[3], words[4])
        if op == "cxqsend":                              # codex 提问框：〔提交〕卡片上选好的
            return self.codex_submit(name, s)
        if op == "key" and len(words) > 3:
            return self.press(name, s, words[2], words[3])
        if op == "cmds":
            return self.cmds_card(name, s)
        if op == "slash" and len(words) > 2 and re.fullmatch(r"/[\w:.\-]+", words[2]):
            return self.run_slash(name, s, words[2])
        return None

    # ------------------------------------------------------------ 它停下来问你：选择题、要权限

    def stayed_card(self, name, s, model):
        """安全检查拦下回答、你选了「不换」：它不会接着干，这张卡片原地换成「这次没答」，你那段话退回了输入框就说清楚。"""
        for _ in range(10):                             # 等框收起来，退回的那段话出现在输入框里
            time.sleep(0.3)
            rows = pane_rows(s)
            if not dialog_options(rows) and not paused_question(rows):
                break
        left = input_text(s, rows)
        buttons = [_button("看屏幕", f"屏 {name}")]
        if left and same_start(left, last_human_text(s.get("sessionId"))):
            body = (f"你那段话退回到了它的输入框里，还没发出去：\n<font color='grey'>「{one_line(left, 60)}」</font>\n\n"
                    "想再试一次就点〔重新发〕（可能还会被拦）；想换个说法，直接打字发给它就行，输入框里那段会先清掉。")
            buttons = [_button("重新发", f"#box {name} send", "primary"), _button("清掉", f"#box {name} clear")] + buttons
        else:
            body = "它停在这里，等你下一句。"
        card = alert(f"⏸【{name}】这次没答，还是用 {model}", "\n".join(x for x in (self.who(name, s), body) if x), "grey", buttons)
        card.in_place = True
        return card

    def box_cmd(self, name, s, how):
        """〔重新发〕〔清掉〕：退回输入框的那段话。先确认输入框里还是它，再按键。"""
        left = input_text(s) if not s.get("waiting") and s["status"] != "busy" else ""
        if not left or not same_start(left, last_human_text(s.get("sessionId"))):
            card = alert(f"✅【{name}】输入框已经变了", "那段话已经不在输入框里了（可能你在电脑上处理过），这次没有按任何键。", "grey",
                         [_button("看屏幕", f"屏 {name}")])
            card.in_place = True
            return card
        if how == "clear":
            ok = clear_box(s)
            card = alert(f"🧹【{name}】输入框清掉了" if ok else f"⚠️【{name}】没清掉", "刚才那段没有发。接下来直接打字发给它就行。" if ok
                         else "按了一下 Ctrl+C，输入框里还有字。点〔看屏幕〕看看。", "grey" if ok else "yellow", [_button("看屏幕", f"屏 {name}")])
            card.in_place = True
            return card
        crterm.key(s, "Enter")
        since = now()
        close = self.pend_since(name, since, s)
        self.state.setdefault("typed_at", {})[name] = since
        self.save()
        card = self.live_card(name, s, since)
        card.pending_for, card.in_place, card.close_old = name, True, close
        return card

    def ask_card(self, name, s, qi=0, pt=None, note=""):
        """它在问你（选择题）或要你批准（权限）：题目、选项、它前面说的话都发过来，一个选项一个按钮。
        找不到可显示的内容（屏幕上也没有选择框）就返回 None。"""
        rows = pane_rows(s)
        opts = dialog_options(rows)
        if not opts and not s.get("waiting"):
            return None
        paused = paused_question(rows) if not opts else None
        pt = None if paused else waiting_on(s, pt)
        b, said = pt if pt else ({}, [])
        if not opts and not pt and not paused:
            return None
        if b.get("name") == "CodexQuestions":           # codex 一次问你几道题
            return self.codex_q_card(name, s, b, note)
        tid, inp = (f"p{s['statusAt']:.0f}" if paused else b.get("id") or "-"), b.get("input") or {}
        who = self.who(name, s)
        context = "\n\n".join(said)
        if not context and is_codex(s):                  # codex：这一轮它在要批准之前说的最后一句
            turn = last_turn(s.get("sessionId")) or {}
            context = (turn.get("texts") or [""])[-1]
        if not context and above_dialog(rows) and b.get("name") != "AskUserQuestion":   # 选择题自己说得清，不贴屏幕上的零碎
            context = f"<font color='grey'>屏幕上它前面说的：</font>\n{above_dialog(rows)}"
        lines, buttons = [], []
        qs = (inp.get("questions") or []) if b.get("name") == "AskUserQuestion" else []
        built = paused or (builtin_question(qs[0]) if len(qs) == 1 else None)
        if built:                                       # Claude Code 自己的框：中文说明，不贴屏幕上的英文和你自己那段话
            title, color = f"🛡️【{name}】{built['title']}", "orange"
            asked = last_human_text(s.get("sessionId"))
            lines = [built["intro"]] + ([f"<font color='grey'>你问的：「{one_line(asked, 60)}」</font>"] if asked else [])
            lines += ["", f"**{built['ask']}**"] + [f"· **{cn}**：{desc}" if desc else f"· **{cn}**" for cn, desc, _ in built["options"]]
            buttons = [_button(cn, f"#pick {name} {tid} 0 {k}") for k, (cn, _, _) in enumerate(built["options"], 1)]
            context, hint, text_opts = "", "", "\n".join(lines)
        elif b.get("name") == "AskUserQuestion":
            title, color = f"❓【{name}】在问你", "orange"
            qels, buttons, hint = self.question_els(name, tid, qs, qi)
            text_opts = "\n".join(f"{i + 1}. {q.get('question', '')}" for i, q in enumerate(qs))
        else:
            title, color = f"🔐【{name}】要你批准", "yellow"
            if b:
                label, desc = describe_tool(b.get("name", ""), inp)
                lines.append(f"它想：**{label}**" + (f"　{desc}" if desc and b.get("name") != "Bash" else ""))
                lines.append(tool_detail(b.get("name", ""), inp))
            else:
                title, color = f"⏸【{name}】停在一个框上等你选", "orange"   # 不知道是什么框：不说「要你批准」
                if not context:
                    lines.append(f"<font color='grey'>屏幕上：</font>\n{clean_screen(rows, 10)}")
            lines.append("")
            for n, text in opts:
                lines.append(f"{n}. {perm_label(text)}　<font color='grey'>{text}</font>")
                kind = "danger" if perm_label(text) == "拒绝" else ("primary" if n == 1 else "default")
                buttons.append(_button(perm_label(text), f"#pick {name} {tid} - {n}", kind))
            if not opts:                              # 屏幕上暂时没认出选项：最常见的两个
                buttons = [_button("允许", f"#pick {name} {tid} - 1", "primary"),
                           _button("拒绝", f"#pick {name} {tid} - esc", "danger")]
            hint = "拒绝后它会停下来，再告诉它改做什么"
            text_opts = "\n".join(lines)
        extra = [_button("看屏幕", f"屏 {name}")]
        if self.state.get("current") != name and not built:   # Claude Code 自己的框打字答不了，不给〔回复它〕
            extra.insert(0, _button("回复它", f"对话 {name}"))
        if not built and b.get("name") != "AskUserQuestion" and b:   # 要你批准：它前面说的话 → 分隔 → 它想做什么（命令放在等宽代码块里）→ 按钮
            label, desc = describe_tool(b.get("name", ""), inp)
            detail = tool_detail(b.get("name", ""), inp).splitlines()
            detail = "\n".join(detail[:30] + (["```"] if len(detail) > 30 and detail[0].startswith("```") else []))
            els = ([_note(note)] if note else []) + [el for chunk in rich(context) for el in chunk][:6]
            els += ([{"tag": "hr"}] if context else []) + [_mk(f"**它想{BUTTON_EMOJI.sub('', label)}**"
                                                              + (f"　<font color='grey'>{one_line(desc, 60)}</font>" if desc and b.get("name") != "Bash" else "")),
                                                          _mk(detail)]
            order = ["允许", "总是允许", "拒绝"]               # 一行：允许 / 总是允许 / 拒绝，别的选项在「···」里
            buttons.sort(key=lambda x: order.index(x["text"]["content"]) if x["text"]["content"] in order else 9)
            els.append(_actions(buttons[:3], buttons[3:]))
            els.append(_actions(extra))
            card = Card(_card(title, color, els, footer=hint, state=self.state), f"{title}\n{context}\n{text_opts}")
            card.ask_id = tid
            return card
        if b.get("name") == "AskUserQuestion" and not built:   # 选择题：它前面说的话 → 分隔 → 题目和选项 → 按钮 → 浅灰提示
            els = ([_note(note)] if note else []) + [el for chunk in rich(context) for el in chunk][:6]
            els += ([{"tag": "hr"}] if context else []) + qels + _pairs(buttons)
            els.append(_actions(extra))
            body = "\n\n".join(x for x in (context, text_opts) if x)
            card = Card(_card(title, color, els, footer=hint, state=self.state), f"{title}\n{body}")
            card.ask_id = tid
            return card
        head = "\n".join(x for x in (note, who) if x)
        body = "\n\n".join(x for x in (context, text_opts) if x)
        els = ([_md(head)] if head else []) + [el for chunk in rich(body) for el in chunk]
        if hint:
            els += [{"tag": "hr"}, _md(f"<font color='grey'>{hint}</font>")]
        els += _pairs(buttons)
        els.append(_actions(extra))
        card = Card(_card(title, color, els, state=self.state), f"{title}\n{body}")
        card.ask_id = tid
        return card

    def codex_q_card(self, name, s, b, note="", calls=()):
        """codex 一次问你几道题（request_user_input）：每道题一行粗体题目，下面是选项按钮（选项多就用下拉）；不贴屏幕
        （上面那些命令输出跟题目无关）。点选项只是在卡片上选上（state["cxq_pick"]）：那个按钮变蓝色实心、题目后面灰字「已选：…」，
        没提交前再点别的就改；点〔提交〕才在它的提问框里按键（codex_submit）。交了的题收成一行灰字「你的回答：…」，
        都交完标题变成「已回答」。calls：屏幕上已经没有题时，用这几次提问在卡片上交过的回答画「已回答」。"""
        qs = (b.get("input") or {}).get("questions") or []
        left = (b.get("input") or {}).get("left") or len(qs)
        calls = set(calls) | {q["call"] for q in qs}
        done = sorted((x for x in (self.state.get("cxq") or {}).get(name, []) if x.get("call") in calls), key=lambda x: x["t"])
        picks = (self.state.get("cxq_pick") or {}).get(name) or {}
        order = list(dict.fromkeys([x["call"] for x in done] + [q["call"] for q in qs]))
        items = sorted([(q, None) for q in qs] + [(None, x) for x in done],
                       key=lambda it: (order.index((it[0] or it[1])["call"]), (it[0] or it[1]).get("idx", 0)))
        title = f"❓【{name}】" + (f"在问你 {len(qs)} 道题" if len(qs) > 1 else "在问你" if qs or not done else "已回答")
        els = [_note(note)] if note else []
        for q, x in items:
            if x:                                        # 交了的：收成一行
                els.append(_mk(f"**{one_line(x['q'], 200)}**\n<font color='grey'>你的回答：{one_line(x['a'], 200)}</font>"))
                continue
            got = picks.get(f"{q['call']}:{q['idx']}")
            opts, notes = q.get("options") or [], q.get("notes") or []
            text = f"**{one_line(q['title'], 200)}**" + (f"　<font color='grey'>已选：{fit(got['a'], 12)}</font>" if got else "")
            if any(notes):                               # 选项带说明的（计划模式里的那种）：说明一行一条灰字
                text += "\n" + "\n".join(f"<font color='grey'>{fit(o + '：' + d, LINE_EM)}</font>" for o, d in zip(opts, notes) if d)
            els.append(_mk(text))
            cmd = lambda k: f"#cxq {name} {q['call'][-8:] or '-'} {q['idx']} {k}"
            mine = (got or {}).get("k")
            if len(opts) > 4:                            # 选项多：下拉，选了的显示在框里
                sel = {"tag": "select_static", "placeholder": {"tag": "plain_text", "content": f"选一个（{len(opts)} 项）"},
                       "options": [{"text": {"tag": "plain_text", "content": fit(o, LINE_EM - 4)}, "value": cmd(k)} for k, o in enumerate(opts, 1)]}
                if mine:
                    sel["initial_option"] = cmd(mine)
                els.append({"tag": "action", "actions": [sel]})
            elif opts:                                   # 一行放几个：字短的两三个一行、一样宽，长的一个一行
                widest = max(text_em(o) for o in opts)
                per = 3 if len(opts) == 3 and widest <= LINE_EM / 3 - 2.6 else 2 if widest <= LINE_EM / 2 - 2.6 else 1
                buttons = [_button(fit(o, LINE_EM - 3), cmd(k), "primary_filled" if k == mine else "default") for k, o in enumerate(opts, 1)]
                els += [{"tag": "action", "layout": {3: "trisection", 2: "bisected", 1: "default"}[per], "actions": buttons[i:i + per]}
                        for i in range(0, len(buttons), per)]
        if not items:                                    # 对话记录里没读到题：只能看屏幕
            els.append(_mk(f"它在问你 {left} 道题，题目没读出来。点〔看屏幕〕，用遥控键 shift+← 打开提问框。"))
        picked = [q for q in qs if f"{q['call']}:{q['idx']}" in picks]
        send = [_button("提交回答" if len(picked) == len(qs) else f"提交已选的 {len(picked)} 道", f"#cxqsend {name}", "primary_filled")] if picked else []
        els.append(_actions(send + [_button("回复它", f"对话 {name}"), _button("看屏幕", f"屏 {name}")]))
        hint = "点选项先选上，选错可以改，点〔提交〕才交给它" if qs else "交给它了，它接着干，做完会发汇报"
        text = "\n\n".join(f"{q['title']}\n" + (f"你的回答：{x['a']}" if x else " / ".join(q.get("options") or [])) for q, x in
                            [(it[0] or {"title": it[1]["q"]}, it[1]) for it in items])
        card = Card(_card(title, "orange" if qs else "grey", els, footer=hint, state=self.state), f"{title}\n{text}")
        card.ask_id = b.get("id") or "q"
        return card

    def _cxq_open(self, s):
        """codex 提问框里还开着的题：(waiting_on 那一项, [题])。"""
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        return (b, (b.get("input") or {}).get("questions") or []) if b.get("name") == "CodexQuestions" else ({}, [])

    def _cxq_done(self, name, s, q, answer):
        """一道题交上去了：对话记录里这条回答标成你从飞书答的，卡片上收成「你的回答」。"""
        note_sent(s, f"回答「{q['title']}」：{answer}")
        done = self.state.setdefault("cxq", {}).setdefault(name, [])
        done.append({"call": q["call"], "idx": q["idx"], "q": q["title"], "a": answer, "t": now()})
        self.state["cxq"][name] = [x for x in done if now() - x["t"] < 86400][-20:]
        (self.state.get("cxq_pick") or {}).get(name, {}).pop(f"{q['call']}:{q['idx']}", None)
        self.save()

    def codex_pick(self, name, s, call, idx, k, text=None):
        """codex 提问框：卡片上点了某道题的某个选项——只记下（选错了再点别的就改），卡片原地刷新，不按键。
        text 不为 None 是你在飞书上打了字：直接交给第一道还开着的题（填进 Other）。"""
        b, qs = self._cxq_open(s)
        picks = self.state.setdefault("cxq_pick", {}).setdefault(name, {})
        if text is not None:
            q = next((x for x in qs if f"{x['call']}:{x['idx']}" not in picks), qs[0] if qs else None)
        else:
            q = next((x for x in qs if (x["call"][-8:] or "-") == call and str(x["idx"]) == str(idx)), None)
        opts = (q or {}).get("options") or []
        if not q or text is None and not (str(k).isdigit() and 1 <= int(k) <= len(opts)):
            card = alert(f"✅【{name}】这道题已经过去了", "可能你已经在电脑上答过或跳过了，这次没有按任何键。", "grey",
                         [_button("看屏幕", f"屏 {name}")])
            card.in_place = True
            return card
        calls = {x["call"] for x in qs}
        if text is None:
            picks[f"{q['call']}:{q['idx']}"] = {"k": int(k), "a": opts[int(k) - 1], "t": now()}
            self.save()
            card = self.codex_q_card(name, s, b)
        else:
            ok, why = crcodex.answer_question(s, q["title"], text=text)
            if ok:
                self._cxq_done(name, s, q, " ".join(text.split()))
                time.sleep(0.3)
                b = self._cxq_open(s)[0]
            card = self.codex_q_card(name, s, b, note="" if ok else f"⚠️ 你打的字没交上：{why}。没有再按别的键。", calls=calls)
        card.in_place = True
        return card

    def codex_submit(self, name, s):
        """〔提交〕：把卡片上选好的题一道道交给它（crcodex.answer_question 每按一下都核对屏幕，对不上就停手），卡片原地刷新。"""
        b, qs = self._cxq_open(s)
        picks = (self.state.get("cxq_pick") or {}).get(name) or {}
        todo = [(q, picks[f"{q['call']}:{q['idx']}"]) for q in qs if f"{q['call']}:{q['idx']}" in picks]
        calls = {q["call"] for q in qs}
        note = ""
        if not todo:
            note = "没有要交的：选好的题可能已经在电脑上答过了。这次没有按任何键。"
        for q, got in todo:
            ok, why = crcodex.answer_question(s, q["title"], got["k"], got["a"])
            if not ok:
                note = f"⚠️ 「{fit(q['title'], 12)}」没交上：{why}。没有再按别的键。"
                break
            self._cxq_done(name, s, q, got["a"])
        time.sleep(0.3)
        card = self.codex_q_card(name, s, self._cxq_open(s)[0], note=note, calls=calls)
        card.in_place = True
        return card

    def question_els(self, name, tid, qs, qi):
        """选择题的题目区：题目前面一个小标签（它给这题起的短名，比如「技能标签」）；每个选项一个浅灰底的块，
        粗体选项名、灰字说明，带预览（ASCII 草图、代码）的放在等宽代码块里，太长只放前 12 行（〔看完整预览〕在「···」里）。
        按钮跟选项一一对应（「1 上方，居中」）。几道题里有多选题时，答案先在卡片上攒着（state["multi"]），最后一起发给它。
        返回 (元素, 按钮, 浅灰提示)。"""
        qi = min(int(qi), max(len(qs) - 1, 0))
        q = qs[qi] if qs else {}
        collect = any(x.get("multiSelect") for x in qs)
        got = self.state.get("multi", {}).get(name) or {}
        answers = got.get("answers", {}) if got.get("tid") == tid else {}
        els = []
        done = [f"{x.get('header') or f'第 {i + 1} 题'}：" + ("、".join((x.get("options") or [{}] * 9)[k - 1].get("label", "") for k in answers.get(str(i), []))
                                                            or "都不选") for i, x in enumerate(qs[:qi])] if collect else []
        if len(qs) > 1:
            els.append(_note(f"第 {qi + 1} / {len(qs)} 题" + ("　已答：" + "；".join(done) if done else "")))
        head = (_pill(one_line(q["header"], 12), 0).replace("neutral", "blue") + " " if q.get("header") else "")
        els.append(_mk(f"{head}**{one_line(q.get('question', ''), 200)}**" + (" " + _pill("多选") if q.get("multiSelect") else "")))
        picked = answers.get(str(qi), [])
        more = []
        for k, o in enumerate(q.get("options") or [], 1):
            label = o.get("label", "").replace("(Recommended)", "（推荐）")
            text = f"**{k}. {one_line(label, 60)}**" + ("　" + _pill("已选", 1) if k in picked else "")
            if o.get("description"):
                text += f"\n<font color='grey'>{one_line(o['description'], 160)}</font>"
            block = [_mk(text)]
            if o.get("preview"):
                lines = [x.rstrip() for x in o["preview"].splitlines() if x.strip()]
                if len(lines) <= PREVIEW_LINES and not BOX_RE.search(o["preview"]) and all(text_em(x) <= LINE_EM - 3 for x in lines):
                    block.append({"tag": "div", "text": {"tag": "plain_text", "content": "\n".join(lines)}})
                else:                                    # 画框的、太宽太长的：手机上会歪，点〔预览 N〕单独看
                    more.append(_button(f"预览 {k}", f"#preview {name} {tid} {qi} {k}"))
            els.append({"tag": "column_set", "flex_mode": "none", "background_style": "grey",
                        "columns": [{"tag": "column", "width": "weighted", "weight": 1, "elements": block}]})
        opts = q.get("options") or []
        short = lambda k, o: f"{k} {one_line(o.get('label', '').replace('(Recommended)', ''), 40)}"
        if not collect:                                  # 都是单选：点了就在它的选择框上按这个数字
            buttons = [_button(short(k, o), f"#pick {name} {tid} {qi} {k}") for k, o in enumerate(opts, 1)]
            hint = "点按钮就是选它  ·  都不合适就引用本卡片回复，直接打字回答"
        elif q.get("multiSelect"):                       # 多选：点一下勾上、再点取消；选好点〔下一题〕/〔提交〕
            buttons = [_button(("☑ " if k in picked else "☐ ") + short(k, o), f"#mpick {name} {tid} {k} {qi}",
                               "primary" if k in picked else "default") for k, o in enumerate(opts, 1)]
            last = qi + 1 >= len(qs)
            buttons.append(_button(("提交" if last else "下一题") + (f"（选了 {len(picked)} 项）" if picked else "（都不选）"),
                                   f"#msubmit {name} {tid} - {qi}", "primary" if picked else "default"))
            hint = "可以选好几个，再点一下取消  ·  答完会把所有题的答案一起发给它"
        else:                                            # 几道题里有多选题，这道是单选：点了记下，进下一题
            buttons = [_button(short(k, o), f"#mpick {name} {tid} {k} {qi}", "primary" if k in picked else "default")
                       for k, o in enumerate(opts, 1)]
            hint = "点了就记下、进下一题  ·  答完会把所有题的答案一起发给它"
        if more:
            els += _rows(more)
        return els, buttons, hint

    def preview_card(self, name, s, tid, qi, k):
        """〔看完整预览〕：一个选项的完整预览。"""
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        qs = (b.get("input") or {}).get("questions") or []
        try:
            o = qs[int(qi)]["options"][int(k) - 1]
        except (IndexError, ValueError, KeyError):
            return alert(f"✅【{name}】这道题已经过去了", "没有可以看的预览了。", "grey", [_button("看屏幕", f"屏 {name}")])
        body = [_mk(f"**{k}. {one_line(o.get('label', ''), 60)}**"), _mk(code_block(o.get("preview", "")))]
        return Card(_card(f"❓【{name}】选项 {k} 的预览", "orange", body, state=self.state), o.get("preview", ""))

    def pending_request(self, name, s):
        """它停下来等你的那件事，数据格式（给看板 / 飞书以外的客户端用，比如手机 App）：
        {"kind": approval / question / screen, "id", "title", "detail", "options": [{"label", "cmd"}]}，
        cmd 就是飞书卡片上那个按钮发的命令（交给 Remote.handle 或 cr 执行）。不在等返回 None。"""
        if not s.get("alive", True) or not s.get("waiting"):
            return None
        rows = pane_rows(s)
        opts = dialog_options(rows)
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        tid, inp = b.get("id") or "-", b.get("input") or {}
        if b.get("name") == "CodexQuestions":
            return {"kind": "questions", "id": tid, "title": f"在问你 {inp.get('left') or 0} 道题", "detail": "",
                    "questions": [{"title": q["title"], "options": [{"label": o, "cmd": f"#cxq {name} {q['call'][-8:] or '-'} {q['idx']} {k}"}
                                                                    for k, o in enumerate(q["options"], 1)]} for q in inp.get("questions") or []],
                    "options": []}
        if b.get("name") == "AskUserQuestion":
            qs = inp.get("questions") or []
            q = qs[0] if qs else {}
            return {"kind": "question", "id": tid, "title": q.get("question", ""), "multi": bool(q.get("multiSelect")),
                    "detail": "", "options": [{"label": o.get("label", ""), "detail": o.get("description", ""),
                                               "cmd": f"#pick {name} {tid} 0 {k}"} for k, o in enumerate(q.get("options") or [], 1)]}
        if not opts:
            return {"kind": "screen", "id": None, "title": "停在一个框上（没认出是什么框）", "detail": screen(s, 20), "options": []}
        label, desc = describe_tool(b.get("name", ""), inp) if b else ("🔐 要你批准", "")
        return {"kind": "approval", "id": tid, "title": f"{label} {desc}".strip(),
                "detail": tool_detail(b.get("name", ""), inp) if b else screen(s, 20),
                "options": [{"label": perm_label(t), "raw": t, "cmd": f"#pick {name} {tid} - {n}"} for n, t in opts]}

    def multi_pick(self, name, s, tid, k, reg, submit=False, qi=0):
        """几道题里有多选题时卡片上的按钮：单选题点了记下、进下一题；多选题点一下勾上、再点取消，〔下一题〕/〔提交〕。
        答案攒在 state["multi"]；最后一题答完，像在电脑上先按 Esc 关掉选择框，再把「我的回答：…」当普通消息发给它，比在终端里一格一格勾稳。"""
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        qs = ((b.get("input") or {}).get("questions") or []) if b.get("name") == "AskUserQuestion" else []
        if tid not in (b.get("id"), b.get("alt_id")) or not qs or not dialog_options(pane_rows(s)):
            card = alert(f"✅【{name}】这一步已经过去了", "可能你已经在电脑上答过了，这次没有按任何键。", "grey", [_button("看屏幕", f"屏 {name}")])
            card.in_place = True
            return card
        multi = self.state.setdefault("multi", {})
        got = multi.get(name) if (multi.get(name) or {}).get("tid") == tid else {"tid": tid, "answers": {}}
        got.setdefault("answers", {})
        qi = min(int(qi), len(qs) - 1)
        q, key = qs[qi], str(qi)
        nxt = qi
        if not submit:
            k = int(k)
            if 1 <= k <= len(q.get("options") or []):
                got["answers"][key] = sorted(set(got["answers"].get(key, [])) ^ {k}) if q.get("multiSelect") else [k]
            nxt = qi if q.get("multiSelect") else qi + 1
        else:
            got["answers"].setdefault(key, [])
            nxt = qi + 1
        multi[name] = got
        self.save()
        if nxt < len(qs):                                # 还有题：卡片原地换成下一题（或刷新这一题的勾选）
            card = self.ask_card(name, s, nxt, pt=pt)
            card.in_place = True
            return card
        multi.pop(name, None)
        if len(qs) == 1:                                 # 只有一道多选题：「我选：2. …；3. …。没选的不做。」
            opts = qs[0].get("options") or []
            chosen = [f"{n}. {opts[n - 1].get('label', '')}" for n in got["answers"].get("0", []) if 1 <= n <= len(opts)]
            text = ("我选：" + "；".join(chosen) + "。没选的不做。") if chosen else "这几项一个都不选，都先不做。"
        else:                                            # 几道题：「我的回答：题名：选项；题名：选项。」
            parts = []
            for i, x in enumerate(qs):
                opts = x.get("options") or []
                chosen = "、".join(opts[n - 1].get("label", "") for n in got["answers"].get(str(i), []) if 1 <= n <= len(opts)) or "都不选"
                parts.append(f"{x.get('header') or x.get('question', f'第 {i + 1} 题')}：{chosen}")
            text = "我的回答：" + "；".join(parts) + "。"
        card = self.deliver(name, text, reg)
        if isinstance(card, Card):
            card.in_place = True
        return card

    def pick(self, name, s, tid, qi, n):
        """卡片上点了某个选项：先确认它还停在这一步（没在电脑上答过），再按对应的数字键。"""
        rows = pane_rows(s)
        if tid.startswith("p"):                          # 「Session paused」那块：编号对得上、还是这一次暂停才按
            paused = paused_question(rows)
            if paused and tid == f"p{s['statusAt']:.0f}" and n.isdigit() and int(n) in paused["keys"]:
                cn, _, how = paused["options"][int(n) - 1]
                send_keys(s, [n])
                if how == "stay":
                    return self.stayed_card(name, s, paused["model"])
                return self._picked_live(name, s, f"选了「{cn}」")
        opts = dialog_options(rows)
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        if tid.startswith("p") or not opts or (tid != "-" and tid not in (b.get("id"), b.get("alt_id"))):
            card = alert(f"✅【{name}】这一步已经过去了", (self.who(name, s) + "\n" if self.who(name, s) else "") +
                         "可能你已经在电脑上处理过了，这次没有按任何键。", "grey",
                         [_button("看屏幕", f"屏 {name}"), _button("最新情况", f"看 {name}")])
            card.in_place = True
            return card
        if b.get("name") == "AskUserQuestion":
            qs = (b.get("input") or {}).get("questions") or []
            qi = int(qi)
            q = qs[qi]
            if q.get("question", "")[:12] not in "\n".join(rows):
                return alert(f"❓【{name}】屏幕上现在不是这道题", "没有按键。点〔看屏幕〕看看它现在停在哪。", "yellow",
                             [_button("看屏幕", f"屏 {name}")])
            built = builtin_question(q) if len(qs) == 1 else None
            picked = f"选了「{built['options'][int(n) - 1][0] if built else q['options'][int(n) - 1]['label']}」"
            send_keys(s, [n])
            time.sleep(0.5)
            now_rows = pane_rows(s)                      # 新版 Claude Code 按数字只是把光标移过去，还停在这道题上就再按回车选中
            if q.get("question", "")[:12] in "\n".join(now_rows) and any(re.match(rf"^❯\s*{n}\.", r) for r in now_rows):
                send_keys(s, ["enter"])
            if built and built["options"][int(n) - 1][2] == "stay":
                return self.stayed_card(name, s, built["model"])   # 它不会接着干：不显示「准备开始」，直接说这次没答
            time.sleep(0.8)
            if qi + 1 < len(qs):
                card = self.ask_card(name, s, qi + 1, pt=pt, note=f"✅ 第 {qi + 1} 题{picked}")   # 下一题，原地换
                card.in_place = True
                return card
            if len(qs) > 1 and any("Submit answers" in r for r in pane_rows(s)):
                send_keys(s, ["1"])                     # 多道题都答完了：提交
        elif n == "esc":
            picked = "选了「拒绝」"
            send_keys(s, ["esc"])
        else:
            text = dict(opts).get(int(n))
            if not text:
                return f"屏幕上没有第 {n} 个选项了，没有按键。点〔看屏幕〕看看。"
            picked = f"选了「{perm_label(text)}」"
            send_keys(s, [n])
        return self._picked_live(name, s, picked)

    def _picked_live(self, name, s, picked):
        """选完了：接着像发消息一样，这张卡片变成实时进度，做完变成回复。"""
        since = now()
        close = self.pend_since(name, since, s)
        self.state.setdefault("asked_note", {})[name] = [since, picked]
        self.save()
        card = self.live_card(name, s, since)
        card.pending_for, card.in_place, card.close_old = name, True, close
        return card

    def answer_with_text(self, name, s, text):
        """它停在选择题上时你打了字：单选题选「Type something」把你的话填进去；
        多选题（或没有「Type something」的题）像在电脑上一样先按 Esc 关掉这道题，再把你的话当普通消息发过去，它看得到刚才问了什么。
        发出去了返回 None（照常显示进度）；要你点按钮（权限框）或还有下一题时返回要发给你的卡片。"""
        rows = pane_rows(s)
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        qs = ((b.get("input") or {}).get("questions") or []) if b.get("name") == "AskUserQuestion" else []
        multi = any(q.get("multiSelect") for q in qs)
        other = None if multi else next((n for n, label in dialog_options(rows)
                                         if re.sub(r"^\[.\]\s*", "", label).startswith("Type something")), None)
        if qs and not other and not builtin_question(qs[0]):
            send_keys(s, ["esc"])
            for _ in range(12):
                time.sleep(0.3)
                if not dialog_options(pane_rows(s)):
                    break
            else:
                return self.ask_card(name, s, pt=pt, note="⚠️ 按了 Esc 想先关掉这道题，没关掉，你的话没有发出去。请点下面的按钮。") \
                    or f"【{name}】停在一个选择框上，打的字没有发出去。点〔看屏幕〕看看。"
            time.sleep(0.5)
            send_text(s, text)
            return None
        if not qs or not other:
            return self.ask_card(name, s, pt=pt, note="⚠️ 它在等你点选项，打的字没有发出去。请点下面的按钮。") \
                or f"【{name}】停在一个选择框上，打的字没有发出去。点〔看屏幕〕看看。"
        shown = "\n".join(rows)
        qi = next((i for i, q in enumerate(qs) if q.get("question", "")[:12] in shown), 0)
        send_keys(s, [str(other)])
        time.sleep(0.3)
        send_text(s, text)
        time.sleep(0.8)
        if qi + 1 < len(qs):
            return self.ask_card(name, s, qi + 1, pt=pt, note=f"✅ 第 {qi + 1} 题回答：{one_line(text, 40)}")
        if len(qs) > 1 and any("Submit answers" in r for r in pane_rows(s)):
            send_keys(s, ["1"])
        return None

    def effort_button(self, name, s):
        """〔思考：很高〕：按钮上直接写当前思考程度，点开就能改。"""
        effort = session_status(s)["effort"] if s.get("alive", True) else ""
        return _button(f"思考：{EFFORT_CN.get(effort, '?')}", f"#effort {name}")

    def codex_effort(self, name, s, level):
        """codex 改思考程度：像在电脑上一样 /model → 还是这个模型 → 选档位 → 按 s（只改这个会话，不动默认设置）。
        它在干活或停在框上时不动（菜单会挡住它，Esc 还会打断它）。"""
        if s.get("status") in ("busy", "waiting") or input_text(s) or not crcodex.ready(s):
            why = ("它正在干活" if s.get("status") == "busy" else "它的输入框里有没发出去的字" if input_text(s)
                   else "它停在一个框上")
            card = alert(f"🧠【{name}】这会儿改不了思考程度", f"{why}，等它停下、输入框空着再改。这次没有按任何键。", "grey",
                         [self.effort_button(name, s), _button("看屏幕", f"屏 {name}")])
        elif level not in crcodex.EFFORTS:
            card = self.run_slash(name, s, "/model")         # 更高的档位在 /model 的「More reasoning…」里：给你菜单自己选
        elif crcodex.set_effort(s, level):
            time.sleep(0.5)
            card = self.effort_card(name, s, done=level)
        else:
            card = self.screen_card(name, s, note="⚠️ /model 菜单没按预期出来，思考程度没改。下面是它现在的屏幕。")
        if isinstance(card, Card):
            card.in_place = True
        return card

    def effort_card(self, name, s, done=None):
        st = session_status(s)
        model, cur = st["model"], st["effort"]
        cur = done or cur
        levels = [(lv, cn) for lv, cn in EFFORTS if not is_codex(s) or lv in crcodex.EFFORTS]
        buttons = [_button(("✓ " if lv == cur else "") + cn, f"#effort {name} {lv}", "primary" if lv == cur else "default")
                   for lv, cn in levels]
        head = f"✅ 已改成「{EFFORT_CN[done]}」，从它的下一步开始生效。" if done else f"现在：**{EFFORT_CN.get(cur, cur or '未知')}**"
        body = (f"{head}\n<font color='grey'>越高想得越久、越仔细；越低回得越快。随手聊天用「中」就够，难题再调高。"
                f"只改【{name}】这一个会话，不影响其他会话。</font>")
        els = [_md(body), {"tag": "action", "actions": buttons}]
        return Card(_card(f"🧠【{name}】思考程度", "purple", els, tags=_pill(model) if model else "", state=self.state),
                    f"🧠【{name}】思考程度：{EFFORT_CN.get(cur, cur)}")

    def effort_confirm_card(self, name, s, level):
        """改思考程度要你确认：跟电脑上弹的「Change effort level?」框一样，确认了才改。"""
        cur = session_status(s)["effort"]
        cn = EFFORT_CN.get(level, level)
        body = (f"这段对话是按「{EFFORT_CN.get(cur, cur or '现在的程度')}」缓存的。改成「{cn}」以后，下一条消息要把整段对话从头重读一遍，"
                "那一条会慢一些、多用一些额度，之后就正常了。")
        held = self.state.get("held_msg")
        if held and held["name"] == name:
            body += f"\n\n你刚才那句还没发：「{one_line(held['body'], 60)}」。点哪个按钮都会接着把它发出去。"
        buttons = [_button(f"改成「{cn}」", f"#effort {name} {level} yes", "primary"),
                   _button("不改了", f"#effort {name} {level} no")]
        els = [_md(f"{self.who(name, s)}\n{body}" if self.who(name, s) else body), {"tag": "action", "actions": buttons}]
        title = f"🧠【{name}】确认改思考程度？"
        return Card(_card(title, "purple", els, state=self.state),
                    f"{title}\n{body}")

    def timeline(self, steps, limit=3, live=""):
        """实时卡片上的过程：浅灰小字，一步一行（单行截断），前面一个词说明是什么（思考 / 命令 / 读取…），只留最近 limit 步；
        live：最后一行正在发生的（「思考中　55秒」：想完才写进对话记录，想得久时这一行在走，看得出没卡）。"""
        steps = [x for i, x in enumerate(steps) if x[0] != "said" and not (x[0] == "think" and i and steps[i - 1][0] == "think")]
        if live:
            limit -= 1
        lines = [f"前面还有 {len(steps) - limit} 步"] if len(steps) > limit else []
        for kind, text, done in steps[-limit:]:
            secs = text[-1] if isinstance(text, tuple) and kind in ("think", "tool") else None
            took = f"　{fmt_secs(secs)}" if secs and secs >= 1 else ""
            if kind == "think":                         # 想的内容多是英文、很长：只写想了多久
                lines.append("思考" + took)
            elif kind == "tool":
                label, detail = text[:2]
                lines.append(f"{BUTTON_EMOJI.sub('', label)}　{one_line(detail, 40)}" + ("　· 进行中" if not done else "") + took)
            else:
                lines.append("回复　" + one_line(text, 50))
        lines += [live] if live else []
        return {"tag": "note", "elements": [{"tag": "plain_text", "content": "\n".join(lines)}]} if lines else None

    def _step_lines(self, steps, limit, full_last=False):
        lines = [] if len(steps) <= limit else [f"<font color='grey'>…前面还有 {len(steps) - limit} 步</font>"]
        for i, (kind, text, done) in enumerate(steps[-limit:]):
            if kind == "said":
                lines.append(said_line(text, width=None)[0])
                continue
            if kind == "text" and full_last and i == len(steps[-limit:]) - 1:
                body = phone_md(text)                    # 它最新写的那段话整段显示，像在电脑前看着它写
                lines.append("💬 " + (body if len(body) <= 2000 else body[:2000] + "……"))
                continue
            if kind == "think":
                thought, secs = text if isinstance(text, tuple) else (text, None)
                lines.append("🧠 想了想" + (f" {fmt_secs(secs)}" if secs and secs >= 1 else "")
                             + (f"：<font color='grey'>{one_line(thought, 140)}</font>" if thought else ""))
            elif kind == "tool":
                label, detail = text[:2]
                detail = f" <font color='grey'>{detail}</font>" if detail else ""
                lines.append(f"{label}{detail}" if done else f"**{label}**{detail} ⏳")
            else:
                lines.append(f"💬 {one_line(text, 160)}")
        return lines

    def pend_since(self, name, t, s=None):
        """从 t 起等它回话（飞书那张实时卡片从这里算起）。上一句还没回完你又说了一句：新卡片接着从上一句算起——
        连发的几句都列出来、已有的进度接着显示，旧卡片撤回也什么都不丢（跑着的 /compact 这类也并进来）。
        上一句其实已经答完、只是卡片还没来得及换成回复：返回那张回复，旧卡片原地换上，新卡片只管这一句。"""
        old, start, close = self.state["remote_pending"].get(name), t, None
        if name in load_json(PENDING, {}):
            slash = self.state.get("slash", {}).pop(name, None)
            if old and s and s["status"] != "busy" and not s.get("waiting") and old <= s["statusAt"] + 1 \
                    and any(turn_steps(s.get("sessionId"), old)):
                close = self.reply_card(name, s, old)
                self.state.setdefault("mon", {}).setdefault("sent", {})[f"idle:{s.get('sessionId')}:{s['statusAt']}"] = now()
            elif old and s and is_assistant(name):     # 助手：回复常常很长，旧卡片原地定格成到这句为止的回复（不撤回），这句另起一张
                close = self.reply_card(name, s, old, until=t)
            elif old:
                start = min(self.pend_start(name, old), t)
            elif slash:
                start = min(slash[0], t)
        self.state["remote_pending"][name] = t
        self.state.setdefault("pending_from", {})[name] = [start, t]
        return close

    def pend_start(self, name, since):
        """这张实时卡片从哪句算起：连发了几句是第一句的时间，不然就是 since（最近这句）。"""
        start, latest = self.state.get("pending_from", {}).get(name) or (since, since)
        return start if latest == since else since

    def live_card(self, name, s, since):
        """实时卡片，从上到下：说给它的话（引用块）→ 过程（浅灰时间线，最近 3 步）→ 它最新写的那段 → 按钮；
        干了多久在标题里，模型、上下文、额度、生成了多少在底栏标签里。连发了几句：从第一句算起（见 pend_since）。"""
        since = self.pend_start(name, since)
        _, steps, _ = turn_steps(s.get("sessionId"), since)
        said = said_between(s.get("sessionId"), since, extra=[t for k, t, _ in steps if k == "said"])
        steps = [x for x in steps if x[0] != "said"]
        note = self.state.get("asked_note", {}).get(name)
        if note and note[0] >= since and not any(x.get("src") == "feishu" and x["t"] >= note[0] - 2 for x in said):
            said = sorted(said + [{"t": note[0], "kind": "human", "src": "feishu", "text": note[1]}], key=lambda x: x["t"])   # 你是点选项 / 打字回答它的问题
        rows = pane_rows(s)
        st = session_status(s)
        spin, effort = spinner_info(rows), st["effort"]
        running = steps and steps[-1][0] == "tool" and not steps[-1][2]
        if running:
            doing = "正在执行"
        elif spin.get("thinking") or not steps:
            doing = "正在思考" if steps or s["status"] == "busy" else "收到，准备开始"
        else:
            doing = "正在写回复" if steps[-1][0] == "text" else "正在思考"
        elapsed = fmt_secs(now() - since)
        waiting = queued(rows)                          # 你发的话在排队：屏幕上看得到，对话记录里还没有
        pending = label_said(s.get("sessionId"), [{"t": now(), "kind": "human", "text": clean_said(w), "queued_now": True,
                                                   "images": len(PASTE_TAG_RE.findall(w))} for w in waiting if w])
        els, cut = said_els(said, feishu=True, pending=pending)   # 起因：你从飞书 / 终端说的话，同事发来的…
        if waiting:
            els.append(_note("排队中的话要等它这一步做完才会看到" + ("" if is_codex(s) else "  ·  点「立即发送」马上插进去")))
        thinking = ""
        if doing == "正在思考" and s["status"] == "busy":   # 这一段从上一步写进对话记录（没有就从你说话）算起
            path = transcript_path(s.get("sessionId")) if steps else None
            began = max(since, os.path.getmtime(path)) if path and os.path.exists(path) else since
            thinking = f"思考中　{fmt_secs(max(0, now() - began))}"
        flow = self.timeline(steps, live=thinking)
        latest = steps[-1][1] if steps and steps[-1][0] == "text" else ""
        if flow or latest:
            els.append({"tag": "hr"})
        if flow:
            els.append(flow)
        if latest:                                      # 它正在写的那段话
            body = phone_md(latest)
            els.append(_md(body if len(body) <= 600 else body[:600] + "……"))
        main = [_button("停止", f"#stop {name}", "danger"), _button("刷新", f"#live {name}"), _button("看屏幕", f"屏 {name}")]
        if self.state.get("current") == name:           # 它还在想，你也能随时回到助手；它会接着干完
            main.insert(0, _button(f"↩ 回到{self.state.get('default') or '默认对象'}", "退出", "primary"))
        if waiting and not is_codex(s):
            main.insert(0, _button("立即发送", f"#sendnow {name}", "primary"))
        more = [_button(f"思考：{EFFORT_CN.get(effort, '?')}", f"#effort {name}"), _button("看过程", f"#steps {name} {since:.0f}")]
        els.append(_actions(main[:3], main[3:] + more))   # 露出 3 个，其余在「···」里
        tags = usage_tags(st, extra=[f"{spin['tokens']} tokens" if spin.get("tokens") else ""])
        return Card(_card(f"⏳【{name}】{doing} · {elapsed}", "wathet", els, tags=tags, live=True, state=self.state),
                    f"⏳【{name}】{doing}… 已 {elapsed}")

    def reply_card(self, name, s, since, until=None):
        """做完了：原地换成回复。从上到下：说给它的话（引用块，飞书发的不重复）→ 分隔 → 回答（主体）→ 按钮；
        用时、做了几步在底栏标签里，〔看过程〕收在「···」里。连发了几句：从第一句算起，几句都列出来（飞书发的也列：
        回复常常发在最底下、实时卡片撤回，不列就看不出它答的是哪几句）。
        until：它还在干活你又发了一句（只对助手）：旧卡片定格成到这句为止的样子，写过的回复整段留着，不再刷新。"""
        since = self.pend_start(name, since)
        _, steps, stopped = turn_steps(s.get("sessionId"), since)
        if until:
            steps = steps[:next((i for i, (k, t, _) in enumerate(steps) if k == "said" and t["t"] >= until - 1), len(steps))]
            stopped = False
        said = [x for x in said_between(s.get("sessionId"), since, until, extra=[t for k, t, _ in steps if k == "said"])
                if not until or x["t"] < until - 1]           # 定格：新那句（和 heard 前后放宽的 2 秒）不算这段的
        bar = session_status(s)
        effort = bar.get("effort", "")
        texts = [t for k, t, _ in steps if k == "text"]
        reply = turn_answer(texts)                     # 只要这一轮的回答；没有就说没有，不拿上一轮的充数
        answers = turn_answers(steps)
        if len(answers) > 1:                           # 它忙时你又发了一句（排队，答完上一句才轮到）：两轮的回答都放上，各标一句答的是哪句
            reply = "\n\n".join(turn_label(qs) + a for qs, a in answers)
        if texts and not until:
            self.note_latest(name, s["statusAt"], texts[-1])
        tools = sum(1 for k, _, _ in steps if k == "tool")
        took = fmt_secs((until or max(s["statusAt"], since)) - since)
        if stopped:
            title, color, body = f"⏹【{name}】已停止", "grey", reply or "（停下时还没写回复）"
        elif until and not reply:
            title, color, body = f"⏸【{name}】这段还没写回复", "grey", "（它还在干活时你又发了一句）"
        else:
            title, color, body = f"💬【{name}】", "green", reply or "（没有文字回复）"
        buttons = [b for b in self.chat_buttons(name) if not (is_assistant(name) and b["value"]["cmd"] == f"屏 {name}")]
        main, more = buttons[:2], buttons[2:]
        if (bar.get("ctx") or 0) >= CTX_WARN:           # 上下文快满了：〔压缩上下文〕露在外面
            main.append(self.compact_button(name))
        more = ([_button("看过程", f"#steps {name} {since:.0f}")] if steps else []) + more + [
            _button(f"思考：{EFFORT_CN.get(effort, '?')}", f"#effort {name}"), _button("命令", f"#cmds {name}")]
        sub = f"当前对话：{first_user_text(s.get('sessionId')) or name}" if is_assistant(name) else None
        chunks = rich(body)
        files = mentioned_files(body, s.get("cwd"))
        quote, _ = said_els(said, feishu=True)
        els = (quote + ([{"tag": "hr"}] if quote else []) + chunks[0]
               + ([{"tag": "action", "actions": file_buttons(files)}] if files else [])
               + ([_note("你又发了一句：接下来的进度和回复在下面的新卡片里")] if until else []) + [_actions(main, more)])
        tags = usage_tags(bar, extra=[(f"跑了 {took}" if stopped else f"这段用时 {took}" if until else f"用时 {took}")
                                      + (f" · {tools} 步" if tools else "")])
        card = Card(_card(title, color, els, tags=tags, state=self.state, sub=sub), f"{title}\n" + re.sub(r"</?font[^>]*>", "", body))
        card.reply_for = name
        return continued(card, title, color, chunks)

    def stop_card(self, name, e, turn):
        """它停下了、而且这一轮有要给你看的：你叫它干的是「📝 汇报」，后台醒来的是「📣 有新进展」。
        正文是这一轮它写的全部的话（不是上一轮的）。回复里写了问句也只是普通消息：不按字面猜「要你拍板」，不加快捷按钮——
        真要你选的，是它弹出来的选择框 / 批准框（ask_card），那个才有按钮。"""
        body = turn_answer(turn["texts"]) or f"（这一轮它没写话，做了 {turn['tools']} 步）"
        if turn["by"] == "you":
            title, color, label = f"📝【{name}】：汇报", "orange", "📝 汇报"
        else:
            title, color, label = f"📣【{name}】：有新进展", "blue", "📣 有新进展"
        bar = session_status(e)
        said = said_between(e.get("sessionId"), turn.get("t") or e["statusAt"], extra=turn.get("said", []))
        quote, cut = said_els(said)                      # 这一轮是你在终端里叫它干的 / 同事叫醒的：起因放在最上面
        chunks = rich(body)
        els = quote + ([{"tag": "hr"}] if quote else []) + chunks[0]
        files = mentioned_files(body, e.get("cwd"))
        if files:
            els.append({"tag": "action", "actions": file_buttons(files)})
        more = [self.effort_button(name, e)] + ([_button("看过程", f"#steps {name} {turn['t']:.0f}")] if turn.get("t") else [])
        els.append(_actions([_button("回复它", f"对话 {name}", "primary"), _button("看全文", f"看 {name} 全部")], more))
        heard_md, _ = said_md(said)
        plain = re.sub(r"<[^>]+>|\*\*", "", heard_md)
        took = fmt_secs(e["statusAt"] - turn["t"]) if turn.get("t") and e["statusAt"] > turn["t"] else ""
        tags = usage_tags(bar, extra=[" · ".join(x for x in (f"用时 {took}" if took else "", f"{turn['tools']} 步" if turn.get("tools") else "") if x)])
        card = Card(_card(title, color, els, tags=tags, state=self.state), f"{title}\n{plain + chr(10) if plain else ''}{body}")
        if len(chunks) == 1:                             # 汇报、进展：10 分钟内同一个员工的下一张并进来，手机只响一次
            card.merge = {"name": name, "label": label, "body": body, "t": now(), "said": quote}
        return continued(card, title, color, chunks)

    def note_latest(self, name, t, text, loud=False):
        """〔📋 员工看板〕上它那一行的「最新」：它最近停下时说的最后一句（没说话的记成空，看板上就不写）。
        第一句只是个引子（「压缩包打好了：」）就接上下一行。"""
        lines = [first_sentence(x) for x in (text or "").splitlines() if first_sentence(x)]
        first = " ".join(lines[:2]) if lines and lines[0].endswith(("：", ":")) else (lines[0] if lines else "")
        self.state.setdefault("bg", {})[name] = {"t": t, "text": one_line(first, 100), "loud": loud}

    def bg_update(self, name, e, turn, loud):
        """它停下了：记下最新一句话。后台醒来、没什么要你管的，就只更新看板，不单独发卡片。"""
        self.note_latest(name, e["statusAt"], turn["texts"][-1] if turn["texts"] else "", loud)

    def board_rows(self):
        """看板上的员工：在上班的和最近一周下班的（刚下班的在前）；助手（就是在跟你说话的那个）、每日资讯这类自动任务不算。"""
        reg = load_json(REGISTRY, {})
        alive = {n: e for n, e in reg.items() if e.get("alive") and not is_quiet(n) and not is_assistant(n)}
        off = sorted((n for n, e in reg.items() if not e.get("alive") and not is_quiet(n) and not is_assistant(n)
                      and now() - e.get("died", 0) < 7 * 86400), key=lambda n: (-reg[n].get("died", 0), n))
        return alive, off

    def board_sig(self):
        alive, off = self.board_rows()
        rows = self.state.get("bg", {})
        why = self.state.get("said_last", {})
        return json.dumps([sorted((n, e.get("status"), e.get("waiting"), rows.get(n, {}).get("t"), why.get(n, {}).get("t"))
                                  for n, e in alive.items()), off,
                           self.state.get("held", 0), self.quiet_now(),
                           sorted(load_json(BADGES, {}).items()), time.strftime("%m%d%H")])   # 换了号、到点了（今天→昨天、一天没动静折起来）也重画

    def status_card(self, minor=False):
        """〔📋 员工看板〕：标题下面一行摘要「2 人干活 · 8 人空闲」。按状态分组（等你 → 干活中 → 等后台 → 空闲），组之间一条细线，
        每组一个灰色小标题带人数；每人一行：彩色小标签（等你红、干活中绿、等后台橙，空闲不加）+ 工号 名字 + CC / Codex 小标签，
        时间靠右（今天 13:28 / 昨天 / 10/29）；干活、等后台、等你的下面一行灰字是最新一句（单行截断），空闲的今天有新话才写。
        空闲里一天以上没动静的折成一行灰字，下班的一行灰字只列名字（下班就没有工号了）。助手不列（它就是在跟你说话的那个）。
        底下「选员工」下拉 + 〔看攒下的 N〕〔刷新〕，其余在「···」。原地更新，飞书不响；在群里会被置顶。"""
        alive, off = self.board_rows()
        rows = {n: r for n, r in self.state.get("bg", {}).items() if n in alive and now() - r["t"] < 3 * 86400}
        self.state["bg"] = rows
        said = self.state.get("said_last", {})
        today = time.localtime()[:3]

        def group(e, name):
            if e.get("waiting"):                         # 弹着批准框 / 选择框等你点
                return 0
            g = {"busy": 1, "shell": 2}.get(e.get("status"), 3)
            return 4 if g == 3 and now() - (e.get("statusAt") or 0) > 86400 else g   # 4：空闲里一天以上没动静的，折成一行

        def latest(name, e, g):
            if g == 0:
                return "要你批准" if "permission" in (e.get("waiting") or "") else "在问你问题"
            why = said.get(name)
            if g == 1 and why and why["t"] >= e.get("statusAt", 0) - 5:
                return why["line"]                           # 干活的起因：你在电脑上说的 / 同事发来的
            r = rows.get(name) or {}
            text = "" if r.get("text", "").startswith("（醒来看了一眼") else r.get("text", "")
            return text if g < 3 or time.localtime(r.get("t", 0))[:3] == today else ""

        def when(t):
            if not t:
                return ""
            lt = time.localtime(t)
            if lt[:3] == today:
                return time.strftime("%H:%M", lt)
            return "昨天" if time.localtime(t + 86400)[:3] == today else f"{lt.tm_mon}/{lt.tm_mday}"

        def folded(label, names, numbered):
            """「久没动静 6：1号 forcing、2号 forcing:4、…」：灰字，一个不落全列出来（放不下就折行），都叫 exp-xxx 的只写 xxx。"""
            pre = os.path.commonprefix(names) if len(names) > 1 else ""
            pre = pre[:pre.rfind("-") + 1]
            line = f"{label} {len(names)}：" + "、".join(" ".join(x for x in (badge(n) if numbered else "", n[len(pre):] or n) if x)
                                                     for n in names)
            return _note(line), line

        order = sorted(alive, key=lambda n: (group(alive[n], n), -alive[n].get("statusAt", 0)))
        groups = {g: [n for n in order if group(alive[n], n) == g] for g in range(5)}
        names = {0: "等你", 1: "干活中", 2: "等后台", 3: "空闲"}
        tags = {0: "red", 1: "green", 2: "orange"}
        els, text = [], []
        for g in range(4):
            members = groups[g] + (groups[4] if g == 3 else [])
            if not members:
                continue
            if els:
                els.append({"tag": "hr"})
            els.append(_note(f"{names[g]} {len(members)}"))
            text.append(f"{names[g]} {len(members)}")
            left, right = [], []
            for name in groups[g]:
                e = alive[name]
                kind = mark(e)
                tag = f"<text_tag color='{'violet' if kind.startswith('Codex') else 'neutral'}'>{kind}</text_tag>"
                state = f"<text_tag color='{tags[g]}'>{names[g]}</text_tag> " if g in tags else ""
                stamp = when(e.get("statusAt"))
                who = badge(name) + " " if badge(name) else ""
                room = LINE_EM - (text_em(names[g]) * 0.85 + 1.9 if g in tags else 0) - text_em(kind) * 0.85 - 1.3 - text_em(stamp) - 1.5
                left.append(f"{state}{who}**{fit(name, room - text_em(who))}** {tag}")
                right.append(f"<font color='grey'>{stamp}</font>" if stamp else "　")
                text.append(f"· {names[g] + ' ' if g in tags else ''}{who}{name} {kind}  {stamp}")
                line = latest(name, e, g)
                if line:
                    left.append(f"<font color='grey'>　{fit(line, LINE_EM - 5)}</font>")
                    right.append("　")                     # 占一行，右边的时间才跟名字对齐
                    text.append(f"    {one_line(line, 60)}")
            if left:
                els.append({"tag": "column_set", "flex_mode": "none", "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [_mk("\n".join(left))]},
                    {"tag": "column", "width": "auto", "elements": [{"tag": "markdown", "content": "\n".join(right), "text_align": "right"}]}]})
            if g == 3 and groups[4]:
                el, line = folded("久没动静", sorted(groups[4], key=lambda n: (badge(n) == "", int(badge(n)[:-1] or 0), n)), True)
                els.append(el)
                text.append(line)
        if off:                                          # 下班的：一行灰字，只有名字（下班就没有工号了），刚下班的在前
            if els:
                els.append({"tag": "hr"})
            el, line = folded("下班", off, False)
            els.append(el)
            text.append(line)
        if not order and not off:
            els.append(_md("现在还没有员工。"))
        held, hours = self.state.get("held", 0), self.night()
        sub = " · ".join([f"{len(groups[g]) + (len(groups[4]) if g == 3 else 0)} 人{names[g].replace('干活中', '干活')}"
                          for g in range(4) if groups[g] or (g == 3 and groups[4])]
                         + ([f"夜间免打扰到 {hours[1]}:00"] if self.quiet_now() else [])) or "没有人在上班"
        pick = {"tag": "select_static", "placeholder": {"tag": "plain_text", "content": "选员工"},
                "options": [{"text": {"tag": "plain_text", "content": fit(label_name(n), LINE_EM - 4)}, "value": f"#enter {n}"}
                            for n in order]
                           + [{"text": {"tag": "plain_text", "content": fit(label_name(n) + "（下班）", LINE_EM - 4)}, "value": f"#enter {n}"}
                              for n in off]}
        main = ([_button(f"看攒下的 {held}", "#night all", "primary")] if held else []) + [_button("刷新", "#board now")]
        more = [_button("名单", "列表"), _button("怎么用", "帮助")]
        while len(main) > 1 and 8.5 + sum(_btn_em(b) + 0.6 for b in main) + 3 > LINE_EM:   # 下拉大约 8 个字宽；放不下的挪进「···」
            more.insert(0, main.pop())
        bar = _actions(main, more)
        if pick["options"]:
            bar["actions"].insert(0, pick)
        els.append(bar)
        card = Card(_card("📋 员工看板", "wathet", els, footer=talking_to(self.state), tags=usage_tags({}), sub=sub),
                    "📋 员工看板\n" + "\n".join([sub] + text))
        card.slot, card.minor = "status", minor
        return card

    def steps_card(self, name, s, since):
        asked, steps, _ = turn_steps(s.get("sessionId"), since)
        inline = [t for k, t, _ in steps if k == "said"]
        older = [x for x in said_between(s.get("sessionId"), since) if x["t"] < min((y["t"] for y in inline), default=now()) - 2]
        steps = [("said", x, True) for x in older] + steps   # 对话记录末尾那截之前听到的话（heard.jsonl 记着）
        if not steps:
            return f"🔎【{name}】这一轮没有记录到过程。"
        els = ([_md(f"<font color='grey'>你：{one_line(asked, 80)}</font>"), {"tag": "hr"}]
               if asked and not any(k == "said" for k, _, _ in steps) else [])
        els.append(_md("\n".join(self._step_lines(steps, 30))))
        return Card(_card(f"🔎【{name}】这一轮的过程", "grey", els), f"🔎【{name}】共 {len(steps)} 步")

    def slash_card(self, name, s, note, done=False):
        """/compact 这类要跑一会儿的命令：跑着的时候计时、转圈；做完写上下文从多少降到多少。note = [发出时间, 命令, 发之前的上下文%]。"""
        word, before = note[1], (note[2] if len(note) > 2 else None)
        label, desc = slash_label(word)
        took = fmt_secs(now() - note[0])
        who = self.who(name, s)
        if done:
            after = session_status(s).get("ctx")
            ctx = f"上下文 **{before}% → {after}%**" if before is not None and after is not None else ctx_text(session_status(s))
            if word == "/compact" and before is not None and after is not None and after >= before:   # 按了停止，或没压成
                return alert(f"⏹【{name}】{label}没做完", "\n".join(x for x in (who, f"跑了 {took} 停下了，上下文还是 {after}%。") if x),
                             "grey", [_button("看屏幕", f"屏 {name}")])
            body = "\n".join(x for x in (who, f"<font color='grey'>✓ 用时 {took}</font>", ctx) if x)
            return alert(f"✅【{name}】{label}做完了", body, "green", [_button("✍️ 接着说", f"对话 {name}") if not is_assistant(name)
                                                                      else _button("看屏幕", f"屏 {name}")])
        dots = "●○○ ○●○ ○○●".split()[int(now() / 3) % 3]
        body = "\n".join(x for x in (who, f"<font color='grey'>{desc}</font>" if desc else "",
                                      f"⏳ **正在{label}** {dots}　已 {took}" + (f"\n<font color='grey'>开始前上下文 {before}%</font>" if before is not None else "")) if x)
        card = alert(f"⏳【{name}】正在{label} · {took}", body, "wathet",
                     [_button("⏹ 停止", f"#stop {name}", "danger"), _button("看屏幕", f"屏 {name}")],
                     live=True, state=self.state)
        return card

    def live_updates(self, names):
        """给飞书守护进程用：这些“思考中”卡片现在该显示什么；做完的返回最终回复（final=True）。"""
        self.state = load_json(STATE, self.state)
        live = discover()
        out = []
        for name in names:
            since, s = self.state["remote_pending"].get(name), live.get(name)
            note = self.state.get("slash", {}).get(name)
            if note and not since:                          # /compact 这类：跑着就刷新，停下来原地换成结果
                if s and (s["status"] == "busy" or now() - note[0] < 5):
                    out.append((name, self.slash_card(name, s, note), False))
                    continue
                self.state["slash"].pop(name, None)
                out.append((name, self.slash_card(name, s, note, done=True) if s else None, True))
                continue
            if not since:
                out.append((name, None, True))              # 已经由别处处理过了
            elif not s:
                self.state["remote_pending"].pop(name, None)
                out.append((name, alert(f"❌【{name}】{agent_word(reg_entry(name))} 退出了", "这句话没等到回复。", "red",
                                        [_button("重启", f"重启 {name}", "danger"), _button("返回列表", "看看")]), True))
            elif s["status"] != "busy" and since <= s["statusAt"] + 1:
                if s.get("waiting") and screen_menu(pane_rows(s)) and not waiting_on(s):
                    s = dict(s, waiting="")                 # 停在你自己打开的菜单上（答完以后你发了 /model）：不是它在问你，回复照常发，菜单在屏幕卡片里选
                ask = self.ask_card(name, s) if s.get("waiting") else None
                heard = ask or any(turn_steps(s.get("sessionId"), since))   # 对话记录里有你这句话（或它的回应）
                if s.get("waiting") and not ask and now() - s["statusAt"] < 10 or not heard and now() - since < 20:
                    out.append((name, self.live_card(name, s, since), False))   # 对话记录可能还没写到，再等几秒
                    continue
                self.state["remote_pending"].pop(name, None)
                sent = self.state.setdefault("mon", {}).setdefault("sent", {})
                sent[f"idle:{s['sessionId']}:{s['statusAt']}"] = now()
                if ask:
                    sent[f"ask:{s['sessionId']}:{ask.ask_id}"] = now()          # 它在问你：这张卡片换成问题和选项
                if self.state.get("current") == name:
                    self.state["current_at"] = now()
                if s.get("waiting") and not ask:              # 停在一个认不出的框上：给屏幕和按键，不能当成答完了
                    out.append((name, self.screen_card(name, s, note=f"⏸【{name}】停在下面这个框上等你选（机器人没认出是什么框）。"
                                                                     "用下面的键操作，屏幕会跟着刷新。"), True))
                    continue
                if not heard:                                 # 一直没收到：别把上一轮的回复当成这次的
                    out.append((name, self.screen_card(name, s, note=f"⚠️ 你那句话好像没送到【{name}】：等了 20 秒它都没开始处理。"
                                                                     "下面是它现在的屏幕，看看是不是被什么挡住了，处理完再发一次。"), True))
                    continue
                out.append((name, ask or self.reply_card(name, s, since), True))
            else:
                out.append((name, self.live_card(name, s, since), False))
        self.save()
        return out

    def topic(self, name, e):
        """它在做什么：Claude Code 自己起的对话标题；没有就用你说的第一句话。"""
        return topic_of(e)

    def who(self, name, e):
        """以前卡片正文第一行的「📌 话题 · 目录」：现在话题是标题下面的副标题、目录在底栏标签里（_card 自动加），正文不再重复。"""
        return ""

    def cmd_rename(self, word, new, reg):
        """给会话起个好认的名字（只改这里显示和用的名字，不动它的 tmux 和对话）。"""
        name, e, err = self.resolve(word, reg)
        if err:
            return err
        if is_assistant(name):
            return "助手的名字是自动起的，不用改；对话记录里按你说的第一句话显示。"
        if not re.fullmatch(r"[\w\u4e00-\u9fff.\-]{1,16}", new) or is_assistant(new) or is_quiet(new):
            return "新名字最多 16 个字，只能有汉字、字母、数字、- 和 _，不能以「助手」「资讯」开头。"
        if new in reg and new != name:
            return f"已经有一个叫「{new}」的会话了，换一个名字。"
        aliases = {k: v for k, v in load_json(ALIASES, {}).items() if _proc_alive(int(k))}
        aliases[str(e["pid"])] = new
        save_json(ALIASES, aliases)
        badges = load_json(BADGES, {})
        if name in badges:                               # 工号跟着人走
            badges[new] = badges.pop(name)
            save_json(BADGES, badges)
        self.state["roster"] = [new if n == name else n for n in self.state.get("roster", [])]
        reg[new] = reg.pop(name)
        save_json(REGISTRY, reg)
        for key in ("default", "current"):
            if self.state.get(key) == name:
                self.state[key] = new
        for key in ("remote_pending", "pending_from"):
            if name in self.state.get(key, {}):
                self.state[key][new] = self.state[key].pop(name)
        pend = load_json(PENDING, {})
        if name in pend:
            pend[new] = pend.pop(name)
            save_json(PENDING, pend)
        self.state.setdefault("renamed", {})[name] = new
        self.save()
        return alert(f"✏️ 已把【{name}】改名为【{new}】", f"{self.who(new, e)}\n之后卡片上都显示【{new}】，命令里也用这个名字。重启后名字不变。",
                     "blue", [_button("进入", f"#enter {new}", "primary"), _button("返回列表", "看看")])

    def chat_buttons(self, name):
        """正在回复它：〔↩ 回到助手〕永远在第一个；否则〔✍️ 回复它〕＝接下来打的字都发给它。"""
        rest = [_button("看全文", f"看 {name} 全部"), _button("看屏幕", f"屏 {name}")]
        if self.state.get("current") == name:
            return [_button(f"↩ 回到{self.state.get('default') or '默认对象'}", "退出", "danger")] + rest
        if self.state.get("default") == name:
            return rest + ([_button("🆕 新对话", "新对话"), _button("🗂 对话记录", "对话记录")] if is_assistant(name) else [])
        return [_button("✍️ 回复它", f"对话 {name}", "primary")] + rest

    def close_other_assistants(self, keep=None):
        """切换/新建前，把正在运行的其他助手正常关掉（只留对话记录），省资源也少一个可被 attach 的进程。"""
        closed = []
        for n, s in discover().items():
            if is_assistant(n) and n != keep:
                self.cmd_close(n, s)
                closed.append(n)
        return closed

    def cmd_new_assistant(self, suffix=""):
        self.state["current"] = None
        cur = self.state.get("default")
        live = discover()
        if cur in live and is_assistant(cur) and not first_user_text(live[cur].get("sessionId")):
            return alert("🆕 现在已经是新对话", f"【{cur}】还没聊过，直接打字就行。", "blue", [_button("🗂 对话记录", "对话记录")])
        self.close_other_assistants()
        live, reg = discover(), load_json(REGISTRY, {})
        taken = set(reg) | set(live) | {s["tmux_session"] for s in live.values()}
        base = f"助手-{suffix}" if suffix else "助手"
        name, i = base, 2
        while name in taken:
            name, i = f"{base}-{i}", i + 1
        args = DEFAULT_NEW_ARGS + ["--append-system-prompt-file", assistant_prompt()]
        ok, out = launch(name, ASSISTANT_DIR, with_effort(args, self.state.get("assistant_effort", ASSISTANT_EFFORT)))
        if not ok:
            return f"🤖 没能开新助手：\n{out}"
        result = self.after_launch(name)
        self.state["default"] = name
        self.save()
        return alert("🆕 新对话已开好", f"现在直接打字就是在新对话【{name}】里聊。之前的对话已关闭并保存，点〔🗂 对话记录〕可以切回去。",
                     "blue", [_button("🗂 对话记录", "对话记录")]) if "claude 已起来" in result else result

    def history(self, reg):
        """助手的对话记录：像 App 的对话列表，点〔切换〕回到那段对话。"""
        names = sorted((n for n in reg if is_assistant(n) and
                        (n == self.state.get("default") or first_user_text(reg[n].get("sessionId")))),
                       key=lambda n: -(reg[n].get("statusAt") or reg[n].get("died") or 0))
        if not names:
            return alert("🗂 对话记录", "还没有助手对话。", "blue", [_button("🆕 新对话", "新对话", "primary")])
        default = self.state.get("default")
        elements, lines = [], []
        for n in names:
            e = reg[n]
            title = first_user_text(e.get("sessionId")) or "（还没说话）"
            when = time.strftime("%m-%d %H:%M", time.localtime(e.get("statusAt") or e.get("died") or 0))
            state = "当前" if n == default else ("运行中" if e.get("alive") else "已关闭")
            row = f"**{title}**\n<font color='grey'>{n} · {state} · {when}</font>"
            btn = (_button("✓ 当前", "看看") if n == default else
                   _button("切换", f"切换 {n}", "primary" if e.get("alive") else "default"))
            elements += [{"tag": "div", "text": {"tag": "lark_md", "content": row}, "extra": btn}, {"tag": "hr"}]
            lines.append(f"{title}　{n}　{state}　{when}")
        if len(names) == 1:
            elements.append(_md("<font color='grey'>目前只有这一段对话。点〔🆕 新对话〕开一个新的，之后这里就能在新旧对话之间切换。</font>"))
        elements.append({"tag": "action", "actions": [_button("🆕 新对话", "新对话", "primary")]})
        return Card(_card("🗂 对话记录", "blue", elements, footer="已关闭的对话切换时会自动接回原来的上下文"),
                    "\n".join(lines))

    def cmd_switch(self, word, reg):
        name, e, err = self.resolve(word, reg, want_alive=False)
        if err:
            return err
        if is_assistant(name):
            self.close_other_assistants(keep=name)
        if not e.get("alive"):
            out = self.cmd_restart(name, e)
            if "claude 已起来" not in str(out):
                return out
        self.state["default"], self.state["current"] = name, None
        self.save()
        title = first_user_text(e.get("sessionId")) or name
        return alert(f"⭐ 已切换到「{title}」", f"【{name}】现在是默认对象，直接打字就是接着这段对话聊。", "blue",
                     [_button("🗂 对话记录", "对话记录"), _button("🆕 新对话", "新对话")])

    def cmd_close(self, name, s, keep_badge=False):
        """正常退出：先发 /exit，8 秒没退再按两次 Ctrl-C。
        codex 发 /quit；它的 /quit 只是断开，手上的活会在后台接着跑，所以在干活 / 停在框上时先按 Esc 让它停下。
        keep_badge：马上就重开（换新版本），工号不收回。"""
        closing = {k: t for k, t in load_json(CLOSING, {}).items() if now() - t < 86400}
        closing[str(s["pid"])] = now()                   # 是你让它下班的：监控看到它没了不发「❌ 退出了」
        save_json(CLOSING, closing)
        if is_codex(s):
            self.codex_settle(s)
            if not self.codex_quit(s):
                return alert(f"⚠️【{name}】没能关闭", "codex 停在一个框上，没敢替你选。点〔看屏幕〕用按键处理完再下班。", "red",
                             [_button("看屏幕", f"屏 {name}")])
        else:
            send_text(s, "/exit")
        for _ in range(8):
            time.sleep(1)
            if not _proc_alive(s["pid"]):
                break
        else:
            send_keys(s, ["ctrl-c", "ctrl-c"])
            time.sleep(3)
        if _proc_alive(s["pid"]):
            return alert(f"⚠️【{name}】没能关闭", "它可能正在执行操作。点〔看屏幕〕看看。", "red", [_button("看屏幕", f"屏 {name}")])
        if is_assistant(name) or is_quiet(name):
            crterm.close_group(s)                        # 助手、资讯关了就不留空壳 shell
        was = badge(name)
        if not keep_badge:
            release_badge(name)                          # 下班就收回工号，回来时取最小的空号
        return alert(f"🌙 {was}【{name}】下班了", f"对话都留着，工号 {was} 收回了。上班后在原目录接着原来的对话干（也可以发「上班 {name}」）。"
                     if was else f"对话都留着。上班后在原目录接着原来的对话干（也可以发「上班 {name}」）。", "grey",
                     [_button("上班", f"上班 {name}", "primary"), _button("看板", "看看")])

    def upgrade_tick(self):
        """客户端自己更新过以后：还在跑旧版本、又闲着的员工，一个个下班再上班换上新版本——接着原来的对话、回原来的 tmux 窗口，
        工号不变。在干活、弹着框、底下还有命令在跑的不动，等它闲下来再说。一轮做完发一张汇总。由 crbot 的 upgrade_loop 定时调用。"""
        live, _ = self.refresh()
        done, bad = [], []
        for name, e in live.items():
            ver = outdated(e)
            if not ver or is_quiet(name) or not free_to_restart(e):
                continue
            logging.getLogger("cc-crew").info("换新版本：重开【%s】（%s → %s）", name, *ver)
            self.cmd_close(name, e, keep_badge=True)
            if _proc_alive(e["pid"]):
                bad.append(f"{label_name(name)}（没能关掉）")
                continue
            out = self.cmd_restart(name, {**e, "alive": False})
            (done if "已起来" in out else bad).append(f"{label_name(name)}（{ver[0]} → {ver[1]}）" if "已起来" in out
                                                     else f"{label_name(name)}（{one_line(out, 60)}）")
        if done or bad:
            body = "\n".join(x for x in (
                f"重开了：{'、'.join(done)}\n都接着原来的对话，还在原来的 tmux 窗口，工号不变。" if done else "",
                f"没能重开：{'、'.join(bad)}\n点〔看看〕找到它，〔上班〕再试。" if bad else "") if x)
            self.push(alert("⬆️ 员工换上了新版本" if not bad else "⚠️ 换新版本没全成", body, "grey" if not bad else "orange",
                            [_button("看板", "看看")]), "upgrade")

    def codex_quit(self, s):
        """打 /quit：先按 Esc 关掉弹着的框（不选任何一项；「Resume paused goal?」就是先不恢复目标），
        确认「/quit」真进了输入框才回车——不然回车会替你选了框里的第一项。打出去了返回 True。"""
        for _ in range(3):
            if not crcodex.menu(pane_rows(s)):
                break
            crterm.key(s, "Escape")
            time.sleep(0.8)
        if not clear_box(s) or not crcodex.ready(s):
            return False
        crterm.literal(s, "/quit")
        for _ in range(8):
            time.sleep(0.25)
            if crcodex.box_text(s).startswith("/quit"):
                crterm.key(s, "Enter")
                return True
        clear_box(s)
        return False

    def codex_settle(self, s, secs=10):
        """codex 在干活或停在批准框上：按 Esc 停下（批准框上 Esc＝不批准），等它真停了。返回停没停。"""
        for _ in range(2):
            cur = next((x for x in discover().values() if x.get("pid") == s.get("pid")), None) or s
            if cur.get("status") not in ("busy", "waiting"):
                return True
            send_keys(cur, ["esc"])
            for _ in range(secs * 2):
                time.sleep(0.5)
                cur = next((x for x in discover().values() if x.get("pid") == s.get("pid")), None)
                if not cur or cur.get("status") not in ("busy", "waiting"):
                    return True
        return False

    def cmd_off_many(self, words, reg):
        """「下班 2 3 6」：几个员工一起下班（各自正常退出，对话记录留着）。"""
        done, bad = [], []
        for w in words:
            name, s, err = self.resolve(w, reg)
            if err:
                bad.append(f"{w}：{err}")
            elif is_assistant(name):
                bad.append(f"{name}：助手是秘书，不下班")
            else:
                label = label_name(name)                 # 下班前的工号（关掉就收回了）
                self.cmd_close(name, s)
                if _proc_alive(s["pid"]):
                    bad.append(f"{label}：没关掉，可能正在执行操作，点〔看屏幕〕看看")
                else:
                    done.append(label)
        lines = [f"已下班：{'、'.join(done)}。工号收回了，对话记录都留着，发「上班 名字」接着干。"] if done else []
        return alert(f"🌙 {len(done)} 位员工下班了", "\n".join(lines + bad), "grey", [_button("看板", "看看")])

    def fire_card(self, name, e):
        """开除前确认：关掉它、从员工名单删掉；对话记录和文件都不删。"""
        if is_assistant(name):
            return "助手是你的秘书，不能开除。想让它忘掉前面的事，发「新对话」。"
        busy = "\n**它现在正在干活，开除会打断它。**" if e.get("alive") and e.get("status") == "busy" else ""
        again = (f"codex resume {e.get('sessionId')}" if e.get("sessionId") else "codex resume") if is_codex(e) else "claude --resume"
        body = (f"{self.who(name, e)}{busy}\n会关掉它，并从员工名单和看板上删掉。"
                f"对话记录和它目录里的文件都不删，以后在电脑上进 {_short_dir(e.get('cwd'))} 运行 {again} 还能找回。")
        return alert(f"⚠️ 确定开除【{name}】？", body, "red",
                     [_button("确定开除", f"#fire {name} yes", "danger"), _button("算了", "看看")])

    def cmd_fire(self, name, e, reg):
        if is_assistant(name):
            return self.fire_card(name, e)
        if e.get("alive"):
            closed = self.cmd_close(name, e)
            if _proc_alive(e["pid"]):
                return closed                            # 没关掉：把原因告诉你，名单不动
        reg.pop(name, None)
        save_json(REGISTRY, reg)
        aliases = load_json(ALIASES, {})
        if aliases.pop(str(e.get("pid")), None):
            save_json(ALIASES, aliases)
        for key in ("default", "current"):
            if self.state.get(key) == name:
                self.state[key] = None
        for key in ("bg", "remote_pending", "asked_note", "slash"):
            self.state.get(key, {}).pop(name, None)
        self.state["roster"] = [n for n in self.state.get("roster", []) if n != name]
        pend = load_json(PENDING, {})
        if pend.pop(name, None):
            save_json(PENDING, pend)
        self.save()
        return alert(f"👋【{name}】已离职", f"已经关掉，并从员工名单里删掉了。对话记录和 {_short_dir(e.get('cwd'))} 里的文件都还在。",
                     "grey", [_button("返回列表", "看看")])

    def chat_target(self):
        """当前「对话」中的会话名；超过 30 分钟没说话自动结束。"""
        cur = self.state.get("current")
        waiting = cur in self.state.get("remote_pending", {})      # 它还在处理你的消息：不计时
        if cur and not waiting and now() - self.state.get("current_at", 0) > CHAT_TIMEOUT_SECS:
            self.state["current"], self.state["expired_chat"] = None, cur
            self.save()
            return None
        return cur

    def menu(self, reg):
        """会话列表卡片：顶部总览 + 每个 claude 一行（名字 / 状态 · 目录 / 按钮）。"""
        # 只显示当前助手；其他（已关闭的）助手只在「对话记录」里
        reg = {n: e for n, e in reg.items() if (not is_assistant(n) or n == self.state.get("default"))
               and not (is_quiet(n) and not e.get("alive"))}          # 每日资讯跑完自己会退出，不算异常
        if not reg:
            return "🤖 没有发现 tmux 里的 claude 会话。"
        busy = sum(1 for e in reg.values() if e.get("alive") and e["status"] == "busy")
        idle = sum(1 for e in reg.values() if e.get("alive") and e["status"] != "busy")
        dead = sum(1 for e in reg.values() if not e.get("alive"))
        overview = f"🟢 {busy} 在干活　🟡 {idle} 停下" + (f"　🌙 {dead} 下班了" if dead else "")
        elements, lines = [_md(overview)], [overview]
        if not proxy_ok():
            elements.append(_md(f"⚠️ **代理 {PROXY} 不通**，所有 claude 都连不上网"))
            lines.append("⚠️ 代理不通")
        default = self.state.get("default")
        order = sorted(reg, key=lambda n: (n != default, not reg[n].get("alive"), reg[n].get("status") != "busy", n))
        bars = {n: session_status(e) for n, e in reg.items() if e.get("alive")}
        efforts = {n: b.get("effort", "") for n, b in bars.items()}
        for name in order:
            e = reg[name]
            talking = "　✍️ 你打的字现在发给它" if self.state.get("current") == name else ""
            think = f" · 思考 {EFFORT_CN.get(efforts[name], '?')}" if name in efforts else ""
            if ctx_text(bars.get(name, {})):
                think += " · " + ctx_text(bars[name])
            grey = f"<font color='grey'>{_state_label(e)}{think} · {_short_dir(e.get('cwd'))}</font>"
            tag = " ".join(x for x in (badge(name), f"<font color='{'purple' if is_codex(e) else 'blue'}'>{mark(e)}</font>") if x)
            if name == default:
                row = f"⭐ {tag} **{name}**　<font color='green'>默认：直接打字就是和它聊</font>\n{grey}"
            else:
                topic = one_line(self.topic(name, e), 30)
                row = f"{tag} **{name}**{'　' + topic if topic else ''}{talking}\n{grey}"
            btn = (_button("进入", f"#enter {name}", "primary") if e.get("alive")
                   else _button("重启", f"重启 {name}", "danger"))
            elements += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": row}, "extra": btn}]
            star = "⭐ 默认（直接打字就是和它聊）" if name == default else talking
            peer = f"　同事间叫 {e['peer']}" if e.get("peer") and e.get("alive") else ""
            lines.append(f"{badge(name)}〔{mark(e)}〕{name}{star}　{_state_label(e)}　{_short_dir(e.get('cwd'))}{peer}")
        bottom = [_button("🔄 刷新", "看看"), _button("🆕 新对话", "新对话"), _button("🗂 对话记录", "对话记录")]
        if default in efforts:
            bottom.append(_button(f"🧠 {default}思考：{EFFORT_CN.get(efforts[default], '?')}", f"#effort {default}"))
        elements += [{"tag": "hr"}, {"tag": "action", "actions": bottom + [_button("帮助", "帮助")]}]
        alive = busy + idle
        who = "Claude Code / Codex" if any(is_codex(e) for e in reg.values()) else "Claude Code"
        return Card(_card(f"🤖 {who} · {alive} 个在运行", "blue", elements, tags=usage_tags({})),
                    "\n".join(lines + ([f"已用：{quota_text()}"] if quota_text() else [])))

    def session_card(self, name, e):
        """单个会话卡片：状态 | 目录 并排，最新进展，操作按钮。"""
        summary = last_summary(e.get("sessionId"), 500) or "（还没有回复）"
        fields = {"tag": "div", "fields": [
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**状态**\n{_state_label(e)}"}},
            {"is_short": True, "text": {"tag": "lark_md", "content": f"**目录**\n{_short_dir(e.get('cwd'))}"}}]}
        bar = session_status(e) if e.get("alive") else {}
        if e.get("alive"):
            fields["fields"].insert(1, {"is_short": True, "text": {"tag": "lark_md", "content": f"**思考程度**\n{EFFORT_CN.get(bar.get('effort'), '?')}"}})
        if bar.get("ctx") is not None:
            fields["fields"].append({"is_short": True, "text": {"tag": "lark_md", "content": f"**上下文已用**\n{ctx_text(bar).replace('上下文 ', '')}"}})
        if e.get("alive") and quota_text():
            fields["fields"].append({"is_short": True, "text": {"tag": "lark_md", "content": f"**额度已用**\n{quota_text().replace(' · ', '，')}"}})
        actions = self.chat_buttons(name)[:1] + [_button("最新情况", f"看 {name}"), _button("看屏幕", f"屏 {name}")]
        more = []
        if self.state.get("default") != name:
            more.append(_button("⭐ 设为默认", f"设为默认 {name}"))
        if e.get("alive"):
            off = _button("🌙 下班", f"下班 {name}", "danger")
            off["confirm"] = {"title": {"tag": "plain_text", "content": f"让【{name}】下班？"},
                              "text": {"tag": "plain_text", "content": ("它正在干活，下班会打断它。" if e.get("status") == "busy" else "")
                                       + "会关掉它，对话记录留着，随时可以叫回来接着干。"}}
            more.append(off)
        else:
            more.append(_button("☀️ 上班", f"上班 {name}", "primary"))
        if not is_assistant(name):
            more.append(_button("开除", f"#fire {name}", "danger"))
        more.append(_button("返回列表", "看看"))
        if self.state.get("current") == name:
            back = self.state.get("default") or "默认对象"
            hint = f"✍️ 现在你直接打字就是发给它，不用引用；说完点〔↩ 回到{back}〕，30 分钟不说话也会自动回到{back}。"
        elif self.state.get("default") == name:
            hint = "⭐ 它是默认对象，你随手打的字会发给它。"
        else:
            hint = "👀 现在只是查看，你打的字不会发给它；要发给它点〔✍️ 回复它〕。"
        topic = self.topic(name, e)
        elements = ([_md(f"📌 **{one_line(topic, 60)}**")] if topic else []) + [fields, {"tag": "hr"}, _md(f"**最新进展**\n{summary}")]
        if not is_assistant(name):
            more.insert(-1, _button("✏️ 改名", f"#rename {name}"))
        if e.get("alive") and e.get("waiting"):
            actions.insert(0, _button("❓ 看它在问什么" if "permission" not in e["waiting"] else "🔐 看它要批准什么",
                                      f"#ask {name}", "primary"))
        if e.get("alive") and e["status"] == "busy":
            _, steps, _ = turn_steps(e.get("sessionId"), e["statusAt"])
            doing = self._step_lines(steps, 3)
            elements.append(_md(f"**正在做**（这一轮已 {fmt_secs(now() - e['statusAt'])}）\n" + ("\n".join(doing) or "🧠 思考中…")))
            more.insert(0, _button("⏹ 停止", f"#stop {name}", "danger"))
        if e.get("alive"):
            actions += [self.effort_button(name, e), _button("⌨️ 命令", f"#cmds {name}")]
        if (bar.get("ctx") or 0) >= CTX_WARN:
            more.insert(0, self.compact_button(name))
        elements += [{"tag": "hr"}, {"tag": "action", "actions": actions}, {"tag": "action", "actions": more}]
        card = _card(f"🤖【{name}】", _state_color(e), elements, footer=hint)
        return Card(card, f"🤖【{name}】\n{_state_label(e)}　{_short_dir(e.get('cwd'))}\n最新进展：{summary}\n{hint}")

    def cmd_restart(self, name, e):
        if e.get("alive"):
            return f"【{name}】的 {agent_word(e)} 还在运行，不重启。真要重启，先「键 @{name} ctrl-c ctrl-c」让它退出。"
        if is_codex(e):                                  # 用它原来那一套 codex 的环境（官方 / 备用…）在原目录 codex resume
            ok, note = crcodex.free_thread(e)            # 对话还挂在共用后台服务里的话，先腾出来
            if not ok:
                return f"【{name}】没有重启：\n{note}"
            ok, out = launch(name, e["cwd"], crcodex.resume_args(e), home=e.get("home") or e.get("cfg"), where=last_spot(e))
            if not ok:
                return f"【{name}】没有重启：\n{out}"
            return "\n".join(filter(None, [note, out, self.after_launch(name, keep=name)]))
        if e.get("cfg") and os.path.realpath(e["cfg"]) != os.path.realpath(CLAUDE_DIR):
            return f"【{name}】没有重启：它用的是另一个配置目录（{e['cfg']}），安全启动脚本只会用正版账号开，请在终端里按原来的方式自己打开。"
        args = list(e.get("args") or [])
        if is_assistant(name):
            args = with_effort(args, self.state.get("assistant_effort", ASSISTANT_EFFORT))
        if e.get("sessionId") and transcript_path(e["sessionId"]):
            args += ["--resume", e["sessionId"]]          # 没有记录就开全新对话，绝不用 --continue
        ok, out = launch(name, e["cwd"], args, where=last_spot(e))
        if not ok:
            return f"【{name}】没有重启：\n{out}"
        return out + "\n" + self.after_launch(name, keep=None if is_assistant(name) else name)

    def lost_card(self, were_on):
        """机器人启动时：上次还在上班、现在进程没了的员工（电脑关机 / 重启过，或它自己退出了）。
        正常重启机器人时员工都还在，不会有这张卡片。资讯这类自动会话不算。"""
        live, reg = self.refresh()
        lost = [n for n in were_on if n not in live and n in reg and not is_quiet(n)]
        if not lost:
            return None
        lost.sort(key=lambda n: (not is_assistant(n), badge(n) or "99"))
        who = "、".join(label_name(n) for n in lost)
        buttons = [_button("☀️ 全部上班", "#wakeall " + " ".join(lost), "primary")]
        buttons += [_button(f"☀️ {badge(n) or n} {mark_of(n)}", f"上班 {n}") for n in lost[:7]]
        body = (f"{who} 上次还在上班，现在进程没了（电脑关机、重启过，或它自己退出了）。\n"
                "对话记录都在，上班就是在原目录接着原来的对话。")
        return alert(f"💤 {len(lost)} 个员工断了", body, "orange", buttons)

    def wake_all(self, words, reg):
        done, bad = [], []
        for w in words:
            name, e, err = self.resolve(w, reg, want_alive=False)
            if err or e.get("alive"):
                continue
            out = self.cmd_restart(name, e)
            (bad if "没有重启" in out or "没看到" in out else done).append(label_name(name))
            live, reg = self.refresh()
        body = (f"上班了：{'、'.join(done)}" if done else "") + (f"\n没能上班：{'、'.join(bad)}（点〔看看〕再试）" if bad else "")
        return alert("☀️ 叫大家上班", body or "大家都已经在上班了。", "green" if not bad else "orange", [_button("看板", "看看")])

    def wake(self, name, e):
        """让下班的员工上班：走安全启动脚本（检查代理、带上代理环境变量），在原目录 --resume 原来的对话，
        名字不变，工号取最小的空号；等它能接话了再返回。返回 (会话, 出错说明)。"""
        out = self.cmd_restart(name, e)
        seen = 0
        for _ in range(60):
            s = discover().get(name)
            if s and is_codex(s) and screen_menu(pane_rows(s)):   # codex 起来时弹的框（信任 hooks、要不要恢复暂停的目标…）：你来选，别往里打字
                return None, self.screen_card(name, s, note="☀️ codex 起来了，但停在一个框上等你选。没有替你按键，也没发别的话。")
            ok = s and s["status"] != "busy" and not s.get("waiting") and input_ready(s)
            seen = seen + 1 if ok else 0
            if seen >= (3 if s and is_codex(s) else 1):   # codex 的框有时晚一两秒才弹出来：连着几秒都能打字才算
                return s, ""
            time.sleep(1)
        return None, out

    def after_launch(self, tag, keep=None):
        """等 launch(tag, …) 开的那个起来（窗格上记着 tag）。"""
        for _ in range(20):
            time.sleep(1)
            live = discover()
            for name, s in live.items():
                if s.get("tag") == tag or (s.get("term") == "ccm" and s.get("private") and s["group"] == tag):   # ccm：名字就是它的组名
                    if keep and name != keep:            # 下班前叫什么，上班后还叫什么
                        aliases = load_json(ALIASES, {})
                        aliases[str(s["pid"])] = keep
                        save_json(ALIASES, aliases)
                        name = keep
                    if is_codex(s):                      # codex 接回长对话要好一会儿：等到能打字（或卡在别的画面上）再看屏
                        for _ in range(40):
                            rows = pane_rows(s)
                            if crcodex.ready(s) or screen_menu(rows) or any("open in another app" in r for r in rows):
                                break
                            time.sleep(1)
                    sync_badges(self.refresh()[1])       # 上班 / 新来的：马上分工号（最小的空号）
                    return f"{agent_word(s)} 已起来（会话名 {name}{'，' + badge(name) if badge(name) else ''}）。屏幕：\n{screen(s, 15)}"
        if crterm.launch_mode() != "tmux" and not LAUNCH:
            return "20 秒内没看到 claude 起来。看一眼电脑上新开的那个终端窗口里写了什么。"
        spot = crterm.find_tagged(tag)
        if not spot:
            return "20 秒内没看到它起来，也没找到它的 tmux 窗口。"
        return "20 秒内没看到它起来，屏幕：\n" + screen({"term": "tmux", "socket": spot[0], "pane": spot[1]}, 20)

    # ------------------------------------------------------------ 监控

    def monitor_tick(self):
        self.state = load_json(STATE, self.state)
        live, reg = self.refresh()
        mon = self.state.setdefault("mon", {})
        first_run = not mon.get("started")
        mon["started"] = True
        sent = mon.setdefault("sent", {})               # 去重：事件 key -> 时间

        def once(key, msg, kind):
            if key in sent:
                return
            sent[key] = now()
            if not first_run:
                self.push(msg, kind, must=kind == "peer")   # 同事的消息等你批准才送达：夜里也推

        self.state.pop("asking", None)                      # 以前按回复字面猜的「待拍板」，不再用
        closing = load_json(CLOSING, {})
        for name, e in reg.items():
            sid = e.get("sessionId") or ""
            if not e.get("alive") and (is_assistant(name) or is_quiet(name)):
                continue                                    # 助手是切换时主动关的，不报警
            if not e.get("alive") and str(e.get("pid")) in closing:
                sent.setdefault(f"dead:{sid}:{e.get('pid')}", now())   # 你让它下班的 / 换新版本重开的：不是意外退出，以后也不补报
                continue
            if not e.get("alive"):
                once(f"dead:{sid}:{e.get('pid')}", alert(f"❌【{name}】{agent_word(e)} 退出了", "重启会先检查环境和代理，再在原目录接回原对话。", "red",
                     [_button("重启", f"重启 {name}", "danger"), _button("看屏幕", f"屏 {name}")]), "dead")
                continue
            episode = f"{sid}:{e['statusAt']}"
            pending = self.state["remote_pending"].get(name)
            if e["status"] != "busy":
                idle_for = now() - e["statusAt"]
                note = self.state.get("slash", {}).get(name)
                if note and (name in load_json(PENDING, {}) or now() - note[0] < CARD_GRACE):
                    continue                                # 飞书那张会动的卡片自己会换成「做完了」
                elif note and note[0] <= e["statusAt"] + 1 and not is_quiet(name):     # 你发的 /compact 之类做完了
                    self.state["slash"].pop(name)
                    after = session_status(e).get("ctx")
                    label = slash_label(note[1])[0]
                    ctx = (f"上下文 {note[2]}% → {after}%" if len(note) > 2 and note[2] is not None and after is not None
                           else ctx_text(session_status(e)))
                    once(f"idle:{episode}", alert(f"✅【{name}】{label}做完了", "\n".join(x for x in (self.who(name, e), ctx) if x), "green",
                                                   [_button("看屏幕", f"屏 {name}"), _button("✍️ 回复它", f"对话 {name}")]), "slash")
                    continue
                remote = pending and pending <= e["statusAt"] + 1
                # 只要停下就通知（你在电脑前也照发）；助手的回复本来就会发卡片，资讯自己会推送；两天前的旧停顿不重复提醒；
                # 刚启动还没干过活的（启动后就一直空闲）不算停下
                fresh = e["statusAt"] - e.get("startedAt", 0) < 10
                if not remote and (idle_for < IDLE_DELAY_SECS or is_assistant(name) or is_quiet(name) or idle_for > 2 * 86400 or fresh):
                    continue
                if not remote and (f"idle:{episode}" in sent or f"limit:{episode}" in sent):
                    continue                                    # 这次停下已经提醒过了（每 3 秒检查一次，别重复读文件）
                held = held_peer(pane_rows(e)) if e.get("waiting") else None
                if held:                                        # 同事发来的消息等你批准才送达
                    if f"idle:{episode}" not in sent:
                        sent[f"idle:{episode}"] = now()
                        once(f"held:{sid}:{e['statusAt']}", self.held_card(name, e, held, reg), "peer")
                    continue
                if e.get("waiting"):                            # 停在选择题 / 权限框上：发「在问你」，不发「停下来了」
                    if (remote and (name in load_json(PENDING, {}) or now() - pending < CARD_GRACE)) or f"idle:{episode}" in sent:
                        continue                                # 飞书那张“思考中”卡片会自己换成问题
                    pt = waiting_on(e) if is_codex(e) else pending_tool(sid)
                    if not pt and idle_for < 10:
                        continue                                # 对话记录可能还没写到，再等几秒
                    sent[f"idle:{episode}"] = now()
                    ask = self.ask_card(name, e, pt=pt)
                    if ask:
                        self.state["remote_pending"].pop(name, None)
                        once(f"ask:{sid}:{ask.ask_id}", ask, "ask")
                    continue
                reply = last_reply(sid)
                if LIMIT_RE.search(reply[-400:]) or LIMIT_RE.search(screen(e, 8)):
                    once(f"limit:{episode}", alert(f"⛔【{name}】额度用完了", self.who(name, e) + "\n" + first_paragraph(screen(e, 8), SUMMARY_CHARS), "red",
                                                    [_button("看屏幕", f"屏 {name}")]), "limit")
                elif remote:
                    if name in load_json(PENDING, {}) or now() - pending < CARD_GRACE:
                        continue                                    # 飞书那张“思考中”卡片会自己换成回复（刚点的按钮：卡片还在登记）
                    self.state["remote_pending"].pop(name, None)
                    if self.state.get("current") == name:
                        self.state["current_at"] = now()            # 它回复了，也算一次活动
                    once(f"idle:{episode}", self.reply_card(name, e, pending), "reply")
                else:
                    sent[f"idle:{episode}"] = now()
                    turn = last_turn(sid) or {"id": episode, "t": e["statusAt"], "by": "you", "texts": [], "tools": 0, "said": []}
                    key = f"turn:{sid}:{turn['id']}:{len(turn['texts'])}"
                    if key in sent:
                        continue                                # 这一轮报过了：它只是又停了一下，或你在电脑上用了 /effort 之类
                    sent[key] = now()
                    if turn["by"] == "you":                     # 你叫它干的：做了事就提醒（只是本地命令、什么都没做的不算）
                        loud = bool(turn["texts"] or turn["tools"])
                    else:                                       # 后台醒来：写了一段实质内容才单独发
                        loud = len(re.sub(r"\s", "", "".join(turn["texts"]))) >= SUBSTANTIAL_CHARS
                    self.bg_update(name, e, turn, loud)
                    if loud and not first_run:
                        self.push(self.stop_card(name, e, turn), "idle")
            else:
                tp = transcript_path(sid)
                quiet = now() - os.path.getmtime(tp) if tp else 0
                if quiet > STUCK_MINS * 60:
                    once(f"stuck:{episode}", alert(f"⚠️【{name}】{int(quiet // 60)} 分钟没有新进展", self.who(name, e) + "\n可能卡住了，也可能在等长任务。", "yellow",
                                                     [_button("看屏幕", f"屏 {name}", "primary")]), "stuck")

        if proxy_ok():
            sent.pop("proxy-down", None)
        else:
            once("proxy-down", f"⚠️ 代理 {PROXY} 不通了，所有 claude 都连不上网。要你手动把代理重启起来。", "proxy")

        self.handle_hook_events(reg, once)
        self.roster_tick(reg, first_run)
        mark_sent_start()
        self.peer_tick(reg, first_run)
        cutoff = now() - 3 * 86400
        mon["sent"] = {k: v for k, v in sent.items() if v > cutoff or k == "proxy-down"}
        self.save()
        if now() - getattr(self, "_snap_at", 0) >= 10:     # 给飞书以外的客户端（手机 App）的统一快照 data/crew.json，见 cragents.py
            self._snap_at = now()
            try:
                import cragents
                cragents.write_snapshot(self, reg)
            except Exception:                               # 快照出错不能耽误监控
                pass

    def roster_tick(self, reg, first_run):
        """新员工入职（不管是机器人开的还是你在电脑上开的）：分一个工号，发一张「👋 入职」；看板内容变了就原地更新。"""
        staff = sorted(n for n, e in reg.items() if e.get("alive") and not is_assistant(n) and not is_quiet(n))
        roster = self.state.get("roster")
        hired = [] if roster is None else [n for n in staff if n not in roster]   # 第一次：现有的都算老员工，不发入职
        roster = [n for n in (roster if roster is not None else staff) + hired if n in reg]
        self.state["roster"] = roster
        new = sync_badges(reg)                           # 只有在上班的占号：下班的收回，新来的、回来的取最小空号
        for name in hired:
            e = reg[name]
            if first_run:
                continue
            st = session_status(e)
            n = new.get(name)
            card = alert(f"👋【{name}】入职了", f"派活：发「@{n} 内容」，或引用本卡片回复。" if n else "引用本卡片回复就能给它派活。", "turquoise",
                         [_button("回复它", f"对话 {name}", "primary"), _button("看屏幕", f"屏 {name}")])
            card.card["elements"][-1] = footer_els("", usage_tags(st))[-1]
            self.push(card, "hire")
        sig = self.board_sig()
        if sig != self.state.get("board_sig") and now() - self.state.get("board_at", 0) >= 10:
            stopped = any(r.get("t", 0) > self.state.get("board_at", 0) for r in self.state.get("bg", {}).values())   # 有人停下说了话：
            # 没建群（看板没置顶）时，被刷上去了才挪到最下面；只是谁开始 / 停止干活这种小变化，原地改就行
            self.state["board_sig"], self.state["board_at"] = sig, now()
            notify(self.status_card(minor=not stopped), "status")

    def peer_tick(self, reg, first_run):
        """员工之间互发的消息（Claude Code 自带的 SendMessage，同一台机器上的会话之间）：
        群里显示一行「💬 2号【A】→ 3号【B】」，老板看得见；两个人 10 分钟内来回太多就问你要不要叫停。"""
        pos = self.state.setdefault("peer_pos", {})
        by_peer = {e["peer"]: n for n, e in reg.items() if e.get("alive") and e.get("peer")}
        talk = [x for x in self.state.get("peer_log", []) if now() - x[0] < 3600]
        for name, e in reg.items():
            sid = e.get("sessionId")
            path = transcript_path(sid) if e.get("alive") and sid and not is_quiet(name) else None
            if not path:
                continue
            size = os.path.getsize(path)
            start = pos.get(sid)
            fresh = getattr(self, "_fresh", {})
            self._fresh = fresh
            if start is None and not first_run and size < 1 << 20:
                start, fresh[sid] = 0, now() - 300       # 开着的时候新出现的对话：从头看，但只认最近 5 分钟的（接回的旧对话不翻旧账）
            elif start is None or start > size:
                pos[sid] = size                          # 第一次看到它：从现在开始看，不翻旧账
                continue
            if size == start:
                continue
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(min(size - start, 8 << 20))
            end = chunk.rfind(b"\n") + 1                # 只看写完整的行
            pos[sid] = start + end
            said = []
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                content = (ev.get("message") or {}).get("content")
                if sid in fresh and _ts(ev.get("timestamp")) < fresh[sid]:
                    continue
                x = said_of(ev)
                if x:                                    # 有人跟它说话了：飞书 / cr / 你在电脑上亲手打的 / 同事…
                    said.append(x)
                goal = goal_event(ev)
                if goal and now() - goal["t"] < 600 and not first_run:
                    self.push(self.goal_card(name, e, goal), "idle")
                if ev.get("type") != "assistant" or ev.get("isSidechain") or not isinstance(content, list):
                    continue
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "SendMessage"):
                        continue
                    inp = b.get("input") or {}
                    to = by_peer.get(re.sub(r"\s*\[[^\]]*\]\s*$", "", str(inp.get("to") or "")).strip())
                    if not to or to == name:
                        continue                         # 发给它自己的子任务 / 队友，不是别的员工
                    msg = str(inp.get("message") or inp.get("content") or "")
                    talk.append([now(), name, to])
                    if not first_run:
                        title = f"💬 {badge(name)}【{name}】→ {badge(to)}【{to}】"
                        self.push(alert(title, msg or "（空消息）", "grey",
                                        [_button("看屏幕", f"屏 {to}"), _button(f"✍️ 回复{badge(name) or name}", f"对话 {name}")]), "peer")
            if said:
                self.heard_tick(name, e, said)
        warned = self.state.setdefault("peer_warn", {})
        for a, b in {tuple(sorted(x[1:])) for x in talk}:
            n = sum(1 for x in talk if tuple(sorted(x[1:])) == (a, b) and now() - x[0] < 600)
            key = f"{a}|{b}"
            if n >= 6 and now() - warned.get(key, 0) > 1800 and not first_run:
                warned[key] = now()
                self.push(alert(f"🙋 {badge(a)}【{a}】和 {badge(b)}【{b}】聊得有点多",
                             f"10 分钟里来回了 {n} 条。没问题就不用管；要叫停就点下面的按钮（相当于按 Esc 打断它手上的活），"
                             "之后再告诉它们怎么做。", "carmine",
                             [_button(f"叫停 {badge(a) or a}", f"#stop {a}", "danger"),
                              _button(f"叫停 {badge(b) or b}", f"#stop {b}", "danger")]), "peer", must=True)
        self.state["peer_log"] = talk
        self.state["peer_warn"] = {k: t for k, t in warned.items() if now() - t < 3600}
        self.state["peer_pos"] = {k: v for k, v in pos.items() if any(e.get("sessionId") == k for e in reg.values())}

    def goal_card(self, name, e, goal):
        """goal 自己停下了（检查超时、额度用完、一直没达成…）：告诉你为什么、怎么让它接着干。
        跟它 10 分钟内的汇报合成一张，手机不多响一次。"""
        if goal["what"] == "done":
            title, body = f"🎯【{name}】goal 完成了", "它认为 goal 已经达成，停下来了。"
        else:
            title = f"⏸【{name}】goal 暂停了"
            body = f"原因：{goal['why']}。" + ("回它一句话就会接着干。" if goal["resume"] else "")
        card = alert(title, "\n".join(x for x in (self.who(name, e), body) if x), "orange" if goal["what"] != "done" else "green",
                     [_button("✍️ 回复它", f"对话 {name}", "primary"), _button("看屏幕", f"屏 {name}")], state=self.state)
        card.merge = {"name": name, "label": "🎯 goal 完成" if goal["what"] == "done" else "⏸ goal 暂停", "body": body, "t": now()}
        return card

    def heard_tick(self, name, e, said):
        """它听到了几句话：标上来源记进 heard.jsonl（卡片、手机 App 读）；你在电脑上亲手打的算你醒着；
        看板上它那一行记下最新一句别处来的话（干活时显示起因）。
        你在电脑上叫它干活、它手上又没有飞书那张实时卡片：跟飞书发的一样推一张，做完原地换成结论。"""
        sid = e.get("sessionId")
        label_said(sid, said)
        note_heard(name, sid, said)
        for x in said:
            if x["kind"] in ("human", "slash") and (x["src"] == "terminal" or x["src"] == "unknown"
                                                    and x["t"] - self.state.get("typed_at", {}).get(name, 0) > 600):
                self.state["seen_you"] = max(self.state.get("seen_you", 0), x["t"])
        typed = next((x for x in said if x["src"] == "terminal" and x["kind"] == "human" and now() - x["t"] < 60), None)
        if typed and name not in self.state["remote_pending"] and (
                e.get("status") == "busy" or e.get("statusAt", 0) < typed["t"]):   # 刚打的字，状态还没来得及变成 busy 也算
            self.pend_since(name, typed["t"])
            card = self.live_card(name, e, typed["t"])
            card.pending_for = name                      # 守护进程发出去后登记成实时卡片，跟飞书发起的一样刷新
            notify(card, "live")
        show = [x for x in said if said_shown(x)]
        if show:
            x = show[-1]
            text = one_line(x["text"], 40) + (f"（带 {x['images']} 张图）" if x.get("images") else "")
            self.state.setdefault("said_last", {})[name] = {"t": x["t"], "line": f"{said_tag(x)}{' · 命令 ' if x['kind'] == 'slash' else '：'}{text}"}

    def handle_hook_events(self, reg, once):
        """Notification hook 写进来的事件：只管“等你批准权限”这类，作为状态文件之外的补充。"""
        pos = self.state.setdefault("events_pos", 0)
        try:
            with open(EVENTS, encoding="utf-8") as f:
                f.seek(pos)
                lines = f.readlines()
                self.state["events_pos"] = f.tell()
        except OSError:
            return
        by_sid = {e.get("sessionId"): n for n, e in reg.items() if e.get("alive")}
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            msg = ev.get("message", "")
            if ev.get("notification_type") != "permission_prompt" and "permission" not in msg.lower():
                continue
            name = by_sid.get(ev.get("session_id"))
            e = reg.get(name) or {}
            if not e.get("alive") or e.get("waiting"):
                continue                                # 状态写着在等你的，上面已经按「在问你 / 要你批准」发过卡片
            ask = self.ask_card(name, e)                # 老版本的 claude 不写等待状态，靠这个提醒（屏幕上得有选择框）
            if ask:
                once(f"ask:{ev.get('session_id')}:{ask.ask_id}", ask, "permission")
