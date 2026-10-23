"""ccm（ccterm.py）在 Windows 上的测试：外层用 pywinpty（ConPTY）当「你的终端」，里面跑 ccm，ccm 再跑假 claude。
运行：python lib/tests/test_ccterm_win.py（只在 Windows 上跑；不会启动真的 claude）"""
import json
import os
import socket
import sys
import tempfile
import time

if os.name != "nt":
    print("跳过：只在 Windows 上跑（Linux / macOS 跑 test_ccterm.py）")
    sys.exit(0)

for stream in (sys.stdout, sys.stderr):                  # CI 的控制台不一定是 UTF-8
    stream.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.dirname(HERE)
DATA = tempfile.mkdtemp(prefix="ccterm-test-")
os.environ["CLAUDE_MONITOR_DATA"] = DATA                 # 不碰真的 data/terms
sys.path.insert(0, LIB)
sys.path.insert(0, HERE)
import ccterm  # noqa: E402
import psutil  # noqa: E402
from harness import Outer, wait  # noqa: E402

passed, soft = [], []


def check(name, cond, detail=""):
    assert cond, f"{name} 失败 {detail}"
    passed.append(name)
    print("✓", name)


def note(name, cond, detail=""):
    """ConPTY 可能改写的东西（比如变暗属性）：不过也不算失败，只记下来。"""
    print("✓" if cond else "△", name, "" if cond else detail)
    if not cond:
        soft.append(name)


def screen_has(rec, text, secs=10):
    return wait(lambda: any(text in r for r in ccterm.screen_rows(rec)), secs)


def dump(rec):
    try:
        return "\n".join(r for r in ccterm.screen_rows(rec) if r)
    except Exception as e:  # noqa: BLE001
        return repr(e)


# 1. 正常启动、登记、读屏
t = Outer(env_extra={"CCM_NAME": "exp-a", "CCM_PRIVATE": "1", "CLAUDECODE": "1"})
recs = wait(lambda: ccterm.records(), 20)
check("登记文件出现", recs and len(recs) == 1, (recs, t.text()[-500:]))
rec = recs[0]
check("登记字段齐全", all(k in rec for k in ("pid", "child", "port", "token", "cwd", "argv", "started", "title", "name", "private")))
check("名字和后台标记", rec["name"] == "exp-a" and rec["private"] is True)
check("屏幕上有输入框", screen_has(rec, "❯", 20), dump(rec))
check("假 claude 看到的大小 = 外层终端", screen_has(rec, "SIZE:100x30"), dump(rec))
check("你的终端也收到了输出", wait(lambda: "❯" in t.text()), t.text()[-500:])
check("find(子进程 pid)", ccterm.find(rec["child"]) == rec)
check("find(不相干 pid) 为 None", ccterm.find(4) is None)

# 2. 变暗的提示字（ConPTY 会重新渲染输出，未必保留 SGR 2）
rows_ansi = ccterm.screen_rows(rec, ansi=True)
note("ansi=True 标出变暗", any("\x1b[2mghost hint" in r for r in rows_ansi), rows_ansi[:3])
check("ansi=False 是纯文字", any("ghost hint" in r and "\x1b" not in r for r in ccterm.screen_rows(rec)))
check("键盘协议序列没画成乱码", not any("1u" in r for r in ccterm.screen_rows(rec)))
note("标题写进登记", wait(lambda: "假对话标题" in (ccterm.records()[0].get("title") or ""), 6), ccterm.records())

# 3. 机器人打字、粘贴、按键
ccterm.send(rec, "hello 中文" + ccterm.key_seq("Enter"))
check("send 到达（含中文）", screen_has(rec, "GOT:hello 中文"), dump(rec))
ccterm.paste(rec, "第一行\n第二行")
ccterm.send(rec, ccterm.key_seq("Enter"))
check("paste 是括号粘贴", screen_has(rec, "[PASTE:第一行"), dump(rec))
ccterm.send(rec, ccterm.key_seq("Up"))
check("方向键到达", screen_has(rec, "KEY:UP"), dump(rec))

# 4. 人在终端前打字照样能用
t.write("typed\r")
check("真人键盘输入透传", screen_has(rec, "GOT:typed"), dump(rec))

# 5. 改窗口大小（ccm 每 0.5 秒查一次，假 claude 每 0.2 秒查一次）
t.resize(132, 40)
check("改大小传到 claude", screen_has(rec, "SIZE:132x40", 15), dump(rec))
check("屏幕模拟器也跟着变", wait(lambda: len(ccterm.screen_rows(rec)) == 40, 10), len(ccterm.screen_rows(rec)))

# 6. 口令不对被拒
try:
    ccterm.screen_rows(dict(rec, token="0" * 32))
    rejected = False
except RuntimeError:
    rejected = True
check("口令不对被拒", rejected)

# 7. 环境变量：CLAUDECODE、CCM_* 不带给 claude
try:
    keys = {k.upper() for k in psutil.Process(rec["child"]).environ()}
    check("不带 CLAUDECODE / CCM_*", "CLAUDECODE" not in keys and not any(k.startswith("CCM_") for k in keys))
except psutil.Error as e:
    note("读子进程环境变量", False, e)

# 8. 退出：退出码透传、登记删掉
ccterm.send(rec, "/exit\r")
code = t.wait(15)
check("退出码透传", code == 3, (code, t.text()[-300:]))
check("登记文件删掉", wait(lambda: not ccterm.records() and not os.listdir(ccterm.TERMS_DIR), 5))
t.close()

# 9. 要求代理但连不上：不启动
s = socket.socket()
s.bind(("127.0.0.1", 0))
closed_port = s.getsockname()[1]
s.close()
t = Outer(env_extra={"CCM_REQUIRE_PROXY": f"127.0.0.1:{closed_port}"})
code = t.wait(20)
check("代理连不上拒绝启动", code == 2, (code, t.text()[-300:]))
check("没有留下登记", not ccterm.records())
t.close()

# 10. 代理通的时候照常启动；extra env 文件生效
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(4)
extra = os.path.join(DATA, "extra.json")
json.dump({"HTTPS_PROXY": "http://127.0.0.1:1"}, open(extra, "w"))
t = Outer(env_extra={"CCM_REQUIRE_PROXY": f"127.0.0.1:{srv.getsockname()[1]}", "CCM_EXTRA_ENV_FILE": extra})
rec = wait(lambda: (ccterm.records() or [None])[0], 20)
check("代理通时启动", bool(rec), t.text()[-300:])
try:
    env = {k.upper(): v for k, v in psutil.Process(rec["child"]).environ().items()}
    check("extra env 带给 claude", env.get("HTTPS_PROXY") == "http://127.0.0.1:1")
except psutil.Error as e:
    note("读子进程环境变量", False, e)
ccterm.send(rec, "/exit\r")
t.wait(15)
t.close()
srv.close()

# 11. 死掉的启动器留下的登记会被清理
wait(lambda: not ccterm.records(), 5)
os.makedirs(ccterm.TERMS_DIR, exist_ok=True)
json.dump({"pid": 999999, "child": 1, "port": 1, "token": "x"}, open(os.path.join(ccterm.TERMS_DIR, "999999.json"), "w"))
check("清理死掉的登记", not ccterm.records() and not os.listdir(ccterm.TERMS_DIR))

print(f"\n全部通过：{len(passed)} 项" + (f"；没过但不算失败：{'、'.join(soft)}" if soft else ""))
