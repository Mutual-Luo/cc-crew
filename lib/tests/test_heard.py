"""「谁跟员工说了话」的测试：对话记录里的每句话分清是飞书发的、cr 转的、你在终端里亲手打的、同事发来的、斜杠命令、
后台叫醒的，飞书卡片（思考中 / 回复 / 汇报 / 看过程 / 看板）上怎么显示；还有飞书里「@工号 + 图片」的路由。
样本是 Claude Code 2.1 实际的对话记录格式。不启动 claude，不碰真的数据，不连飞书。
运行：python lib/tests/test_heard.py"""
import json
import os
import queue
import sys
import tempfile
import threading
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
check("回复卡片：最上面是这期间说给它的话（助手转的、终端里打的斜杠命令、飞书发的也列：实时卡片撤回后看得出答的是哪几句）",
      ">助手</text_tag>" in body and "/effort" in body and ">你 · 终端</text_tag>" in body and "第一行" in body.split("做完了")[0],
      body[:600])
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
check("看板：干活中的员工写上起因", "干活中 1" in board.text and "你 · 终端：这是一段很长的话" in board.text, board.text)
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
    _, _, files, text, quoted, _ = b.jobs.get_nowait()
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

# 6b. 助手单独一个对话框（你和机器人的单聊）：在那儿说的都给助手；助手的消息发到单聊，别的员工的、看板发到群里
b = Bot()
b.group = "grp"
for chat_type, chat_id, want in [("p2p", "dm-chat", "dm"), ("group", "grp", "group"), ("group", "别的群", None)]:
    crbot.Feishu.receive(b, "m-" + chat_id, chat_type, chat_id, "me", "text", json.dumps({"text": "你好"}), None)
    job = b.jobs.get_nowait() if not b.jobs.empty() else None
    check(f"收消息：{chat_type} {chat_id} 记成 {want}", (job[-1] if job else None) == want, job)
say0 = r.say
r.say = lambda text, chat, reg: got.append(("say", chat)) or "SAY"
r.state.update(current=NAME, current_at=time.time(), default="助手")
crcore.save_json(crcore.STATE, r.state)
got.clear()
r.handle("帮我看下进度", typed=True)
r.handle("帮我看下进度", typed=True, dm=True)
check("群里正跟某个员工对话时：群里打的字给它，单聊里打的字给助手（不算在对话里）", got == [("say", NAME), ("say", None)], got)
got.clear()
r.handle_files([os.path.join(TMP, "a.png")], "看这张图", None, dm=True)
check("单聊里发图加一句话：给助手", got == [("助手", "看这张图", 1)], got)
# 「截图 + @6 文字」：文字开头 @了人就发给被 @ 的人，图跟着过去——单聊（默认给助手）、引用了别人的卡片都不例外
r.say = lambda text, chat, reg: got.append(("say", chat, text)) or "SAY"
for where in ("dm", "group"):
    for order in ("图在前", "图在后", "图在中间"):
        for at in ("@2", f"@{NAME}"):
            words = [{"tag": "text", "text": f"{at} 这也不对呀"}]
            rows = {"图在前": [[img], words], "图在后": [words, [img]],
                    "图在中间": [[{"tag": "text", "text": f"{at} "}], [img], [{"tag": "text", "text": "这也不对呀"}]]}[order]
            b = Bot()
            b.group = "grp"
            crbot.Feishu.receive(b, "m1", "p2p" if where == "dm" else "group", "dm-chat" if where == "dm" else "grp", "me", "post",
                                 json.dumps({"title": "", "content": rows}), None)
            _, _, files, text, quoted, side = b.jobs.get_nowait()
            r.state.pop("attach", None)
            crcore.save_json(crcore.STATE, r.state)
            got.clear()
            r.handle_files([os.path.join(TMP, "a.png")] * len(files), text, quoted, dm=side == "dm")
            check(f"{'单聊' if where == 'dm' else '群里'}「{order} + {at} 文字」：发给被 @ 的人，图跟着过去，不记成给助手",
                  got == [(NAME, "这也不对呀", 1)] and not (r.held_files() or {}).get("for"), (text, got, r.held_files()))
for label, call, want in [
        ("引用 5号的卡片发「截图 + @2 文字」：以 @2 为准", lambda: r.handle_files([os.path.join(TMP, "a.png")], "@2 这也不对呀", "eval-bot"),
         [(NAME, "这也不对呀", 1)]),
        ("引用 5号的卡片打「@2 文字」：以 @2 为准", lambda: r.handle("@2 这也不对呀", typed=True, to="eval-bot"), [(NAME, "这也不对呀", 0)]),
        ("引用 5号的卡片打字没 @：照旧发给 5号", lambda: r.handle("这也不对呀", typed=True, to="eval-bot"), [("eval-bot", "这也不对呀", 0)]),
        ("单聊「截图 + 文字」没 @：照旧给助手", lambda: r.handle_files([os.path.join(TMP, "a.png")], "这也不对呀", None, dm=True),
         [("助手", "这也不对呀", 1)]),
        ("单聊「截图 + 文字中间带 @」（讨论里提到 @2、邮箱）：不算点名，照旧给助手",
         lambda: r.handle_files([os.path.join(TMP, "a.png")], "上次 @2 说的不对，发 a@b.com 问问", None, dm=True),
         [("助手", "上次 @2 说的不对，发 a@b.com 问问", 1)]),
        ("单聊打字中间带 @：不算点名，照旧给助手", lambda: r.handle("看下 x@y.com 和 @2 的代码", typed=True, dm=True),
         [("say", None, "看下 x@y.com 和 @2 的代码")]),
        ("引用 5号的卡片、文字中间带 @2：照旧发给 5号", lambda: r.handle("跟 @2 说的一样吗", typed=True, to="eval-bot"),
         [("eval-bot", "跟 @2 说的一样吗", 0)]),
        ("单聊「截图 + @助手 文字」：给助手（交给 say，不算群里正在对话的人）",
         lambda: r.handle_files([os.path.join(TMP, "a.png")], "@助手 这也不对呀", None, dm=True), [("say", None, "这也不对呀")])]:
    r.state.pop("attach", None)
    crcore.save_json(crcore.STATE, r.state)
    got.clear()
    call()
    check(label, got == want, got)
r.state.pop("attach", None)
r.state.update(current=None, default=None)
crcore.save_json(crcore.STATE, r.state)
r.say = say0


class Sides(crbot.Feishu):
    def __init__(self, group="grp"):
        self.group, self.allow, self.sent = group, "me", []
        self.after, self.merges, self.chat_of = {}, {}, {}
        self.slot_lock = threading.Lock()

    def create(self, msg_type, content, p2p=False):
        self.sent.append("dm" if p2p else "group")
        return f"m{len(self.sent)}"


f = Sides()
f.send(crcore.alert("💬【助手】回复", "好的", "green"))
f.send(crcore.alert(f"💬【{NAME}】回复", "好的", "green"))
live = crcore.alert("⏳【助手】正在思考", "", "blue")
live.pending_for = "助手"
f.send(live)
f.send("出错了：x", "dm")
f.send("出错了：x")
f.send(crcore.alert("📋 员工看板", "", "blue"), f.route(crcore.alert("📋 员工看板", "", "blue"), "dm"))
check("发到哪：助手的去单聊、别的员工的去群里；不属于谁的回到你发命令的那边（没说就去群里）",
      f.sent == ["dm", "group", "dm", "dm", "group", "dm"], f.sent)
f.after = {"m1": 0, "m2": 0}
f.bump("group")
check("群里来了新消息：只算群里的卡片被刷上去，单聊里的不动", f.after == {"m1": 0, "m2": 1}, f.after)
f0 = Sides(group=None)
f0.send(crcore.alert(f"💬【{NAME}】回复", "好的", "green"))
check("还没建群：照旧都发单聊", f0.sent == ["dm"], f0.sent)

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
import subprocess
other = subprocess.Popen(["sleep", "60"])                  # 第二个后台任务：前台从原来那个切到了它（终端标题变成它的名字）
crcore.save_json(os.path.join(sess, f"{other.pid}.json"), {"pid": other.pid, "sessionId": "fork-sid", "kind": "bg", "jobId": "e5f6a7b8",
                                                           "status": "idle", "statusUpdatedAt": (T0 + 99) * 1000, "cwd": TMP, "name": "评测基准 ⑂"})
attach0 = crcore.crterm.attach_info
crcore.crterm.attach_info = lambda pid, d: dict(attach0(pid, d), title="✳ 评测基准 ⑂")
moved = crcore.discover().get("paper")
crcore.crterm.attach_info = lambda pid, d: dict(attach0(pid, d), title="✳ 评测基准")
stay = crcore.discover().get("paper")
crcore.crterm.attach_info = attach0
other.kill()
os.remove(os.path.join(sess, f"{other.pid}.json"))
check("前台切到了另一个后台会话：跟着终端标题认，状态和对话记录换成它的", moved and moved["sessionId"] == "fork-sid"
      and moved["jobPid"] == other.pid and moved["status"] == "idle" and moved["pane"] == "%17", moved)
check("标题还是原来那个的名字：照旧认原来那个", stay and stay["sessionId"] == SID and stay["jobPid"] == BACK, stay)
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
crcore.pane_rows = lambda s: []
quiet = json.dumps(crcore.Remote().live_card(NAME, dict(E, status="busy"), time.time() + 3600).card, ensure_ascii=False)
going = json.dumps(crcore.Remote().live_card(NAME, dict(E, status="busy"), T0).card, ensure_ascii=False)
crcore.pane_rows = real_pane
check("实时卡片：还在想（想完才写进对话记录）：过程最后一行「思考中　N秒」跟着走；在跑命令时不显示", "思考中　" in quiet
      and "思考中　" not in going, quiet[-600:])

# 12. 上一句还没等到回复又发了一句：旧的实时卡片撤回，新卡片从第一句算起——连发的几句都列出来、进度接着显示，什么都不丢
SID3, N3, T1 = "33333333-2222-3333-4444-555555555555", "demo-test", int(time.time()) - 300
with open(os.path.join(proj, f"{SID3}.jsonl"), "w", encoding="utf-8") as f:
    f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
        user(T1 + 1, "帮我看看这个报错？还有", **HUMAN),
        assistant(T1 + 2, {"type": "tool_use", "id": "g1", "name": "Bash", "input": {"command": "grep -r claude", "description": "搜一下 claude"}}),
        user(T1 + 5, [{"type": "tool_result", "tool_use_id": "g1", "content": "ok"}]),
        queued(T1 + 61, [{"type": "text", "text": "截图里的按钮太大了 [Image #1]"}], {"kind": "human"}, imagePasteIds=[1]),
        assistant(T1 + 70, {"type": "text", "text": "好，按钮也改小了。"})]))
for t, text, files in ((T1 + 0.5, "帮我看看这个报错？还有", 0), (T1 + 60.5, "截图里的按钮太大了", 1)):
    real, crcore.now = crcore.now, lambda t=t: t
    crcore.note_sent({"sessionId": SID3}, text, files)
    crcore.now = real
E3 = dict(E, sessionId=SID3, pid=998)
crcore.save_json(crcore.REGISTRY, {N3: E3})
busy, idle = dict(E3, status="busy", statusAt=T1), dict(E3, status="idle", statusAt=T1 + 71)
r = crcore.Remote()
r.state["remote_pending"] = {N3: T1}
crcore.save_json(crcore.PENDING, {N3: "old-card"})
close = r.pend_since(N3, T1 + 60, busy)
check("连发：记下第一句的时间，最近这句照常用来判断答没答完", close is None and r.state["remote_pending"][N3] == T1 + 60
      and r.state["pending_from"][N3] == [T1, T1 + 60], r.state.get("pending_from"))
live = r.live_card(N3, busy, T1 + 60)
body = json.dumps(live.card, ensure_ascii=False)
quote = json.dumps([el for el in live.card["elements"] if el["tag"] == "column_set"], ensure_ascii=False)
check("连发：新卡片列出两句（同一来源一块，第二句前面一个淡「·」，标插话、带图），进度从第一句接着显示",
      "帮我看看这个报错？还有" in quote and "<font color='grey'>·</font>" in quote and "插话" in quote and "1 张图" in quote
      and "搜一下 claude" in body and f"#steps {N3} {T1}" in body, body[:900])
reply = json.dumps(r.reply_card(N3, idle, T1 + 60).card, ensure_ascii=False)
check("连发：回复卡片也列出两句，答的是最后的话", "帮我看看这个报错" in reply and "截图里的按钮" in reply and "按钮也改小了" in reply, reply[:600])
r = crcore.Remote()
r.state["remote_pending"] = {N3: T1}
close = r.pend_since(N3, T1 + 100, idle)
check("上一句其实已经答完、卡片还没换：旧卡片换成那张回复，新卡片只管这一句", close is not None and "按钮也改小了" in close.text
      and r.state["pending_from"][N3] == [T1 + 100, T1 + 100], getattr(close, "text", None))
r = crcore.Remote()
r.state["remote_pending"] = {N3: T1}
crcore.save_json(crcore.PENDING, {})
r.pend_since(N3, T1 + 60, busy)
check("没有在等的实时卡片（早就换成回复了）：不往前接", r.state["pending_from"][N3] == [T1 + 60, T1 + 60])
check("从别处（旧版本）记的等待时间：不认 pending_from", r.pend_start(N3, T1 + 7) == T1 + 7)


class FakeFeishu:
    def __init__(self, ok=True):
        self.log, self.after, self.ok = [], {"old-card": 3}, ok

    def patch(self, mid, card):
        self.log.append(("patch", mid))
        return self.ok

    def send(self, card):
        self.log.append(("send",))
        return "new"

    def recall(self, mid):
        self.log.append(("recall", mid))


fb = FakeFeishu()
crbot.Feishu.retire(fb, "old-card", live)
check("旧实时卡片：内容都搬进新卡片了，撤回", fb.log == [("recall", "old-card")] and "old-card" not in fb.after, fb.log)
live.close_old = close
fb = FakeFeishu()
crbot.Feishu.retire(fb, "old-card", live)
check("旧卡片其实已经答完：原地换成回复，不撤回", fb.log == [("patch", "old-card")], fb.log)
fb = FakeFeishu(ok=False)
crbot.Feishu.retire(fb, "old-card", live)
check("换不了（太久了之类）：回复照发，再撤回", fb.log == [("patch", "old-card"), ("send",), ("recall", "old-card")], fb.log)

# 12b. 助手（回复常常很长）：它还在干活你又发了一句——旧卡片原地定格成到这句为止的回复（整段都在，不撤回），这句另起一张；
#      别的员工照旧并进新卡片、旧的撤回（上面 12）
SID4, NA, T2 = "44444444-2222-3333-4444-555555555555", "助手", int(time.time()) - 600
LONG = "是 cc-crew 的 bug，我已经交给 8 号去修了。" + "原因是单聊里图文消息开头的 @ 被忽略了。" * 12 + "修好后我告诉你。"
with open(os.path.join(proj, f"{SID4}.jsonl"), "w", encoding="utf-8") as f:
    f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in [
        user(T2 + 1, "我不是@6 了吗？怎么还是发给你了", **HUMAN),
        assistant(T2 + 5, {"type": "tool_use", "id": "h1", "name": "Bash", "input": {"command": "grep heard", "description": "查一下记录"}}),
        user(T2 + 8, [{"type": "tool_result", "tool_use_id": "h1", "content": "ok"}]),
        assistant(T2 + 30, {"type": "text", "text": LONG}),
        assistant(T2 + 32, {"type": "tool_use", "id": "h2", "name": "Bash", "input": {"command": "cr say", "description": "告诉8号"}}),
        queued(T2 + 61, [{"type": "text", "text": "算了还是开头是@ 比较好"}], {"kind": "human"})]))
crcore.note_heard(NA, SID4, [{"t": T2 + 60.0, "kind": "human", "src": "feishu", "by": "", "text": "算了还是开头是@ 比较好", "images": 0}])   # 新那句很快就听到了（heard 前后放宽 2 秒）
for t, text in ((T2 + 0.5, "我不是@6 了吗？怎么还是发给你了"), (T2 + 60.5, "算了还是开头是@ 比较好")):
    real, crcore.now = crcore.now, lambda t=t: t
    crcore.note_sent({"sessionId": SID4}, text)
    crcore.now = real
busyA = dict(E, sessionId=SID4, pid=997, status="busy", statusAt=T2)
r = crcore.Remote()
r.state["remote_pending"] = {NA: T2}
r.state["pending_from"] = {NA: [T2, T2]}
crcore.save_json(crcore.PENDING, {NA: "old-card"})
close = r.pend_since(NA, T2 + 59.5, busyA)
frozen = json.dumps(close.card, ensure_ascii=False) if close else ""
check("助手：又发一句时旧卡片定格成到这句为止的回复：整段回复都在、标出这段用时，下面一行说接着看新卡片；新那句不混进来",
      close is not None and "交给 8 号去修了" in frozen and "修好后我告诉你" in frozen and "这段用时" in frozen
      and "下面的新卡片" in frozen and "我不是@6" in frozen and "算了还是开头" not in frozen and close.reply_for == NA, frozen[:900])
check("助手：新卡片只从这句算起，不并进上一张", r.state["pending_from"][NA] == [T2 + 59.5, T2 + 59.5], r.state["pending_from"])
fresh = json.dumps(r.live_card(NA, busyA, T2 + 59.5).card, ensure_ascii=False)
check("助手：新卡片里是新那句，上一段的回复不重复", "算了还是开头" in fresh and "修好后我告诉你" not in fresh, fresh[:900])
live = crcore.Card({}, "live")
live.close_old = close
fb = FakeFeishu()
crbot.Feishu.retire(fb, "old-card", live)
check("助手：旧卡片原地换成定格的样子，不撤回", fb.log == [("patch", "old-card")], fb.log)
crcore.save_json(crcore.PENDING, {})

# 13. 不是守护进程（员工自己写脚本直接调 crcore 发话）：不能算「你 · 飞书」，记成 cli、写上是哪个员工
real_caller, crcore.caller_name = crcore.caller_name, lambda: "crew-x"
crcore.VIA.update(via="", by="")
crcore.note_sent({"sessionId": SID3}, "脚本发的一句")
crcore.caller_name = real_caller
rec = crcore._sent_tail()[-1]
check("脚本直接调 crcore 发的：记成 cli + 哪个员工发的", rec["via"] == "cli" and rec["by"] == "crew-x", rec)
crcore.VIA.update(via="feishu", by="")

# 14. 看板：标题下一行摘要；按状态分组（等你 → 干活中 → 等后台 → 空闲），组之间细线，每人一行（彩色小标签 + 工号 名字 + 类型，右边时间），
#     空闲的今天有新话才多一行，一天以上没动静的折成一行，下班一行只有名字，助手不列；底下下拉 + 〔看攒下的 N〕
import re  # noqa: E402
now_t = time.time()
midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
base = {"agent": "claude", "alive": True, "cwd": TMP, "startedAt": now_t - 9000, "pid": os.getpid()}
crcore.save_json(crcore.REGISTRY, {
    "worker": dict(base, sessionId="s1", status="busy", statusAt=now_t - 60, startedAt=now_t - 9900),
    "waiter": dict(base, sessionId="s2", status="shell", statusAt=now_t - 120, startedAt=now_t - 9800),
    "asker": dict(base, sessionId="s3", status="waiting", waiting="permission prompt", statusAt=now_t - 30, startedAt=now_t - 9700),
    "idle-new": dict(base, sessionId="s4", status="idle", statusAt=now_t - 40),
    "idle-quiet": dict(base, sessionId="s5", status="idle", statusAt=now_t - 50),
    "exp-old1": dict(base, sessionId="s6", status="idle", statusAt=now_t - 30 * 3600),
    "exp-old2": dict(base, sessionId="s8", status="idle", statusAt=now_t - 50 * 3600),
    "gone": dict(base, alive=False, died=now_t - 3600),
    "助手": dict(base, sessionId="s7", status="idle", statusAt=now_t - 50)})
crcore.save_json(crcore.BADGES, {})
crcore.sync_badges(crcore.load_json(crcore.REGISTRY, {}))
r = crcore.Remote()
r.state["default"] = "助手"
r.state["held"] = 17
r.state["bg"] = {"worker": {"t": now_t - 900, "text": "上一轮的话"}, "waiter": {"t": now_t - 120, "text": "等评测跑完再看"},
                 "idle-new": {"t": now_t - 40, "text": "压缩包打好了 在 /tmp/x.zip"},
                 "idle-quiet": {"t": midnight - 3600, "text": "昨天说的话"},
                 "exp-old1": {"t": now_t - 30 * 3600, "text": "很久以前的话"}}
r.state["said_last"] = {}
board = r.status_card()
els = board.card["elements"]
notes = [e["elements"][0]["content"] for e in els if e["tag"] == "note"]
check("看板分组：等你 → 干活中 → 等后台 → 空闲，小标题带人数；空闲里久没动静的折一行，下班一行",
      notes[:4] == ["等你 1", "干活中 1", "等后台 1", "空闲 4"] and notes[4].startswith("久没动静 2：")
      and notes[5] == "下班 1：gone", notes)
check("看板：组之间一条细线", [e["tag"] for e in els].count("hr") == 4, [e["tag"] for e in els])
check("看板标题下面一行摘要：几人等你 · 干活 · 等后台 · 空闲（攒的条数不在这里）",
      board.card["header"]["subtitle"]["content"] == "1 人等你 · 1 人干活 · 1 人等后台 · 4 人空闲", board.card["header"])
blob = json.dumps(board.card, ensure_ascii=False)
check("看板：不用表情和圆点，没有「醒来看了一眼」", not re.search("[⏸🟢🟡🌙🙋⏳●]", blob) and "醒来看了一眼" not in blob)
sets = [e for e in els if e["tag"] == "column_set"]
left = [c["columns"][0]["elements"][0]["content"] for c in sets]
check("看板：助手不在列表里", not any("助手" in x for x in left)
      and "#enter 助手" not in json.dumps(els[-2], ensure_ascii=False), left)
check("看板：状态是彩色小标签（干活中绿、等后台橙、等你红），空闲的不加",
      left[0].startswith("<text_tag color='red'>等你</text_tag> ") and left[1].startswith("<text_tag color='green'>干活中</text_tag> ")
      and left[2].startswith("<text_tag color='orange'>等后台</text_tag> ") and "text_tag color='green'" not in left[3]
      and re.match(r"\d+号 \*\*", left[3]), left)
check("看板：每组左右两栏行数一样（时间跟名字对齐）", all(len(c["columns"][0]["elements"][0]["content"].split("\n"))
                                              == len(c["columns"][1]["elements"][0]["content"].split("\n")) for c in sets))
check("看板：空闲的今天有新话才多一行，昨天的话不写", "压缩包打好了" in left[3] and "昨天说的话" not in left[3]
      and len(left[3].split("\n")) == 3, left[3])
check("看板：一天以上没动静的不单独列，折成一行（带工号，都叫 exp-xxx 的只写 xxx）",
      "old1" not in left[3] and re.fullmatch(r"久没动静 2：\d+号 old1、\d+号 old2", notes[4]), notes[4])
check("看板：干活、等后台、等你的下面一行最新一句", "上一轮的话" in left[1] and "等评测跑完再看" in left[2] and "要你批准" in left[0])
times = [c["columns"][1]["elements"][0]["content"] for c in sets]
check("看板：时间今天写 HH:MM", all(re.search(r"\d\d:\d\d", t) for t in times), times)
check("看板：每行放得下（手机上不折行）", all(crcore.text_em(re.sub(r"<[^>]+>|\*\*", "", x)) <= crcore.LINE_EM - 3
                                     for x in "\n".join(left).split("\n"))
      and all(crcore.text_em(n) <= crcore.NOTE_EM for n in notes if not n.startswith(("久没动静", "下班"))))
reg0 = crcore.load_json(crcore.REGISTRY, {})
crcore.save_json(crcore.REGISTRY, dict(reg0, **{f"exp-run:{i}": dict(base, alive=False, died=now_t - 3700 - i) for i in range(10)}))
notes2 = [e["elements"][0]["content"] for e in crcore.Remote().status_card().card["elements"] if e["tag"] == "note"]
off_line = next((n for n in notes2 if n.startswith("下班")), "")
check("看板：下班 / 久没动静的再多也一个不落全列出来（放不下就折行），不写「…」", off_line.startswith("下班 11：")
      and "…" not in off_line and "gone" in off_line and all(f"exp-run:{i}" in off_line for i in range(10)), off_line)
crcore.save_json(crcore.REGISTRY, reg0)
bar = [e for e in els if e["tag"] == "action"][-1]["actions"]
check("看板底下：「选员工」下拉（下班的也在）+ 〔看攒下的 17〕，其余在「···」", bar[0]["tag"] == "select_static"
      and bar[0]["placeholder"]["content"] == "选员工" and "#enter gone" in [o["value"] for o in bar[0]["options"]]
      and [b["text"]["content"] for b in bar[1:-1]] == ["看攒下的 17"] and bar[-1]["tag"] == "overflow"
      and "#board now" in [o["value"] for o in bar[-1]["options"]], bar)
r.state["held"] = 0
bar = [e for e in r.status_card().card["elements"] if e["tag"] == "action"][-1]["actions"]
check("没攒消息：下拉 + 〔刷新〕", [b["text"]["content"] for b in bar[1:-1]] == ["刷新"], bar)
check("看板文字版（命令行）第二行是摘要", board.text.split("\n")[1] == "1 人等你 · 1 人干活 · 1 人等后台 · 4 人空闲", board.text)
keep_reg = crcore.load_json(crcore.REGISTRY, {})
three = now_t - 3 * 86400
crcore.save_json(crcore.REGISTRY, {"y1": dict(base, status="idle", statusAt=max(midnight - 60, now_t - 23 * 3600)),
                                   "w3": dict(base, status="shell", statusAt=three)})
times = "".join(c["columns"][1]["elements"][0]["content"] for c in crcore.Remote().status_card().card["elements"] if c["tag"] == "column_set")
lt = time.localtime(three)
check("看板时间：昨天写「昨天」，更早写 月/日", "昨天" in times and f"{lt.tm_mon}/{lt.tm_mday}" in times, times)
crcore.save_json(crcore.REGISTRY, keep_reg)
again = r.handle("#board now")
check("〔刷新〕：看板原地换成最新的", getattr(again, "in_place", False) and again.slot == "status")
r.note_latest("x", now_t, "压缩包打好了：\n/tmp/x.zip，3 个文件")
check("最新一句只是个引子（以冒号结尾）：接上下一行", r.state["bg"]["x"]["text"] == "压缩包打好了： /tmp/x.zip，3 个文件", r.state["bg"]["x"])
r.note_latest("y", now_t, "")
check("没说话：最新一句记成空，不写「醒来看了一眼」", r.state["bg"]["y"]["text"] == "")

# 15. 工号：只给在上班的；下班就收回，回来 / 新来的取最小空号，在班的号码不变；整理一次排成 1～N，旧号再认一天
reg = {n: dict(base, startedAt=now_t - 100 * i) for i, n in enumerate(["a", "b", "c", "d"], 1)}
reg["助手"] = dict(base)
crcore.save_json(crcore.BADGES, {"a": 3, "b": 1, "c": 7, "old": 2, "gone-long": 4})
reg["gone-long"] = dict(base, alive=False, died=now_t - 3600)
reg["blink"] = dict(base, alive=False, died=now_t - 30)
crcore.save_json(crcore.BADGES, dict(crcore.load_json(crcore.BADGES, {}), blink=5))
got = crcore.sync_badges(reg)
check("工号：在班的号码不动；下班久了的收回；刚断一下的（重启）先留着；新来的取最小空号；助手不占号",
      got == {"a": 3, "b": 1, "c": 7, "blink": 5, "d": 2}, got)
crcore.release_badge("c")
reg["c"] = dict(base, alive=False, died=now_t)
check("「下班」：工号马上收回，别人的号不动", crcore.sync_badges(reg) == {"a": 3, "b": 1, "blink": 5, "d": 2})
reg["c"] = dict(base, startedAt=now_t)
check("上班回来：取最小的空号", crcore.sync_badges(reg)["c"] == 4)
reg["dead-pid"] = dict(base, pid=99999999, startedAt=now_t)
check("进程其实已经没了（注册表还没更新）：不分号", "dead-pid" not in crcore.sync_badges(reg))
reg.pop("dead-pid")

reg = {"paper:1": dict(base), "forcing": dict(base), "crew": dict(base), "paper:0": dict(base), "test": dict(base),
       "off": dict(base, alive=False, died=now_t - 7200), "助手": dict(base)}
crcore.save_json(crcore.BADGES, {"forcing": 1, "off": 2, "crew": 3, "test": 6, "paper:0": 10, "paper:1": 11})
crcore.save_json(crcore.REGISTRY, reg)
r = crcore.Remote()
old, new = crcore.renumber_badges(r.state, ["forcing", "paper:0", "paper:1", "crew"], reg)
check("整理工号：在班的按项目排成连续的 1～N（没写到的接在后面），下班的、助手不占号",
      new == {"forcing": 1, "paper:0": 2, "paper:1": 3, "crew": 4, "test": 5}, new)
check("整理工号：旧号记一天（只记换了人的）", r.state["renum"]["old"] == {"2": "off", "3": "crew", "6": "test", "10": "paper:0", "11": "paper:1"},
      r.state["renum"])
r.save()
sent = []


def fake_deliver(target, body, reg, urgent=False):          # 真的 deliver 也是先 resolve 再发
    name, _, err = r.resolve(target, reg)
    if err:
        return err
    sent.append((name, body))
    return crcore.Card({}, "OK")


r.deliver = fake_deliver
real_discover, crcore.discover = crcore.discover, lambda: {n: dict(e) for n, e in reg.items() if e.get("alive")}
crcore.VIA.update(via="cli", by="")
out = r.handle("@11 接着做")
check("cr 里用旧号（这个号现在没人用）：照旧发给原来那个人，回复前面提醒「11号现在是3号」",
      sent[-1] == ("paper:1", "接着做") and str(out).split("\n")[1].startswith("ℹ️ 11号现在是3号（paper:1）"), (sent, str(out)))
sent.clear()
out = r.handle("@3 接着做")
check("cr 里用的号新旧两个人都在上班：不执行，说清楚现在的 3号是谁、原来的 3号现在几号",
      not sent and "3号换人了" in str(out) and "现在的 3号是 paper:1" in str(out) and "原来的 3号 crew 现在是 4号" in str(out), str(out))
out = r.handle("@2 在吗")
check("旧号原来那个人下班了：新号照常用，不问也不提醒", sent[-1] == ("paper:0", "在吗") and "ℹ️" not in str(out), (sent, str(out)))
crcore.VIA.update(via="feishu", by="")
sent.clear()
ask = r.handle("@3 帮我看看", typed=True)
check("飞书里 @ 的号新旧两个人都在上班：先问发给谁（两个按钮），这句还没发",
      not sent and "3号换人了" in ask.text and ["#renum 0", "#renum 1"] == [b["value"]["cmd"] for e in ask.card["elements"]
                                                                       if e["tag"] == "action" for b in e["actions"]], ask.text)
r.handle("#renum 1")
check("点〔原 3号〕：发给原来的 3号", sent[-1] == ("crew", "帮我看看") and not r.state["renum"].get("new_ok"), sent)
check("这句已经发了：再点一次不重发", "处理过了" in str(r.handle("#renum 1")) and len(sent) == 1)
ask = r.handle("@3 你好", typed=True)
check("没点过新号：下次 @ 换了人的号还问", len(sent) == 1 and "3号换人了" in ask.text)
r.handle("#renum 0")
check("点〔新号〕：发给现在的 3号，记住你已经换用新号", sent[-1] == ("paper:1", "你好") and r.state["renum"]["new_ok"])
r.handle("@3 继续", typed=True)
check("换用新号以后：@ 新号直接发，不再问", sent[-1] == ("paper:1", "继续"), sent)
out = r.handle("@11 看下", typed=True)
check("飞书里用旧号（现在没人用）：发给原来那个人，卡片下面跟一句「11号现在是3号」",
      sent[-1] == ("paper:1", "看下") and out.more and str(out.more[-1]).startswith("ℹ️ 11号现在是3号"), (sent, out.more))
r.state["renum"]["t"] -= crcore.RENUM_SECS + 1
r.save()
n = len(sent)
r.handle("@11 看下", typed=True)
check("过了一天：旧号不认了（11 号没人：不直接发，交给秘书）", len(sent) == n, sent)
crcore.VIA.update(via="cli", by="")
check("过了一天：cr 里旧号也不认了", "没有找到" in str(r.handle("@11 看下")) and len(sent) == n)
crcore.VIA.update(via="feishu", by="")
crcore.discover = real_discover
# 16. 它忙时你又发了一句（排队，上一句答完才轮到）：回复卡片两轮的回答都要有；按钮行在手机上横排
q1 = {"t": 1, "kind": "human", "text": "明天北京会下雨吗", "images": 0}
q2 = {"t": 2, "kind": "human", "text": "看着和卡了一样", "images": 1, "queued": True}
mid = {"t": 3, "kind": "human", "text": "顺便看下日志", "images": 0, "mid": True}
two = [("said", q1, None), ("think", ("", 2), True), ("text", "会，下午有小雨。", True),
       ("said", q2, None), ("text", "不是我卡了，是卡片没更新。", True)]
got = crcore.turn_answers(two)
check("排队的第二句是新的一轮：两轮的回答都留下、各对上自己问的那句",
      [(qs[0]["text"], a) for qs, a in got] == [("明天北京会下雨吗", "会，下午有小雨。"), ("看着和卡了一样", "不是我卡了，是卡片没更新。")], got)
one = [("said", q1, None), ("text", "我先查一下。", True), ("said", mid, None), ("tool", ("🌐 上网搜", "x", 1), True), ("text", "查到了：是。", True)]
check("干活时插的话不算新的一轮：还是只有一个回答（不把「我先查一下」当回答）",
      [a for _, a in crcore.turn_answers(one)] == ["查到了：是。"], crcore.turn_answers(one))
check("连发两句、它一起答（第一句后面没写话）：算一轮", len(crcore.turn_answers([("said", q1, None), ("said", q2, None), ("text", "答。", True)])) == 1)
bg = {"t": 4, "kind": "background", "text": 'Monitor "Wait for the web image build to finish"', "images": 0}
check("后台任务的通知那一轮：不写成「答「Monitor …」」，你问的照旧「答「…」」",
      crcore.turn_label([bg]) == "<font color='grey'>后台任务有结果后：</font>\n"
      and "答「看着和卡了一样」" in crcore.turn_label([bg, q2]) and crcore.turn_label([]) == "", crcore.turn_label([bg]))
card = crcore._card("💬【x】", "green", [{"tag": "action", "actions": []}, {"tag": "action", "layout": "bisected", "actions": []}])
check("按钮行没指定排法的一律 flow（手机上不再一个按钮占一整行），指定了的不动",
      [e.get("layout") for e in card["elements"] if e["tag"] == "action"][:2] == ["flow", "bisected"], card["elements"])

# 17. 答完以后你发了 /model（菜单开着）：回复照常发，不当成它在问你；屏幕卡片每行写全；菜单选完直接说结果
MENU = ["Select model", "Switch between Claude models.", "", "1. Default (recommended)  Opus 5.5 · Best for everyday",
        "2. Opus 5.5               For complex work", "❯ 3. Sonnet 5.5 ✔           Most efficient", "", "Enter to confirm · Esc to exit"]
crcore.pane_rows = lambda s: list(MENU)
on_menu = dict(E3, status="waiting", waiting="?", statusAt=T1 + 71)
real_discover, crcore.discover = crcore.discover, lambda: {N3: dict(on_menu)}
r = crcore.Remote()
r.state["remote_pending"] = {N3: T1 + 60}
r.save()
out = r.live_updates([N3])
check("停在你自己打开的 /model 菜单上：这一轮的回复照常发（不是「要你批准」、不贴屏幕）",
      out and out[0][2] and "按钮也改小了" in out[0][1].text and "批准" not in out[0][1].text and "屏幕" not in out[0][1].text,
      [(n, getattr(c, "text", c)[:120], f) for n, c, f in out])
crcore.discover = real_discover
long = "长" * 80
cleaned = crcore.clean_screen(["❯ 帮我推荐几个播客", "Thought for 11s", "● " + long, "❯ 1. Yes", "  2. No"])
check("屏幕卡片：每行写全不截断、「Thought for」去掉、你说的话写成「你：」、菜单光标那行不动",
      "• " + long in cleaned and "Thought" not in cleaned and cleaned.startswith("你：帮我推荐几个播客") and "❯ 1. Yes" in cleaned, cleaned)
DONE = ["你要是想先挑一个开始，我选张小珺。", "❯ /model", "⎿  Set model to Opus 5.5 for this session only",
        "✔ Update installed · /restart to apply", "─" * 40, "❯", "─" * 40, "Opus 5.5 · medium · ctx 13%"]
crcore.pane_rows = lambda s: list(DONE)
card = crcore.Remote().menu_done_card(N3, E3)
check("菜单里选完：直接说结果「已切到 Opus 5.5」，不贴整屏", card is not None and "已切到 Opus 5.5（只对这次对话）" in card.text
      and "张小珺" not in card.text, getattr(card, "text", None))
crcore.pane_rows = lambda s: ["❯ /model", "⎿  Set model to Opus 5.5", "❯ 接着说", "好的。", "─" * 40, "❯", "─" * 40]
check("最近一句不是斜杠命令：不当成菜单结果（照常给屏幕卡片）", crcore.Remote().menu_done_card(N3, E3) is None)
crcore.pane_rows = lambda s: []
tags = crcore.usage_tags({"model": "Opus 5.5", "effort": "xhigh", "ctx": 12})
check("底栏标签：模型后面跟着思考程度「思考 很高」", "Opus 5.5" in tags and "思考 很高" in tags
      and tags.index("Opus 5.5") < tags.index("思考 很高") < tags.index("上下文 12%"), tags)

print(f"\n全部通过：{len(passed)} 项")
