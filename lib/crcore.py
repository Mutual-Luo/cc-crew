"""cc-crew 核心：发现本机的 Claude Code 会话、提取回复正文、遥控（tmux / ccm，见 crterm.py）、命令解析、监控。

状态来源是 Claude Code 自己维护的 ~/.claude/sessions/<pid>.json（sessionId / cwd / tmux / busy|idle），
回复正文来自对话记录 ~/.claude/projects/*/<sessionId>.jsonl，不靠识别屏幕。
"""
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import psutil

import crterm
from crterm import tmux, PRIVATE_SOCK

# 路径都可以用环境变量改（安装时写进 cc-crew 的 env 文件）；不设就用各系统的默认位置
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
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
ALIASES = f"{DATA}/aliases.json"                # claude 进程 pid -> 你给它起的名字
HELD = f"{DATA}/held.jsonl"                     # 夜里你没在用时攒下、没推送的卡片（点看板上的〔🌙 看攒下的〕再发）
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
HOLD_KINDS = ("idle", "stuck", "hire", "peer", "limit", "dead", "slash", "proxy")   # 夜里可以先攒着的通知（要你拍板的除外）
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
            "for this session only": "只用于这个会话", "adjust": "调整"}

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


def phone_md(text):
    """把 Claude 的 markdown 改成飞书卡片能显示的样子：标题变粗体，表格拆成一行一条，
    `代码` 去掉反引号换成「」，``` 代码块的围栏去掉（飞书卡片都不认）。"""
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
        line = re.sub(r"`([^`\n]+)`", r"「\1」", line)
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
    """把 Claude 写的 markdown 变成卡片元素：普通文字照 phone_md 转（标题变粗体、`代码` 变「」），
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
        line = re.sub(r"`([^`\n]+)`", r"「\1」", line)
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


def discover():
    """返回 {显示名: 会话信息}：本机所有交互式 claude。
    在 tmux / ccm 里跑的能遥控；直接在终端里打 claude 开的只能看（term == "none"），见 crterm.py。"""
    found = []
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        d = load_json(path, None)
        if not d or d.get("kind") != "interactive":
            continue
        pid = d.get("pid")
        if not pid or not _proc_alive(pid):
            continue
        t = crterm.attach_info(pid, d)
        if not t:
            continue
        title = re.sub(r"^\W+", "", t["title"]).strip()      # Claude Code 把对话标题写在终端标题里，前面是状态符号
        args = _proc_args(pid)[1:]
        found.append({
            "pid": pid, "sessionId": d.get("sessionId"), "cwd": d.get("cwd"),
            "status": d.get("status", "idle"), "statusAt": (d.get("statusUpdatedAt") or 0) / 1000,
            "startedAt": (d.get("startedAt") or 0) / 1000,
            "waiting": (d.get("waitingFor") or "?") if d.get("status") == "waiting" else "",   # input needed / permission prompt
            "peer": d.get("name") or "",                 # 同一台机器上的会话之间互相发消息时用的名字
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
        name = s["group"]
        if per_sess[name] > 1:
            name = f"{name}:{s['window']}"          # 同一个 tmux 里有好几个 claude：exp-a:0 / exp-a:1
        result[aliases.get(str(s["pid"]), name)] = s
    return result


def transcript_path(session_id):
    hits = glob.glob(f"{CLAUDE_DIR}/projects/*/{session_id}.jsonl")
    return hits[0] if hits else None


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
    return "🔧 " + name.split("__")[-1], ""


def turn_steps(session_id, since):
    """since 之后这一轮的过程，像 App 里那样一步一步列出来。
    返回 (你发的那句话, [(类型, 文字, 是否做完)], 是否被打断)；类型是 think / tool / text。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return "", [], False
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - (4 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    asked, steps, finished, stopped = "", [], set(), False
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if _ts(e.get("timestamp")) < since - 2:
            continue
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                finished.update(b.get("tool_use_id") for b in content if isinstance(b, dict))
                continue
            text = content if isinstance(content, str) else "".join(
                b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text")
            if "[Request interrupted by user" in text:
                stopped = True
            elif text.strip() and not e.get("isMeta") and not asked and not text.lstrip().startswith(
                    ("<command-name>", "<command-message>", "<local-command-")):   # /effort 这类本地命令的记录不是你说的话
                asked = text.strip()
        elif e.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                kind = b.get("type") if isinstance(b, dict) else None
                if kind == "thinking":
                    steps.append(("think", b.get("thinking", "").strip(), None))
                elif kind == "tool_use":
                    label, detail = describe_tool(b.get("name", ""), b.get("input") or {})
                    steps.append(("tool", (label, one_line(detail, 50)), b.get("id")))
                elif kind == "text" and b.get("text", "").strip():
                    steps.append(("text", b["text"].strip(), None))
    return asked, [(k, t, i is None or i in finished) for k, t, i in steps], stopped


BACKGROUND_ORIGINS = ("task_notification", "scheduled", "auto_continuation", "peer")   # 不是你叫醒的：后台任务、定时唤醒…
SUBSTANTIAL_CHARS = 200     # 后台醒来写了这么多字（不算空白）才单独发卡片，否则只更新〔后台进度〕
ASK_RE = re.compile(r"(?<!不)需要你(?!们)|你(来)?(定|决定|选|判断)一下|你(觉得|看)(哪个|选哪|怎么样)"
                    r"|要不要|确定吗|可以吗|行吗|好吗|选哪个|哪个更|你选哪|选哪(个|种)|哪(个|种)更"
                    r"|要我[^。！!\n]{0,40}[吗么]"                     # 「要我直接帮你改成上面的参数吗？改完你需要……」
                    r"|等你(的)?(决定|确认|拍板|一句|回复|回话|定|点头)"
                    r"|你(同意|确认|点头|拍板|说可以)[了后]?.{0,8}我(再|就)"   # 「你确认后我再动手」
                    r"|(贴给|发给|告诉)我(?!们)"                         # 「你把日志贴给我」「告诉我身高、体重」
                    r"|你.{0,15}(觉得|看).{0,25}的话[，,]?\s*我(就|再)"
                    r"|(两|三|四|几)(个|种)(方向|方案|选择|选项|办法).{0,6}(选|挑)"
                    r"|(?i:\bwhich (one|option)\b|\blet me know\b|\bdo you want\b|\bwould you like\b|\byour call\b"
                    r"|\bshall I\b|\bshould I\b|\bwant me to\b)")
YESNO_RE = re.compile(r"要我[^。！!\n]{0,40}[吗么]|要不要|可以吗|行吗|好吗|确定吗|同意吗|你(同意|确认|点头)[了后]?.{0,8}我(再|就)"
                      r"|(?i:\bshall I\b|\bshould I\b|\bwant me to\b|\bok to\b)")
INFO_RE = re.compile(r"(贴给|发给|告诉|提供给?)我(?!们)|(?<!不)需要你(提供|给|发|贴|上传|自己|手动|亲自)|你(自己|手动)(重启|操作|运行|执行|去)|(?i:\bsend me\b|\bpaste\b|\bshare (the|your)\b)")   # 要你给它东西：日志、截图、数字
OFFER_RE = re.compile(r"需要的话[，,]?\s*我(可以|就|来)|要的话[，,]?\s*我(就|可以|来)|如果你(需要|想要|愿意|想)[^。\n]{0,20}我(可以|就)"
                      r"|(?i:\bif you('d)? like,? I can\b)")
OPTION_LINE_RE = re.compile(r"^\s*(?:[-*•·]\s*)?(?:\*\*)?(?:方案|方向|选项)?\s*([A-F])\s*(?:\*\*)?\s*[.、．:：)）]\s*(?:\*\*)?\s*(.+)$")


def last_turn(session_id):
    """最后一轮：谁叫醒它的（你 / 后台）、它在这一轮里写了什么。
    返回 {"id": 这一轮开头那条记录, "by": "you" / "background", "texts": [它写的话], "tools": 做了几步}；读不到返回 None。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return None
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - (4 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
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
            turn = {"id": e.get("uuid") or e.get("timestamp"), "by": "background" if bg else "you", "texts": [], "tools": 0}
        elif e.get("type") == "assistant" and turn and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    turn["texts"].append(b["text"].strip())
                elif b.get("type") == "tool_use":
                    turn["tools"] += 1
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


def asks_you(text):
    """它最后这段话是不是在问你、等你拿主意（问句可能在中间：「要我改吗？改完你要重启一下」）。"""
    tail = (text or "").strip()[-400:]
    last = [r for r in tail.splitlines() if r.strip()][-1:]
    return bool(ASK_RE.search(tail) or (last and re.search(r"[？?][\s*）)]*$", last[0])))


def ask_kind(text):
    """它这一轮最后在干嘛：("options", 选项) 列了几个让你选；("yesno", []) 问你要不要 / 行不行；
    ("open", []) 别的问题（要你提供东西、开放问题）；("offer", []) 只是提议「需要的话我可以…」；(None, []) 不是在问你。"""
    if asks_you(text):
        opts = decision_options(text)
        ask = " ".join([r for r in (text or "").splitlines() if r.strip()][-2:])[-160:]
        if opts and opts[0][0].isdigit() and YESNO_RE.search(ask) and not CHOICE_CUE_RE.search(ask):
            opts = []                                    # 「…1. 2. 3.…要我改吗？」：数字是说明，问的是做不做
        if opts:
            return "options", opts
        if YESNO_RE.search(ask) or YESNO_RE.search((text or "")[-400:]) and not INFO_RE.search(ask):
            return "yesno", []
        return ("info" if INFO_RE.search(ask) else "open"), []
    if OFFER_RE.search((text or "").strip()[-300:]):
        return "offer", []
    return None, []


DECIDE_KINDS = ("options", "yesno", "open")   # 要你拍板的；「把日志贴给我」这类要你给东西的、「需要的话我可以…」都不算


def is_decision(text):
    """它是不是在等你拿主意（选哪个、做不做、怎么定）：只有这种才发「🙋 拍板」、记进待拍板、夜里照常推。"""
    return ask_kind(text)[0] in DECIDE_KINDS


NUM_OPTION_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\*\*)?(?:方案|方向|选项|第)?\s*([1-6一二三四五六])\s*(?:种|个)?\s*(?:\*\*)?\s*[.、．:：)）]\s*(?:\*\*)?\s*(.+)$")
CHOICE_CUE_RE = re.compile(r"选(哪|择|一)|你选|给你.{0,10}选|哪(个|种|一)|二选一|(?i:\bwhich\b)")   # 「要我按这个方案开始吗」不算


def decision_options(text):
    """它列出来让你选的 A / B / C…，或者 1 / 2 / 3、方案一 / 方案二：[(编号, 简短说明)]；不是从头连着的就当没有。
    数字编号只在它说了「选 / 哪个 / 方案 / 方向」时才算选项（普通的 1. 2. 3. 是汇报里的列表）；
    每一项本身都是问句的（「1. 范围：要不要……？」）是好几个问题，不是选项。"""
    def clean(t):
        t = re.sub(r"[*`“”\"]", "", t).strip()
        return re.split(r"[：:，,。；;（(]", t, 1)[0].strip() or t
    opts = {}
    for row in (text or "").splitlines():
        m = OPTION_LINE_RE.match(row)
        if m and m.group(1) not in opts:
            opts[m.group(1)] = clean(m.group(2))
    out = []
    for letter in "ABCDEF":
        if letter not in opts:
            break
        out.append((letter, opts[letter]))
    if len(out) >= 2:
        return out
    tail = (text or "")[-1500:]
    ask = " ".join([r for r in tail.splitlines() if r.strip()][-2:])[-160:]   # 只看最后的问话：「你选哪种？」「给你两个方向选」
    if not CHOICE_CUE_RE.search(ask) and not re.search(r"给你[^。！？!?\n]{0,10}选", tail[-600:]):
        return []
    nums, rows = {}, {}
    for row in tail.splitlines():
        m = NUM_OPTION_RE.match(row)
        if m:
            k = "一二三四五六".find(m.group(1)) + 1 if m.group(1) in "一二三四五六" else int(m.group(1))
            if k == 1:
                nums, rows = {}, {}                          # 只要最后一组从 1 开始的
            if k not in nums:
                nums[k], rows[k] = clean(m.group(2)), m.group(2)
    out = []
    for k in range(1, 7):
        if k not in nums:
            break
        out.append((str(k), nums[k]))
    if len(out) < 2 or sum(1 for k, _ in out if re.search(r"[？?]\s*$", rows[int(k)])) >= 2:
        return []
    return out


def is_human(e):
    """对话记录里这一条是不是人说的话（电脑上打的，或机器人替你发过去的）：
    工具结果、系统附带的说明、后台唤醒、本地命令、打断都不算。"""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isSidechain") or e.get("isCompactSummary"):
        return False
    content = (e.get("message") or {}).get("content")
    if e.get("turnOrigin") in BACKGROUND_ORIGINS or isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = content if isinstance(content, str) else "".join(
        b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text")
    return bool(text.strip()) and not text.lstrip().startswith(("<", "[Request interrupted"))


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


def user_spoke_since(session_id, t):
    """t 之后你有没有跟它说过话（电脑上打的，或飞书发的；后台唤醒不算）。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
        return False
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - (1 << 20)))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        try:
            ts = datetime.fromisoformat(e.get("timestamp", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if ts < t:
            return False
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "user" and not e.get("isMeta") and e.get("turnOrigin") not in BACKGROUND_ORIGINS and not (
                isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)):
            return True
    return False


def pending_tool(session_id):
    """它停在哪一步等你：(那次工具调用, 它在这之前写给你的话)；没有就是 None。选择题、要权限都靠它显示内容。"""
    path = transcript_path(session_id) if session_id else None
    if not path:
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
    对话记录要等你答完才写进去，所以先看 PermissionRequest hook 刚记下的；对话记录里对得上就用它（带着前面的话）。"""
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
        return []
    opts = []
    for r in tail[:ends[-1]]:
        m = OPTION_RE.match(r)
        if m and int(m.group(1)) == 1:
            opts = []                                # 只要最后一组从 1 开始的编号
        if m and int(m.group(1)) == len(opts) + 1:
            opts.append((len(opts) + 1, m.group(2).strip()))
    return opts


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
            return None
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


def slash_catalog(s):
    """这个会话能用的全部斜杠命令（内置的、技能、自定义命令）：[(命令, 说明)]。
    像在电脑上一样：在它的输入框里打一个「/」，把弹出的补全列表往下翻一遍，再删掉那个「/」。
    按目录缓存 12 小时。输入框里有字（你在电脑上打了一半）或正停在选择框上时不动它，返回缓存或 None。"""
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
        return "允许，并切到自动模式"
    if t.startswith(("yes, and", "yes, allow", "yes, don")):
        return "允许，以后同类不再问"
    if t.startswith("yes"):
        return "允许这一次"
    if t.startswith("no"):
        return "不允许"
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
            return {"tokens": tokens.group(1) if tokens else "", "thinking": bool(re.search(r"\bthinking\b", inside))}
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
    缺的再从屏幕底部的状态栏读。"""
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
    return "" if ctx is None else f"{'⚠️ ' if ctx >= CTX_WARN else ''}上下文 {ctx}%"


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
    """卡片上一行：上下文 + 额度。"""
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


def screen(s, lines=40):
    rows = pane_rows(s)
    rows = ["────" if r and set(r) <= set("─━-") else r for r in rows]
    rows = [r for i, r in enumerate(rows) if r or (i and rows[i - 1])]      # 连续空行只留一行
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[-lines:])


# ---------------------------------------------------------------- 遥控动作

def send_text(s, text, files=()):
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


def send_keys(s, tokens):
    for t in tokens:
        key = KEYMAP.get(t.lower(), KEYMAP.get(t))
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


def preflight(cwd):
    """启动前的检查，任何一项不过就不启动（返回原因）。"""
    if not os.path.isdir(cwd):
        return f"工作目录不存在：{cwd}"
    if not under_root(cwd):
        return f"工作目录不在 {FILES_ROOT} 下：{cwd}"
    if not shutil.which("claude"):
        return "找不到 claude 命令"
    if PROXY and not proxy_ok(max_age=0):              # 启动前一定现查
        return f"代理 {PROXY} 不通，不启动（不经代理直接连很危险）"
    return ""


def launch(name, cwd, args):
    """开一个新的交互式 claude：服务器上开在私有 tmux 里，Windows / macOS 在桌面上弹一个终端窗口（用 ccm 跑）。"""
    env = launch_env()
    if LAUNCH:                                           # 自己的安全启动脚本（这台服务器：claude-launch.sh）
        r = subprocess.run([LAUNCH, name, cwd] + args, capture_output=True, text=True, timeout=60, env=env)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    why = preflight(cwd)
    if why:
        return False, "启动中止：" + why
    if crterm.launch_mode() == "tmux":
        return crterm.start_tmux(name, cwd, [shutil.which("claude")] + args, env)
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
            "**直接打字**：交给秘书（助手），派活、上下班、问进展都跟它说\n"
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
        self.more = []               # 正文太长时，接着发的「续」卡片
        self.slot = None             # 固定的一张卡片（〔📋 员工看板〕）：原地更新，被刷上去了才挪到最下面
        self.minor = False           # 固定卡片只是状态变了（谁开始干活了）：只原地改，不挪位置
        self.summon = False          # 你发「看看」叫出来的：挪到最下面
        self.merge = None            # 汇报 / 进展：10 分钟内同一个员工的下一张并进这张里。{"name", "label", "body", "t"}

    def __str__(self):
        return self.text


def _button(label, cmd, kind="default"):
    return {"tag": "button", "text": {"tag": "plain_text", "content": label},
            "type": kind, "value": {"cmd": cmd}}


def _note(text):
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _md(text):
    return {"tag": "div", "text": {"tag": "lark_md", "content": text}}


def talking_to(state=None, about=None):
    """卡片底部一行：你现在随手打的字发给谁；卡片说的是别的会话时，顺便告诉你怎么回它。"""
    state = load_json(STATE, {}) if state is None else state
    cur, default = state.get("current"), state.get("default")
    if cur:
        line = f"✍️ 你现在打的字发给【{cur}】，点〔↩ 回到{default or '默认对象'}〕或发「回到助手」"
    elif default:
        line = f"⭐ 你现在打的字发给【{default}】"
    else:
        line = "你现在打的字不会发给任何 Claude"
    if about and about != (cur or default):
        line += f"\n要回【{about}】：引用这张卡片回复，或点〔✍️ 回复它〕"
    return line


def badge(name):
    """「2号」；助手（秘书）和没分到工号的返回空。"""
    n = load_json(BADGES, {}).get(name)
    return f"{n}号" if n else ""


def _card(title, color, elements, footer=None):
    """统一卡片样式：彩色标题 + 内容 + 底部一行「你现在打的字发给谁」和时间。标题里的员工自动带上工号。"""
    about = re.search(r"【([^】]+)】", title)
    if about and badge(about.group(1)) and not title[:about.start()].endswith("号"):
        title = title[:about.start()] + badge(about.group(1)) + title[about.start():]
    foot = footer or f"{talking_to(about=about and about.group(1))} · {time.strftime('%H:%M')}"
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "elements": elements + [_note(foot)]}


def continued(card, title, color, chunks):
    """正文太长：第一块留在这张卡片里，其余做成「续」卡片挂在 card.more，一个字都不截。"""
    n = len(chunks)
    card.more = [Card(_card(f"{title}（续 {i}/{n}）", color, c if isinstance(c, list) else [_md(c)]), f"{title}（续 {i}/{n}）")
                 for i, c in enumerate(chunks[1:], 2)]
    return card


def alert(title, body, color, buttons=(), footer=None):
    """带按钮的提醒卡片；命令行里显示成「标题 + 正文」。"""
    chunks = rich(body) if body else []
    els = list(chunks[0]) if chunks else []
    if buttons:
        els.append({"tag": "action", "actions": list(buttons)})
    card = Card(_card(title, color, els, footer=footer), f"{title}\n{body}" if body else title)
    return continued(card, title, color, chunks)


def merged_card(entries, latest):
    """同一个员工接连几条汇报合成一张：前面几条在上（放不下的只留第一句），最新一条完整放在最下面，按钮跟着最新的。
    entries 是前面几条的 Card.merge，latest 是最新的那张 Card。"""
    stamp = lambda e: f"**{time.strftime('%H:%M', time.localtime(e['t']))}　{e['label']}**"
    room = CARD_CHARS - len(latest.text)
    blocks = []
    for e in reversed(entries):                         # 越新的越优先放全文
        chunks = rich(e["body"])
        if len(e["body"]) <= room and len(chunks) == 1:
            room -= len(e["body"])
            blocks.append([_md(stamp(e))] + chunks[0])
        else:
            blocks.append([_md(f"{stamp(e)}　<font color='grey'>{one_line(first_sentence(e['body']), 60)}</font>")])
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
HELP = """🤖 我是监控机器人，帮你管这台机器上的 Claude 员工。
**怎么使唤**
· 直接打字：交给秘书（助手）。派活、谁上班下班、问进展、让员工之间传话，都跟它说，它会去办
· 「@3 内容」或引用某个员工的卡片回复：直接对那个员工说（最快）；「@所有人 内容」：开会
  点了好几个人（「@1 2 6 下班」）或只说「@3 下班」这类动作，还是交给秘书去办
· 点卡片上的按钮：〔✍️ 回复它〕〔🌙 下班〕〔☀️ 上班〕〔看屏幕〕、A/B/C 选项……点了马上执行，不经过秘书
**我自己认的几个词**（整句一模一样才算，其他话都交给秘书）
· 看看：员工看板叫到最底下
· 列表：详细的员工列表（工号、目录、上下文）
· 新对话 / 对话记录：跟秘书开新对话 / 切回以前的对话
· 思考程度：改秘书的思考程度
· 回到助手：你在跟某个员工单聊时，回到秘书
**卡片上的东西**
· 「3号」是员工工号；🙋 老板，需要你拍板；📝 汇报；📣 有新进展；👋 入职了
· 💬 2号 → 3号：员工之间在互发消息；📨 同事消息要你批准才送达
· 「上下文 85%」「5 小时额度」：用量；〔🗜 压缩上下文〕快满时出现
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
        """现在是夜里、你这半小时也没动过：不用你拍板的先攒着不推。"""
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
        """发一条通知。夜里你没在用时，不用你拍板的（汇报、进展、退出、卡住…）先攒着，看板上显示攒了几条；
        要你拍板、要你批准的照常推。"""
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
        head = alert(f"🌙 你不在时攒下的 {len(items)} 条", f"{first} 以来没推送的消息，按时间顺序在下面（要你拍板的当时已经推了）。", "grey")
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
        return live, reg

    def resolve(self, word, reg, want_alive=True):
        m = re.fullmatch(r"(\d+)号?", word or "")
        if m:                                            # 工号：@2 / 2号
            word = {n: k for k, n in load_json(BADGES, {}).items()}.get(int(m.group(1)), word)
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

    def handle(self, text, typed=False, to=None):
        """处理一条命令或一句话。typed=True 是你在手机上手打的：除了 TYPED_WORDS 这几个词，机器人不猜你的意思，
        一律交给助手（或你正在对话的员工）去理解；to 是你引用了哪个员工的卡片，就直接对它说。
        命令行（助手用的 cr）和卡片按钮走下面的命令。"""
        text = text.strip()
        if not text:
            return "（空消息）"
        self.state = load_json(STATE, self.state)
        live, reg = self.refresh()
        head, _, rest = text.partition(" ")
        rest = rest.strip()

        chat = self.chat_target()
        if typed and to:
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
                return f"【{name}】没能上班：\n{why}"
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
                return "用法：新建 名字 目录 [claude 参数…]"
            ok, out = launch(parts[0], parts[1], parts[2:] or DEFAULT_NEW_ARGS)
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
            return self.deliver(target, body.strip(), reg)
        return self.say(text, chat, reg)

    def direct_at(self, text, reg):
        """手打的「@3 内容」：只点了一个人、对得上、内容是正常的话，就直接发给它（不经过秘书，最快）。
        点了好几个人（「@1 2 6 下班」）、只说了「下班 / 上班」这类动作、对不上是谁，都返回 None 交给秘书去理解。"""
        m = re.match(r"@\s*(\d+号?)(?![\d号])[\s，,:：、]*(.+)", text, re.S) or \
            re.match(r"@\s*([^\s，,:：、]+(?::\d+)?)[\s，,:：、]+(.+)", text, re.S)     # 工号后面不加空格也认（「@3帮我看看」）；名字可以带「:0」
        if not m:
            return None
        who, body = m.group(1), m.group(2).strip()
        if re.match(r"@|\d+号?(?:[\s，,、]|$)", body) or body in ACTION_WORDS:
            return None
        if who in ("所有人", "全体", "all"):
            return self.broadcast(body, reg)
        default = self.state.get("default")
        if who in ("助手", "秘书") or (default and who == default):
            return None                                  # 本来就交给秘书
        n = re.fullmatch(r"(\d+)号?", who)
        if n and int(n.group(1)) not in load_json(BADGES, {}).values():
            return None                                  # 没有这个工号：别拿数字去模糊匹配名字
        name, _, err = self.resolve(who, reg, want_alive=False)
        if err or not name or name.startswith("助手"):
            return None
        return self.deliver(name, body, reg)

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

    def deliver(self, target, body, reg):
        """把你的话（连同你先发来的图片 / 文件）发给它，回一张实时进度卡片。"""
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
                return f"【{off}】下班了，想叫它上班但没成功：\n{why}"
            name = off
        m = re.fullmatch(r"/effort(?:\s+(\w+))?", body.strip())
        if m and not files:                               # 「@4 /effort」：不让终端弹那个只能左右拨的滑块，给一张选档位的卡片
            level = (m.group(1) or "").lower()
            level = {cn: en for en, cn in EFFORTS}.get(level, level)    # 「/effort 很高」也认
            return self.button_cmd(["effort", name, level], reg) if level in EFFORT_CN else self.effort_card(name, s)
        rows = pane_rows(s)
        if s.get("waiting") and not dialog_options(rows):     # 停在一个不是问题的框上：打的字会被框当成按键吃掉，不能发
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
            self.state.get("asking", {}).pop(name, None)
        else:
            send_text(s, body, files)
            self.state.get("asking", {}).pop(name, None)     # 你回它了：不再算「待拍板」
            if files:
                self.state.pop("attach", None)
                if not body:
                    self.state.setdefault("asked_note", {})[name] = [sent_at, f"（发了 {len(files)} 个图片 / 文件）"]
        self.state["remote_pending"][name] = sent_at
        self.state.setdefault("typed_at", {})[name] = sent_at   # 机器人替人打的字：不算你在电脑前
        self.save()
        card = self.live_card(name, s, sent_at)
        card.pending_for = name
        return card

    # ------------------------------------------------------------ 斜杠命令：像在电脑上打「/」

    def run_slash(self, name, s, body):
        """发斜杠命令。只打了「/」或命令名的一部分：像电脑上一样列出候选命令，不直接执行。
        发出后回一张屏幕卡片（弹出菜单的用遥控键盘操作）；要跑一会儿的（/compact）做完再通知你。"""
        word = body.split()[0]
        if body == word:
            cat = slash_catalog(s) or []
            if word not in dict(cat or [(c, d) for c, _, d in SLASH]):
                return self.cmds_card(name, s, query=word, cat=cat)   # 只打了「/」或半截：不能回车，回车会执行列表里第一个
        was_busy = s["status"] == "busy"
        sent_at = now()
        send_text(s, body)
        time.sleep(2.5)
        if not was_busy and (discover().get(name) or s)["status"] == "busy":
            self.state.setdefault("slash", {})[name] = [sent_at, word]   # 比如 /compact：做完发「✅ 做完了」
            self.save()
        return self.screen_card(name, s, note=f"➡️ 已把 {body} 发给【{name}】，下面是它现在的屏幕：")

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
            self.state["remote_pending"][name] = now()
            sent.append(name)
        self.save()
        who = lambda names: "、".join(f"{badge(n)}{n}" for n in names)
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
        cat = cat or [(c, d) for c, _, d in SLASH]          # 列不出全部时，只在常用的里面找
        q = query.lstrip("/").lower()
        bar = session_status(s)
        head = f"<font color='grey'>{usage_text(bar)}</font>\n" if usage_text(bar) else ""
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
            lines = [f"**{c}** {label}　<font color='grey'>{desc}</font>" for c, label, desc in SLASH]
            els.append(_md(head + "**常用**\n" + "\n".join(lines)))
            buttons = [_button(label, f"#slash {name} {c}") for c, label, _ in SLASH]
            if full:
                others = [c for c, _ in cat if c not in {x[0] for x in SLASH}]
                els += [{"tag": "hr"}, _md(f"**全部 {len(cat)} 个**（含技能、自定义命令）\n<font color='grey'>{'　'.join(others)}</font>")]
            else:
                els.append(_md("<font color='grey'>它的输入框现在有字，或正停在选择框上，暂时列不出全部命令。</font>"))
        els += [{"tag": "action", "actions": buttons[i:i + 4]} for i in range(0, len(buttons), 4)]
        els.append(_md("<font color='grey'>发「/关键词」搜索，比如 /co；带内容的命令直接发，比如「/goal 把论文改完」。"
                       "点了会弹菜单的（/model、/resume），屏幕卡片上用遥控键盘选。</font>"))
        return Card(_card(title, "blue", els), f"{title}\n" + "\n".join(c for c, _ in cat))

    # ------------------------------------------------------------ 图片 / 文件：像在电脑上贴截图、点开图片

    def held_files(self):
        """你先发来、还没发出去的图片 / 文件（30 分钟内有效）。"""
        held = self.state.get("attach")
        if held and now() - held.get("at", 0) > 30 * 60:
            self.state.pop("attach", None)
            return None
        return held

    def handle_files(self, paths, text, name=None):
        """你从飞书发来图片 / 文件：像在电脑上先贴截图再打字，附在你下一句话前面一起发。
        图文一起发的（带了文字）马上发出去。"""
        self.state = load_json(STATE, self.state)
        held = self.held_files() or {"paths": []}
        held.update(paths=held["paths"] + paths, at=now(), **{"for": name or held.get("for")})
        self.state["attach"] = held
        self.save()
        if text.strip():
            return self.handle(text, typed=True, to=name)
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
        text = re.sub(r"(?m)^(?:[⛀-⛿]\s?)+\s*", "", screen(s, 30))       # /context 的方格图手机上没用，只留文字
        text = "\n".join(r for r in text.split("\n") if r.strip())

        def key(label, k, kind="default"):
            return _button(label, f"#key {name} {sig} {k}", kind)

        els = ([_md(note)] if note else []) + [{"tag": "div", "text": {"tag": "plain_text", "content": text or "（空白）"}}]
        if menu:
            items = [key(("❯ " if n == menu["cursor"] else "") + f"{n}. {one_line(label, 14)}", f"item:{n}",
                         "primary" if n == menu["cursor"] else "default") for n, label in menu["items"][:24]]
            pick_key, pick_desc = pick_action(menu)
            pad = ([] if items else [key("↑", "Up"), key("↓", "Down")]) + [key(label, k) for k, label in menu["keys"]
                                                                        if not (items and k in ("Up", "Down"))]
            if not any(k == "Escape" for k, _ in menu["keys"]):
                pad.append(key("Esc：取消", "Escape"))
            hint = (f"**点一下选项就{pick_desc}**。" + ("下面的键是电脑上那几个，一般用不到。" if pad else "")) if items \
                else "用下面的键操作，屏幕会跟着刷新。"
            els.append({"tag": "hr"})
            els.append(_md(f"<font color='grey'>{hint}</font>"))
            els += [{"tag": "action", "actions": items[i:i + 4]} for i in range(0, len(items), 4)]
            els += [{"tag": "action", "actions": pad[i:i + 4]} for i in range(0, len(pad), 4)]
        tail = [key("🔄 刷新", "refresh")]
        if s.get("status") == "busy" and not menu:
            tail.append(_button("⏹ 停止", f"#stop {name}", "danger"))
        if self.state.get("current") != name:
            tail.append(_button("✍️ 回复它", f"对话 {name}"))
        els.append({"tag": "action", "actions": tail})
        return Card(_card(f"🖥【{name}】屏幕 · {time.strftime('%H:%M:%S')}", "grey", els),
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
        card = self.screen_card(name, s, note=note)
        card.in_place = True
        return card

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
        if op == "fire":
            return self.cmd_fire(name, s, reg) if words[2:3] == ["yes"] else self.fire_card(name, s)
        if op == "stop":
            send_keys(s, ["esc"])                       # 卡片会自己变成「⏹ 已停止」，不用再发消息
            return None
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
        if op == "pick" and len(words) > 4:
            return self.pick(name, s, words[2], words[3], words[4])
        if op == "key" and len(words) > 3:
            return self.press(name, s, words[2], words[3])
        if op == "cmds":
            return self.cmds_card(name, s)
        if op == "slash" and len(words) > 2 and re.fullmatch(r"/[\w:.\-]+", words[2]):
            return self.run_slash(name, s, words[2])
        return None

    # ------------------------------------------------------------ 它停下来问你：选择题、要权限

    def ask_card(self, name, s, qi=0, pt=None, note=""):
        """它在问你（选择题）或要你批准（权限）：题目、选项、它前面说的话都发过来，一个选项一个按钮。
        找不到可显示的内容（屏幕上也没有选择框）就返回 None。"""
        rows = pane_rows(s)
        opts = dialog_options(rows)
        if not opts and not s.get("waiting"):
            return None
        pt = waiting_on(s, pt)
        b, said = pt if pt else ({}, [])
        if not opts and not pt:
            return None
        tid, inp = b.get("id") or "-", b.get("input") or {}
        who = self.who(name, s)
        context = "\n\n".join(said)
        if not context and above_dialog(rows):
            context = f"<font color='grey'>它前面说的（屏幕上能看到的部分）：</font>\n{above_dialog(rows)}"
        lines, buttons = [], []
        if b.get("name") == "AskUserQuestion":
            title, color = f"❓【{name}】在问你", "orange"
            qs = inp.get("questions") or []
            qi = min(int(qi), max(len(qs) - 1, 0))
            for i, q in enumerate(qs):
                mark = "✅ " if i < qi else ("👉 " if len(qs) > 1 and i == qi else "")
                num = f"第 {i + 1}/{len(qs)} 题：" if len(qs) > 1 else ""
                lines.append(f"{mark}**{num}{q.get('question', '')}**" + ("（多选）" if q.get("multiSelect") else ""))
                for k, o in enumerate(q.get("options") or [], 1):
                    desc = f"\n<font color='grey'>　　{o['description']}</font>" if o.get("description") else ""
                    lines.append(f"{k}. {o.get('label', '')}{desc}")
                lines.append("")
            q = qs[qi] if qs else {}
            if q and not q.get("multiSelect"):
                buttons = [_button(f"{k}. {one_line(o.get('label', '').replace('(Recommended)', '（推荐）'), 16)}",
                                   f"#pick {name} {tid} {qi} {k}") for k, o in enumerate(q.get("options") or [], 1)]
                hint = "点按钮就是选它。都不合适？直接打字回答：引用这张卡片回复，或点〔✍️ 回复它〕再打字。"
            else:
                hint = "这是多选题：直接打字回答（比如「1 和 3」）：引用这张卡片回复，或点〔✍️ 回复它〕再打字。"
            text_opts = "\n".join(lines)
        else:
            title, color = f"🔐【{name}】要你批准", "yellow"
            if b:
                label, desc = describe_tool(b.get("name", ""), inp)
                lines.append(f"它想：**{label}**" + (f"　{desc}" if desc and b.get("name") != "Bash" else ""))
                lines.append(tool_detail(b.get("name", ""), inp))
            else:
                lines.append(f"它的屏幕：\n{screen(s, 25)}")
            lines.append("")
            for n, text in opts:
                lines.append(f"{n}. {perm_label(text)}　<font color='grey'>{text}</font>")
                kind = "danger" if perm_label(text) == "不允许" else ("primary" if n == 1 else "default")
                buttons.append(_button(perm_label(text), f"#pick {name} {tid} - {n}", kind))
            if not opts:                              # 屏幕上暂时没认出选项：最常见的两个
                buttons = [_button("允许这一次", f"#pick {name} {tid} - 1", "primary"),
                           _button("不允许", f"#pick {name} {tid} - esc", "danger")]
            hint = "点按钮就是选它；选「不允许」后它会停下来，你再告诉它改做什么。"
            text_opts = "\n".join(lines)
        extra = [_button("看屏幕", f"屏 {name}")]
        if self.state.get("current") != name:
            extra.insert(0, _button("✍️ 回复它", f"对话 {name}"))
        head = "\n".join(x for x in (note, who) if x)
        body = "\n\n".join(x for x in (context, text_opts) if x)
        els = ([_md(head)] if head else []) + [el for chunk in rich(body) for el in chunk]
        els += [{"tag": "hr"}, _md(f"<font color='grey'>{hint}</font>")]
        els += [{"tag": "action", "actions": buttons[i:i + 4]} for i in range(0, len(buttons), 4)]
        els.append({"tag": "action", "actions": extra})
        card = Card(_card(title, color, els), f"{title}\n{body}")
        card.ask_id = tid
        return card

    def pick(self, name, s, tid, qi, n):
        """卡片上点了某个选项：先确认它还停在这一步（没在电脑上答过），再按对应的数字键。"""
        rows = pane_rows(s)
        opts = dialog_options(rows)
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        if not opts or (tid != "-" and tid not in (b.get("id"), b.get("alt_id"))):
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
            picked = f"选了「{q['options'][int(n) - 1]['label']}」"
            send_keys(s, [n])
            time.sleep(0.8)
            if qi + 1 < len(qs):
                card = self.ask_card(name, s, qi + 1, pt=pt, note=f"✅ 第 {qi + 1} 题{picked}")   # 下一题，原地换
                card.in_place = True
                return card
            if len(qs) > 1 and any("Submit answers" in r for r in pane_rows(s)):
                send_keys(s, ["1"])                     # 多道题都答完了：提交
        elif n == "esc":
            picked = "选了「不允许」"
            send_keys(s, ["esc"])
        else:
            text = dict(opts).get(int(n))
            if not text:
                return f"屏幕上没有第 {n} 个选项了，没有按键。点〔看屏幕〕看看。"
            picked = f"选了「{perm_label(text)}」"
            send_keys(s, [n])
        since = now()                                   # 接着像发消息一样：这张卡片变成实时进度，做完变成回复
        self.state["remote_pending"][name] = since
        self.state.setdefault("asked_note", {})[name] = [since, picked]
        self.save()
        card = self.live_card(name, s, since)
        card.pending_for, card.in_place = name, True
        return card

    def answer_with_text(self, name, s, text):
        """它停在选择题上时你打了字：选「Type something」把你的话填进去。
        填好了返回 None（照常显示进度）；要你点按钮（权限框）或还有下一题时返回要发给你的卡片。"""
        rows = pane_rows(s)
        pt = waiting_on(s)
        b = pt[0] if pt else {}
        qs = ((b.get("input") or {}).get("questions") or []) if b.get("name") == "AskUserQuestion" else []
        other = next((n for n, label in dialog_options(rows) if label.startswith("Type something")), None)
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
        """〔🧠 思考：很高〕：按钮上直接写当前思考程度，点开就能改。"""
        effort = session_status(s)["effort"] if s.get("alive", True) else ""
        return _button(f"🧠 思考：{EFFORT_CN.get(effort, '?')}", f"#effort {name}")

    def effort_card(self, name, s, done=None):
        st = session_status(s)
        model, cur = st["model"], st["effort"]
        cur = done or cur
        buttons = [_button(("✓ " if lv == cur else "") + cn, f"#effort {name} {lv}", "primary" if lv == cur else "default")
                   for lv, cn in EFFORTS]
        head = f"✅ 已改成「{EFFORT_CN[done]}」，从它的下一步开始生效。" if done else f"现在：**{EFFORT_CN.get(cur, cur or '未知')}**"
        body = (f"{head}\n<font color='grey'>越高想得越久、越仔细；越低回得越快。随手聊天用「中」就够，难题再调高。"
                f"只改【{name}】这一个会话，不影响其他会话。</font>")
        els = [_md(body), {"tag": "action", "actions": buttons}]
        return Card(_card(f"🧠【{name}】思考程度", "purple", els, footer=f"{model or 'Claude'} · {time.strftime('%H:%M')}"),
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
        return Card(_card(title, "purple", els, footer=f"{talking_to(self.state, name)} · {time.strftime('%H:%M')}"),
                    f"{title}\n{body}")

    def _step_lines(self, steps, limit, full_last=False):
        lines = [] if len(steps) <= limit else [f"<font color='grey'>…前面还有 {len(steps) - limit} 步</font>"]
        for i, (kind, text, done) in enumerate(steps[-limit:]):
            if kind == "text" and full_last and i == len(steps[-limit:]) - 1:
                body = phone_md(text)                    # 它最新写的那段话整段显示，像在电脑前看着它写
                lines.append("💬 " + (body if len(body) <= 2000 else body[:2000] + "……"))
                continue
            if kind == "think":
                lines.append("🧠 想了想" + (f"：<font color='grey'>{one_line(text, 140)}</font>" if text else ""))
            elif kind == "tool":
                label, detail = text
                detail = f" <font color='grey'>{detail}</font>" if detail else ""
                lines.append(f"{label}{detail}" if done else f"**{label}**{detail} ⏳")
            else:
                lines.append(f"💬 {one_line(text, 160)}")
        return lines

    def live_card(self, name, s, since):
        """“思考中”卡片：你问了什么、它想了什么、做到哪一步、用了多久；带〔⏹ 停止〕〔🧠 思考程度〕。"""
        asked, steps, _ = turn_steps(s.get("sessionId"), since)
        note = self.state.get("asked_note", {}).get(name)
        if not asked and note and note[0] == since:
            asked = note[1]                             # 你是点选项 / 打字回答它的问题，不是发了新消息
        rows = pane_rows(s)
        st = session_status(s)
        spin, model, effort = spinner_info(rows), st["model"], st["effort"]
        running = steps and steps[-1][0] == "tool" and not steps[-1][2]
        if running:
            doing = "正在执行"
        elif spin.get("thinking") or not steps:
            doing = "正在思考" if steps or s["status"] == "busy" else "收到，准备开始"
        else:
            doing = "正在写回复" if steps[-1][0] == "text" else "正在思考"
        elapsed = fmt_secs(now() - since)
        els = [_md(self.who(name, s))] if self.who(name, s) else []
        if asked:
            els.append(_md(f"<font color='grey'>你 → 【{name}】：{one_line(asked, 80)}</font>"))
        lines = self._step_lines(steps, 6, full_last=True)
        if lines:
            els += [{"tag": "hr"}, _md("\n".join(lines))]
        stat = f"⏳ **{doing}…**　已 {elapsed}" + (f" · 已生成 {spin['tokens']} tokens" if spin.get("tokens") else "")
        buttons = [_button("⏹ 停止", f"#stop {name}", "danger"),
                   _button(f"🧠 思考：{EFFORT_CN.get(effort, '?')}", f"#effort {name}"), _button("看屏幕", f"屏 {name}")]
        if self.state.get("current") == name:           # 它还在想，你也能随时回到助手；它会接着干完
            buttons.insert(0, _button(f"↩ 回到{self.state.get('default') or '默认对象'}", "退出", "primary"))
        els += [{"tag": "hr"}, _md(stat), {"tag": "action", "actions": buttons}]
        ctx = usage_text(st)
        foot = f"{talking_to(self.state, name)}\n{model or 'Claude'}{' · ' + ctx if ctx else ''} · 自动刷新 {time.strftime('%H:%M:%S')}"
        return Card(_card(f"⏳【{name}】{doing} · {elapsed}", "wathet", els, footer=foot), f"⏳【{name}】{doing}… 已 {elapsed}")

    def reply_card(self, name, s, since):
        """做完了：原地换成回复，顶上一行灰字「用时、想了几次、做了几步」，〔🔎 看过程〕可以展开。"""
        _, steps, stopped = turn_steps(s.get("sessionId"), since)
        bar = session_status(s)
        effort = bar.get("effort", "")
        texts = [t for k, t, _ in steps if k == "text"]
        reply = turn_answer(texts)                     # 只要这一轮的回答；没有就说没有，不拿上一轮的充数
        if texts:
            self.note_latest(name, s["statusAt"], texts[-1])
        thinks = sum(1 for k, _, _ in steps if k == "think")
        tools = sum(1 for k, _, _ in steps if k == "tool")
        took = fmt_secs(max(s["statusAt"], since) - since)
        meta = f"✓ 用时 {took}" + (f" · 想了 {thinks} 次" if thinks else "") + (f" · 做了 {tools} 步" if tools else "")
        if usage_text(bar):
            meta += " · " + usage_text(bar)
        if stopped:
            title, color, body = f"⏹【{name}】已停止", "grey", reply or "（停下时还没写回复）"
            meta = f"⏹ 已按你的要求停下（跑了 {took}）"
        else:
            title, color, body = f"💬【{name}】", "green", reply or "（没有文字回复）"
            if texts and is_decision(texts[-1]) and not is_assistant(name):
                title, color = f"🙋【{name}】：老板，需要你拍板", "carmine"
                self.mark_asking(name, texts[-1])
        options = self.option_rows(name, texts[-1]) if texts and not stopped else []
        buttons = self.chat_buttons(name)
        buttons = [b for b in buttons if not (is_assistant(name) and b["value"]["cmd"] == f"屏 {name}")]
        extra = [_button(f"🧠 思考：{EFFORT_CN.get(effort, '?')}", f"#effort {name}"), _button("⌨️ 命令", f"#cmds {name}")]
        if (bar.get("ctx") or 0) >= CTX_WARN:
            extra.insert(0, self.compact_button(name))
        if steps:
            extra.insert(0, _button("🔎 看过程", f"#steps {name} {since:.0f}"))
        buttons = buttons[:2] + extra + buttons[2:]
        foot = talking_to(self.state, name)
        if is_assistant(name):
            foot += f"\n当前对话「{first_user_text(s.get('sessionId')) or name}」"
        foot += f" · {time.strftime('%H:%M')}"
        who = self.who(name, s)
        chunks = rich(body)
        files = mentioned_files(body, s.get("cwd"))
        els = ([_md((who + "\n" if who else "") + f"<font color='grey'>{meta}</font>")] + chunks[0]
               + ([{"tag": "action", "actions": file_buttons(files)}] if files else []) + options
               + [{"tag": "action", "actions": buttons}])
        card = Card(_card(title, color, els, footer=foot), f"{title}\n{body}")
        card.reply_for = name
        return continued(card, title, color, chunks)

    def mark_asking(self, name, text):
        """它在问你：记进「待拍板」，看板顶上显示；你回了它（或在电脑上跟它说了话）就消掉。"""
        asking = self.state.setdefault("asking", {})
        rows = [r.strip() for r in (text or "").splitlines() if r.strip()]
        q = one_line(re.sub(r"[*`#>]", "", next((r for r in reversed(rows) if re.search(r"[？?]|吗|要不要|你(定|选|确认)", r)),
                                                    rows[-1] if rows else "")), 40)
        if asking.get(name, {}).get("q") != q:           # 新的问题换掉旧的；同一个问题（卡片刷新）不重记时间
            asking[name] = {"t": now(), "q": q}
            self.save()

    def option_rows(self, name, final):
        """它在问你时，一点就能回的按钮（回它的话跟你自己打的一样，直接发给它）：
        列了选项 → 每个选项一个〔A. …〕+〔👍 按你推荐的〕；问做不做 → 〔✅ 好，做吧〕〔✋ 先别〕；
        别的问题 → 〔👍 你来定〕；只是提议「需要的话我可以…」→〔👍 好，做吧〕。想多说几句还是点〔✍️ 回复它〕。"""
        kind, opts = ask_kind(final)
        say = lambda label, text, style="default": _button(label, f"@{name} {text}", style)
        if kind == "options":
            buttons = [say(f"{k}. {one_line(t, 16)}", f"我选 {k}", "primary") for k, t in opts]
            buttons.append(say("👍 按你推荐的", "按你推荐的做。"))
        elif kind == "yesno":
            buttons = [say("✅ 好，做吧", "好，就按你说的做。", "primary"), say("✋ 先别", "先别做，等我想想。")]
        elif kind == "open":
            buttons = [say("👍 你来定（按你推荐的）", "你来定，按你推荐的方案做。", "primary")]
        elif kind == "info":
            return []                                    # 要你给它日志、数字之类：只能你自己回（〔✍️ 回复它〕）
        elif kind == "offer":
            buttons = [say("👍 好，做吧", "好，做吧。")]
        else:
            return []
        return [{"tag": "action", "actions": buttons[i:i + 2]} for i in range(0, len(buttons), 2)]

    def stop_card(self, name, e, turn):
        """它停下了、而且这一轮有要给你看的：「⏸ 停下来了」；在问你、等你拿主意时是醒目的「🙋 需要你决定」。
        正文是这一轮它写的全部的话（不是上一轮的）。"""
        body = turn_answer(turn["texts"]) or f"（这一轮它没写话，做了 {turn['tools']} 步）"
        final = turn["texts"][-1] if turn["texts"] else ""
        decide = is_decision(final)                      # 「把日志贴给我」「需要的话我可以…」不算拍板，照常是汇报
        if decide:
            title, color = f"🙋【{name}】：老板，需要你拍板", "carmine"
        elif turn["by"] == "you":
            title, color, label = f"📝【{name}】：汇报", "orange", "📝 汇报"
        else:
            title, color, label = f"📣【{name}】：有新进展", "blue", "📣 有新进展"
        bar = session_status(e)
        head = "\n".join(x for x in (self.who(name, e), f"<font color='grey'>{usage_text(bar)}</font>" if usage_text(bar) else "") if x)
        chunks = rich(body)
        els = ([_md(head)] if head else []) + chunks[0]
        els += self.option_rows(name, final)
        if decide:
            els.append(_md("<font color='grey'>点按钮就是替你回它一句（选项是「我选 X」）；想多说几句就点〔✍️ 回复它〕或引用这张卡片回复。</font>"))
            self.mark_asking(name, final)
        els.append({"tag": "action", "actions": [_button("✍️ 回复它", f"对话 {name}", "primary"),
                                                 _button("看全文", f"看 {name} 全部"), self.effort_button(name, e)]})
        files = mentioned_files(body, e.get("cwd"))
        if files:
            els.append({"tag": "action", "actions": file_buttons(files)})
        card = Card(_card(title, color, els), f"{title}\n{body}")
        if not decide and len(chunks) == 1:              # 汇报、进展：10 分钟内同一个员工的下一张并进来，手机只响一次
            card.merge = {"name": name, "label": label, "body": body, "t": now()}
        return continued(card, title, color, chunks)

    def note_latest(self, name, t, text, loud=False):
        """〔📋 员工看板〕上它那一行的「最新」：它最近停下时说的最后一句。"""
        first = first_sentence(text)
        self.state.setdefault("bg", {})[name] = {"t": t, "text": one_line(first, 100) if first else "（醒来看了一眼，没写新的话）",
                                                 "loud": loud}

    def bg_update(self, name, e, turn, loud):
        """它停下了：记下最新一句话。后台醒来、没什么要你管的，就只更新看板，不单独发卡片。"""
        self.note_latest(name, e["statusAt"], turn["texts"][-1] if turn["texts"] else "", loud)

    def board_rows(self):
        """看板上的员工：活着的（助手排第一）和最近一周下班的；每日资讯这类自动任务不算。"""
        reg = load_json(REGISTRY, {})
        alive = {n: e for n, e in reg.items() if e.get("alive") and not is_quiet(n)
                 and (not is_assistant(n) or n == self.state.get("default"))}
        off = sorted(n for n, e in reg.items() if not e.get("alive") and not is_quiet(n) and not is_assistant(n)
                     and now() - e.get("died", 0) < 7 * 86400)
        return alive, off

    def board_sig(self):
        alive, off = self.board_rows()
        rows = self.state.get("bg", {})
        return json.dumps([sorted((n, e.get("status"), e.get("waiting"), rows.get(n, {}).get("t")) for n, e in alive.items()), off,
                           sorted(self.state.get("asking", {})), self.state.get("held", 0), self.quiet_now()])

    def status_card(self, minor=False):
        """〔📋 员工看板〕：每个员工一行，谁在干活、谁在等你、最新说了什么；原地更新，飞书不响。
        在群里会被置顶；还没建群时被新消息刷上去太多了，下次有人停下时挪到最下面。"""
        alive, off = self.board_rows()
        rows = {n: r for n, r in self.state.get("bg", {}).items() if n in alive and now() - r["t"] < 3 * 86400}
        self.state["bg"] = rows
        order = sorted(alive, key=lambda n: (not is_assistant(n), alive[n].get("status") != "busy", -rows.get(n, {}).get("t", 0)))
        lines = []
        for name in order:
            e = alive[name]
            if e.get("status") == "busy":
                state = "🟢 干活中"
            elif name in self.state.get("asking", {}):
                state = "🙋 等你拍板"
            elif e.get("waiting"):
                state = "🙋 等你" + ("批准" if "permission" in e["waiting"] else "回答")
            elif e.get("status") == "shell":
                state = "⏳ 等后台任务"
            else:
                state = "⏸ 空闲"
            r = rows.get(name)
            when = f" · {time.strftime('%H:%M', time.localtime(r['t']))}" if r else ""
            role = "（秘书）" if is_assistant(name) else ""
            name_md = f"{badge(name)} **{name}**" if badge(name) else f"**{name}**"
            latest = f"\n<font color='grey'>{r['text']}</font>" if r and not is_assistant(name) else ""
            lines.append(f"{state}　{name_md}{role}{when}{latest}")
        if off:
            lines.append(f"🌙 下班了：{'、'.join(off)}　<font color='grey'>（发「上班 工号」接着干）</font>")
        held, hours = self.state.get("held", 0), self.night()
        if self.quiet_now():
            lines.insert(0, f"🌙 夜间免打扰（{hours[0]}:00–{hours[1]}:00，你发消息或在电脑上用 Claude 就恢复）：只推要你拍板的"
                            + (f"，其余攒了 {held} 条" if held else ""))
        elif held:
            lines.insert(0, f"🌙 你不在时攒了 {held} 条没推（要你拍板的当时推了），点〔🌙 看攒下的〕")
        asking = {n: a for n, a in self.state.get("asking", {}).items() if n in alive}
        if asking:                                       # 最上面：谁在等你拍板、问的是什么
            todo = "\n".join(f"· {badge(n) or ''} **{n}**：{a['q']}" for n, a in sorted(asking.items(), key=lambda x: x[1]["t"]))
            lines.insert(0, f"**🙋 待拍板 {len(asking)}**\n{todo}")
        els = [_md("\n\n".join(lines) or "现在还没有员工。")]
        buttons = [_button(f"{badge(n)} {n}".strip(), f"#enter {n}") for n in order if not is_assistant(n)][:8]
        els += [{"tag": "action", "actions": buttons[i:i + 4]} for i in range(0, len(buttons), 4)]
        if held:
            els.append({"tag": "action", "actions": [_button(f"🌙 看攒下的 {held} 条", "#night all", "primary")]})
        els.append(_md("<font color='grey'>派活：打「@工号 内容」（比如 @2 先跑诊断），或引用它的卡片回复；「@所有人 …」发给全体。"
                       "只说「还在等」的只更新这张看板；要你拍板、有新进展时才单独发卡片，同一个员工 10 分钟内的合成一张。"
                       "夜里你不在时只推要你拍板的。点名字＝看详情并开始跟它说话。</font>"))
        foot = f"{talking_to(self.state)}\n{quota_text() + ' · ' if quota_text() else ''}更新于 {time.strftime('%H:%M')}"
        card = Card(_card("📋 员工看板", "wathet", els, footer=foot), "📋 员工看板\n" + "\n".join(lines))
        card.slot, card.minor = "status", minor
        return card

    def steps_card(self, name, s, since):
        asked, steps, _ = turn_steps(s.get("sessionId"), since)
        if not steps:
            return f"🔎【{name}】这一轮没有记录到过程。"
        els = ([_md(f"<font color='grey'>你：{one_line(asked, 80)}</font>"), {"tag": "hr"}] if asked else [])
        els.append(_md("\n".join(self._step_lines(steps, 30))))
        return Card(_card(f"🔎【{name}】这一轮的过程", "grey", els), f"🔎【{name}】共 {len(steps)} 步")

    def live_updates(self, names):
        """给飞书守护进程用：这些“思考中”卡片现在该显示什么；做完的返回最终回复（final=True）。"""
        self.state = load_json(STATE, self.state)
        live = discover()
        out = []
        for name in names:
            since, s = self.state["remote_pending"].get(name), live.get(name)
            if not since:
                out.append((name, None, True))              # 已经由别处处理过了
            elif not s:
                self.state["remote_pending"].pop(name, None)
                out.append((name, alert(f"❌【{name}】claude 退出了", "这句话没等到回复。", "red",
                                        [_button("重启", f"重启 {name}", "danger"), _button("返回列表", "看看")]), True))
            elif s["status"] != "busy" and since <= s["statusAt"] + 1:
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
        return e.get("title") or first_user_text(e.get("sessionId"), 24) or ""

    def who(self, name, e):
        """卡片顶上一行灰字：📌 话题 · 目录。同一个 tmux 里有好几个 claude 时靠它分清是哪个。"""
        if is_assistant(name):
            return ""
        topic = self.topic(name, e)
        return f"<font color='grey'>📌 {one_line(topic, 40) + ' · ' if topic else ''}{_short_dir(e.get('cwd'))}</font>"

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
        if name in self.state["remote_pending"]:
            self.state["remote_pending"][new] = self.state["remote_pending"].pop(name)
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

    def cmd_close(self, name, s):
        """正常退出：先发 /exit，8 秒没退再按两次 Ctrl-C。"""
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
        return alert(f"🌙【{name}】下班了", "对话记录都留着；点〔上班〕或发「上班 " + (badge(name)[:-1] or name) + "」，它会在原目录接着原来的对话干。", "grey",
                     [_button("☀️ 上班", f"上班 {name}"), _button("看板", "看看")])

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
                self.cmd_close(name, s)
                if _proc_alive(s["pid"]):
                    bad.append(f"{badge(name)}{name}：没关掉，可能正在执行操作，点〔看屏幕〕看看")
                else:
                    done.append(f"{badge(name)}{name}")
        lines = [f"已下班：{'、'.join(done)}。对话记录都留着，发「上班 工号」或「@工号 上班」接着干。"] if done else []
        return alert(f"🌙 {len(done)} 位员工下班了", "\n".join(lines + bad), "grey", [_button("看板", "看看")])

    def fire_card(self, name, e):
        """开除前确认：关掉它、从员工名单删掉；对话记录和文件都不删。"""
        if is_assistant(name):
            return "助手是你的秘书，不能开除。想让它忘掉前面的事，发「新对话」。"
        busy = "\n**它现在正在干活，开除会打断它。**" if e.get("alive") and e.get("status") == "busy" else ""
        body = (f"{self.who(name, e)}{busy}\n会关掉它，并从员工名单和看板上删掉。"
                f"对话记录和它目录里的文件都不删，以后在电脑上进 {_short_dir(e.get('cwd'))} 运行 claude --resume 还能找回。")
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
        if quota_text():
            elements.append(_md(f"<font color='grey'>已用：{quota_text()}</font>"))
        for name in order:
            e = reg[name]
            talking = "　✍️ 你打的字现在发给它" if self.state.get("current") == name else ""
            think = f" · 思考 {EFFORT_CN.get(efforts[name], '?')}" if name in efforts else ""
            if ctx_text(bars.get(name, {})):
                think += " · " + ctx_text(bars[name])
            grey = f"<font color='grey'>{_state_label(e)}{think} · {_short_dir(e.get('cwd'))}</font>"
            if name == default:
                row = f"⭐ **{name}**　<font color='green'>默认：直接打字就是和它聊</font>\n{grey}"
            else:
                topic = one_line(self.topic(name, e), 30)
                row = f"**{name}**{'　' + topic if topic else ''}{talking}\n{grey}"
            btn = (_button("进入", f"#enter {name}", "primary") if e.get("alive")
                   else _button("重启", f"重启 {name}", "danger"))
            elements += [{"tag": "hr"}, {"tag": "div", "text": {"tag": "lark_md", "content": row}, "extra": btn}]
            star = "⭐ 默认（直接打字就是和它聊）" if name == default else talking
            peer = f"　同事间叫 {e['peer']}" if e.get("peer") and e.get("alive") else ""
            lines.append(f"{badge(name)}{name}{star}　{_state_label(e)}　{_short_dir(e.get('cwd'))}{peer}")
        bottom = [_button("🔄 刷新", "看看"), _button("🆕 新对话", "新对话"), _button("🗂 对话记录", "对话记录")]
        if default in efforts:
            bottom.append(_button(f"🧠 {default}思考：{EFFORT_CN.get(efforts[default], '?')}", f"#effort {default}"))
        elements += [{"tag": "hr"}, {"tag": "action", "actions": bottom + [_button("帮助", "帮助")]}]
        alive = busy + idle
        return Card(_card(f"🤖 Claude Code · {alive} 个在运行", "blue", elements), "\n".join(lines))

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
        card = _card(f"🤖 {name}", _state_color(e), elements, footer=f"{hint} · {time.strftime('%H:%M')}")
        return Card(card, f"🤖 {name}\n{_state_label(e)}　{_short_dir(e.get('cwd'))}\n最新进展：{summary}\n{hint}")

    def cmd_restart(self, name, e):
        if e.get("alive"):
            return f"【{name}】的 claude 还在运行，不重启。真要重启，先「键 @{name} ctrl-c ctrl-c」让它退出。"
        args = list(e.get("args") or [])
        if is_assistant(name):
            args = with_effort(args, self.state.get("assistant_effort", ASSISTANT_EFFORT))
        if e.get("sessionId") and transcript_path(e["sessionId"]):
            args += ["--resume", e["sessionId"]]          # 没有记录就开全新对话，绝不用 --continue
        tname = name.replace(":", "-")
        ok, out = launch(tname, e["cwd"], args)
        if not ok:
            return f"【{name}】没有重启：\n{out}"
        return out + "\n" + self.after_launch(tname, keep=None if is_assistant(name) else name)

    def lost_card(self, were_on):
        """机器人启动时：上次还在上班、现在进程没了的员工（电脑关机 / 重启过，或它自己退出了）。
        正常重启机器人时员工都还在，不会有这张卡片。资讯这类自动会话不算。"""
        live, reg = self.refresh()
        lost = [n for n in were_on if n not in live and n in reg and not is_quiet(n)]
        if not lost:
            return None
        lost.sort(key=lambda n: (not is_assistant(n), badge(n) or "99"))
        who = "、".join(f"{badge(n)} {n}".strip() for n in lost)
        buttons = [_button("☀️ 全部上班", "#wakeall " + " ".join(lost), "primary")]
        buttons += [_button(f"☀️ {badge(n) or n}", f"上班 {n}") for n in lost[:7]]
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
            (bad if "没有重启" in out or "没看到" in out else done).append(f"{badge(name)} {name}".strip())
            live, reg = self.refresh()
        body = (f"上班了：{'、'.join(done)}" if done else "") + (f"\n没能上班：{'、'.join(bad)}（点〔看看〕再试）" if bad else "")
        return alert("☀️ 叫大家上班", body or "大家都已经在上班了。", "green" if not bad else "orange", [_button("看板", "看看")])

    def wake(self, name, e):
        """让下班的员工上班：走安全启动脚本（检查代理、带上代理环境变量），在原目录 --resume 原来的对话，
        名字和工号不变；等它能接话了再返回。返回 (会话, 出错说明)。"""
        out = self.cmd_restart(name, e)
        for _ in range(60):
            s = discover().get(name)
            if s and s["status"] != "busy" and not s.get("waiting") and _input_box(pane_rows(s)):
                return s, ""
            time.sleep(1)
        return None, out

    def after_launch(self, tname, keep=None):
        for _ in range(20):
            time.sleep(1)
            live = discover()
            for name, s in live.items():
                if s.get("private") and s["group"] == tname:
                    if keep and name != keep:            # 下班前叫什么，上班后还叫什么（工号也跟着）
                        aliases = load_json(ALIASES, {})
                        aliases[str(s["pid"])] = keep
                        save_json(ALIASES, aliases)
                        name = keep
                    return f"claude 已起来（会话名 {name}）。屏幕：\n{screen(s, 15)}"
        if crterm.launch_mode() != "tmux" and not LAUNCH:
            return "20 秒内没看到 claude 起来。看一眼电脑上新开的那个终端窗口里写了什么。"
        sess = {"term": "tmux", "socket": PRIVATE_SOCK, "pane": f"={tname}:"}
        return "20 秒内没看到 claude 起来，屏幕：\n" + screen(sess, 20)

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

        asking = self.state.setdefault("asking", {})
        for name in list(asking):                           # 待拍板：它下班了，或你在电脑上已经跟它说了话，就消掉
            e = reg.get(name) or {}
            if not e.get("alive") or (e.get("status") == "busy" and user_spoke_since(e.get("sessionId"), asking[name]["t"])):
                asking.pop(name)
        for name, e in reg.items():
            sid = e.get("sessionId") or ""
            if not e.get("alive") and (is_assistant(name) or is_quiet(name)):
                continue                                    # 助手是切换时主动关的，不报警
            if not e.get("alive"):
                once(f"dead:{sid}:{e.get('pid')}", alert(f"❌【{name}】claude 退出了", self.who(name, e) + "\n点〔重启〕会先检查环境和代理，再在原目录接回原对话。", "red",
                     [_button("重启", f"重启 {name}", "danger"), _button("看屏幕", f"屏 {name}")]), "dead")
                continue
            episode = f"{sid}:{e['statusAt']}"
            pending = self.state["remote_pending"].get(name)
            if e["status"] != "busy":
                idle_for = now() - e["statusAt"]
                note = self.state.get("slash", {}).get(name)
                if note and note[0] <= e["statusAt"] + 1 and not is_quiet(name):     # 你发的 /compact 之类做完了
                    self.state["slash"].pop(name)
                    ctx = ctx_text(session_status(e))
                    once(f"idle:{episode}", alert(f"✅【{name}】{note[1]} 做完了", self.who(name, e) + (f"\n现在{ctx}" if ctx else ""), "green",
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
                    if (remote and name in load_json(PENDING, {})) or f"idle:{episode}" in sent:
                        continue                                # 飞书那张“思考中”卡片会自己换成问题
                    pt = pending_tool(sid)
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
                    if name in load_json(PENDING, {}):
                        continue                                    # 飞书那张“思考中”卡片会自己换成回复
                    self.state["remote_pending"].pop(name, None)
                    if self.state.get("current") == name:
                        self.state["current_at"] = now()            # 它回复了，也算一次活动
                    once(f"idle:{episode}", self.reply_card(name, e, pending), "reply")
                else:
                    sent[f"idle:{episode}"] = now()
                    turn = last_turn(sid) or {"id": episode, "by": "you", "texts": [], "tools": 0}
                    key = f"turn:{sid}:{turn['id']}:{len(turn['texts'])}"
                    if key in sent:
                        continue                                # 这一轮报过了：它只是又停了一下，或你在电脑上用了 /effort 之类
                    sent[key] = now()
                    final = turn["texts"][-1] if turn["texts"] else ""
                    if turn["by"] == "you":                     # 你叫它干的：做了事就提醒（只是本地命令、什么都没做的不算）
                        loud = bool(turn["texts"] or turn["tools"])
                    else:                                       # 后台醒来：要你决定、或写了一段实质内容才单独发
                        loud = bool(final) and (asks_you(final) or
                                                len(re.sub(r"\s", "", "".join(turn["texts"]))) >= SUBSTANTIAL_CHARS)
                    self.bg_update(name, e, turn, loud)
                    if loud and not first_run:
                        self.push(self.stop_card(name, e, turn), "idle", must=is_decision(final))
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
        self.peer_tick(reg, first_run)
        cutoff = now() - 3 * 86400
        mon["sent"] = {k: v for k, v in sent.items() if v > cutoff or k == "proxy-down"}
        self.save()

    def roster_tick(self, reg, first_run):
        """新员工入职（不管是机器人开的还是你在电脑上开的）：分一个工号，发一张「👋 入职」；看板内容变了就原地更新。"""
        staff = sorted(n for n, e in reg.items() if e.get("alive") and not is_assistant(n) and not is_quiet(n))
        roster = self.state.get("roster")
        hired = [] if roster is None else [n for n in staff if n not in roster]   # 第一次：现有的都算老员工，不发入职
        roster = [n for n in (roster if roster is not None else staff) + hired if n in reg]
        self.state["roster"] = roster
        badges = load_json(BADGES, {})
        new = {n: v for n, v in badges.items() if n in roster}            # 开除了（名单里没了）的收回工号
        for name in sorted(roster, key=lambda n: (reg[n].get("startedAt") or 0, n)):
            if name not in new:
                new[name] = min(set(range(1, len(new) + 2)) - set(new.values()))
        if new != badges:
            save_json(BADGES, new)
        for name in hired:
            e = reg[name]
            if first_run:
                continue
            st = session_status(e)
            info = " · ".join(x for x in (st.get("model"), f"思考{EFFORT_CN.get(st.get('effort'), '')}" if st.get("effort") else "") if x)
            n = new.get(name)
            self.push(alert(f"👋【{name}】入职了，来报到", "\n".join(x for x in (self.who(name, e), info,
                         f"给它派活：打「@{n} 内容」，或引用这张卡片回复。" if n else "引用这张卡片回复就能给它派活。") if x), "turquoise",
                         [_button("✍️ 回复它", f"对话 {name}", "primary"), _button("看屏幕", f"屏 {name}")]), "hire")
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
            if start is None or start > size:
                pos[sid] = size                          # 第一次看到它：从现在开始看，不翻旧账
                continue
            if size == start:
                continue
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(min(size - start, 8 << 20))
            end = chunk.rfind(b"\n") + 1                # 只看写完整的行
            pos[sid] = start + end
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                content = (ev.get("message") or {}).get("content")
                t = _ts(ev.get("timestamp"))
                if is_human(ev) and t - self.state.get("typed_at", {}).get(name, 0) > 600:   # 你在电脑上跟它说话了（不是机器人替谁打的）
                    self.state["seen_you"] = max(self.state.get("seen_you", 0), t)
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
