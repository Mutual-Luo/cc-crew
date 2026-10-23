"""机器人核心（crcore / crterm / crcli）在各个系统上的冒烟测试：用临时目录，假 claude 跑在 ccm 里，
伪造一份 Claude Code 的会话登记，看机器人能不能找到它、读屏、打字；再测几个和路径有关的小函数。
运行：python lib/tests/test_core_smoke.py（Windows / macOS / Linux 都能跑；不碰真的数据，不启动真的 claude）"""
import json
import os
import sys
import tempfile
import time

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.dirname(HERE)
REPO = os.path.dirname(LIB)
TMP = tempfile.mkdtemp(prefix="cr-smoke-")
FILES_ROOT = os.path.join(TMP, "work")
CLAUDE_DIR = os.path.join(TMP, "claude")
os.makedirs(os.path.join(CLAUDE_DIR, "sessions"))
os.makedirs(os.path.join(FILES_ROOT, "proj"))
# 先设好环境变量：crcli 读 config.env 时只补没设过的，所以本机真实配置不会混进来
os.environ.update(CLAUDE_CONFIG_DIR=CLAUDE_DIR, CLAUDE_MONITOR_DATA=os.path.join(TMP, "data"),
                  CLAUDE_MONITOR_FILES_ROOT=FILES_ROOT, CLAUDE_MONITOR_PROXY="", CLAUDE_MONITOR_LAUNCH_SCRIPT="",
                  CLAUDE_MONITOR_QUIET_PREFIXES="")
os.makedirs(os.environ["CLAUDE_MONITOR_DATA"])
sys.path.insert(0, LIB)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import crcli  # noqa: E402
import crcore  # noqa: E402
import crterm  # noqa: E402
import ccterm  # noqa: E402
import install  # noqa: E402
from harness import Outer, wait  # noqa: E402

passed = []


def check(name, cond, detail=""):
    assert cond, f"{name} 失败 {detail}"
    passed.append(name)
    print("✓", name)


check("配置指向临时目录", crcore.CLAUDE_DIR == CLAUDE_DIR and crcore.DATA == os.environ["CLAUDE_MONITOR_DATA"]
      and ccterm.TERMS_DIR.startswith(TMP), (crcore.CLAUDE_DIR, crcore.DATA, ccterm.TERMS_DIR))

# 1. ccm 里跑假 claude，伪造 Claude Code 的会话登记，机器人能找到、读屏、打字
t = Outer(env_extra={"CCM_NAME": "exp-smoke"})
rec = wait(lambda: (ccterm.records() or [None])[0], 20)
check("ccm 登记出现", bool(rec), t.text()[-300:])
with open(os.path.join(CLAUDE_DIR, "sessions", f"{rec['child']}.json"), "w") as f:
    json.dump({"pid": rec["child"], "sessionId": "00000000-0000-0000-0000-000000000001",
               "cwd": os.path.join(FILES_ROOT, "proj"), "kind": "interactive", "status": "idle",
               "startedAt": int(time.time() * 1000)}, f)
found = crcore.discover()
check("discover 找到它", "exp-smoke" in found, found)
s = found["exp-smoke"]
check("终端类型是 ccm", s["term"] == "ccm" and crterm.controllable(s), s.get("term"))
check("读屏不为空", wait(lambda: any("❯" in r for r in crcore.pane_rows(s)), 10), crcore.pane_rows(s))
crcore.send_text(s, "你好 smoke")
check("send_text 到达", wait(lambda: any("GOT:你好 smoke" in r for r in crcore.pane_rows(s)), 10), crcore.pane_rows(s))
crcore.send_text(s, "第一行\n第二行")
check("多行走粘贴", wait(lambda: any("[PASTE:第一行" in r for r in crcore.pane_rows(s)), 10), crcore.pane_rows(s))
crcore.send_keys(s, ["上"])
check("send_keys 到达", wait(lambda: any("KEY:UP" in r for r in crcore.pane_rows(s)), 10), crcore.pane_rows(s))
check("卡片上的目录写成 ~", crcore._short_dir(s["cwd"]).replace("\\", "/").endswith("/proj")
      and crcore._short_dir(s["cwd"]).startswith("~"), crcore._short_dir(s["cwd"]))
crcore.send_text(s, "/exit")
check("退出", t.wait(15) == 3)
t.close()
check("退出后 discover 不再有它", wait(lambda: "exp-smoke" not in crcore.discover(), 5))

# 2. 没在 tmux / ccm 里的会话：能看到，但不能遥控
with open(os.path.join(CLAUDE_DIR, "sessions", f"{os.getpid()}.json"), "w") as f:
    json.dump({"pid": os.getpid(), "sessionId": "00000000-0000-0000-0000-000000000002",
               "cwd": FILES_ROOT, "kind": "interactive", "status": "idle"}, f)
found = crcore.discover()
plain = [v for v in found.values() if v["pid"] == os.getpid()]
check("直接打 claude 的会话也能看到", len(plain) == 1, found)
check("但标成不能遥控", plain[0]["term"] == "none" and not crterm.controllable(plain[0]))
try:
    crcore.send_text(plain[0], "x")
    refused = False
except crterm.NoTerminal:
    refused = True
check("对它打字会被拒绝（不会乱发）", refused)
os.remove(os.path.join(CLAUDE_DIR, "sessions", f"{os.getpid()}.json"))

# 3. 路径相关的小函数
check("under_root：目录下面", crcore.under_root(os.path.join(FILES_ROOT, "proj", "a.png")))
check("under_root：外面的不算", not crcore.under_root(TMP) and not crcore.under_root(os.path.dirname(TMP)))
if os.name == "nt":
    parts = crcore.split_args(r"exp C:\Users\me\proj --x")
    check("Windows 路径的反斜杠保留", parts == ["exp", r"C:\Users\me\proj", "--x"], parts)
    parts = crcore.split_args(r'exp "C:\Program Files\x" --x')
    check("带空格的 Windows 路径", parts == ["exp", r"C:\Program Files\x", "--x"], parts)
else:
    parts = crcore.split_args("exp '/tmp/with space' --x")
    check("带空格的路径", parts == ["exp", "/tmp/with space", "--x"], parts)
check("install.short_path 没空格时不变", install.short_path(os.path.join(TMP, "a")) == os.path.join(TMP, "a").replace("\\", "/"))
check("preflight：目录不存在", "不存在" in crcore.preflight(os.path.join(FILES_ROOT, "nope")))
check("preflight：目录不在允许的范围", "不在" in crcore.preflight(TMP))

# 4. cr 的几个只读命令（不碰开机自启、不启动机器人）
st = crcli.status(2)
check("cr status 能跑", "机器人" in st and "看门狗" in st, st)
check("cr 通知（还没有）", "还没有" in crcli.notices(5) or crcli.notices(5) == "", crcli.notices(5))
check("帮助文字", "助手" in str(crcore.Remote().handle("帮助")))

# 5. 降噪：只有真要你选的才算拍板；夜里不用你拍板的攒着；同一个员工的汇报合成一张
check("问选哪个算拍板", crcore.is_decision("两个方案：\nA. 改成直观版\nB. 去掉这张图\n你选哪个？"))
check("要你贴日志不算拍板", crcore.ask_kind("你把 nvidia-smi 的输出贴给我？")[0] == "info"
      and not crcore.is_decision("你把 nvidia-smi 的输出贴给我？"))
proj = os.path.join(CLAUDE_DIR, "projects", "p")
os.makedirs(proj)
with open(os.path.join(proj, "s.jsonl"), "w", encoding="utf-8") as f:   # 6 天里每天 10 点、22 点各说一句话
    for d in range(6):
        for hour in (10, 22):
            t = time.mktime(time.localtime(time.time() - (d + 1) * 86400)[:3] + (hour, 0, 0, 0, 0, -1))
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
            f.write(json.dumps({"type": "user", "timestamp": stamp, "message": {"role": "user", "content": "继续"}}) + "\n")
check("按说话的钟点算夜里", crcore.learn_night() == [23, 9], crcore.learn_night())
r = crcore.Remote()
r.state.update(seen_you=0, held=0, night={"day": time.strftime("%Y-%m-%d"), "hours": [time.localtime().tm_hour, (time.localtime().tm_hour + 1) % 24]})
check("夜里、你没在用：免打扰", r.quiet_now())
report = crcore.alert("📝【exp】：汇报", "跑完了", "orange")
report.merge = {"name": "exp", "label": "📝 汇报", "body": "跑完了", "t": time.time()}
r.push(report, "idle")
r.push(crcore.alert("🙋【exp】：老板，需要你拍板", "选哪个？", "carmine"), "idle", must=True)
check("拍板照推，汇报攒着", r.state["held"] == 1 and "拍板" in crcli.notices(5) and "汇报" not in crcli.notices(5), crcli.notices(5))
r.push(report, "idle")
out = r.release_held()
check("攒下的点了再发，同一个员工合成一张", "攒下的 2 条" in out.text and len(out.more) == 1
      and "（2 条）" in out.more[0].card["header"]["title"]["content"], [m.card["header"]["title"] for m in out.more])
r.state["seen_you"] = time.time()
check("你刚动过：不算夜里", not r.quiet_now())

print(f"\n全部通过：{len(passed)} 项")
