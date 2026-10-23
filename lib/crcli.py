"""cr 命令（Windows / macOS / Linux 通用）：

  cr 看看 / cr @3 内容 / cr 下班 3 …   和飞书里说的一样（助手也用它）
  cr start      启动机器人（后台运行，自带看门狗：挂了或卡住自动重启）
  cr stop       停止机器人（不影响任何 claude）
  cr status     机器人在不在跑 + 最近几行日志
  cr 通知 [N]   最近 N 条通知
  cr 授权       把最近一个给机器人发过消息的飞书账号设为唯一允许的人
  cr autostart on|off   开机（登录）自动启动机器人
  cr daemon     前台运行机器人（调试用）

配置在 <安装目录>/config.env（KEY=VALUE，一行一个），启动时读进环境变量；已经在环境里的不覆盖。
"""
import json
import os
import subprocess
import sys
import time

LIB = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LIB)
IS_WIN = os.name == "nt"
CONFIG = os.path.join(ROOT, "config.env")
OLD_CONFIG = os.path.join(ROOT, "feishu.env")            # 老版本的配置文件名


def read_config():
    env = {}
    for path in (OLD_CONFIG, CONFIG):
        try:
            for line in open(path, encoding="utf-8"):
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() and not k.lstrip().startswith("#"):
                    env[k.strip()] = v.strip().strip('"')
        except OSError:
            pass
    return env


def load_config():
    for k, v in read_config().items():
        os.environ.setdefault(k, v)


def set_config(key, value):
    """改 config.env 里的一项（没有就加上），只有自己能读。"""
    path = CONFIG if os.path.exists(CONFIG) or not os.path.exists(OLD_CONFIG) else OLD_CONFIG
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        lines = []
    lines = [l for l in lines if not l.startswith(key + "=")] + [f"{key}={value}"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    if not IS_WIN:
        os.chmod(path, 0o600)


load_config()
sys.path.insert(0, LIB)
import crcore  # noqa: E402

DATA = crcore.DATA
LOGS = os.path.join(ROOT, "logs")
SUPERVISOR_PID = os.path.join(DATA, "supervisor.json")
HEARTBEAT = os.path.join(DATA, "heartbeat")
STARTED = os.path.join(DATA, "daemon-started")
STOPPED = os.path.join(DATA, "daemon-stopped")
WATCHDOG_NOTE = os.path.join(DATA, "watchdog.json")
HANG_SECS = 180          # 心跳这么久没更新就当卡死
GRACE_SECS = 300         # 刚启动的这段时间不算（光加载飞书的库就可能要一分多钟）


def _python(console=False):
    """后台进程用 pythonw（Windows 上不弹黑窗口）；要控制台时用 python。"""
    py = sys.executable
    if IS_WIN:
        d = os.path.dirname(py)
        want = os.path.join(d, "python.exe" if console else "pythonw.exe")
        if os.path.exists(want):
            return want
    return py


def _read_epoch(path):
    """读文件里写的时间（不用文件修改时间：网络盘的时钟可能和本机对不上）。"""
    try:
        with open(path) as f:
            return float("".join(c for c in f.read(30) if c.isdigit() or c == ".") or 0)
    except (OSError, ValueError):
        return 0.0


def _write_epoch(path, t=None):
    with open(path, "w") as f:
        f.write(f"{t or time.time():.0f}\n")


def _alive(info):
    """supervisor.json 里记的进程还活着（并且是同一个进程，不是 pid 被复用）。"""
    import psutil
    try:
        p = psutil.Process(info["pid"])
        return abs(p.create_time() - info.get("created", 0)) < 2 and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, KeyError, TypeError):
        return False


def _spawn_detached(args, log):
    os.makedirs(LOGS, exist_ok=True)
    out = open(log, "ab")
    kw = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT, "cwd": LIB}
    if IS_WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(args, **kw)


# ---------------------------------------------------------------- 看门狗（守护进程的管家）

def supervise():
    """一直运行：把机器人跑起来；它退出了、或心跳 3 分钟没更新（卡死），就重启它。你用 cr stop 停的不管。"""
    import psutil
    me = psutil.Process()
    with open(SUPERVISOR_PID, "w") as f:
        json.dump({"pid": me.pid, "created": me.create_time()}, f)
    log = open(os.path.join(LOGS, "watchdog.log"), "a", encoding="utf-8")

    def note(msg):
        log.write(time.strftime("%F %T ") + msg + "\n")
        log.flush()

    child = None
    while True:
        if os.path.exists(STOPPED):
            if child and child.poll() is None:
                child.terminate()
            note("cr stop：看门狗退出")
            return
        now = time.time()
        if child is None or child.poll() is not None:
            if child is not None:
                planned = now - _read_epoch(STARTED) < 60      # 刚 cr start 过：是你让它重启的，不算挂了
                note(f"机器人退出了（退出码 {child.returncode}），重启" + ("（cr start）" if planned else ""))
                if not planned:
                    _write_watchdog_note(now)
            child = _spawn_detached([_python(), os.path.join(LIB, "crcli.py"), "daemon"], os.path.join(LOGS, "daemon.log"))
            _write_epoch(STARTED)
        else:
            hb, st = _read_epoch(HEARTBEAT), _read_epoch(STARTED)
            if not hb:                                   # 没读到（文件正在被换掉）：下一轮再看，不能当成「从 1970 年起没心跳」
                time.sleep(2)
                hb = _read_epoch(HEARTBEAT) or now
            if now - hb > HANG_SECS and now - st > GRACE_SECS:
                note(f"心跳 {now - hb:.0f} 秒没更新，重启机器人")
                _write_watchdog_note(now)
                _kill_tree(child.pid)
                child.wait(timeout=30)
                child = None                             # 下一轮直接重启，不再记一次「退出了」
                continue
        time.sleep(20)


def _write_watchdog_note(now):
    hb, st = _read_epoch(HEARTBEAT), _read_epoch(STARTED)
    with open(WATCHDOG_NOTE, "w") as f:
        json.dump({"t": now, "down_since": max(hb, st) or now}, f)


def _kill_tree(pid):
    import psutil
    try:
        p = psutil.Process(pid)
        procs = p.children(recursive=True) + [p]
    except psutil.Error:
        return
    for q in procs:
        try:
            q.terminate()
        except psutil.Error:
            pass
    _, left = psutil.wait_procs(procs, timeout=10)
    for q in left:
        try:
            q.kill()
        except psutil.Error:
            pass


ME = os.path.normcase(os.path.abspath(__file__))


def _daemon_pids():
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        cmd = p.info.get("cmdline") or []
        if ME in (os.path.normcase(os.path.abspath(c)) for c in cmd) and "daemon" in cmd and p.pid != os.getpid():   # 只认本安装目录的（同一个账号下可能还有别人装的）
            out.append(p.pid)
    return out


def start(boot=False):
    """（重新）启动机器人。boot=True 是开机自启：要是上次停了很久（关机），启动后告诉你停了多久。"""
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    if os.path.exists(STOPPED):
        os.remove(STOPPED)
    hb = _read_epoch(HEARTBEAT)
    if boot and hb and time.time() - hb > GRACE_SECS and not os.path.exists(WATCHDOG_NOTE):
        with open(WATCHDOG_NOTE, "w") as f:
            json.dump({"t": time.time(), "down_since": hb, "boot": True}, f)
    sup = crcore.load_json(SUPERVISOR_PID, None)
    _write_epoch(STARTED)
    for pid in _daemon_pids():                           # 先停掉正在跑的机器人；看门狗还活着的话它会马上再起一个
        _kill_tree(pid)
    if sup and _alive(sup):
        return "机器人已重启（看门狗在跑，日志：logs/daemon.log）"
    _spawn_detached([_python(), os.path.join(LIB, "crcli.py"), "supervise"], os.path.join(LOGS, "watchdog.log"))
    return "机器人已在后台启动，带看门狗（日志：logs/daemon.log）"


def stop():
    with open(STOPPED, "w") as f:
        f.write(f"{time.time():.0f}\n")
    sup = crcore.load_json(SUPERVISOR_PID, None)
    killed = False
    if sup and _alive(sup):
        _kill_tree(sup["pid"])
        killed = True
    for pid in _daemon_pids():
        _kill_tree(pid)
        killed = True
    return "机器人已停止" if killed else "机器人本来就没在运行"


def status(lines=8):
    sup = crcore.load_json(SUPERVISOR_PID, None)
    running = bool(_daemon_pids())
    hb = _read_epoch(HEARTBEAT)
    out = [f"机器人：{'运行中' if running else '没在运行（cr start 启动）'}"
           + (f"，{time.time() - hb:.0f} 秒前心跳" if running and hb else ""),
           f"看门狗：{'运行中' if sup and _alive(sup) else '没在运行'}", "---- 最近日志 ----"]
    try:
        tail = open(os.path.join(LOGS, "daemon.log"), encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        tail = []
    import re
    tail = [re.sub(r"(access_key|ticket)=[^&\s]+", r"\1=***", l)[:160] for l in tail if not l.startswith("[Lark]")]
    return "\n".join(out + tail[-lines:])


# ---------------------------------------------------------------- 开机自启

AUTOSTART_NAME = "cc-crew"


def autostart(on):
    py, me = _python(), os.path.join(LIB, "crcli.py")
    if IS_WIN:
        folder = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
        path = os.path.join(folder, AUTOSTART_NAME + ".cmd")
        if not on:
            return _remove(path)
        with open(path, "w", encoding="utf-8") as f:     # 登录时在后台启动（pythonw 不弹窗口）
            f.write(f'@echo off\r\nstart "" "{py}" "{me}" start --boot\r\n')
        return f"已设置：登录 Windows 后自动启动（{path}）"
    if sys.platform == "darwin":
        path = os.path.expanduser(f"~/Library/LaunchAgents/com.{AUTOSTART_NAME}.plist")
        if not on:
            subprocess.run(["launchctl", "unload", path], capture_output=True)
            return _remove(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.{AUTOSTART_NAME}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{me}</string><string>start</string><string>--boot</string></array>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>{LIB}</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "load", path], capture_output=True)
        return f"已设置：登录 macOS 后自动启动（{path}）"
    # Linux：有 systemd 用户服务就用它，没有就用 crontab @reboot
    if subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True).returncode == 0:
        path = os.path.expanduser(f"~/.config/systemd/user/{AUTOSTART_NAME}.service")
        if not on:
            subprocess.run(["systemctl", "--user", "disable", AUTOSTART_NAME], capture_output=True)
            return _remove(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"[Unit]\nDescription=cc-crew\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                    f"ExecStart={py} {me} start --boot\n\n[Install]\nWantedBy=default.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", AUTOSTART_NAME], capture_output=True)
        return f"已设置：开机自动启动（systemd 用户服务 {AUTOSTART_NAME}）"
    line = f"@reboot {py} {me} start --boot  # {AUTOSTART_NAME}"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    keep = [l for l in cur.splitlines() if f"# {AUTOSTART_NAME}" not in l]
    new = "\n".join(keep + ([line] if on else [])) + "\n"
    subprocess.run(["crontab", "-"], input=new, text=True, check=True)
    return "已设置：开机自动启动（crontab @reboot）" if on else "已取消开机自动启动"


def _remove(path):
    if os.path.exists(path):
        os.remove(path)
    return "已取消开机自动启动"


# ---------------------------------------------------------------- 其他命令

def notices(n):
    try:
        lines = open(crcore.OUTBOX, encoding="utf-8").readlines()[-n:]
    except OSError:
        return "还没有通知"
    out = []
    for l in lines:
        d = json.loads(l)
        out.append(time.strftime("%m-%d %H:%M", time.localtime(d["t"])) + "  " + d["text"] + "\n")
    return "\n".join(out)


def authorize():
    try:
        last = open(os.path.join(DATA, "rejected.log"), encoding="utf-8").read().splitlines()[-1].split("\t")[1]
    except (OSError, IndexError):
        return "还没有人给机器人发过消息"
    set_config("FEISHU_ALLOW_OPEN_ID", last)
    return f"已授权 {last}（重启机器人后生效：cr start）"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):              # Windows 控制台 / 管道默认是 GBK，emoji 会报错
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cmd = argv[0] if argv else "帮助"
    if cmd == "daemon":
        import crbot
        return crbot.main()
    if cmd == "supervise":
        return supervise()
    if cmd in ("start", "cr-start"):
        print(start(boot="--boot" in argv))
    elif cmd in ("stop", "cr-stop"):
        print(stop())
    elif cmd in ("status", "cr-status"):
        print(status(int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 8))
    elif cmd == "autostart":
        print(autostart(len(argv) < 2 or argv[1] != "off"))
    elif cmd == "通知":
        print(notices(int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 10))
    elif cmd == "授权":
        print(authorize())
    elif cmd.startswith("#fire"):
        print("开除只能由你在飞书里点确认按钮，命令行不能执行。")
        return 1
    else:
        print(crcore.Remote().handle(" ".join(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
