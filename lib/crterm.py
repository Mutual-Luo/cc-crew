"""终端后端：机器人怎么看到 Claude 的屏幕、怎么替你打字和按键、怎么开新会话。

两种后端，crcore 只调这里的函数，不直接碰 tmux：
- tmux（Linux / macOS）：Claude Code 跑在 tmux 里时，会话文件里有 "tmux" 字段，用 tmux 读屏、打字。
- ccm（Windows / macOS / Linux）：用 `ccm` 代替 `claude` 启动，ccm 在伪终端里跑 claude，
  界面和直接跑一样，同时开一个本机端口给机器人读屏、打字（见 ccterm.py）。
两个都不是的会话（直接在终端里打 claude）：照样能收到进展和回复，只是不能远程打字。
"""
import os
import shutil
import subprocess
import sys
import time

import ccterm

IS_WIN = os.name == "nt"
PRIVATE_SOCK = os.environ.get("CLAUDE_MONITOR_TMUX_SOCK") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmux.sock")   # 机器人自己开的会话放这个 socket，别人 tmux ls 看不到


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
                    "#{session_name}\t#{window_index}\t#{pane_current_path}\t#{pane_title}").strip()
        if not info:
            return None
        group, widx, _, title = (info.split("\t") + [""])[:4]
        return {"term": "tmux", "socket": sock, "pane": pane, "group": group, "window": widx, "title": title,
                "private": sock == PRIVATE_SOCK}
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
    if s.get("term", "tmux") == "tmux" and s.get("private") and s.get("group"):
        tmux(s["socket"], "kill-session", "-t", f"={s['group']}")


# ---------------------------------------------------------------- 开新会话

def launch_mode():
    """机器人新开 / 叫回一个会话时用什么：tmux（服务器，看不见但一直在）或 window（桌面上弹一个终端窗口，用 ccm 跑）。"""
    mode = os.environ.get("CLAUDE_MONITOR_LAUNCH_MODE", "")
    if mode in ("tmux", "window"):
        return mode
    if IS_WIN or sys.platform == "darwin":
        return "window"
    return "tmux" if shutil.which("tmux") else "window"


def start_tmux(name, cwd, argv, env):
    """在私有 socket 的 tmux 里开一个会话跑 argv（窗口 220x50）。"""
    if subprocess.run(["tmux", "-S", PRIVATE_SOCK, "has-session", "-t", f"={name}"], capture_output=True).returncode == 0:
        cmds = tmux(PRIVATE_SOCK, "list-panes", "-s", "-t", f"={name}", "-F", "#{pane_current_command}").split()
        if "claude" in cmds or "ccm" in cmds:
            return False, f"tmux 会话 {name} 里的 claude 还在运行"
        tmux(PRIVATE_SOCK, "kill-session", "-t", f"={name}")
    r = subprocess.run(["tmux", "-S", PRIVATE_SOCK, "new-session", "-d", "-s", name, "-c", cwd, "-x", "220", "-y", "50"]
                       + argv, capture_output=True, text=True, env=env, timeout=20)
    if r.returncode != 0:
        return False, r.stderr.strip() or "tmux 启动失败"
    return True, f"已在 tmux 会话 {name} 启动"


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
