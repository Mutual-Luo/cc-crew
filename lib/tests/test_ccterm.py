"""ccm（ccterm.py）的测试：用假 claude，在自己开的伪终端里跑启动器，像真人坐在终端前一样。
运行：venv/bin/python lib/tests/test_ccterm.py（只在 Linux / macOS 上跑；不会启动真的 claude）"""
import os
import sys

if os.name == "nt":
    print("跳过：Windows 上跑 test_ccterm_win.py")
    sys.exit(0)

import fcntl
import json
import os
import pty
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.dirname(HERE)
DATA = tempfile.mkdtemp(prefix="ccterm-test-")
os.environ["CLAUDE_MONITOR_DATA"] = DATA                 # 不碰真的 data/terms
sys.path.insert(0, LIB)
import ccterm  # noqa: E402

FAKE = os.path.join(HERE, "fake_claude.py")
passed = []


def check(name, cond, detail=""):
    assert cond, f"{name} 失败 {detail}"
    passed.append(name)
    print("✓", name)


def wait(pred, secs=10, step=0.1):
    end = time.time() + secs
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def start(env_extra=None, cols=100, rows=30, args=()):
    """在一个新伪终端里启动 ccm（有控制终端，和你在终端里打 ccm 一样）。返回 (进程, 外层 master, 输出缓冲)。"""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = dict(os.environ, CCM_CLAUDE_BIN=f"{sys.executable} {FAKE}", **(env_extra or {}))

    def ctty():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    p = subprocess.Popen([sys.executable, os.path.join(LIB, "ccterm.py"), *args], stdin=slave, stdout=slave,
                         stderr=slave, env=env, preexec_fn=ctty, close_fds=True)
    os.close(slave)
    out = bytearray()

    def drain():                                         # 外层终端：把 ccm 往外写的都收下（和真终端一样）
        while True:
            try:
                d = os.read(master, 65536)
            except OSError:
                return
            if not d:
                return
            out.extend(d)
    threading.Thread(target=drain, daemon=True).start()
    return p, master, out


def screen_has(rec, text, ansi=False):
    return wait(lambda: any(text in r for r in ccterm.screen_rows(rec, ansi)))


# 1. 正常启动、登记、读屏
p, master, out = start(env_extra={"CCM_NAME": "exp-a", "CCM_PRIVATE": "1", "CLAUDECODE": "1"})
recs = wait(lambda: ccterm.records())
check("登记文件出现", recs and len(recs) == 1, recs)
rec = recs[0]
check("登记字段齐全", all(k in rec for k in ("pid", "child", "port", "token", "cwd", "argv", "started", "title", "name", "private")))
check("名字和后台标记", rec["name"] == "exp-a" and rec["private"] is True)
check("屏幕上有输入框", screen_has(rec, "❯"))
check("假 claude 看到的大小 = 外层终端", screen_has(rec, "SIZE:100x30"))
check("你的终端也收到了原样输出", wait(lambda: b"\xe2\x9d\xaf" in out))
check("find(子进程 pid)", ccterm.find(rec["child"]) == rec)
check("find(不相干 pid) 为 None", ccterm.find(1) is None)

# 2. 变暗的提示字
rows_ansi = ccterm.screen_rows(rec, ansi=True)
check("ansi=True 标出变暗", any("\x1b[2mghost hint\x1b[22m" in r for r in rows_ansi), rows_ansi[:3])
check("ansi=False 是纯文字", any("ghost hint" in r and "\x1b" not in r for r in ccterm.screen_rows(rec)))
check("键盘协议序列没画成乱码", not any("1u" in r for r in ccterm.screen_rows(rec)))

# 3. 标题
check("标题写进登记", wait(lambda: "假对话标题" in (ccterm.records()[0].get("title") or ""), 6))

# 4. 机器人打字、粘贴、按键
ccterm.send(rec, "hello 中文" + ccterm.key_seq("Enter"))
check("send 到达", screen_has(rec, "GOT:hello 中文"))
ccterm.paste(rec, "第一行\n第二行")
ccterm.send(rec, ccterm.key_seq("Enter"))
check("paste 是括号粘贴", wait(lambda: any("[PASTE:第一行" in r for r in ccterm.screen_rows(rec))))
ccterm.send(rec, ccterm.key_seq("Up"))
check("方向键到达", screen_has(rec, "KEY:UP"))
check("key_seq 映射", ccterm.key_seq("BTab") == "\x1b[Z" and ccterm.key_seq("C-c") == "\x03"
      and ccterm.key_seq("C-End") == "\x1b[1;5F" and ccterm.key_seq("nope") is None)

# 5. 人在终端前打字照样能用
os.write(master, b"typed\r")
check("真人键盘输入透传", screen_has(rec, "GOT:typed"))

# 6. 改窗口大小
fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 132, 0, 0))
check("改大小传到 claude", screen_has(rec, "SIZE:132x40"), ([r for r in ccterm.screen_rows(rec) if r], len(ccterm.screen_rows(rec)), out[-400:]))
check("屏幕模拟器也跟着变宽", wait(lambda: len(ccterm.screen_rows(rec)) == 40))

# 7. 口令不对被拒
bad = dict(rec, token="0" * 32)
try:
    ccterm.screen_rows(bad)
    rejected = False
except RuntimeError:
    rejected = True
check("口令不对被拒", rejected)

# 8. 环境变量：CLAUDECODE、CCM_* 不带给 claude（用 send 让假 claude 什么也不做，直接查 /proc）
env_child = open(f"/proc/{rec['child']}/environ", "rb").read().split(b"\0") if os.path.exists("/proc") else None
if env_child is not None:
    keys = {e.split(b"=", 1)[0] for e in env_child}
    check("不带 CLAUDECODE / CCM_*", b"CLAUDECODE" not in keys and not any(k.startswith(b"CCM_") for k in keys))

# 9. 退出：退出码透传、登记删掉
ccterm.send(rec, "/exit\r")
code = p.wait(10)
check("退出码透传", code == 3, code)
check("登记文件删掉", not ccterm.records() and not os.listdir(ccterm.TERMS_DIR))
os.close(master)

# 10. 要求代理但连不上：不启动
s = socket.socket()
s.bind(("127.0.0.1", 0))
closed_port = s.getsockname()[1]
s.close()
p, master, out = start(env_extra={"CCM_REQUIRE_PROXY": f"127.0.0.1:{closed_port}"})
code = p.wait(10)
time.sleep(0.2)
check("代理连不上拒绝启动", code == 2 and "代理" in out.decode("utf-8", "replace"), (code, bytes(out)))
check("没有留下登记", not ccterm.records())
os.close(master)

# 11. 代理通的时候照常启动；extra env 文件生效
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(4)
extra = os.path.join(DATA, "extra.json")
json.dump({"HTTPS_PROXY": "http://127.0.0.1:1"}, open(extra, "w"))
p, master, out = start(env_extra={"CCM_REQUIRE_PROXY": f"127.0.0.1:{srv.getsockname()[1]}",
                                  "CCM_EXTRA_ENV_FILE": extra})
rec = wait(lambda: (ccterm.records() or [None])[0])
check("代理通时启动", bool(rec))
if os.path.exists("/proc"):
    env_child = open(f"/proc/{rec['child']}/environ", "rb").read()
    check("extra env 带给 claude", b"HTTPS_PROXY=http://127.0.0.1:1" in env_child)
ccterm.send(rec, "/exit\r")
p.wait(10)
os.close(master)
srv.close()

# 12. 没接终端（后台启动）也能跑，靠控制口操作
env = dict(os.environ, CCM_CLAUDE_BIN=f"{sys.executable} {FAKE}")
# 假 claude 自己要原始模式：后台启动时 ccm 仍给它一个伪终端，所以没问题
p = subprocess.Popen([sys.executable, os.path.join(LIB, "ccterm.py")], stdin=subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
rec = wait(lambda: (ccterm.records() or [None])[0])
check("后台启动有登记", bool(rec))
check("后台启动能读屏", screen_has(rec, "❯"))
ccterm.send(rec, "hi\r")
check("后台启动能打字", screen_has(rec, "GOT:hi"))
ccterm.send(rec, "/exit\r")
check("后台启动正常退出", p.wait(10) == 3)
check("后台不往 stdout 乱写", b"GOT:hi" not in p.stdout.read())

# 13. 死掉的启动器留下的登记会被清理
os.makedirs(ccterm.TERMS_DIR, exist_ok=True)
json.dump({"pid": 999999, "child": 1, "port": 1, "token": "x"}, open(os.path.join(ccterm.TERMS_DIR, "999999.json"), "w"))
check("清理死掉的登记", not ccterm.records() and not os.listdir(ccterm.TERMS_DIR))

print(f"\n全部通过：{len(passed)} 项")
