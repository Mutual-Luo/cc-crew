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

# 6. Claude Code 自己弹的英文框（安全检查拦下回答）：翻成中文；选「不换」原地说清楚，不出「准备开始」；退回输入框的话下次发之前先清掉
FLAGGED = {"question": "Opus 5.5's safeguards flagged this session. You may be seeing this for the first time: Opus 5.5 is more capable and "
           "has stronger safeguards as a result, which can sometimes flag non-cybersecurity work. We're improving these safeguards to "
           "reduce the amount of incorrectly flagged messages. Switch to Opus 4.8 and keep going whenever this happens? You can change "
           "this later in /config.", "header": "Model switch", "multiSelect": False,
           "options": [{"label": "Switch automatically", "description": "Continue on Opus 4.8 now, and switch without asking from now on"},
                       {"label": "Stay on Opus 5.5", "description": "Stop here without switching, and ask me each time a message is flagged"}]}
built = crcore.builtin_question(FLAGGED)
check("认出安全检查的框，翻成中文", built and built["model"] == "Opus 5.5" and [o[2] for o in built["options"]] == ["switch", "stay"]
      and "Opus 4.8" in built["options"][0][0] and "网络攻击" in built["intro"], built)
check("普通的选择题不当成它", crcore.builtin_question({"question": "用哪个方案？", "options": [{"label": "A"}]}) is None)
W, ASKED = 100, "帮我看看 Claude-Red 这个项目是做什么的，会不会被拒绝执行"
DIALOG = ["❯ " + ASKED, "  ⎿  Opus 5.5's safeguards stopped the response above", "─" * W, "☐ Model switch", "",
          FLAGGED["question"][:90], FLAGGED["question"][90:], "", "❯ 1. Switch automatically",
          "     Continue on Opus 4.8 now, and switch without asking from now on", "  2. Stay on Opus 5.5",
          "     Stop here without switching, and ask me each time a message is flagged", "", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
BOX = ["上一轮的回复", "─" * W, "❯ " + ASKED[:12], "  " + ASKED[12:], "─" * W, "  Opus 5.5 · xhigh"]
EMPTY = ["上一轮的回复", "─" * W, "❯", "─" * W, "  Opus 5.5 · xhigh"]
screen_now, pressed = [DIALOG], []
saved = crcore.pane_rows, crcore.send_keys, crcore.send_text, crcore.crterm.key, crcore.last_human_text, crcore.time.sleep
def fake_key(s, k):
    pressed.append(k)
    if k == "C-c" and screen_now[0] is BOX:
        screen_now[0] = EMPTY
crcore.pane_rows = lambda s: list(screen_now[0])
crcore.send_keys = lambda s, keys: (pressed.extend(keys), screen_now.__setitem__(0, BOX))
crcore.send_text = lambda s, text, files=(): pressed.append("TEXT:" + text)
crcore.crterm.key, crcore.last_human_text, crcore.time.sleep = fake_key, lambda sid: ASKED, lambda x: None
sid = "00000000-0000-0000-0000-000000000009"
os.makedirs(crcore.ASKS, exist_ok=True)
with open(os.path.join(crcore.ASKS, sid + ".json"), "w", encoding="utf-8") as f:
    json.dump({"session_id": sid, "tool_name": "AskUserQuestion", "tool_input": {"questions": [FLAGGED]}, "tool_use_id": None, "t": time.time()}, f)
r = crcore.Remote()
r.save = lambda: None
s9 = {"sessionId": sid, "waiting": True, "status": "waiting", "statusAt": time.time() - 3, "cwd": FILES_ROOT, "pid": 1}
ask = r.ask_card("exp", s9)
text = json.dumps(ask.card, ensure_ascii=False)
check("拦下的框：中文、不贴英文原文和你自己那段话", "安全检查" in ask.card["header"]["title"]["content"] and "safeguards" not in text
      and "它前面说的" not in text and "换 Opus 4.8 接着答" in text and "对话 exp" not in text, text)
r.state["remote_pending"].pop("exp", None)
out = r.pick("exp", s9, ask.ask_id, "0", "2")
check("选「不换」：按 2，原地说这次没答，不出「准备开始」", pressed == ["2"] and out.in_place and "没答" in out.text
      and "exp" not in r.state["remote_pending"] and "#box exp send" in json.dumps(out.card, ensure_ascii=False), (pressed, out.text))
s9.update(waiting=False, status="idle")
reg = {"exp": {"pid": 1}}
r.resolve = lambda target, reg, want_alive=True: ("exp", s9, None)
r.live_card = lambda name, s, since: crcore.Card({}, "live")
r.held_files = lambda: None
r.deliver("exp", "换个问法：Claude-Red 是什么", reg)
check("输入框里退回的上一句先清掉，再发你这句", pressed == ["2", "C-c", "TEXT:换个问法：Claude-Red 是什么"], pressed)
screen_now[0], pressed[:] = ["─" * W, "❯ 我在电脑上打了一半", "─" * W], []
r.deliver("exp", "新的一句", reg)
check("你在电脑上打了一半的字不动", pressed == ["TEXT:新的一句"], pressed)
MULTI = {"question": "审稿人要这些实验，你同意做哪些？（可多选）", "header": "实验", "multiSelect": True,
         "options": [{"label": "批量打分重新计时"}, {"label": "均匀刷新对照"}]}
with open(os.path.join(crcore.ASKS, sid + ".json"), "w", encoding="utf-8") as f:
    json.dump({"session_id": sid, "tool_name": "AskUserQuestion", "tool_input": {"questions": [MULTI]}, "tool_use_id": None, "t": time.time()}, f)
screen_now[0], pressed[:] = ["─" * W, "←  ☐ 实验  ✔ Submit  →", MULTI["question"], "❯ 1. [ ] 批量打分重新计时", "2. [ ] 均匀刷新对照",
                             "3. [ ] Type something", "Submit", "─" * W, "4. Chat about this",
                             "Enter to select · ↑/↓ to navigate · Esc to cancel"], []
s9.update(waiting="input needed", status="waiting", statusAt=time.time() - 30)
r.deliver("exp", "是我们故事讲得有问题吗？还是太强调打分了", reg)
check("多选题上打字：先 Esc 关掉题，再当普通消息发出去", pressed == ["esc", "TEXT:是我们故事讲得有问题吗？还是太强调打分了"], pressed)
os.remove(os.path.join(crcore.ASKS, sid + ".json"))
MSCREEN = ["─" * W, "←  ☐ 实验  ✔ Submit  →", MULTI["question"], "❯ 1. [ ] 批量打分重新计时", "2. [ ] 均匀刷新对照",
           "3. [ ] Type something", "Submit", "─" * W, "4. Chat about this", "Enter to select · ↑/↓ to navigate · Esc to cancel"]
with open(os.path.join(crcore.ASKS, sid + ".json"), "w", encoding="utf-8") as f:
    json.dump({"session_id": sid, "tool_name": "AskUserQuestion", "tool_input": {"questions": [MULTI]}, "tool_use_id": None, "t": time.time()}, f)
screen_now[0], pressed[:] = MSCREEN, []
s9.update(statusAt=time.time() - 5)
ask = r.ask_card("exp", s9)
cmds = [b["value"]["cmd"] for e in ask.card["elements"] if e.get("tag") == "action" for b in e["actions"]]
check("多选题卡片有按钮：每项一个勾选，外加提交", f"#mpick exp {ask.ask_id} 1" in cmds and f"#msubmit exp {ask.ask_id}" in cmds, cmds)
out = r.button_cmd(["mpick", "exp", ask.ask_id, "2"], reg)
labels = [b["text"]["content"] for e in out.card["elements"] if e.get("tag") == "action" for b in e["actions"]]
check("点一下勾上，卡片原地刷新", out.in_place and "☑ 2. 均匀刷新对照" in labels and "✅ 提交（选了 1 项）" in labels and pressed == [], (labels, pressed))
r.button_cmd(["msubmit", "exp", ask.ask_id], reg)
check("提交：关掉题，把「我选：…」发给它", pressed == ["esc", "TEXT:我选：2. 均匀刷新对照。没选的不做。"], pressed)
os.remove(os.path.join(crcore.ASKS, sid + ".json"))
screen_now[0], pressed[:] = ["上一轮", "● Opus 5.5's safeguards stopped the response above · continuing once with that noted", "─" * W,
                             "Session paused", "Opus 5.5's safeguards flagged this session. You may be seeing this for the first time: "
                             "Opus 5.5 is more capable and has stronger safeguards as a result, which can sometimes flag",
                             "non-cybersecurity work. Edit and retry, or continue with Opus 4.8.", "Details: `[cyber]`",
                             "❯ 1. Switch to Opus 4.8", "2. Edit prompt and retry with Opus 5.5",
                             "✻ Waiting for API response · will retry in 2m 40s"], []
s9.update(waiting="dialog open", status="waiting", statusAt=time.time() - 60)
# 「Session paused」不走 hook，没有记录
ask = r.ask_card("exp", s9)
text = json.dumps(ask.card, ensure_ascii=False) if ask else ""
check("「Session paused」也认出来，翻成中文", ask and "暂停" in ask.card["header"]["title"]["content"] and "Session paused" not in text
      and "换 Opus 4.8 接着答" in text and "改一下再问" in text, text[:300])
check("旧卡片上的按钮不乱按", r.pick("exp", s9, "p123", "0", "1").text.startswith("✅") and pressed == [], pressed)
out = r.pick("exp", s9, ask.ask_id, "0", "1")
check("点〔换 Opus 4.8〕：按 1，卡片变成实时进度", pressed == ["1"] and out.pending_for == "exp" and r.state["remote_pending"].get("exp"), pressed)
crcore.pane_rows, crcore.send_keys, crcore.send_text, crcore.crterm.key, crcore.last_human_text, crcore.time.sleep = saved

# 7. /compact：回一张会动的卡片（计时），做完原地换成「上下文 37% → 9%」，不另发
cst = {"status": "busy", "ctx": 37}
saved7 = crcore.pane_rows, crcore.send_text, crcore.session_status, crcore.slash_catalog, crcore.discover, crcore.time.sleep
crcore.pane_rows = lambda s: ["❯ /compact", "✻ Compacting conversation… (2s)", "─" * 90, "❯", "─" * 90]
crcore.send_text, crcore.time.sleep = (lambda s, t, files=(): None), (lambda x: None)
crcore.session_status = lambda s: {"ctx": cst["ctx"], "effort": "medium", "model": "Opus 5.5"}
crcore.slash_catalog = lambda s: [("/compact", "")]
s7 = {"sessionId": "y", "status": "idle", "statusAt": time.time(), "cwd": FILES_ROOT, "pid": 1}
crcore.discover = lambda: {"exp": dict(s7, status=cst["status"])}
r = crcore.Remote()
c = r.run_slash("exp", s7, "/compact")
check("/compact：会动的进度卡片，交给实时刷新", c.pending_for == "exp" and "正在压缩上下文" in c.text, c.text)
r.state["slash"]["exp"][0] -= 23
r.save()
[(_, c, final)] = r.live_updates(["exp"])
check("跑着：计时在走", not final and "23秒" in c.card["header"]["title"]["content"], c.card["header"]["title"])
cst.update(status="idle", ctx=9)
r.state["slash"]["exp"][0] -= 30
r.save()
[(_, c, final)] = r.live_updates(["exp"])
check("做完：原地换成上下文 37% → 9%", final and "37% → 9%" in c.text and "exp" not in r.state.get("slash", {}), c.text)
crcore.pane_rows, crcore.send_text, crcore.session_status, crcore.slash_catalog, crcore.discover, crcore.time.sleep = saved7

print(f"\n全部通过：{len(passed)} 项")
