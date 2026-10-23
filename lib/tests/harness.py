"""测试用：在一个「外层终端」里启动 ccm，就像你在终端里打 ccm 一样。
Linux / macOS 用 pty，Windows 用 pywinpty（ConPTY），接口一样：
    t = Outer(env_extra, cols, rows, args)
    t.write("typed\\r")   像真人敲键盘
    t.resize(132, 40)    拖窗口改大小
    t.text()             外层终端收到的所有输出
    t.wait(10)           等它退出，返回退出码（超时返回 None）
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.dirname(HERE)
FAKE = os.path.join(HERE, "fake_claude.py")
CCM = os.path.join(LIB, "ccterm.py")
IS_WIN = os.name == "nt"


def fake_bin():
    """CCM_CLAUDE_BIN：用当前 python 跑假 claude。"""
    return f"{sys.executable} {FAKE}"


def wait(pred, secs=10, step=0.1):
    end = time.time() + secs
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


class Outer:
    def __init__(self, env_extra=None, cols=100, rows=30, args=()):
        env = dict(os.environ, CCM_CLAUDE_BIN=fake_bin(), **(env_extra or {}))
        self.out = bytearray()
        self.lock = threading.Lock()
        argv = [sys.executable, CCM, *args]
        if IS_WIN:
            from winpty import PtyProcess
            self.proc = PtyProcess.spawn(argv, env=env, dimensions=(rows, cols))
            threading.Thread(target=self._drain_win, daemon=True).start()
        else:
            self._spawn_unix(argv, env, cols, rows)

    # ---- Linux / macOS
    def _spawn_unix(self, argv, env, cols, rows):
        import fcntl
        import pty
        import struct
        import subprocess
        import termios
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        def ctty():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                     preexec_fn=ctty, close_fds=True)
        os.close(slave)
        self.master = master
        threading.Thread(target=self._drain_unix, daemon=True).start()

    def _drain_unix(self):
        while True:
            try:
                d = os.read(self.master, 65536)
            except OSError:
                return
            if not d:
                return
            with self.lock:
                self.out.extend(d)

    def _drain_win(self):
        while True:
            try:
                text = self.proc.read(65536)
            except EOFError:
                return
            except Exception:  # noqa: BLE001
                if not self.proc.isalive():
                    return
                time.sleep(0.05)
                continue
            if text:
                with self.lock:
                    self.out.extend(text.encode("utf-8", "replace"))
            elif not self.proc.isalive():
                return
            else:
                time.sleep(0.02)

    # ---- 通用
    def write(self, s):
        if IS_WIN:
            self.proc.write(s)
        else:
            os.write(self.master, s.encode())

    def resize(self, cols, rows):
        if IS_WIN:
            self.proc.setwinsize(rows, cols)
        else:
            import fcntl
            import struct
            import termios
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def text(self):
        with self.lock:
            return bytes(self.out).decode("utf-8", "replace")

    def wait(self, secs=10):
        if IS_WIN:
            if not wait(lambda: not self.proc.isalive(), secs):
                return None
            try:
                self.proc.wait()
            except Exception:  # noqa: BLE001
                pass
            return self.proc.exitstatus
        import subprocess
        try:
            return self.proc.wait(secs)
        except subprocess.TimeoutExpired:
            return None

    def close(self):
        if IS_WIN:
            try:
                if self.proc.isalive():
                    self.proc.terminate(force=True)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
            except OSError:
                pass
            try:
                os.close(self.master)
            except OSError:
                pass
