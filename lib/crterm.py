"""终端后端：机器人怎么看到 Claude 的屏幕、怎么替你打字和按键、怎么开新会话。

两种后端，crcore 只调这里的函数，不直接碰 tmux：
- tmux（Linux / macOS）：Claude Code 跑在 tmux 里时，会话文件里有 "tmux" 字段，用 tmux 读屏、打字。
- ccm（Windows / macOS / Linux）：用 `ccm` 代替 `claude` 启动，ccm 在伪终端里跑 claude，
  界面和直接跑一样，同时开一个本机端口给机器人读屏、打字（见 ccterm.py）。
两个都不是的会话（直接在终端里打 claude）：照样能收到进展和回复，只是不能远程打字。
"""
import os
import re
import shutil
import subprocess
import sys
import time

import ccterm

IS_WIN = os.name == "nt"
PRIVATE_SOCK = os.environ.get("CLAUDE_MONITOR_TMUX_SOCK") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmux.sock")   # 以前机器人开的会话放这个 socket（tmux ls 看不到）；现在只认，不再往里开
USER_SOCK = os.environ.get("CLAUDE_MONITOR_LAUNCH_SOCK") or (None if IS_WIN else os.path.join(
    os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", "default"))   # 招来、叫回的员工开在你平常用的 tmux 里
PREFIX = os.environ.get("CLAUDE_MONITOR_TMUX_PREFIX", "")   # 机器人开的会话名前面加上它（比如 me-）：共用机器上别人一看就知道是你的
TAG = "@cc-crew"                                         # 机器人开的窗格上记着员工名字：下次上班回到同一个窗口
SHELLS = ("bash", "zsh", "sh", "fish", "dash", "ksh")


class NoTerminal(RuntimeError):
    """这个会话不是在 tmux / ccm 里跑的，不能远程打字。"""


# ---------------------------------------------------------------- tmux

def tmux(sock, *args, check=False):
    cmd = ["tmux"] + (["-S", sock] if sock else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "tmux 失败")
    return r.stdout


def _tmux_socket(pid):
    """claude 进程环境里的 TMUX=socket,pid,idx。"""
    try:
        import psutil
        val = psutil.Process(pid).environ().get("TMUX", "")
        return val.split(",")[0] or None
    except Exception:
        return None


# ---------------------------------------------------------------- 找到会话的终端

def attach_info(pid, sess):
    """会话文件（~/.claude/sessions/<pid>.json）→ 它的终端信息，合进 discover() 的结果里。
    返回 {"term": "tmux"|"ccm"|"none", "group": 分组名（tmux 会话名 / 目录名）, "window": 同组里的序号, "title": 终端标题, ...}"""
    if sess.get("tmux"):
        sock = _tmux_socket(pid)
        pane = sess["tmux"].rsplit(".", 1)[-1]           # "exp-a:@7.%7" -> "%7"
        info = tmux(sock, "display", "-p", "-t", pane,
                    "#{session_name}\t#{window_index}\t#{pane_current_path}\t#{" + TAG + "}\t#{pane_title}").strip()
        if not info:
            return None
        group, widx, _, tag, title = (info.split("\t", 4) + [""] * 4)[:5]
        return {"term": "tmux", "socket": sock, "pane": pane, "group": group, "window": widx, "title": title,
                "tag": tag, "private": sock == PRIVATE_SOCK or bool(tag)}
    rec = ccterm.find(pid)
    if rec:
        group = rec.get("name") or os.path.basename(os.path.normpath(rec.get("cwd") or sess.get("cwd") or "")) or "claude"
        return {"term": "ccm", "ccm": rec, "group": group, "window": str(rec.get("pid")), "title": rec.get("title") or "",
                "private": bool(rec.get("private"))}
    group = os.path.basename(os.path.normpath(sess.get("cwd") or "")) or "claude"
    return {"term": "none", "group": group, "window": str(pid), "title": "", "private": False}


def controllable(s):
    return s.get("term", "tmux" if s.get("pane") else "none") in ("tmux", "ccm")


def _kind(s):
    k = s.get("term") or ("tmux" if s.get("pane") else "none")
    if k == "none":
        raise NoTerminal("这个会话是直接在终端里打 claude 开的，机器人只能看、不能替你打字。"
                         "以后用 ccm（或在 tmux 里）启动它，就能远程打字了。")
    return k


# ---------------------------------------------------------------- 读屏、打字、按键

def rows(s, ansi=False):
    """当前屏幕的每一行（去掉右边空白）。ansi=True 时淡色字（输入框里的「下一句建议」）用 ESC[2m 包着。"""
    k = s.get("term") or ("tmux" if s.get("pane") else "none")
    if k == "tmux":
        out = tmux(s["socket"], "capture-pane", "-p", *(["-e"] if ansi else []), "-t", s["pane"])
        return [r.rstrip() for r in out.splitlines()]
    if k == "ccm":
        try:
            return ccterm.screen_rows(s["ccm"], ansi=ansi)
        except OSError:
            return []
    return []


def key(s, name):
    """按一个键，名字用 tmux 的写法：Enter Escape Tab BTab Up Down Left Right C-c Space BSpace C-End。"""
    if _kind(s) == "tmux":
        tmux(s["socket"], "send-keys", "-t", s["pane"], name, check=True)
    else:
        seq = ccterm.key_seq(name)
        if seq is None:
            raise ValueError(f"不认识的键：{name}")
        ccterm.send(s["ccm"], seq)


def literal(s, text):
    """原样打几个字（不回车）。"""
    if _kind(s) == "tmux":
        tmux(s["socket"], "send-keys", "-t", s["pane"], "-l", "--", text, check=True)
    else:
        ccterm.send(s["ccm"], text)


def paste(s, text):
    """像在终端里粘贴一样（多行不会被当成回车发出去）。"""
    if _kind(s) == "tmux":
        subprocess.run(["tmux"] + (["-S", s["socket"]] if s["socket"] else []) +
                       ["load-buffer", "-b", "cc-crew", "-"], input=text, text=True, check=True)
        tmux(s["socket"], "paste-buffer", "-p", "-d", "-b", "cc-crew", "-t", s["pane"], check=True)
    else:
        ccterm.paste(s["ccm"], text)


def close_group(s):
    """机器人自己开的会话退出后，把空壳收掉（tmux 里留下的 shell）。ccm 的窗口会随 claude 退出自己关。"""
    if s.get("term", "tmux") != "tmux" or not s.get("private"):
        return
    if s.get("socket") == PRIVATE_SOCK and s.get("group"):
        tmux(s["socket"], "kill-session", "-t", f"={s['group']}")
    elif s.get("tag") and _spare(s["socket"], s["pane"]):
        tmux(s["socket"], "kill-pane", "-t", s["pane"])  # 在你平常用的 tmux 里：只收它自己那个窗格，不动别的窗口


# ---------------------------------------------------------------- 开新会话

def launch_mode():
    """机器人新开 / 叫回一个会话时用什么：tmux（服务器，看不见但一直在）或 window（桌面上弹一个终端窗口，用 ccm 跑）。"""
    mode = os.environ.get("CLAUDE_MONITOR_LAUNCH_MODE", "")
    if mode in ("tmux", "window"):
        return mode
    if IS_WIN or sys.platform == "darwin":
        return "window"
    return "tmux" if shutil.which("tmux") else "window"


def _ok(sock, *args):
    return subprocess.run(["tmux", "-S", sock] + list(args), capture_output=True, timeout=15).returncode == 0


def _tagged(sock, name):
    """sock 这个 tmux 里记着员工 name 的窗格，新开的在后面。"""
    rows = (r.split("\t", 1) for r in tmux(sock, "list-panes", "-a", "-F", "#{pane_id}\t#{" + TAG + "}").splitlines())
    return sorted((r[0] for r in rows if len(r) == 2 and r[1] == name), key=lambda p: int(p.lstrip("%") or 0))


def find_tagged(name):
    """员工 name 最近一次开在哪：(socket, 窗格)，没有返回 None。"""
    for sock in (USER_SOCK, PRIVATE_SOCK):
        panes = _tagged(sock, name) if sock and os.path.exists(sock) else []
        if panes:
            return sock, panes[-1]
    return None


def _spare(sock, target):
    """target（窗格 / 窗口）上只是个闲着的 shell（没在跑任何程序）：返回它的窗格号，可以换成员工；不然返回 None。"""
    out = tmux(sock, "display", "-p", "-t", target, "#{pane_id}\t#{pane_current_command}\t#{pane_pid}").strip().split("\t")
    if len(out) != 3 or out[1] not in SHELLS:
        return None
    try:
        import psutil
        return None if psutil.Process(int(out[2])).children() else out[0]
    except Exception:
        return None


def free_session(name):
    """你平常用的 tmux 里还没有的会话名：PREFIX + 员工名（「:」「.」换成「-」），重名就加 -2、-3。"""
    base = re.sub(r"[:.]", "-", name)
    if not base.startswith(PREFIX):
        base = PREFIX + base
    sess, i = base, 2
    while USER_SOCK and _ok(USER_SOCK, "has-session", "-t", f"={sess}"):
        sess, i = f"{base}-{i}", i + 1
    return sess


def running(name):
    """员工 name 上次开的窗格里 claude / codex 还在跑：返回是哪个，不然返回 ""。"""
    spot = find_tagged(name)
    if not spot:
        return ""
    try:                                                 # 经启动脚本跑的，窗格前台显示的是 bash：看整棵进程树
        import psutil
        top = psutil.Process(int(tmux(spot[0], "display", "-p", "-t", spot[1], "#{pane_pid}").strip()))
        names = {p.name() for p in [top] + top.children(recursive=True)}
    except Exception:
        return ""
    return "claude" if "claude" in names else "codex" if names & {"codex", "node"} else ""   # npm 装的 codex 是 node


def place(tmp, name, label="", where=None):
    """刚开好的 tmux 会话 tmp（一个窗格）挪到员工 name 该在的地方，窗格上记下名字。返回「在哪」的说明，找不到 tmp 返回 ""。
    - 以前开过的：回到上次那个窗格的位置（那里只剩空 shell 就换掉它）；
    - 知道它原来在你 tmux 里哪个会话的哪个窗口（where，登记里记的；没记就看名字是不是「会话:窗口号」，比如 exp-a:4），
      那个会话还在：放回那个窗口号，
      那个窗口只剩空 shell 就换掉，被别的程序占着就排到这个会话最后；
    - 别的：就是 tmp 这个会话（名字已经是员工名，重名时带 -2）。
    label：窗口名（codex / claude）。员工经启动脚本跑，tmux 自动起的名字是「bash」，在窗口栏上认不出来。"""
    sock = USER_SOCK
    if not sock or not _ok(sock, "has-session", "-t", f"={tmp}"):
        if os.path.exists(PRIVATE_SOCK) and _ok(PRIVATE_SOCK, "has-session", "-t", f"={tmp}"):   # 老的启动脚本还开在私有 tmux 里：只记名字
            tmux(PRIVATE_SOCK, "set-option", "-p", "-t", f"={tmp}:", TAG, name)
        return ""
    new = tmux(sock, "display", "-p", "-t", f"={tmp}:", "#{pane_id}").strip()
    spot = None
    for old in _tagged(sock, name):
        spot = spot or _spare(sock, old)
        if old != spot:
            tmux(sock, "set-option", "-p", "-u", "-t", old, TAG)    # 那里你已经在用了：不再算它的
    tmux(sock, "set-option", "-p", "-t", new, TAG, name)
    m = re.fullmatch(r"(.+):(\d+)", name)
    home = tuple(where) if where and all(where) else m.groups() if m else None   # 原来在哪：登记里记的，或者名字里带的
    if not spot and home and _ok(sock, "has-session", "-t", f"={home[0]}"):
        sess, idx = home
        if not _ok(sock, "list-panes", "-t", f"={sess}:{idx}"):
            tmux(sock, "move-window", "-s", f"={tmp}:", "-t", f"={sess}:{idx}")
        else:
            spot = _spare(sock, f"={sess}:{idx}")
            if not spot:
                tmux(sock, "move-window", "-s", f"={tmp}:", "-t", f"={sess}:")
    if spot:
        tmux(sock, "swap-pane", "-d", "-s", new, "-t", spot)
        tmux(sock, "kill-session", "-t", f"={tmp}")      # tmp 里现在是换下来的那个空 shell
    sess, win, n = (tmux(sock, "display", "-p", "-t", new, "#{session_name}\t#{window_index}\t#{window_panes}").strip().split("\t") + ["", ""])[:3]
    if label and n == "1":                               # 窗口里还有你分出来的别的窗格：不改你的窗口名
        tmux(sock, "rename-window", "-t", new, label)
    return f"开在你平常用的 tmux 里：{sess} 的 {win} 号窗口（tmux attach -t {sess}）"


def start_tmux(name, cwd, argv, env, label="", where=None):
    """在你平常用的 tmux 里开一个窗格跑 argv，再放到员工 name 该在的地方（见 place）。
    tmux 已经开着时新窗格拿的是它自己的环境，所以 env 里跟本进程不同的变量用 -e 带上、去掉的用 env -u 去掉。"""
    busy = running(name)
    if busy:
        return False, f"【{name}】的 {busy} 还在运行"
    tmp = free_session(name)
    extra = [x for k, v in env.items() if os.environ.get(k) != v for x in ("-e", f"{k}={v}")]
    drop = [x for k in os.environ if k not in env for x in ("-u", k)]
    r = subprocess.run(["tmux", "-S", USER_SOCK, "new-session", "-d", "-s", tmp, "-c", cwd, "-x", "220", "-y", "50"]
                       + extra + (["env"] + drop if drop else []) + argv, capture_output=True, text=True, env=env, timeout=20)
    if r.returncode != 0:
        return False, r.stderr.strip() or "tmux 启动失败"
    return True, place(tmp, name, label, where)


def start_window(name, cwd, ccm_argv, env):
    """在桌面上开一个新的终端窗口 / 标签页，里面跑 ccm（ccm 再跑 claude）。"""
    if IS_WIN:
        wt = shutil.which("wt") or shutil.which("wt.exe")
        if wt:                                           # Windows Terminal：在已有窗口里开个新标签页
            cmd = [wt, "-w", "0", "new-tab", "--title", name, "-d", cwd] + ccm_argv
        else:                                            # 没装 Windows Terminal：开一个 PowerShell 窗口
            cmd = ["cmd.exe", "/c", "start", name, "/D", cwd] + ccm_argv
        subprocess.Popen(cmd, cwd=cwd, env=env, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return True, f"已在新的终端标签页「{name}」启动"
    if sys.platform == "darwin":
        import shlex
        line = f"cd {shlex.quote(cwd)} && " + " ".join(shlex.quote(a) for a in ccm_argv)
        script = f'tell application "Terminal" to do script {_applescript_str(line)}'
        r = subprocess.run(["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'],
                           capture_output=True, text=True, env=env, timeout=20)
        return (r.returncode == 0), (f"已在新的终端窗口启动" if r.returncode == 0 else r.stderr.strip())
    for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        exe = shutil.which(term)
        if exe:
            args = [exe, "--"] + ccm_argv if term == "gnome-terminal" else [exe, "-e"] + ccm_argv
            subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True)
            return True, "已在新的终端窗口启动"
    return False, "这台机器上没有 tmux，也找不到能开窗口的终端"


def _applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wait_ready(pid_filter, timeout=20):
    """等新会话出现在 ~/.claude/sessions 里（由调用方的 discover 判断）；这里只提供统一的等待节奏。"""
    end = time.time() + timeout
    while time.time() < end:
        hit = pid_filter()
        if hit:
            return hit
        time.sleep(1)
    return None
