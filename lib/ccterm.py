"""ccm：不用 tmux 也能被机器人看屏幕、替你打字的 claude 启动器（Windows / macOS / Linux 通用）。

用法：在终端里打 `ccm [claude 参数…]` 代替 `claude`。看起来、用起来和直接跑 claude 一模一样：
- 它在一个伪终端里运行 claude（Linux / macOS 用 pty，Windows 用 ConPTY，经 pywinpty），
  claude 输出的每个字节原样转给你的终端，你敲的每个键原样转给 claude；
- 同时把输出喂给一个内存里的终端模拟器（pyte），随时知道“屏幕上现在是什么”；
- 在 127.0.0.1 上开一个只有本机、带口令的控制口，机器人通过它读屏幕、打字、按键（tmux 干的就是这个活）；
- 在 data/terms/<启动器pid>.json 登记自己（端口、口令、claude 的 pid），退出时删掉。

机器人这边用 records() / find(pid) 找到会话，用 screen_rows() / send() / paste() / key_seq() 操作它。
"""
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("CLAUDE_MONITOR_DATA") or os.path.join(REPO, "data")
TERMS_DIR = os.path.join(DATA_DIR, "terms")
DROP_ENV = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
DEFAULT_SIZE = (200, 50)                                 # 没有真终端时（后台启动）用的大小：列, 行

KEYS = {
    "enter": "\r", "escape": "\x1b", "esc": "\x1b", "tab": "\t", "btab": "\x1b[Z",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "c-c": "\x03", "space": " ", "bspace": "\x7f", "c-end": "\x1b[1;5F", "end": "\x1b[F", "home": "\x1b[H",
    "c-d": "\x04", "c-l": "\x0c", "c-u": "\x15", "pageup": "\x1b[5~", "pagedown": "\x1b[6~", "delete": "\x1b[3~",
}


def key_seq(token):
    """tmux 风格的键名（Enter、Escape、Up、C-c、BTab…）→ 终端按键序列；不认识返回 None。"""
    return KEYS.get((token or "").lower())


# ---------------------------------------------------------------- 屏幕模拟

# Claude Code 会发 CSI > 1 u / CSI < u / CSI = 1 u（键盘协议）等，pyte 不认识会把尾巴当字画出来：先滤掉
_ODD_CSI = re.compile(rb"\x1b\[[<=>][0-9;:]*[\x40-\x7e]")
_TAIL = re.compile(rb"\x1b(\[[0-9;:<=>?]*)?$")


def _make_screen(cols, rows):
    import pyte

    class Screen(pyte.Screen):
        """pyte 不记「变暗」（SGR 2）：借 blink 这一位来记（claude 不用闪烁），输入框里的灰色提示字就靠它认出来。"""

        def select_graphic_rendition(self, *attrs, **kw):
            seg, i = [], 0
            attrs = list(attrs)

            def flush():
                if seg:
                    super(Screen, self).select_graphic_rendition(*seg)
                    seg.clear()
            if not attrs:
                attrs = [0]
            while i < len(attrs):
                a = attrs[i]
                if a in (38, 48):                        # 38;5;n / 38;2;r;g;b：后面几个数是参数，不能当成 2（变暗）
                    n = 3 if attrs[i + 1:i + 2] == [5] else 5 if attrs[i + 1:i + 2] == [2] else 1
                    seg.extend(attrs[i:i + n])
                    i += n
                    continue
                if a in (2, 22, 5, 25):
                    flush()
                    if a == 2:
                        self.cursor.attrs = self.cursor.attrs._replace(blink=True)
                    elif a == 22:
                        self.cursor.attrs = self.cursor.attrs._replace(blink=False, bold=False)
                elif a == 0:
                    flush()
                    super(Screen, self).select_graphic_rendition(0)
                else:
                    seg.append(a)
                i += 1
            flush()

    return Screen(cols, rows)


class Term:
    """claude 输出的内存镜像：feed() 喂字节，rows() 取当前屏幕。线程安全。"""

    def __init__(self, cols, rows):
        import pyte
        self.lock = threading.Lock()
        self.screen = _make_screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.tail = b""

    def feed(self, data):
        data = self.tail + data
        m = _TAIL.search(data)
        if m and len(data) - m.start() < 64:            # 结尾是半个转义序列：留到下一块再处理
            data, self.tail = data[:m.start()], data[m.start():]
        else:
            self.tail = b""
        data = _ODD_CSI.sub(b"", data)
        with self.lock:
            try:
                self.stream.feed(data)
            except Exception:                            # 模拟器出错不能影响你的终端
                pass

    def resize(self, cols, rows):
        with self.lock:
            self.screen.resize(rows, cols)

    @property
    def title(self):
        return getattr(self.screen, "title", "") or ""

    def rows(self, ansi=False):
        with self.lock:
            out = []
            for y in range(self.screen.lines):
                line, parts, dim = self.screen.buffer[y], [], False
                for x in range(self.screen.columns):
                    c = line[x]
                    if c.data == "":                     # 双宽字（中文）后面的占位格
                        continue
                    if ansi and c.blink != dim:
                        parts.append("\x1b[2m" if c.blink else "\x1b[22m")
                        dim = c.blink
                    parts.append(c.data)
                if dim:
                    parts.append("\x1b[22m")
                text = "".join(parts)
                if ansi:
                    text = re.sub(r"\s+((?:\x1b\[22m)?)$", r"\1", text)
                    text = text.replace("\x1b[2m\x1b[22m", "")
                out.append(text.rstrip())
            return out


# ---------------------------------------------------------------- 登记和控制口（机器人这一侧）

def _pid_alive(pid, started=None):
    try:
        import psutil
        p = psutil.Process(int(pid))
        return started is None or abs(p.create_time() - started) < 2
    except ImportError:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    except Exception:
        return False


def records():
    """活着的 ccm 会话（启动器已经不在的登记文件顺手删掉）。"""
    out = []
    try:
        names = os.listdir(TERMS_DIR)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        path = os.path.join(TERMS_DIR, n)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if _pid_alive(rec.get("pid"), rec.get("pid_started")):
            out.append(rec)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return out


def find(pid):
    """claude 进程 pid（Claude Code 的 sessions/<pid>.json 里那个）属于哪个 ccm 会话。
    Windows 上 claude.cmd 会再起一个 node，所以子孙进程也算。"""
    recs = records()
    for r in recs:
        if r.get("child") == pid:
            return r
    try:
        import psutil
        chain = {p.pid for p in psutil.Process(int(pid)).parents()}
    except Exception:
        return None
    for r in recs:
        if r.get("child") in chain or r.get("pid") in chain:
            return r
    return None


def _call(rec, req, timeout=5):
    req = dict(req, token=rec["token"])
    with socket.create_connection(("127.0.0.1", rec["port"]), timeout=timeout) as c:
        c.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            buf += chunk
    rep = json.loads(buf or b"{}")
    if not rep.get("ok"):
        raise RuntimeError(rep.get("error") or "ccm 没有回应")
    return rep


def screen_rows(rec, ansi=False):
    return _call(rec, {"op": "screen", "ansi": ansi})["rows"]


def send(rec, data):
    _call(rec, {"op": "send", "data": data})


def paste(rec, text):
    _call(rec, {"op": "paste", "data": text})


# ---------------------------------------------------------------- 启动器（ccm 这一侧）

def _claude_argv():
    over = os.environ.get("CCM_CLAUDE_BIN")
    if over:
        if os.name != "nt":
            return shlex.split(over)
        # Windows 路径里的反斜杠不能当转义符；"C:\Program Files\…" 两边的引号去掉
        return [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in shlex.split(over, posix=False)]
    if os.name == "nt":
        exe = shutil.which("claude.exe") or shutil.which("claude.cmd") or shutil.which("claude")
        if exe and exe.lower().endswith((".cmd", ".bat")):
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe]
        return [exe] if exe else []
    exe = shutil.which("claude")
    return [exe] if exe else []


def _child_env():
    env = dict(os.environ)
    drop = set(DROP_ENV) | {v.strip() for v in os.environ.get("CCM_DROP_ENV", "").split(",") if v.strip()}
    for k in drop:
        env.pop(k, None)
    extra = os.environ.get("CCM_EXTRA_ENV_FILE")
    if extra:
        with open(extra, encoding="utf-8") as f:
            env.update({str(k): str(v) for k, v in json.load(f).items()})
    for k in [k for k in env if k.startswith("CCM_")]:
        env.pop(k)
    return env


def _proxy_ok(spec):
    host, _, port = spec.rpartition(":")
    try:
        socket.create_connection((host or "127.0.0.1", int(port)), timeout=3).close()
        return True
    except (OSError, ValueError):
        return False


def _write_json(path, obj):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


class Launcher:
    def __init__(self, argv):
        self.argv = argv
        self.token = secrets.token_hex(16)
        self.wlock = threading.Lock()
        self.term = None
        self.rec = None
        self.rec_path = os.path.join(TERMS_DIR, f"{os.getpid()}.json")
        self.write_child = None                          # 往 claude 写字节的函数，由各平台设置

    # 控制口：一个连接一个 JSON 请求
    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        self.port = srv.getsockname()[1]

        def loop():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=self.handle, args=(c,), daemon=True).start()
        threading.Thread(target=loop, daemon=True).start()

    def handle(self, c):
        try:
            c.settimeout(10)
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < (4 << 20):
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
            req = json.loads(buf or b"{}")
            if not hmac.compare_digest(str(req.get("token", "")), self.token):
                rep = {"ok": False, "error": "口令不对"}
            elif req.get("op") == "ping":
                rep = {"ok": True}
            elif req.get("op") == "screen":
                rep = {"ok": True, "rows": self.term.rows(bool(req.get("ansi"))), "title": self.term.title}
            elif req.get("op") in ("send", "paste"):
                data = str(req.get("data", ""))
                if req["op"] == "paste":
                    data = "\x1b[200~" + data + "\x1b[201~"
                self.write(data.encode("utf-8"))
                rep = {"ok": True}
            else:
                rep = {"ok": False, "error": "不认识的操作"}
            c.sendall(json.dumps(rep, ensure_ascii=False).encode("utf-8") + b"\n")
        except Exception as e:
            try:
                c.sendall(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
            except OSError:
                pass
        finally:
            c.close()

    def write(self, data):
        with self.wlock:
            self.write_child(data)

    def register(self, child_pid):
        os.makedirs(TERMS_DIR, exist_ok=True)
        started = None
        try:
            import psutil
            started = psutil.Process(os.getpid()).create_time()
        except Exception:
            pass
        self.rec = {"pid": os.getpid(), "pid_started": started, "child": child_pid, "port": self.port,
                    "token": self.token, "cwd": os.getcwd(), "argv": self.argv, "started": time.time(), "title": "",
                    "name": os.environ.get("CCM_NAME") or None,          # 机器人启动时给的名字
                    "private": os.environ.get("CCM_PRIVATE") == "1"}     # 机器人在后台开的（不是你在终端里开的）
        _write_json(self.rec_path, self.rec)

        def watch_title():                               # 标题变了就更新登记（claude 把对话标题写在终端标题里）
            while self.rec:
                time.sleep(2)
                t = self.term.title
                if self.rec and t != self.rec.get("title"):
                    self.rec["title"] = t
                    try:
                        _write_json(self.rec_path, self.rec)
                    except OSError:
                        pass
        threading.Thread(target=watch_title, daemon=True).start()

    def unregister(self):
        self.rec = None
        try:
            os.remove(self.rec_path)
        except OSError:
            pass

    def run(self):
        exe = _claude_argv()
        if not exe or not exe[0]:
            sys.stderr.write("ccm：找不到 claude 命令（PATH 里没有）。\n")
            return 127
        need = os.environ.get("CCM_REQUIRE_PROXY")
        if need and not _proxy_ok(need):
            sys.stderr.write(f"ccm：代理 {need} 连不上，为了安全不启动 claude。\n")
            return 2
        env = _child_env()
        try:
            return (self.run_windows if os.name == "nt" else self.run_unix)(exe + self.argv, env)
        finally:
            self.unregister()

    # ---- Linux / macOS
    def run_unix(self, argv, env):
        import fcntl
        import pty
        import select
        import signal
        import struct
        import termios
        import tty

        in_fd, out_fd = sys.stdin.fileno(), sys.stdout.fileno()
        interactive = os.isatty(in_fd)
        show = os.isatty(out_fd)                         # 没接终端（后台启动）：输出只喂给屏幕模拟器，不往外写

        def size():
            try:
                s = os.get_terminal_size(out_fd if os.isatty(out_fd) else in_fd)
                return (s.columns, s.lines) if s.columns and s.lines else DEFAULT_SIZE   # 有的终端刚开时报 0x0
            except OSError:
                return DEFAULT_SIZE
        cols, rows = size()
        self.term = Term(cols, rows)

        pid, master = pty.fork()                         # 先 fork 再开线程（控制口），子进程里不留线程
        if pid == 0:
            try:
                os.execvpe(argv[0], argv, env)
            except OSError as e:
                os.write(2, f"ccm：启动 {argv[0]} 失败：{e}\r\n".encode())
            os._exit(127)
        self.serve()

        def set_size(c, r):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", r, c, 0, 0))
        set_size(cols, rows)

        def write_child(data):
            while data:
                n = os.write(master, data)
                data = data[n:]
        self.write_child = write_child
        self.register(pid)

        # 改窗口大小：信号来了只往管道里写一个字节（set_wakeup_fd，C 层直接写，信号落在哪个线程都行），
        # 主循环的 select 被唤醒后再改大小。不能在信号处理函数里直接 term.resize：主线程可能正拿着 term 的锁在 feed，
        # 同一个线程再要这把锁会死锁；信号也可能落到控制口线程上，主线程一直卡在 select 里收不到。
        winch_r, winch_w = os.pipe()
        os.set_blocking(winch_r, False)
        os.set_blocking(winch_w, False)
        old_wakeup = signal.set_wakeup_fd(winch_w)
        old_winch = signal.signal(signal.SIGWINCH, lambda *_: None)

        def on_term(signum, _):                          # 启动器被关：把 claude 一起关掉
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass
        old_term = signal.signal(signal.SIGTERM, on_term)
        old_hup = signal.signal(signal.SIGHUP, on_term)

        saved = termios.tcgetattr(in_fd) if interactive else None
        if interactive:
            tty.setraw(in_fd)
        fds = [master, winch_r] + ([in_fd] if interactive else [])
        try:
            while True:
                try:
                    r, _, _ = select.select(fds, [], [])
                except InterruptedError:
                    continue
                if winch_r in r:
                    try:
                        os.read(winch_r, 4096)
                    except OSError:
                        pass
                    c, h = size()
                    try:
                        set_size(c, h)
                    except OSError:
                        pass
                    self.term.resize(c, h)
                if master in r:
                    try:
                        data = os.read(master, 65536)
                    except OSError:                      # Linux 上 claude 退出后读 pty 得到 EIO
                        data = b""
                    if not data:
                        break
                    view = memoryview(data) if show else memoryview(b"")
                    while view:
                        try:
                            n = os.write(out_fd, view)
                        except BlockingIOError:
                            time.sleep(0.01)
                            continue
                        except OSError:                  # 你的终端没了：claude 照样跑，只是不往外显示
                            break
                        view = view[n:]
                    self.term.feed(data)
                if in_fd in r:
                    try:
                        data = os.read(in_fd, 65536)
                    except OSError:
                        data = b""
                    if not data:                         # 你的终端关了：不再转发输入
                        fds.remove(in_fd)
                        continue
                    self.write(data)
        finally:
            if saved is not None:
                termios.tcsetattr(in_fd, termios.TCSAFLUSH, saved)
            signal.signal(signal.SIGWINCH, old_winch)
            signal.set_wakeup_fd(old_wakeup)
            for fd in (winch_r, winch_w):
                try:
                    os.close(fd)
                except OSError:
                    pass
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGHUP, old_hup)
            try:
                os.close(master)
            except OSError:
                pass
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status >> 8

    # ---- Windows（ConPTY，经 pywinpty）
    def run_windows(self, argv, env):
        import ctypes
        from ctypes import wintypes
        from winpty import PtyProcess                    # pip install pywinpty

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_in, h_out = k32.GetStdHandle(-10), k32.GetStdHandle(-11)
        ENABLE_PROCESSED_INPUT, ENABLE_LINE_INPUT, ENABLE_ECHO_INPUT = 0x1, 0x2, 0x4
        ENABLE_VIRTUAL_TERMINAL_INPUT = 0x200
        ENABLE_VIRTUAL_TERMINAL_PROCESSING, DISABLE_NEWLINE_AUTO_RETURN = 0x4, 0x8
        in_mode, out_mode = wintypes.DWORD(), wintypes.DWORD()
        interactive = bool(k32.GetConsoleMode(h_in, ctypes.byref(in_mode)))
        has_out = bool(k32.GetConsoleMode(h_out, ctypes.byref(out_mode)))
        old_cp, old_out_cp = k32.GetConsoleCP(), k32.GetConsoleOutputCP()

        def size():
            s = shutil.get_terminal_size(DEFAULT_SIZE)
            return (s.columns, s.lines) if s.columns and s.lines else DEFAULT_SIZE
        cols, rows = size()
        self.term = Term(cols, rows)

        proc = PtyProcess.spawn(argv, cwd=os.getcwd(), env=env, dimensions=(rows, cols))
        self.serve()
        self.write_child = lambda data: proc.write(data.decode("utf-8", "replace"))
        self.register(proc.pid)

        if interactive:
            k32.SetConsoleCP(65001)
            k32.SetConsoleMode(h_in, (in_mode.value | ENABLE_VIRTUAL_TERMINAL_INPUT)
                               & ~(ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT))
        if has_out:
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleMode(h_out, out_mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING | DISABLE_NEWLINE_AUTO_RETURN)

        out = sys.stdout.buffer
        done = threading.Event()

        def pump_out():
            while True:
                try:
                    text = proc.read(65536)
                except EOFError:
                    break
                except Exception:
                    if not proc.isalive():
                        break
                    time.sleep(0.01)
                    continue
                if not text:
                    if not proc.isalive():
                        break
                    time.sleep(0.005)
                    continue
                data = text.encode("utf-8", "replace")
                if has_out:
                    try:
                        out.write(data)
                        out.flush()
                    except OSError:
                        pass
                self.term.feed(data)
            done.set()

        def pump_in():                                   # ReadConsoleW 读 UTF-16，中文输入不会坏
            buf = ctypes.create_unicode_buffer(4096)
            n = wintypes.DWORD()
            while not done.is_set():
                if not k32.ReadConsoleW(h_in, buf, 4096, ctypes.byref(n), None):
                    return
                if n.value:
                    try:
                        self.write(buf[:n.value].encode("utf-8"))
                    except Exception:
                        return

        def pump_size():
            last = (cols, rows)
            while not done.wait(0.5):
                now_size = size()
                if now_size != last:
                    last = now_size
                    try:
                        proc.setwinsize(now_size[1], now_size[0])
                    except Exception:
                        pass
                    self.term.resize(*now_size)

        threading.Thread(target=pump_out, daemon=True).start()
        if interactive:
            threading.Thread(target=pump_in, daemon=True).start()
        threading.Thread(target=pump_size, daemon=True).start()
        try:
            while not done.wait(0.5):
                pass
        finally:
            if interactive:
                k32.SetConsoleMode(h_in, in_mode.value)
                k32.SetConsoleCP(old_cp)
            if has_out:
                k32.SetConsoleMode(h_out, out_mode.value)
                k32.SetConsoleOutputCP(old_out_cp)
        try:
            proc.wait()
        except Exception:
            pass
        code = getattr(proc, "exitstatus", None)
        return code if isinstance(code, int) else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    return Launcher(argv).run()


if __name__ == "__main__":
    sys.exit(main())
