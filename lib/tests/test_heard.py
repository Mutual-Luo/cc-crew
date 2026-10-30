"""「谁跟员工说了话」的测试：对话记录里的每句话分清是飞书发的、cr 转的、你在终端里亲手打的、同事发来的、斜杠命令、
后台叫醒的，飞书卡片（思考中 / 回复 / 汇报 / 看过程 / 看板）上怎么显示；还有飞书里「@工号 + 图片」的路由。
样本是 Claude Code 2.1 实际的对话记录格式。不启动 claude，不碰真的数据，不连飞书。
运行：python lib/tests/test_heard.py"""
import json
import os
import queue
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
TMP = tempfile.mkdtemp(prefix="cr-heard-")
os.environ.update(CLAUDE_CONFIG_DIR=os.path.join(TMP, "claude"), CLAUDE_MONITOR_DATA=os.path.join(TMP, "data"),
                  CLAUDE_MONITOR_FILES_ROOT=TMP, CLAUDE_MONITOR_PROXY="", CLAUDE_MONITOR_LAUNCH_SCRIPT="",
                  CLAUDE_MONITOR_CODEX_HOMES="", CLAUDE_MONITOR_QUIET_PREFIXES="")
os.makedirs(os.environ["CLAUDE_MONITOR_DATA"])
sys.path.insert(0, LIB)
import crcore  # noqa: E402

passed = []


def check(name, cond, detail=""):
    assert cond, f"{name} 失败 {detail}"
    passed.append(name)
    print("✓", name)


crcore.pane_rows = lambda s: []                   # 不读真的屏幕
SID = "11111111-2222-3333-4444-555555555555"
T0 = int(time.time()) - 7200            # 两小时前：快照只带一天内的
proj = os.path.join(crcore.CLAUDE_DIR, "projects", "-w")
os.makedirs(proj)
path = os.path.join(proj, f"{SID}.jsonl")


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def user(t, content, **kw):
    return {"type": "user", "timestamp": stamp(t), "message": {"role": "user", "content": content}, **kw}


def queued(t, prompt, origin, **kw):
    return {"type": "attachment", "timestamp": stamp(t),
            "attachment": {"type": "queued_command", "prompt": prompt, "origin": origin, "commandMode": "prompt", **kw}}


def assistant(t, *blocks):
    return {"type": "assistant", "timestamp": stamp(t), "message": {"role": "assistant", "content": list(blocks)}}


HUMAN = {"origin": {"kind": "human"}, "promptSource": "typed", "turnOrigin": "human"}
LONG = "这是一段很长的话" * 30
records = [
    user(T0 + 1, "帮我看看日志", **HUMAN),                                                     # 飞书发的
    assistant(T0 + 2, {"type": "text", "text": "好的，先看日志。"},
              {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "tail log", "description": "看日志"}}),
    user(T0 + 3, [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]),
    queued(T0 + 10, "顺便看看显存", {"kind": "human"}, humanTurn=True),                         # 它干活时你在终端里插的话
    queued(T0 + 20, "<cross-session-message from=\"uds:/tmp/cc-socks-1000/4242.sock\" from-name=\"评测基准\">\n"
                    "search-api 别重启\n</cross-session-message>",
           {"kind": "peer", "name": "评测基准", "verifiedPeerPid": 4242, "body": "search-api 别重启"}),   # 同事发来的
    queued(T0 + 25, "<task-notification>\n<summary>Background command done</summary>\n</task-notification>",
           {"kind": "task-notification"}),                                                      # 后台任务做完叫醒它
    queued(T0 + 31, [{"type": "text", "text": "[Image #5]"}], {"kind": "human"}, imagePasteIds=[5]),   # 飞书只发了一张图
    user(T0 + 41, "把字体改大一点", **HUMAN),                                                   # 秘书用 cr 转的
    user(T0 + 51, "\n\n<pasted_content id=\"ab12\">\n第一行\n第二行\n</pasted_content id=\"ab12\">", **HUMAN),   # 飞书发的多行
    user(T0 + 60, "<command-name>/effort</command-name>\n            <command-message>effort</command-message>\n"
                  "            <command-args>high</command-args>"),                                # 你在终端里打的斜杠命令
    user(T0 + 61, "<local-command-stdout>Set effort level to high</local-command-stdout>"),
    user(T0 + 70, "Stop hook feedback:\n[继续]: keep going", isMeta=True),
    user(T0 + 80, LONG, **HUMAN),                                                                  # 你在终端里打的一大段
    assistant(T0 + 90, {"type": "text", "text": "做完了，字体已经改大。"}),
]
with open(path, "w", encoding="utf-8") as f:
    f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

NAME = "exp-ui"
E = {"agent": "claude", "alive": True, "sessionId": SID, "cwd": TMP, "status": "idle", "statusAt": T0 + 91,
     "startedAt": T0 - 500, "pid": 999}
crcore.save_json(crcore.REGISTRY, {NAME: E, "eval-bot": {"agent": "claude", "alive": True, "pid": 4242, "peer": "评测基准"},
                                   "助手": {"agent": "claude", "alive": True, "pid": 7}})
crcore.save_json(crcore.BADGES, {NAME: 2, "eval-bot": 5})

# 1. 机器人替人打的字记进 sent.jsonl
crcore.mark_sent_start(T0 - 100)
fake = {"sessionId": SID}


def sent_at(t, text, files=0, via="feishu", by=""):
    real, crcore.now = crcore.now, lambda: t
    crcore.VIA.update(via=via, by=by)
    try:
        crcore.note_sent(fake, text, files)
    finally:
        crcore.now = real
        crcore.VIA.update(via="feishu", by="")


sent_at(T0 + 0.5, "帮我看看日志")
sent_at(T0 + 30, "", files=1)
sent_at(T0 + 40, "把字体改大一点", via="cli", by="助手")
sent_at(T0 + 50, "第一行\n第二行")
check("机器人替人打的字记下来了（飞书 / cr）", len(crcore._sent_tail()) == 5)

# 2. 对话记录里的每句话：谁说的
asked, steps, stopped = crcore.turn_steps(SID, T0)
said = [x for k, x, _ in steps if k == "said"]
got = [(x["kind"], x["src"], x.get("by"), x["text"][:12]) for x in said]
check("分清来源：飞书 / 终端 / 同事 / 后台 / cr / 斜杠命令", got == [
    ("human", "feishu", "", "帮我看看日志"),
    ("human", "terminal", "", "顺便看看显存"),
    ("peer", "peer", "eval-bot", "search-api 别"),
    ("background", "background", "", "Background c"),
    ("human", "feishu", "", ""),
    ("human", "cli", "助手", "把字体改大一点"),
    ("human", "feishu", "", "第一行\n第二行"),
    ("slash", "terminal", "", "/effort high"),
    ("human", "terminal", "", LONG[:12]),
], got)
check("你从飞书发的那句话（粘贴的包装去掉了）", asked == "帮我看看日志" and said[6]["text"] == "第一行\n第二行")
check("只发了图：记着几张图", said[4]["images"] == 1)
check("过程照常：它说的话、跑的命令（带耗时）", ("tool", ("⚙️ 运行命令", "看日志", 1.0), True) in steps and not stopped, steps)
SID2 = "22222222-2222-3333-4444-555555555555"
with open(os.path.join(proj, f"{SID2}.jsonl"), "w", encoding="utf-8") as f:
    f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
        user(T0, "想一想", **HUMAN), assistant(T0 + 12, {"type": "thinking", "thinking": "let me think"}),
        assistant(T0 + 13, {"type": "text", "text": "想好了"})]))
think = [t for k, t, _ in crcore.turn_steps(SID2, T0)[1] if k == "think"]
check("思考带时长：这条记录的时间减上一条", think == [("let me think", 12.0)], think)
line = crcore.Remote().timeline(crcore.turn_steps(SID2, T0)[1])["elements"][0]["content"]
check("时间线上写「思考 12秒」", "思考　12秒" in line, line)

# 3. 卡片上怎么写
md, cut = crcore.said_md(said)
check("卡片只列别处来的话（飞书发的、后台叫醒的不重复列），多了只留最近 3 句", md.startswith("<font color='grey'>…前面还有 2 句</font>")
      and "飞书" not in md and "后台" not in md and md.count("\n") == 3 and cut, md)
check("同事、cr 转话、斜杠命令、你亲手打的各有说法",
      "**5号 eval-bot**：" in crcore.said_md(said, 9)[0] and "**助手**：把字体改大一点" in md
      and "**你 · 终端** · 命令 /effort high" in md and "**你 · 终端**：这是一段很长的话" in md, md)
line, cut1 = crcore.said_line(said[-1])
check("太长就截断（〔🔎 看过程〕看全文）", cut1 and "…" in line and len(line) < 200)
check("没经过机器人的同一句话：算你亲手打的", crcore.label_said(SID, [dict(said[1], src=None)])[0]["src"] == "terminal")
real_now, crcore.now = crcore.now, lambda: T0 + 95
crcore.note_sent({"pid": 999}, "新会话的第一句")              # codex 发第一句话前还没有会话 id：按进程对
crcore.now = real_now
check("发的时候还没有会话 id（codex 刚开）：按进程对上", crcore.label_said(SID, [{"t": T0 + 96, "kind": "human", "text": "新会话的第一句"}])[0]["src"]
      == "feishu")
check("sent.jsonl 开始记之前的话：分不清，不列", crcore.label_said(SID, [{"t": T0 - 500, "kind": "human", "text": "早先的话"}])[0]["src"] == "unknown"
      and not crcore.said_shown({"src": "unknown", "kind": "human"}))

r = crcore.Remote()
live = r.live_card(NAME, dict(E, status="busy"), T0)
body = json.dumps(live.card, ensure_ascii=False)
quote = json.dumps([el for el in live.card["elements"] if el["tag"] == "column_set"], ensure_ascii=False)
check("实时卡片：说给它的话放在消息块里，来源是小标签，同来源接着说的前面一个淡「·」，最多 4 行", "还有 5 条更早的" in body
      and ">你 · 飞书</text_tag> 第一行" in quote and ">你 · 终端</text_tag>" in quote and "/effort" in quote
      and "<font color='grey'>·</font> 这是一段很长的话" in quote and quote.count("\\n") == 1, quote)
check("实时卡片：「···」里能看过程（全文）", f"#steps {NAME} {T0}" in body)
reply = r.reply_card(NAME, E, T0 + 40)
body = json.dumps(reply.card, ensure_ascii=False)
check("回复卡片：最上面是这期间别处来的话（助手转的、你在终端里打的斜杠命令），飞书发的不重复", ">助手</text_tag>" in body
      and "/effort" in body and ">你 · 终端</text_tag>" in body and "第一行" not in body.split("做完了")[0], body[:600])
turn = crcore.last_turn(SID)
check("最后一轮：从你在终端里打的那段开始", turn["t"] == T0 + 80 and [x["src"] for x in turn["said"]] == ["terminal"], turn)
stop = r.stop_card(NAME, E, turn)
body = json.dumps(stop.card, ensure_ascii=False)
check("汇报卡片：写上这一轮是你在终端里叫它干的", "📝" in stop.text and "你 · 终端" in body and "你 · 终端" in stop.text, body[:500])
check("汇报卡片：截断了给〔🔎 看过程〕", f"#steps {NAME} {T0 + 80}" in body)
check("合成一张时也带着起因", "你 · 终端" in json.dumps((stop.merge or {}).get("said", ""), ensure_ascii=False))
merged = crcore.merged_card([stop.merge], stop)
check("合成的卡片：前一条的起因也在", json.dumps(merged.card, ensure_ascii=False).count("你 · 终端") == 2)
steps_card = json.dumps(r.steps_card(NAME, E, T0).card, ensure_ascii=False)
check("〔🔎 看过程〕：每句话都在，全文不截断（飞书发的也列上）", LONG in steps_card and "你 · 飞书" in steps_card
      and "5号 eval-bot" in steps_card, steps_card[:400])

# 4. 守护进程边看边记 heard.jsonl（卡片、手机 App 读）；看板上干活的员工写上起因
r.heard_tick(NAME, E, [dict(x) for x in said])
log = crcore.heard_log(SID)
check("heard.jsonl：每句话带来源，Claude / Codex 一个格式", len(log) == len(said) and log[1]["src"] == "terminal"
      and set(log[0]) == {"name", "sid", *crcore.SAID_KEYS}, log[:2])
check("对话记录读到的和 heard.jsonl 记下的合起来不重复", len(crcore.said_between(SID, T0, extra=said)) == len(said))
check("你在终端里亲手打了字：算你醒着（夜里照常推）", r.state.get("seen_you") == T0 + 80)
crcore.save_json(crcore.REGISTRY, {NAME: dict(E, status="busy", statusAt=T0 + 79)})
board = r.status_card()
check("看板：干活中的员工写上起因", "🟢 干活中" in board.text and "你 · 终端：这是一段很长的话" in board.text, board.text)
import cragents  # noqa: E402
snap = cragents.snapshot(r, crcore.load_json(crcore.REGISTRY, {}), with_pending=False)
emp = next(x for x in snap["employees"] if x["name"] == NAME)
check("统一快照（手机 App 用）：每个员工带最近听到的话", [x["src"] for x in emp["heard"]][-3:] == ["feishu", "terminal", "terminal"]
      and "by" in emp["heard"][0], emp["heard"][-1])

# 5. 命令行 cr：在哪个员工的终端里跑的（秘书派活）
crcore.save_json(crcore.REGISTRY, {"助手": {"alive": True, "socket": os.path.join(TMP, "x.sock"), "pane": "%5"}})
os.environ.update(TMUX=os.path.join(TMP, "x.sock") + ",1,0", TMUX_PANE="%5")
check("cr 是秘书在自己的终端里跑的：记成助手转的", crcore.caller_name() == "助手")
os.environ["TMUX_PANE"] = "%6"
check("不是员工的终端：你自己用 cr 发的", crcore.caller_name() == "")
del os.environ["TMUX"], os.environ["TMUX_PANE"]

# 6. 飞书里「@工号 + 图片」：图在前在后都发给那个员工，图跟着过去
import crbot  # noqa: E402
crcore.save_json(crcore.REGISTRY, {NAME: E})
r = crbot.remote
got = []
r.deliver = lambda target, body, reg, urgent=False: got.append((target, body, len((r.held_files() or {}).get("paths", [])))
                                                             + (("立即",) if urgent else ())) or "OK"
r.say = lambda text, chat, reg: got.append(("秘书", text)) or "SAY"


class Bot:
    group, allow = None, "me"

    def __init__(self):
        self.jobs = queue.Queue()

    claim = authorized = lambda self, x: True
    bump = create = lambda self, *a, **k: None


img = {"tag": "img", "image_key": "img_1"}
for label, rows, want in [("@2 + 图", [[{"tag": "text", "text": "@2 "}, img]], (NAME, "", 1)),
                          ("图 + @2", [[img], [{"tag": "text", "text": "@2"}]], (NAME, "", 1)),
                          ("图 + @2 加一句话", [[img, {"tag": "text", "text": " @2 看看这个logo"}]], (NAME, "看看这个logo", 1)),
                          ("@名字 + 图", [[{"tag": "text", "text": f"@{NAME}"}], [img]], (NAME, "", 1)),
                          ("@所有人 + 图：交给秘书", [[{"tag": "text", "text": "@所有人"}], [img]], ("秘书", "@所有人"))]:
    b = Bot()
    crbot.Feishu.receive(b, "m1", "p2p", "c", "me", "post", json.dumps({"title": "", "content": rows}), None)
    _, _, files, text, quoted = b.jobs.get_nowait()
    r.state.pop("attach", None)
    crcore.save_json(crcore.STATE, r.state)
    got.clear()
    r.handle_files([os.path.join(TMP, "a.png")] * len(files), text, quoted)
    check(f"飞书「{label}」", got == [want], (text, got))
r.state.pop("attach", None)
crcore.save_json(crcore.STATE, r.state)
got.clear()
r.handle("@2", typed=True)
check("只打「@2」没带图：照旧交给秘书", got == [("秘书", "@2")], got)
got.clear()
r.handle("@2 看看", typed=True)
check("「@2 看看」：照旧直接发", got == [(NAME, "看看", 0)], got)

# 7. Claude Code 把对话挪成后台任务（parked）：tmux 里那个只是前台，状态、对话记录、同事名都跟后台那个走
crcore.crcodex.discover = lambda: []                       # 不认这台机器上真的 codex
crcore.crterm.attach_info = lambda pid, d: {"term": "tmux", "socket": "/s", "pane": "%17", "group": "paper", "window": "0",
                                            "title": "", "private": False}
sess = os.path.join(crcore.CLAUDE_DIR, "sessions")
os.makedirs(sess, exist_ok=True)
FRONT, BACK = os.getpid(), os.getppid()                   # 两个活着的进程当前台、后台
crcore.save_json(os.path.join(sess, f"{FRONT}.json"), {"pid": FRONT, "sessionId": "old-sid", "kind": "interactive", "status": "idle",
                                                       "statusUpdatedAt": 1000, "cwd": TMP, "name": "proj-07", "parkedJobId": "a1b2c3d4"})
crcore.save_json(os.path.join(sess, f"{BACK}.json"), {"pid": BACK, "sessionId": SID, "kind": "bg", "jobId": "a1b2c3d4", "status": "busy",
                                                      "statusUpdatedAt": (T0 + 95) * 1000, "cwd": TMP, "name": "评测基准"})
found = crcore.discover()
d = found.get("paper")
check("停放的会话：跟着后台任务看状态和对话记录，终端还是 tmux 里那个", d and d["sessionId"] == SID and d["status"] == "busy"
      and d["pid"] == FRONT and d["jobPid"] == BACK and d["pane"] == "%17" and d["peer"] == "评测基准", d)
check("后台任务自己不算一个员工", list(found) == ["paper"], list(found))
crcore.save_json(crcore.REGISTRY, {NAME: E, "paper": dict(d, alive=True)})
peer_said = crcore.label_said(SID, [{"t": T0, "kind": "peer", "from": "评测基准", "pid": BACK, "text": "别重启"}])[0]
check("后台任务发来的同事消息：认得出是哪个员工", peer_said["by"] == "paper", peer_said)

# 8. goal 自己停了：推一条（跟它 10 分钟内的汇报合成一张）；你打断它导致的暂停不推
goal = crcore.goal_event({"type": "system", "subtype": "informational", "timestamp": stamp(T0),
                          "content": "Goal paused · the goal check timed out · send a message to continue"})
check("Claude 的 goal 暂停：原因说成人话", goal and goal["why"] == "检查 goal 有没有达成时超时了" and goal["resume"], goal)
cx = lambda status: crcore.goal_event({"type": "event_msg", "timestamp": stamp(T0),
                                       "payload": {"type": "thread_goal_updated", "goal": {"status": status}}})
check("codex：你打断它变成 paused 不推，完成了推", cx("paused") is None and cx("active") is None and cx("complete")["what"] == "done")
r = crcore.Remote()
card = r.goal_card(NAME, E, goal)
check("goal 暂停卡片：为什么停、怎么让它接着干，能并进汇报", "goal 暂停了" in card.text and "回它一句话就会接着干" in card.text
      and card.merge["label"] == "⏸ goal 暂停", card.text)

# 9. 你在终端里叫它干活（它手上没有飞书那张实时卡片）：跟飞书发的一样推一张实时卡片，做完换成结论
crcore.save_json(crcore.REGISTRY, {NAME: dict(E, status="busy", statusAt=time.time() - 1)})
r = crcore.Remote()
r.state["remote_pending"] = {}
before = os.path.getsize(crcore.OUTBOX) if os.path.exists(crcore.OUTBOX) else 0
typed = {"t": time.time(), "kind": "human", "from": "", "text": "还没好么", "images": 0}
r.heard_tick(NAME, dict(E, status="busy", statusAt=time.time() - 1), [typed])
with open(crcore.OUTBOX, encoding="utf-8") as f:
    f.seek(before)
    out = [json.loads(line) for line in f]
live_item = next((x for x in out if x.get("pending_for") == NAME), None)
check("终端里发起的一轮：推实时卡片，标明你在终端里说的话", live_item and "你 · 终端" in json.dumps(live_item["card"], ensure_ascii=False)
      and "还没好么" in json.dumps(live_item["card"], ensure_ascii=False) and r.state["remote_pending"][NAME] == typed["t"], out)
before = os.path.getsize(crcore.OUTBOX)
r.heard_tick(NAME, dict(E, status="busy"), [dict(typed, t=time.time(), text="再快点")])
with open(crcore.OUTBOX, encoding="utf-8") as f:
    f.seek(before)
    check("已经有实时卡片了：插的话只并进去，不再推新卡片", not [x for x in map(json.loads, f) if x.get("pending_for")])
r.state["remote_pending"] = {}
before = os.path.getsize(crcore.OUTBOX)
r.heard_tick(NAME, dict(E, status="busy"), [dict(typed, t=time.time() - 3600)])
with open(crcore.OUTBOX, encoding="utf-8") as f:
    f.seek(before)
    check("一小时前的旧话（守护进程停了一阵子）：不补推实时卡片", not [x for x in map(json.loads, f) if x.get("pending_for")])

# 10. 底栏：模型、上下文、额度做成小标签，快满了变色；提示一行浅灰小字，少表情、不用全角括号
tags = crcore.usage_tags({"model": "Opus 5.5", "ctx": 82, "agent": "codex", "5h": 2, "7d": 75})
check("底栏标签：上下文 82% 标红；额度合成一个，最高 75% 标橙；不用 ⚠️",
      tags == "<text_tag color='neutral'>Opus 5.5</text_tag> <text_tag color='red'>上下文 82%</text_tag> "
              "<text_tag color='orange'>额度 2% / 75%</text_tag>", tags)
r.state.update(current=None, default="助手")
foot = crcore._card(f"⏳【{NAME}】正在思考", "wathet", [], tags=tags, live=True, state=r.state)["elements"]
check("底栏：一行标签，时间跟在最后（自动刷新的写明）；平时不加提示行", len(foot) == 1 and foot[0]["content"].startswith(tags)
      and "自动刷新 " in foot[0]["content"], foot)
plain = crcore._card("📋 员工看板", "wathet", [], state=r.state)["elements"]
check("没有用量的卡片：只有一行灰字", len(plain) == 1 and "〔" not in json.dumps(plain, ensure_ascii=False) and "⭐" not in json.dumps(plain, ensure_ascii=False), plain)
r.state["current"] = NAME
check("正在跟某个员工单聊：才提示一句怎么回到助手", crcore.talking_to(r.state) == "你正在和 2号 单聊，打的字都发给它  ·  发“回到助手”结束")

# 11. 它在忙时发的话在排队：认出来，给〔立即发送〕；「@3! 内容」直接立即发
QUEUED = ["● Running 45-second Python sleep in foreground · 3s", "  ⎿  $ python3 -c 'import time; time.sleep(45)' (3s)",
          "❯ 排队测试：这句会排队吗", "  ctrl+x ctrl+s to send now", "✶ Frolicking… (7s · ↓ 290 tokens)", "",
          "─" * 40, "❯ Press up to edit queued messages", "─" * 40, "  Haiku 5.5 · low · ctx 4% · 5h 6% · 7d 25%"]
check("屏幕上认出排队的话", crcore.queued(QUEUED) == ["排队测试：这句会排队吗"] and crcore.queued(QUEUED[:2]) == [])
check("遥控键盘认 ctrl+字母（不再当成字打进去）", crcore.key_name("ctrl-x") == "C-x" and crcore.key_name("Ctrl+S") == "C-s"
      and crcore.key_name("C-c") == "C-c" and crcore.key_name("hello") is None)
import ccterm  # noqa: E402
check("ccm 里也能按 ctrl+x ctrl+s", ccterm.key_seq("C-x") == "\x18" and ccterm.key_seq("C-s") == "\x13")
crcore.save_json(crcore.REGISTRY, {NAME: E})
got.clear()
crbot.remote.handle("@2! 先停下看我这句", typed=True)
check("「@2! 内容」：立即发", got == [(NAME, "先停下看我这句", 0, "立即")], got)
got.clear()
crbot.remote.handle(f"@{NAME}！ 先停下", typed=True)
check("「@名字！ 内容」（全角叹号）也认", got == [(NAME, "先停下", 0, "立即")], got)
real_pane = crcore.pane_rows
crcore.pane_rows = lambda s: QUEUED
live = json.dumps(crcore.Remote().live_card(NAME, dict(E, status="busy"), T0).card, ensure_ascii=False)
crcore.pane_rows = real_pane
check("实时卡片：排着队的话标「排队中」，给〔立即发送〕", "排队中" in live and f"#sendnow {NAME}" in live, live[-800:])

print(f"\n全部通过：{len(passed)} 项")
