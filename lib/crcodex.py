"""Codex CLI 员工：和 Claude Code 员工一样上看板、派活、批准、下班上班。

Codex 一样是 tmux 里的交互式 TUI，机器人只看屏幕、打字、按键，不用 codex exec / app-server 去驱动它。
状态从这几处来，都不需要 codex 配合：
- 进程：codex TUI 进程的环境变量里有 CODEX_HOME（用的哪套配置）和 TMUX_PANE（在哪个 tmux 窗格）；
- 窗格标题：config.toml 里 tui.terminal_title 以 thread-id 开头时，标题里能拿到当前会话 id；
  等你批准时标题会变成「[ . ] Action Required | <id> | …」；
- 对话记录：$CODEX_HOME/sessions/年/月/日/rollout-*-<id>.jsonl。task_started / task_complete / turn_aborted
  是一轮的开始和结束，item_completed 里是它说的话、跑的命令、改的文件，token_count 里是上下文和额度；
- 屏幕：批准框「Would you like to run the following command?」、/model 菜单、输入框。
可选：codex 的 PermissionRequest hook 接 cr-hook，把要批准的完整命令记到 data/asks/<会话>.json（见 install_hooks）。

这里只放和 codex 有关的读取、解析；卡片怎么画、什么时候推，都在 crcore 里，跟 Claude 员工共用。
"""
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime

import psutil

import crterm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 不是交互式界面的子命令（这些进程不算员工）
NON_TUI = {"exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control", "completion",
           "update", "doctor", "sandbox", "debug", "apply", "a", "queue", "archive", "delete", "migrate-rollouts",
           "migrate-nfs-db", "unarchive", "cloud", "exec-server", "features", "help", "agents", "mcp-server"}
VALUE_OPTS = {"-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env", "-m", "--model",
              "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd", "--add-dir", "-a",
              "--ask-for-approval", "-i", "--image"}
ID_RE = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{0,12})")
SPIN = set("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
EFFORT_LABEL = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high"}   # /model 菜单里的叫法
EFFORTS = ("low", "medium", "high", "xhigh")       # 能从卡片上直接改的档位（更高的在 /model 的「More reasoning…」里）
OPTION_RE = re.compile(r"^(?:›\s*)?(\d+)\.\s+(.+)$")
OPTION_AT_RE = re.compile(r"(?:^|\s{2,})(›\s*)?(\d+)\.\s+(\S.*)$")
FOOT_SEG = r"(?:enter|esc|tab|space|[a-z]|←/→|↑/↓)\s+[\w ]+?"           # 菜单底部提示的一段：「enter select」「s session」
POPUP_FOOT_RE = re.compile(rf"(?:^|\s{{2,}})((?:enter|esc)\s+[\w ]+?(?:\s+·\s+{FOOT_SEG})+)$")
DIALOG_FOOT = "Press enter to confirm or esc to cancel"
DIALOG_ASK = {"run the following command": ("Bash", "⚙️ 运行命令"), "make the following edits": ("Edit", "✏️ 修改文件"),
              "grant these permissions": ("Permissions", "🔓 放宽权限"), "send input to": ("Input", "⌨️ 往终端里输入")}
SLASH = [("/compact", "🗜 压缩上下文", "把前面的对话压成要点，腾出上下文空间"),
         ("/status", "📊 状态和用量", "模型、权限、上下文和额度"),
         ("/model", "🤖 换模型 / 思考程度", "弹出菜单，用遥控键盘选"),
         ("/resume", "📂 接回以前的对话", "弹出列表，用遥控键盘选"),
         ("/new", "🧹 新对话", "开一段新的空对话（旧的还在，可以 /resume 接回）"),
         ("/diff", "🔀 看改动", "git diff（含没跟踪的文件）")]


# ---------------------------------------------------------------- 配置：本机有哪几套 codex

def homes():
    """[{"label": "官方", "home": CODEX_HOME, "env": 启动前要 source 的环境脚本}, …]。
    CLAUDE_MONITOR_CODEX_HOMES 写成「标签|CODEX_HOME|env脚本;标签|…」；不设就是默认的那一套（~/.codex，不 source 什么）。"""
    raw = os.environ.get("CLAUDE_MONITOR_CODEX_HOMES", "").strip()
    out = []
    for part in raw.split(";") if raw else []:
        f = [x.strip() for x in part.split("|")]
        if len(f) >= 2 and f[1]:
            out.append({"label": f[0] or os.path.basename(os.path.dirname(f[1])), "home": f[1], "env": f[2] if len(f) > 2 else ""})
    if not out:
        out.append({"label": "", "home": os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex"), "env": ""})
    return out


def home_info(home):
    """CODEX_HOME → 本机配置里的那一套；没配置过的也给个标签（目录名）。"""
    real = os.path.realpath(home or "")
    for h in homes():
        if os.path.realpath(h["home"]) == real:
            return h
    return {"label": os.path.basename(os.path.dirname(real)) or "codex", "home": home, "env": ""}


def pick_home(word):
    """「codex」→ 第一套；「codex-备用」「codex:备用」（或按目录名「codex-b」）→ 标签对得上的那套；对不上返回 None。"""
    word = (word or "").lower()
    if word == "codex":
        return homes()[0]
    m = re.fullmatch(r"codex[-:：](.+)", word)
    if not m:
        return None
    for h in homes():
        if h["label"].lower() == m.group(1) or os.path.basename(os.path.dirname(h["home"])).lower() in (m.group(1), f"codex-{m.group(1)}"):
            return h
    return None


def tag(e):
    """卡片上的标记：「Codex·官方」「Codex·备用」（只配了一套、没起标签时就是「Codex」）。看它实际用的 CODEX_HOME，不看名字。"""
    home = e.get("home") or e.get("cfg")
    label = home_info(home).get("label") if home else ""
    return "Codex" + (f"·{label}" if label else "")


# ---------------------------------------------------------------- 小工具

def _ts(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def _tail_lines(path, size=4 << 20):
    with open(path, "rb") as f:
        total = os.path.getsize(path)
        f.seek(max(0, total - size))
        lines = f.read().decode("utf-8", "replace").splitlines()
    return lines[1:] if total > size else lines


def _turn_size(path, size):
    """要读多少才包得住最后一轮（一轮跑了很久、写了很多的时候，开头可能早就不在末尾 4MB 里了），最多 32MB。"""
    turn = INDEX.get(path).get("turn") or {}
    if not turn.get("pos"):
        return size
    return min(max(size, os.path.getsize(path) - turn["pos"] + 1), 32 << 20)


def _records(path, size=4 << 20, whole_turn=True):
    if whole_turn:
        size = _turn_size(path, size)
    for line in _tail_lines(path, size):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _payload(e):
    p = e.get("payload")
    return p if isinstance(p, dict) else {}


def _item(e):
    """event_msg 里 item_completed 的那一项（它说的话、跑的命令…）；别的返回 {}。"""
    p = _payload(e)
    if e.get("type") == "event_msg" and p.get("type") == "item_completed" and isinstance(p.get("item"), dict):
        return p["item"]
    return {}


def _text(item):
    return "".join(c.get("text", "") for c in item.get("content") or [] if isinstance(c, dict)).strip()


def readable(text):
    """提问框里的回答在对话记录里是「<send_user_message_question_reply>」包着的一段 JSON：换成「回答「题目」：答案」。"""
    if "<send_user_message_question_reply>" not in (text or ""):
        return text
    body = text.split("<send_user_message_question_reply>", 1)[1].split("</send_user_message_question_reply>", 1)[0]
    try:
        items = json.loads(body)
        return "\n".join(f"回答「{it.get('question', '')}」：{it.get('answer', '')}" for it in items if isinstance(it, dict)) or text
    except (ValueError, TypeError):
        return text


def turn_error(p):
    """task_complete 里带的错误（模型不可用、额度用完、网络断了…）→ 一句给人看的话；没有错误返回 ""。"""
    err = p.get("error") if p.get("type") == "task_complete" else None
    if not err:
        return ""
    msg = err.get("message") if isinstance(err, dict) else str(err)
    try:                                             # 常见的是接口原样返回的 JSON：取里面那句 message
        inner = json.loads(msg)
        msg = (inner.get("error") or {}).get("message") or inner.get("message") or msg
    except (TypeError, ValueError, AttributeError):
        pass
    return f"⚠️ 这一轮出错了：{msg}"


def quota_windows(rl):
    """rate_limits → {"5h": %, "7d": %, "30d": %}（按窗口长短认是哪一种）。"""
    out = {}
    for w in ((rl or {}).get("primary"), (rl or {}).get("secondary")):
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)):
            mins = w.get("window_minutes") or 0
            out["5h" if mins <= 24 * 60 else "7d" if mins <= 8 * 24 * 60 else "30d"] = round(w["used_percent"])
    return out


def is_rollout(path):
    return bool(path) and os.path.basename(path).startswith("rollout-") and path.endswith(".jsonl")


# ---------------------------------------------------------------- 找对话记录

_paths, _paths_lock = {}, threading.Lock()


def rollout_path(sid, home=None):
    """会话 id（完整的，或标题里截短的前缀）→ rollout 文件；找不到返回 None。"""
    if not sid or not re.fullmatch(r"[0-9a-f-]{8,40}", sid) or len(sid) > 14 and sid[14] != "7":
        return None                                      # codex 的会话 id 是 UUIDv7；Claude 的是 v4，不用去翻 codex 的目录
    with _paths_lock:
        hit = _paths.get(sid)
    if isinstance(hit, float) and time.time() - hit < 30:
        return None                                      # 刚找过没有（新会话还没写记录）：网络盘上 glob 慢，30 秒内不再找
    if isinstance(hit, str) and os.path.exists(hit):
        return hit
    found = []
    for h in [home] if home else [x["home"] for x in homes()]:
        found += glob.glob(os.path.join(h, "sessions", "*", "*", "*", f"rollout-*-{sid}*.jsonl"))
    if not found:
        with _paths_lock:
            _paths[sid] = time.time()
        return None
    path = max(found, key=os.path.getmtime)
    with _paths_lock:
        _paths[sid] = path
    return path


def sid_of(path):
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path or "")
    return m.group(1) if m else None


_names = {}


def thread_name(home, sid):
    """codex 给这段对话起的名字（session_index.jsonl，后写的为准）。网络盘上开文件慢，记 1 分钟。"""
    hit = _names.get((home, sid))
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    name = ""
    try:
        lines = open(os.path.join(home or "", "session_index.jsonl"), encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    for line in reversed(lines):
        if sid and sid in line:
            try:
                name = json.loads(line).get("thread_name") or ""
                break
            except ValueError:
                continue
    _names[(home, sid)] = (time.time(), name)
    return name


# ---------------------------------------------------------------- 一轮的状态：增量读 rollout

class _Index:
    """每个 rollout 文件读到哪了、最后一轮开始 / 结束在什么时候、模型、上下文和额度。只读新写进来的部分。"""
    MARKS = (b'"task_started"', b'"task_complete"', b'"turn_aborted"', b'"turn_context"', b'"token_count"',
             b'"thread_settings_applied"', b'"request_user_input')

    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}

    def get(self, path):
        with self.lock:
            st = self.files.get(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                return {}
            if st is None or size < st["pos"]:
                st = {"pos": 0, "turn": None, "last": 0.0, "model": "", "effort": "", "tok": None, "rl": None}
                self.files[path] = st
            if size > st["pos"]:
                with open(path, "rb") as f:
                    f.seek(st["pos"])
                    chunk = f.read(size - st["pos"])
                end = chunk.rfind(b"\n") + 1                 # 只看写完整的行
                at = st["pos"]
                for line in chunk[:end].split(b"\n"):
                    pos, at = at, at + len(line) + 1
                    if not line:
                        continue
                    m = re.match(rb'\{"timestamp":"([^"]+)"', line)
                    if m:
                        st["last"] = _ts(m.group(1).decode())
                    if not any(k in line[:400] for k in self.MARKS):
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    self._take(st, e, pos)
                st["pos"] += end
            return dict(st)

    @staticmethod
    def _take(st, e, pos):
        p, t = _payload(e), _ts(e.get("timestamp"))
        kind = p.get("type")
        if kind in ("task_started", "task_complete", "turn_aborted"):
            prev = st["turn"] or {}
            trig = (p.get("turn_attribution") or {}).get("turn_trigger") or "user"
            st["turn"] = {"kind": kind, "t": t, "id": p.get("turn_id"), "trigger": trig if kind == "task_started" else prev.get("trigger", "user"),
                          "pos": pos if kind == "task_started" else prev.get("pos", 0)}   # 这一轮开头在文件里的位置
        elif e.get("type") == "turn_context":
            st["model"], st["effort"] = p.get("model") or st["model"], p.get("effort") or st["effort"]
        elif kind == "thread_settings_applied":
            ts = p.get("thread_settings") or {}
            st["model"], st["effort"] = ts.get("model") or st["model"], ts.get("reasoning_effort") or st["effort"]
        elif kind == "function_call" and str(p.get("name", "")).startswith("request_user_input"):
            st["ask_t"] = t                              # 最近一次问你题（提问框开着时，这就是开始等你的时间）
        elif kind == "token_count":
            if isinstance(p.get("info"), dict):
                st["tok"] = p["info"]
            rl = p.get("rate_limits") or {}
            if rl.get("primary") or rl.get("secondary"):
                st["rl"] = dict(rl, t=t)


INDEX = _Index()


def turn_state(path):
    """(状态 busy / idle, 从什么时候开始, 最后一条记录的时间)。还没有记录的返回 None。"""
    st = INDEX.get(path) if path else {}
    turn = st.get("turn")
    if not turn:
        return None
    return ("busy" if turn["kind"] == "task_started" else "idle"), turn["t"], st["last"]


def session_status(s):
    """模型、思考程度、上下文和额度（这个 codex 账号自己的，跟 Claude 的分开）。"""
    st = INDEX.get(s["transcript"]) if s.get("transcript") else {}
    tok = st.get("tok") or {}
    win = tok.get("model_context_window") or 0
    used = (tok.get("last_token_usage") or {}).get("total_tokens")
    out = {"model": st.get("model") or "", "effort": st.get("effort") or "", "agent": "codex",
           "ctx": round(used * 100 / win) if win and used else None, "5h": None, "7d": None, "30d": None, "at": 0}
    rl = st.get("rl") or latest_quota(s.get("home"))
    out.update(quota_windows(rl))
    out["at"] = rl.get("t", 0)
    return out


_quota = {}


def latest_quota(home):
    """这套 codex 最近一次记下的额度（5 小时 / 每周）。额度是整个账号的：取最近改过的几个 rollout 里最新的那份。"""
    if not home:
        return {}
    hit = _quota.get(home)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    files = sorted(glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*.jsonl")), key=os.path.getmtime)[-3:]
    best = {}
    for path in files:
        rl = INDEX.get(path).get("rl") or {}
        if rl.get("t", 0) > best.get("t", 0):
            best = rl
    _quota[home] = (time.time(), best)
    return best


# ---------------------------------------------------------------- 发现本机的 codex 员工

def _first_arg(args):
    """跳过选项（和选项的值），第一个位置参数：子命令、resume 的会话 id 或者开场白。"""
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        if a in VALUE_OPTS:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return a
    return None


def keep_args(args):
    """上班（codex resume）时沿用的启动参数：去掉子命令、会话 id、开场白、--last / 图片这些一次性的。"""
    out, skip, sub = [], None, True
    for a in args:
        if skip is not None:
            if skip:
                out.append(a)
            skip = None
            continue
        if a in VALUE_OPTS:
            keep = a not in ("-i", "--image")
            if keep:
                out.append(a)
            skip = keep
            continue
        if a.startswith("-"):
            if a not in ("--last", "--all", "--include-non-interactive") and not a.startswith(("--image=", "-i=")):
                out.append(a)
            continue
        if sub and a in ("resume", "fork"):
            sub = False
            continue
        sub = False                                      # 会话 id、开场白：不要
    return _drop_own(out)


OWN_ARGS = ["--no-daemon", "-c", "check_for_update_on_startup=false"]
# 机器人开的 codex 都带：不用共用的后台服务（见 launch_argv）；启动时不弹「有新版本，要不要更新」的框（更不更新你自己定）


def _drop_own(args):
    out, i = [], 0
    while i < len(args):
        if args[i] == "--no-daemon":
            i += 1
        elif args[i] in ("-c", "--config") and i + 1 < len(args) and args[i + 1].startswith("check_for_update_on_startup="):
            i += 2
        else:
            out.append(args[i])
            i += 1
    return out


def _tui_procs():
    """本机的 codex 交互式界面进程（用 npm 装的 codex 是 node 包着原生程序，取原生的那个）。"""
    out = []
    me = psutil.Process().username()
    for p in psutil.process_iter(["pid", "name", "cmdline", "username"]):
        try:
            if p.info["name"] != "codex" or p.info["username"] != me:
                continue
            cmd = p.info["cmdline"] or []
            if not cmd or os.path.basename(cmd[0]) != "codex" or (_first_arg(cmd[1:]) or "") in NON_TUI:
                continue
            if {"--version", "-V", "--help", "-h"} & set(cmd[1:]):
                continue                                 # codex 自己查版本之类，一闪就过
            parent = p.parent()
            if p.status() == psutil.STATUS_ZOMBIE or (parent and parent.name() == "codex"):
                continue                                 # codex 自己开的子进程（后台服务、查更新…）不是界面
            out.append(p)
        except psutil.Error:
            continue
    first = {}                                           # 同一个窗格里只有一个界面：取最早起的那个
    for p in out:
        try:
            env = p.environ()                            # 窗格编号只在同一个 tmux 服务器里唯一
            key = (env.get("TMUX", "").split(",")[0], env["TMUX_PANE"]) if env.get("TMUX_PANE") else p.pid
            if key not in first or p.create_time() < first[key].create_time():
                first[key] = p
        except psutil.Error:
            continue
    return list(first.values())


def _title_info(title):
    """窗格标题：(会话 id 或前缀, 在等你批准吗, 在转圈吗)。"""
    m = ID_RE.search(title or "")
    return (m.group(1).rstrip("-") if m else None), "Action Required" in (title or ""), bool(SPIN & set(title or ""))


def discover():
    """本机所有交互式 codex：跟 crcore.discover() 里 Claude 的会话一个样子，多了 agent / home / transcript。名字由 crcore 起。"""
    found = []
    for p in _tui_procs():
        try:
            env = p.environ()
            cwd = p.cwd()
            started = p.create_time()
            args = p.cmdline()[1:]
        except psutil.Error:
            continue
        home = env.get("CODEX_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".codex")
        pane = env.get("TMUX_PANE") if env.get("TMUX") else None
        t = crterm.attach_info(p.pid, {"tmux": f"x.{pane}" if pane else "", "cwd": cwd})
        if not t:
            continue
        prefix, action, spinning = _title_info(t.get("title"))
        first = _first_arg(args)
        if not prefix and first in ("resume", "fork"):   # 标题里没有 id（没配 tui.terminal_title）：看启动参数
            rest = args[args.index(first) + 1:]
            prefix = next((a for a in rest if ID_RE.fullmatch(a)), None)
        path = rollout_path(prefix, home) if prefix else None
        sid = sid_of(path) if path else None
        state = turn_state(path)
        status, at = ("idle", started) if not state else state[:2]
        waiting = ""
        if action or (status == "busy" and not prefix and t["term"] != "none" and dialog(pane_rows(t))):
            waiting, status = "permission prompt", "waiting"
            at = (state[2] if state else 0) or at          # 停在批准框上：这段时间对话记录不再写，最后一条的时间就是开始等的时间
            rows = pane_rows(t) if t["term"] != "none" else []
            if not dialog(rows) and question_box(rows):    # 一次问你几道题：它可能还在接着干，对话记录照样在写，从问的那一刻算起
                waiting = "questions"
                at = (INDEX.get(path).get("ask_t") if path else 0) or at
        info = home_info(home)
        found.append({
            "pid": p.pid, "agent": "codex", "sessionId": sid, "threadPrefix": prefix, "cwd": cwd, "cfg": home, "home": home,
            "flavor": info["label"], "env": info["env"], "transcript": path,
            "status": status, "statusAt": at, "startedAt": started, "waiting": waiting, "peer": "",
            **{k: v for k, v in t.items() if k != "title"},
            "tmux_session": t["group"], "args": keep_args(args),
            "title": thread_name(home, sid) if sid else "",
        })
    return found


# ---------------------------------------------------------------- 读对话：结论、过程、最后一轮

def _tool_label(item):
    """item_completed 里做完的一步 → (说法, 细节)。"""
    kind = item.get("type")
    if kind == "CommandExecution":
        cmd = item.get("command") or []
        cmd = cmd[-1] if isinstance(cmd, list) and len(cmd) >= 3 and cmd[1] in ("-lc", "-c") else " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)
        return "⚙️ 运行命令", cmd
    if kind == "FileChange":
        ch = item.get("changes") or {}
        names = "、".join(os.path.basename(k) for k in list(ch)[:3]) + ("…" if len(ch) > 3 else "")
        added = ch and all((v or {}).get("type") == "add" for v in ch.values())
        return ("📝 写入" if added else "✏️ 修改"), names
    if kind == "WebSearch":
        return "🌐 上网搜", item.get("query") or ""
    if kind == "ImageView":
        return "🖼 看图", os.path.basename(str(item.get("path") or ""))
    if kind == "ContextCompaction":
        return "🗜 压缩上下文", ""
    if kind in ("McpToolCall", "DynamicToolCall"):
        return "🔧 " + str(item.get("tool") or item.get("name") or kind), ""
    return "🔧 " + str(kind), ""


def _call_label(p):
    """还在跑的那一步（只有调用、还没有结果）：从调用的参数里认出是在跑命令还是改文件。"""
    raw = p.get("input") if p.get("type") == "custom_tool_call" else p.get("arguments")
    raw = raw if isinstance(raw, str) else json.dumps(raw or {}, ensure_ascii=False)
    m = re.search(r'\bcmd"?\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if m:
        try:
            return "⚙️ 运行命令", json.loads(f'"{m.group(1)}"')
        except ValueError:
            return "⚙️ 运行命令", m.group(1)
    if "apply_patch" in raw or p.get("name") == "apply_patch":
        files = re.findall(r"\*\*\* (?:Update|Add) File: ([^\\\n]+)", raw)
        return "✏️ 修改", "、".join(os.path.basename(f) for f in files[:3])
    return "🔧 " + str(p.get("name") or "工具"), ""


TOOL_ITEMS = ("CommandExecution", "FileChange", "WebSearch", "ImageView", "ContextCompaction", "McpToolCall", "DynamicToolCall")


def _took(p):
    """item_completed 里记着开始、结束的时间：这一步花了几秒。"""
    a, b = p.get("started_at_ms"), p.get("completed_at_ms")
    return (b - a) / 1000 if a and b else None


def turn_steps(path, since):
    """since 之后的过程：(你发的那句话, [(think / tool / text, 内容, 做完没)], 是否被打断)。跟 crcore.turn_steps 一个格式。"""
    asked, steps, stopped, calls, done = "", [], False, {}, set()
    for e in _records(path):
        if _ts(e.get("timestamp")) < since - 2:
            continue
        p, item = _payload(e), _item(e)
        if p.get("type") == "turn_aborted":
            stopped = True
        elif turn_error(p):
            steps.append(("text", turn_error(p), True))
        elif item.get("type") == "UserMessage":
            if not asked and _text(item):
                asked = readable(_text(item))
            if said_of(e):
                steps.append(("said", said_of(e), True))   # 谁说的（飞书 / 终端…）由 crcore.label_said 对出来
        elif item.get("type") == "AgentMessage" and _text(item):
            steps.append(("text", _text(item), True))
        elif item.get("type") == "Reasoning":
            summary = " ".join(x if isinstance(x, str) else str((x or {}).get("text", "")) for x in item.get("summary_text") or []).strip()
            if summary:
                steps.append(("think", (summary, _took(p)), True))
        elif item.get("type") in TOOL_ITEMS:
            label, detail = _tool_label(item)
            steps.append(("tool", (label, re.sub(r"\s+", " ", str(detail))[:50], _took(p)), True))
        elif e.get("type") == "response_item" and p.get("type") in ("custom_tool_call", "function_call"):
            calls[p.get("call_id")] = dict(p, at=_ts(e.get("timestamp")))
        elif e.get("type") == "response_item" and p.get("type") in ("custom_tool_call_output", "function_call_output"):
            done.add(p.get("call_id"))
    if not stopped:
        for cid, p in calls.items():
            if cid not in done:                          # 还在跑的那一步
                label, detail = _call_label(p)
                steps.append(("tool", (label, re.sub(r"\s+", " ", str(detail))[:50], time.time() - p["at"] if p.get("at") else None), False))
    return asked, steps, stopped


def last_turn(path):
    """最后一轮：{"id", "t": 开始时间, "by": you / background, "texts": [它写的话], "tools": 做了几步, "said": [这一轮里别人对它说的话]}；
    读不到返回 None。不是你发起的（/goal 自己接着干的一轮之类）算后台。"""
    turn = None
    for e in _records(path):
        p, item = _payload(e), _item(e)
        if p.get("type") == "task_started":
            trig = (p.get("turn_attribution") or {}).get("turn_trigger") or "user"
            turn = {"id": p.get("turn_id") or e.get("timestamp"), "t": _ts(e.get("timestamp")),
                    "by": "you" if trig == "user" else "background", "texts": [], "tools": 0, "said": []}
        elif turn and said_of(e):
            turn["said"].append(said_of(e))
        elif turn and item.get("type") == "AgentMessage" and _text(item):
            turn["texts"].append(_text(item))
        elif turn and turn_error(p):
            turn["texts"].append(turn_error(p))
        elif turn and item.get("type") in TOOL_ITEMS:
            turn["tools"] += 1
    return turn


def last_reply(path, final_only=False):
    """最后一轮里它写给你的正文；final_only 只要最后一条（通常就是结论）。"""
    texts, final = [], ""
    for e in _records(path):
        p, item = _payload(e), _item(e)
        if p.get("type") == "task_started":
            texts = []
        elif item.get("type") == "AgentMessage" and _text(item):
            texts.append(_text(item))
        elif turn_error(p):
            texts.append(turn_error(p))
        elif p.get("type") == "task_complete" and p.get("last_agent_message"):
            final = p["last_agent_message"]
    if final_only:
        return texts[-1] if texts else final
    return "\n\n".join(texts) or final


def user_texts(path, size=4 << 20):
    """[(时间, 你说的话)]：电脑上打的，或机器人替你打进去的。"""
    out = []
    for e in _records(path, size):
        item = _item(e)
        if item.get("type") == "UserMessage" and _text(item):
            out.append((_ts(e.get("timestamp")), readable(_text(item))))
    return out


def first_user_text(path, limit=16):
    with open(path, "rb") as f:
        head = f.read(4 << 20).decode("utf-8", "replace").splitlines()
    for line in head:
        if '"UserMessage"' not in line:
            continue
        try:
            text = _text(_item(json.loads(line))).replace("\n", " ")
        except ValueError:
            continue
        if text:
            return text if len(text) <= limit else text[:limit] + "…"
    return ""


def last_human_text(path):
    size = 2 << 20
    while True:
        hits = user_texts(path, size)
        if hits or size >= 32 << 20 or size >= os.path.getsize(path):
            return hits[-1][1] if hits else ""
        size *= 4


def is_human(e):
    """rollout 里这一条是不是人说的话（peer_tick 用来知道你在电脑上跟它说话了）。"""
    return _item(e).get("type") == "UserMessage" and bool(_text(_item(e)))


def said_of(e):
    """rollout 里这一条是不是有人在跟它说话（输入框里提交的，干活中途插的话也算），格式同 crcore.said_of。
    codex 不记斜杠命令；/goal 自己接着干的一轮不是 UserMessage，不算。"""
    item = _item(e)
    if item.get("type") != "UserMessage":
        return None
    text = readable(_text(item))
    images = sum(1 for c in item.get("content") or [] if isinstance(c, dict) and "image" in str(c.get("type", "")).lower())
    if not text and not images:
        return None
    return {"t": _ts(e.get("timestamp")), "kind": "human", "from": "", "text": text, "images": images}


# ---------------------------------------------------------------- 屏幕：批准框、菜单、输入框

def pane_rows(s, ansi=False):
    return [r.rstrip() if ansi else r.strip() for r in crterm.rows(s, ansi=ansi)]


def dialog(rows):
    """屏幕底部的批准框：{"ask": 问句, "body": [命令、原因…], "options": [(编号, 文字)], "tool": 归类, "label", "sig"}；没有返回 None。
        Would you like to run the following command?
        Reason: …
        $ touch hello.txt
      › 1. Yes, proceed (y)
        2. Yes, and don't ask again for commands that start with `touch hello.txt` (p)
        3. No, and tell Codex what to do differently (esc)
        Press enter to confirm or esc to cancel"""
    tail = [r for r in rows if r][-60:]
    foot = next((i for i in range(len(tail) - 1, max(len(tail) - 5, -1), -1) if DIALOG_FOOT in tail[i]), None)
    if foot is None:
        return None
    opts, first = [], None
    for i in range(foot):
        m = OPTION_RE.match(tail[i])
        if m and int(m.group(1)) == 1:
            opts, first = [], i
        if m and int(m.group(1)) == len(opts) + 1:
            opts.append((len(opts) + 1, m.group(2).strip()))
    if not opts:
        return None
    top = next((i for i in range(first - 1, max(first - 40, -1), -1) if re.match(r"(Would you like to|Do you want to|Allow )", tail[i])), None)
    ask = tail[top] if top is not None else ""
    body = tail[top + 1:first] if top is not None else []
    tool, label = next((v for k, v in DIALOG_ASK.items() if k in ask), ("Codex", "🔐 要你批准"))
    sig = "c" + hashlib.md5("\n".join([ask] + body).encode()).hexdigest()[:8]   # 同一个框不变；换了个框（新的命令）就变
    return {"ask": ask, "body": body, "options": opts, "tool": tool, "label": label, "sig": sig}


def dialog_options(rows):
    d = dialog(rows)
    return d["options"] if d else []


def waiting_on(s, rows, hook=None):
    """它停在批准框上等的是哪一步：({"id", "name", "input"}, [])，跟 crcore.waiting_on 一个格式；不在等返回 None。
    有 PermissionRequest hook 记下的（hook，必须是这次等之前刚记的）就用它（完整命令）；没有就用屏幕上框里的内容。
    一次问你几道题（提问框）：{"name": "CodexQuestions", "input": {"questions": 还开着的题, "left": 屏幕上还剩几道}}。"""
    d = dialog(rows)
    if not d:
        oq = open_questions(s, rows)
        if oq:
            last = oq["questions"][-1] if oq["questions"] else {}
            return {"id": "q" + (last.get("call") or "screen")[-8:], "name": "CodexQuestions",
                    "input": {"questions": oq["questions"], "left": oq["box"]["left"]}}, []
    if hook and hook.get("t", 0) >= (s.get("statusAt") or 0) - 10:
        inp = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {"command": hook.get("tool_input")}
        name = {"exec_command": "Bash", "shell": "Bash", "local_shell": "Bash", "shell_command": "Bash"}.get(hook.get("tool_name"), hook.get("tool_name"))
        return {"id": f"h{int(hook['t'] * 1000)}", "name": name, "input": inp or {}, "alt_id": d["sig"] if d else None}, []
    if not d:
        return None
    cmd = "\n".join(r[2:] for r in d["body"] if r.startswith("$ "))
    reason = next((r.split(":", 1)[1].strip() for r in d["body"] if r.startswith("Reason:")), "")
    return {"id": d["sig"], "name": "CodexDialog", "input": {"ask": d["ask"], "label": d["label"], "command": cmd,
                                                             "reason": reason, "body": "\n".join(d["body"])}}, []


QBOX_FOOT_RE = re.compile(r"\benter submit\b.*\^\]\s*skip")     # 提问框展开时的底部提示
QCOUNT_RE = re.compile(r"^\?\s+(\d+)\s+questions?\b")              # 收起时：「? 2 questions · 20s」
QPOS_RE = re.compile(r"^(\d+) of (\d+)$")
QREPLY_TAG = "<send_user_message_question_reply>"


def _norm(text):
    return re.sub(r"\s+", "", text or "")


def question_box(rows):
    """codex 一次问你几道题（request_user_input）时屏幕底部的提问框；没有返回 None。
    收起时（光标在主输入框，它可能还在接着干）只有一个提示，返回 {"open": False, "left": 还剩几道}：
        ? 2 questions · 20s
          shift+← to answer
    展开时一次显示一道，按数字键就选上并交上去，最后一项 Other 可以打字：
        1 of 2
        测试题一：选哪种颜色？
        › 1. 红色
          2. 绿色
          3. Other
        enter submit   ^] skip   shift+→ main prompt   shift+← next question
    返回 {"open": True, "left", "pos", "text": 题目那几行, "options": [(编号, 文字)], "cursor",
    "first": 这是第一道（再按 shift+→ 就回主输入框）, "next": 后面还有（shift+← 下一道）}。只剩一道时没有「1 of 1」那行。"""
    tail = [r.strip() for r in rows if r.strip()][-40:]
    foot = next((i for i in range(len(tail) - 1, max(len(tail) - 4, -1), -1) if QBOX_FOOT_RE.search(tail[i])), None)
    if foot is None:
        near = tail[-14:]
        for i, r in enumerate(near):
            m = QCOUNT_RE.match(r)
            if m and any("to answer" in x for x in near[i:i + 3]):
                return {"open": False, "left": int(m.group(1))}
        return None
    opts, cursor, more, i = [], None, [], foot - 1
    while i >= 0:                                        # 从底往上读选项；选项太长折行的，接到上面那一项后面
        m = OPTION_RE.match(tail[i])
        if m:
            opts.insert(0, [int(m.group(1)), " ".join([m.group(2).strip()] + more)])
            more = []
            if tail[i].startswith("›"):
                cursor = int(m.group(1))
            if opts[0][0] == 1:
                break
        elif opts and not more:
            more.insert(0, tail[i])
        else:
            break
        i -= 1
    if not opts or [n for n, _ in opts] != list(range(1, len(opts) + 1)):
        return None
    text, pos, left = [], 1, 1
    for r in reversed(tail[max(0, i - 6):i]):
        m = QPOS_RE.match(r)
        if m:
            pos, left = int(m.group(1)), int(m.group(2))
            break
        if r.startswith(("•", "›", "└", "─")):
            break
        text.insert(0, r)
    footer = tail[foot]
    return {"open": True, "left": left, "pos": pos, "text": " ".join(text), "options": [tuple(o) for o in opts], "cursor": cursor,
            "first": "main prompt" in footer, "next": "next question" in footer}


def asked_questions(path, size=4 << 20, answered=False):
    """对话记录里它用 request_user_input 问你、还没收到回答的题，按问的先后：[{"call", "idx", "title", "options": [文字], "notes": [说明]}]。
    answered=True 时返回 (没答的, 答了的)。
    你在框里答的，作为一条「<send_user_message_question_reply>」消息写进对话记录（它在干活时要等它下一步才写进去）；
    跳过（^]）的什么都不写——所以还要跟屏幕上剩几道对一下（open_questions）。"""
    asked, done, sync = [], set(), {}
    if not path or not os.path.exists(path):
        return []
    for line in _tail_lines(path, size):
        if "request_user_input" not in line and "question_reply" not in line and not any(c in line for c, y in sync.items() if y):
            continue                                     # 只解析跟提问有关的行（对话记录很大，每几秒就要看一次）
        try:
            e = json.loads(line)
        except ValueError:
            continue
        p = _payload(e)
        kind = p.get("type")
        if kind == "function_call" and str(p.get("name", "")).startswith("request_user_input"):
            try:
                args = json.loads(p.get("arguments") or "{}")
            except ValueError:
                continue
            call = p.get("call_id") or ""
            sync[call] = p["name"] == "request_user_input"           # 不带 _async 的：答完才回结果
            for i, q in enumerate(args.get("questions") or []):
                if not isinstance(q, dict):
                    continue
                opts = [o if isinstance(o, str) else (o or {}).get("label", "") for o in q.get("options") or []]
                notes = ["" if isinstance(o, str) else (o or {}).get("description", "") for o in q.get("options") or []]
                asked.append({"call": call, "idx": i, "title": q.get("title") or q.get("question") or q.get("header") or "",
                              "options": opts, "notes": notes})
        elif kind == "function_call_output" and sync.get(p.get("call_id")):
            done |= {(q["call"], q["idx"]) for q in asked if q["call"] == p.get("call_id")}
        elif kind == "message" and p.get("role") == "user" and QREPLY_TAG in _text(p):
            body = _text(p).split(QREPLY_TAG, 1)[1].split("</send_user_message_question_reply>", 1)[0]
            try:
                items = json.loads(body)
            except ValueError:
                continue
            for it in items if isinstance(items, list) else []:
                try:
                    _, call, idx = json.loads(it.get("questionItemId") or "[]")
                    done.add((call, int(idx)))
                except (ValueError, TypeError):
                    continue
    todo = [q for q in asked if (q["call"], q["idx"]) not in done]
    return (todo, [q for q in asked if (q["call"], q["idx"]) in done]) if answered else todo


def open_questions(s, rows):
    """提问框里还开着的题。题目、选项从对话记录里取（屏幕上的可能折行、被截断），跟提问框对上：
    已经在框里答了、排着队还没写进对话记录的（屏幕上有「› > 题目」那条回显）去掉；还多的（跳过的）只留最后几道。
    没有提问框返回 None；对话记录里找不到题的，用屏幕上展开的那一道。返回 {"box", "questions"}。"""
    box = question_box(rows)
    if not box:
        return None
    shown = _norm("\n".join(rows))
    todo, done = asked_questions(s.get("transcript"), answered=True)
    extra = {}                                           # 屏幕上的回显比对话记录里的回答多几条：多的那几条是排着队、还没写进去的
    for q in todo:
        key = _norm(q["title"])
        if key not in extra:
            extra[key] = shown.count(">" + key) - sum(_norm(x["title"]) == key for x in done)
    qs = []
    for q in todo:
        key = _norm(q["title"])
        if extra[key] > 0:
            extra[key] -= 1
            continue
        qs.append(q)
    qs = qs[-box["left"]:] if box["left"] else qs        # 跳过的题对话记录里没有痕迹：只留最近的那几道
    if not qs and box["open"]:
        opts = [label for _, label in box["options"][:-1]]
        qs = [{"call": "", "idx": 0, "title": box["text"], "options": opts, "notes": [""] * len(opts)}]
    return {"box": box, "questions": qs}


def answer_question(s, title, k=None, label="", text=None, wait=lambda: time.sleep(0.3)):
    """在提问框里答一道题，像在电脑上一样按键；每按一下都重新看屏幕核对，对不上就停手，不乱按。
    收起的先按 shift+← 打开 → 翻到这道题（shift+→ 往前、shift+← 往后）→ 核对第 k 项就是 label → 按数字键 k（选上并交上去）；
    text 不为 None 时挪到最后的 Other，打这段字再回车。原来是收起的，答完还有别的题就按 shift+→ 收回主输入框。
    返回 (成功, 没成功的原因)。"""
    want = _norm(title)[:30]
    hit = lambda b: bool(b and b["open"] and want and want in _norm(b["text"]))

    def look(pred, n=10):
        for _ in range(n):
            b = question_box(pane_rows(s))
            if b and pred(b):
                return b
            wait()
        return None

    box = question_box(pane_rows(s))
    if not box:
        return False, "屏幕上已经没有提问框了"
    was_open = box["open"]
    if not was_open:
        crterm.key(s, "S-Left")
        box = look(lambda b: b["open"])
        if not box:
            return False, "按 shift+← 没打开提问框"
    for _ in range(box["left"] + 1):                     # 先往前翻（停在第一道或就是这道）
        if box["first"] or hit(box):
            break
        pos = box["pos"]
        crterm.key(s, "S-Right")
        box = look(lambda b: b["open"] and b["pos"] != pos)
        if not box:
            return False, "往前翻题时提问框变了"
    for _ in range(box["left"] + 1):                     # 再往后找
        if hit(box) or not box["next"]:
            break
        pos = box["pos"]
        crterm.key(s, "S-Left")
        box = look(lambda b: b["open"] and b["pos"] != pos)
        if not box:
            return False, "往后翻题时提问框变了"
    if not hit(box):
        return False, "提问框里没找到这道题（可能已经答过或跳过了）"
    opts = dict(box["options"])
    last = max(opts)
    if text is not None:
        if opts[last] != "Other":
            return False, f"最后一项不是 Other（屏幕上是「{opts[last]}」）"
        k = last
    elif k not in opts or k == last or _norm(opts[k]) != _norm(label):
        return False, f"第 {k} 项对不上（屏幕上是「{opts.get(k, '')}」）"
    if text is None and k <= 9:
        crterm.literal(s, str(k))                        # 数字键：选上并直接交上去
    else:
        for _ in range(len(opts) + 1):                   # 光标一格格挪到第 k 项
            cur = box["cursor"]
            if cur == k:
                break
            crterm.key(s, "Down" if cur is None or cur < k else "Up")
            box = look(lambda b: hit(b) and b["cursor"] != cur)
            if not box:
                return False, "挪光标时提问框变了"
        if box["cursor"] != k:
            return False, "光标没挪到这一项"
        if text is not None:
            crterm.literal(s, " ".join(text.split()))
            if not look(lambda b: hit(b) and _norm(text)[:10] in _norm(dict(b["options"]).get(k, ""))):
                return False, "Other 那一项里没看到打进去的字，没有交"
        crterm.key(s, "Enter")
    for _ in range(15):
        wait()
        box = question_box(pane_rows(s))
        if not hit(box):
            break
    else:
        return False, "按了键，这道题还在框里"
    if not was_open:                                     # 原来在主输入框：还有别的题就收回去（第一道上按 shift+→ 回主输入框，别的是往前一道）
        for _ in range(8):
            if not box or not box["open"]:
                break
            pos = box["pos"]
            crterm.key(s, "S-Right")
            for _ in range(6):
                wait()
                box = question_box(pane_rows(s))
                if not box or not box["open"] or box["pos"] != pos:
                    break
    return True, ""


def menu(rows):
    """codex 的菜单（/model、/resume…）：跟 crcore.screen_menu 一个格式 {"items", "cursor", "keys", "sig"}；没有返回 None。
    底部提示是「enter select · esc back」「enter default · s session · esc back」这种。"""
    tail = [r for r in rows if r][-40:]
    foot = footer = None
    for i in range(len(tail) - 1, max(len(tail) - 25, -1), -1):
        if i >= len(tail) - 3 and (DIALOG_FOOT in tail[i] or re.fullmatch(f"(?:{FOOT_SEG})(?:\\s+·\\s+{FOOT_SEG})*", tail[i])):
            foot, footer = i, tail[i]
            break
        m = POPUP_FOOT_RE.search(tail[i])                # 弹在屏幕中间的框，输入框还在它下面（比如接回有暂停目标的对话时「Resume paused goal?」）
        if m:
            foot, footer = i, m.group(1)
            break
    if foot is None:
        return None
    items, cursor, seen = [], None, []
    for r in tail[max(0, foot - 30):foot]:
        m = OPTION_AT_RE.search(r)                       # 中间弹的框左边可能还露着对话内容：编号项不一定在行首
        if m:
            n = int(m.group(2))
            if n == 1:
                items, cursor, seen = [], None, []
            items.append((n, re.split(r"\s{2,}", m.group(3).strip())[0]))
            seen.append(r)
            if m.group(1):
                cursor = n
    if not items:
        return None
    keys = []
    if DIALOG_FOOT in footer:
        keys = [("Enter", "回车：确认"), ("Escape", "Esc：取消")]
    else:
        names = {"enter": ("Enter", "回车"), "esc": ("Escape", "Esc"), "tab": ("Tab", "Tab"), "space": ("Space", "空格")}
        desc_cn = {"select": "选中", "back": "返回", "default": "设为默认", "session": "只用于这个会话", "cancel": "取消",
                   "confirm": "确认", "close": "关闭", "toggle": "勾选", "submit": "提交"}
        for seg in footer.split("·"):
            m = re.match(r"\s*(\S+)\s+(.+?)\s*$", seg)
            if not m:
                continue
            k, desc = m.group(1).lower(), desc_cn.get(m.group(2).lower(), m.group(2))
            if k in names:
                keys.append((names[k][0], f"{names[k][1]}：{desc}"))
            elif re.fullmatch(r"[a-z]", k):
                keys.append((k, f"{k}：{desc}"))
    sig = hashlib.md5("\n".join(seen + [footer]).encode()).hexdigest()[:8]
    return {"items": items, "cursor": cursor, "keys": keys, "sig": sig}


def _composer(arows):
    """输入框那一行的位置：粗体的「›」（你以前说的话前面的「›」是灰的）。停在批准框 / 菜单上时没有输入框，返回 None。"""
    plain = [re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", r).strip() for r in arows]
    if dialog(plain) or menu(plain):
        return None, plain
    for i in [i for i, r in enumerate(plain) if r][-30:][::-1]:   # 刚起来时内容在屏幕上半截，下面全是空行：只数有字的行
        if arows[i].startswith("\x1b[1m›"):
            return i, plain
    return None, plain


def box_text(s):
    """输入框里还没发出去的字；空的（只有灰色提示语）、找不到输入框返回 ""。"""
    arows = pane_rows(s, ansi=True)
    i, plain = _composer(arows)
    if i is None or re.match(r"\x1b\[1m›\x1b\[0m(?:\x1b\[[0-9;]*m)*\s*\x1b\[2m", arows[i]):
        return ""
    lines = [plain[i][1:].strip()]
    for r in plain[i + 1:]:
        if not r:
            break
        lines.append(r)
    return "\n".join(x for x in lines if x).strip()


def ready(s):
    """能接话了：输入框在、没停在框上、不是还在「Resuming session…」。"""
    i, plain = _composer(pane_rows(s, ansi=True))
    return i is not None and not any(r.startswith(("Resuming session", "Starting")) for r in [r for r in plain if r][-12:])


def working(rows):
    """「• Working (20m 32s • esc to interrupt)」：在干活时屏幕上的那一行，返回括号里的时间；没有返回 ""。"""
    for r in reversed([r for r in rows if r][-12:]):
        m = re.match(r"^•\s+\w+.*\((\d[^•)]*?)\s*•\s*esc to interrupt\)", r)
        if m:
            return m.group(1)
    return ""


def slash_items(rows):
    """打「/」弹出的补全：「› /model         choose what model…」「  /fast          2x speed…」。"""
    out = []
    for r in rows:
        m = re.match(r"^(?:›\s*)?(/[\w:.\-]+)\s{2,}(.*)$", r)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def set_effort(s, level, wait=lambda: time.sleep(0.3)):
    """只改这一个会话的思考程度：/model → 回车（还是当前模型）→ 选档位 → 按 s（只用于这个会话，不改默认）。
    改好了返回 True；菜单没按预期出来就按 Esc 退出来，返回 False。"""
    want = EFFORT_LABEL.get(level)
    if not want:
        return False

    def until(pred, n=20):
        for _ in range(n):
            wait()
            rows = pane_rows(s)
            if pred(rows):
                return rows
        return None

    def bail():
        """没按预期走：菜单开着就按 Esc 一层层退出来（菜单里的 Esc 是返回，不会打断它）；输入框里剩下「/model」就清掉。"""
        for _ in range(3):
            if not menu(pane_rows(s)):
                break
            crterm.key(s, "Escape")
            time.sleep(0.4)
        if box_text(s).startswith("/model"):
            crterm.key(s, "C-c")
        return False

    if box_text(s) or not ready(s):
        return False                                     # 输入框里有你没发出去的字、或停在框上（回车会替你选）：不动它
    crterm.literal(s, "/model")
    time.sleep(0.4)
    crterm.key(s, "Enter")
    rows = until(lambda r: any(x.startswith("Select Model") for x in r) and menu(r))
    if not rows:
        return bail()
    m = menu(rows)
    cur = next((n for n, label in m["items"] if "(current)" in label), m["cursor"])
    for _ in range(abs((cur or 1) - (m["cursor"] or 1))):
        crterm.key(s, "Down" if cur > m["cursor"] else "Up")
        time.sleep(0.08)
    crterm.key(s, "Enter")
    rows = until(lambda r: any(x.startswith("Select Reasoning Level") for x in r) and menu(r))
    if not rows:
        return bail()
    m = menu(rows)
    target = next((n for n, label in m["items"] if re.sub(r"\s*\(.*?\)\s*$", "", label).strip().lower() == want.lower()), None)
    if target is None or m["cursor"] is None:
        return bail()
    for _ in range(abs(target - m["cursor"])):
        crterm.key(s, "Down" if target > m["cursor"] else "Up")
        time.sleep(0.08)
    session_key = next((k for k, label in m["keys"] if label.endswith("只用于这个会话")), "Enter")
    crterm.key(s, session_key)
    return bool(until(lambda r: not menu(r), 15))


# ---------------------------------------------------------------- 启动 / 上班

def launch_argv(home, cwd, args, proxy=""):
    """在 tmux 里跑的那条命令：bin/codex-inner.sh 先 source 这套 codex 的环境脚本（没有就不 source），再在目录里跑 codex。
    带 --no-daemon：干活的部分也在这个窗格的 codex 进程里，不交给每套 CODEX_HOME 共用的后台服务——
    那个服务是第一个打开的 codex 顺手起的，一直带着那个终端的环境（比如你 ssh 进来开的 tmux）。"""
    info = home_info(home)
    argv = [os.path.join(ROOT, "bin", "codex-inner.sh")]
    if proxy:
        argv += ["--proxy", f"http://{proxy}"]
    return argv + ["--home", info["home"], info["env"] or "-", cwd] + OWN_ARGS + _drop_own(list(args))


def check(home):
    """启动前检查这套 codex：环境脚本在、加载后找得到 codex。有问题返回原因，没问题返回 ""。"""
    info = home_info(home)
    if info["env"] and not os.path.isfile(info["env"]):
        return f"找不到这套 codex 的环境脚本：{info['env']}"
    line = (f"source {shlex.quote(info['env'])} >/dev/null 2>&1; " if info["env"] else "") + "command -v codex"
    try:
        r = subprocess.run(["bash", "-c", line], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return f"检查 codex 时出错：{e}"
    if r.returncode != 0 or not r.stdout.strip():
        return "加载环境后找不到 codex 命令" + (f"（{info['env']}）" if info["env"] else "")
    return ""


def resume_args(e):
    """上班：codex resume <会话 id> + 原来的参数；没有对话记录就开新的。"""
    args = list(e.get("args") or [])
    sid = e.get("sessionId")
    if sid and rollout_path(sid, e.get("home")):
        return ["resume", sid] + args
    return args


def _same_dir(a, b):
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


def _daemon_procs(home):
    """这套 codex 共用的后台服务（codex app-server …）的进程。"""
    out = []
    me = psutil.Process().username()
    for p in psutil.process_iter(["name", "cmdline", "username"]):
        try:
            cmd = p.info["cmdline"] or []
            if p.info["name"] == "codex" and p.info["username"] == me and cmd[1:2] == ["app-server"] \
                    and _same_dir(p.environ().get("CODEX_HOME"), home):
                out.append(p)
        except psutil.Error:
            continue
    return out


def _held(procs):
    files = set()
    for p in procs:
        try:
            files |= {f.path for f in p.open_files() if "rollout-" in f.path}
        except psutil.Error:
            continue
    return files


def free_thread(e):
    """上班前：你自己开的 codex 用的是共用的后台服务，退出后服务还开着这段对话，
    机器人开的 codex（--no-daemon）就打不开它（屏幕上是「This conversation is open in another app」）。
    后台服务闲着（没有别的 codex 连着、开着的对话都没在跑）就把它关掉，下回你自己开 codex 时它会自动再起。
    返回 (能不能上班, 说明)。"""
    home = e.get("home") or e.get("cfg")
    path = rollout_path(e.get("sessionId"), home)
    procs = _daemon_procs(home) if path else []
    held = _held(procs)
    if path not in held:
        return True, ""
    if any(turn_state(h)[0] == "busy" for h in held):
        return False, "这段对话还开在 codex 的后台服务里，后台服务里还有活在跑，关不得。等那边停下再上班。"
    for p in _tui_procs():
        try:
            if "--no-daemon" not in p.cmdline() and _same_dir(p.environ().get("CODEX_HOME"), home):
                return False, "这段对话还开在 codex 的后台服务里，还有你自己开的 codex 连着它，关不得。退出那边的 codex 再上班。"
        except psutil.Error:
            continue
    info = home_info(home)
    line = (f"source {shlex.quote(info['env'])} >/dev/null 2>&1; " if info["env"] else "") + \
        f"export CODEX_HOME={shlex.quote(info['home'])}; codex app-server daemon stop"
    try:
        subprocess.run(["bash", "-c", line], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as ex:
        return False, f"关 codex 后台服务时出错：{ex}"
    for _ in range(10):
        if path not in _held(_daemon_procs(home)):
            return True, "这段对话之前挂在 codex 的后台服务里（你自己开 codex 时起的，带着那个终端的环境），服务闲着，已把它关掉。"
        time.sleep(1)
    return False, "codex 后台服务没关掉，这段对话还被它占着。"


# ---------------------------------------------------------------- 可选：PermissionRequest hook

HOOK_DESC = "cc-crew：要你批准时记下要批准的是什么，飞书卡片上显示完整命令。只记录，不做决定（不输出、总是 exit 0）"


def hook_command():
    py = shutil.which("python3") or "python3"
    return f"{shlex.quote(py)} {shlex.quote(os.path.join(ROOT, 'bin', 'cr-hook'))}"


def install_hooks():
    """在每套 codex 的 CODEX_HOME 下放一个 hooks.json（已有的话合并进去，原文件先备份）。
    codex 要你在 /hooks 里看过、点了信任才会跑它；不信任也没关系，机器人照样从屏幕上认批准框。返回每套的结果。"""
    out = []
    for h in homes():
        path = os.path.join(h["home"], "hooks.json")
        try:
            cur = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        except (OSError, ValueError) as e:
            out.append(f"{h['label'] or h['home']}：{path} 读不了（{e}），没动它")
            continue
        groups = cur.setdefault("hooks", {}).setdefault("PermissionRequest", [])
        cmd = hook_command()
        if any(x.get("command") == cmd for g in groups for x in g.get("hooks", [])):
            out.append(f"{h['label'] or h['home']}：已经装过了")
            continue
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak-cc-crew")
        cur.setdefault("description", HOOK_DESC)
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
        out.append(f"{h['label'] or h['home']}：已写入 {path}，在 codex 里打 /hooks 信任一下才会生效")
    return out
