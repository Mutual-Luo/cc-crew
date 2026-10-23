#!/usr/bin/env python3
"""测试用的假 claude：画一个输入框，原始模式读键，回显收到的东西；/exit 退出（退出码 3）。
Linux / macOS：tty 原始模式 + SIGWINCH 报大小；Windows：控制台开 VT 输入（方向键、括号粘贴都是 VT 序列），
轮询窗口大小。"""
import os
import shutil
import sys
import threading
import time

IS_WIN = os.name == "nt"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h_in, h_out = k32.GetStdHandle(-10), k32.GetStdHandle(-11)
    k32.SetConsoleCP(65001)
    k32.SetConsoleOutputCP(65001)
    mode = wintypes.DWORD()
    k32.GetConsoleMode(h_in, ctypes.byref(mode))
    # 关掉行输入 / 回显 / Ctrl-C 处理，打开 VT 输入：和真 claude（node 的原始模式）一样
    k32.SetConsoleMode(h_in, (mode.value | 0x200) & ~(0x1 | 0x2 | 0x4))
    k32.GetConsoleMode(h_out, ctypes.byref(mode))
    k32.SetConsoleMode(h_out, mode.value | 0x4 | 0x8)   # VT 输出处理、不自动换行回车
    _wlock = threading.Lock()

    def w(s):
        data = s.encode("utf-8")
        n = wintypes.DWORD()
        with _wlock:
            k32.WriteFile(h_out, data, len(data), ctypes.byref(n), None)

    def read_input():
        buf = ctypes.create_unicode_buffer(4096)
        n = wintypes.DWORD()
        if not k32.ReadConsoleW(h_in, buf, 4096, ctypes.byref(n), None):
            return b""
        return buf[:n.value].encode("utf-8")

    def cur_size():
        s = shutil.get_terminal_size((0, 0))
        return s.columns, s.lines
else:
    import select
    import signal
    import tty

    fd = sys.stdin.fileno()
    tty.setraw(fd)

    def w(s):
        os.write(1, s.encode())

    def read_input():
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.2)
            except InterruptedError:
                continue
            if r:
                return os.read(fd, 4096)

    def cur_size():
        s = os.get_terminal_size(1)
        return s.columns, s.lines


def report_size(*_):
    c, r = cur_size()
    w(f"\r\nSIZE:{c}x{r}\r\n")


if IS_WIN:
    def watch_size():                                    # Windows 没有 SIGWINCH：轮询
        last = cur_size()
        while True:
            time.sleep(0.2)
            now = cur_size()
            if now != last:
                last = now
                report_size()
    threading.Thread(target=watch_size, daemon=True).start()
else:
    signal.signal(signal.SIGWINCH, report_size)

w("\x1b]0;✳ 假对话标题\x07")
w("\x1b[>1u\x1b[?2004h")                                 # 和真 claude 一样开键盘协议、括号粘贴
w("╭────────────╮\r\n│ ❯ \x1b[2mghost hint\x1b[22m │\r\n╰────────────╯\r\n")
report_size()
line, buf = "", b""
while True:
    data = read_input()
    if not data:
        break
    buf += data
    while buf:
        if buf.startswith(b"\x1b[200~"):
            end = buf.find(b"\x1b[201~")
            if end < 0:
                break
            line += "[PASTE:" + buf[6:end].decode().replace("\r\n", "\n").replace("\r", "\n") + "]"
            buf = buf[end + 6:]
        elif buf[:3] in (b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"):
            w("KEY:" + {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}[chr(buf[2])] + "\r\n")
            buf = buf[3:]
        elif buf[:1] in (b"\r", b"\n"):
            w(f"GOT:{line}\r\n")
            if line == "/exit":
                sys.exit(3)
            line, buf = "", buf[1:]
        elif buf[:1] == b"\x1b" and len(buf) < 6 and b"\x1b[200~".startswith(buf[:6]):
            break                                        # 括号粘贴的开头还没收全
        else:
            ch = buf.decode("utf-8", "ignore")[:1] or "?"
            line += ch
            buf = buf[len(ch.encode()):] if ch != "?" else buf[1:]
