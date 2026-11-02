"""Codex 员工的解析（crcodex）和类型标记的测试：屏幕上的批准框、菜单、斜杠补全、启动参数，rollout 里的状态、过程、错误、额度。
样本是 codex 0.164 实际的屏幕和记录格式。不启动 codex，不碰真的数据。
运行：python lib/tests/test_codex.py"""
import json
import re
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
TMP = tempfile.mkdtemp(prefix="cr-codex-")
HOME_A, HOME_B = os.path.join(TMP, "codex", ".codex"), os.path.join(TMP, "codex-b", ".codex")
os.environ.update(CLAUDE_CONFIG_DIR=os.path.join(TMP, "claude"), CLAUDE_MONITOR_DATA=os.path.join(TMP, "data"),
                  CLAUDE_MONITOR_FILES_ROOT=TMP, CLAUDE_MONITOR_PROXY="", CLAUDE_MONITOR_LAUNCH_SCRIPT="",
                  CLAUDE_MONITOR_CODEX_HOMES=f"官方|{HOME_A}|;备用|{HOME_B}|/nonexistent-env.sh")
os.makedirs(os.environ["CLAUDE_MONITOR_DATA"])
sys.path.insert(0, LIB)
import crcodex  # noqa: E402
import crcore  # noqa: E402

passed = []


def check(name, cond, detail=""):
    assert cond, f"{name} 失败 {detail}"
    passed.append(name)
    print("✓", name)


# ---------------------------------------------------------------- 屏幕
APPROVAL = """• 我会运行 touch hello.txt，并报告结果。
• Running touch hello.txt
  Would you like to run the following command?
  Environment: local
  Reason: 当前环境为只读沙箱，是否允许运行 touch hello.txt 创建文件？
  $ touch hello.txt
› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `touch hello.txt` (p)
  3. No, and tell Codex what to do differently (esc)
  Press enter to confirm or esc to cancel""".splitlines()
rows = [r.strip() for r in APPROVAL]
d = crcodex.dialog(rows)
check("认出批准框和三个选项", d and [n for n, _ in d["options"]] == [1, 2, 3] and d["tool"] == "Bash", d)
check("crcore.dialog_options 认不出 Claude 的框时试 codex 的", crcore.dialog_options(rows) == d["options"])
check("批准框翻成中文按钮", [crcore.perm_label(t) for _, t in d["options"]] == ["允许", "总是允许", "拒绝"])
pt, _ = crcodex.waiting_on({"statusAt": 100}, rows, None)
check("没装 hook：从屏幕上拿命令和原因", pt["input"]["command"] == "touch hello.txt" and "只读沙箱" in pt["input"]["reason"]
      and pt["id"].startswith("c"), pt)
check("同一个框编号不变，换了命令就变", crcodex.dialog(rows)["sig"] == d["sig"]
      and crcodex.dialog([r.replace("hello", "b") for r in rows])["sig"] != d["sig"])
hook = {"tool_name": "Bash", "tool_input": {"command": "touch hello.txt", "description": "原因"}, "t": 105}
pt2, _ = crcodex.waiting_on({"statusAt": 100}, rows, hook)
check("装了 hook：用 hook 记下的完整命令，屏幕上的框当备用编号", pt2["name"] == "Bash" and pt2["id"] == "h105000"
      and pt2["alt_id"] == d["sig"], pt2)
check("旧的 hook 记录不算", crcodex.waiting_on({"statusAt": 1000}, rows, hook)[0]["id"] == d["sig"])
check("批准框的说法和细节", crcore.describe_tool("CodexDialog", pt["input"])[0] == "⚙️ 运行命令"
      and "touch hello.txt" in crcore.tool_detail("CodexDialog", pt["input"]))

EFFORT_MENU = """  Select Reasoning Level for GPT-6.1-Sol
  1. Low (default)    Fast responses with lighter reasoning
  2. Medium           Balances speed and reasoning depth for everyday tasks
› 3. High (current)   Greater reasoning depth for complex problems
  4. Extra high       Extra high reasoning depth for complex problems
  5. More reasoning…  Max and Ultra consume usage limits faster
  enter default · s session · esc back""".splitlines()
m = crcore.screen_menu([r.strip() for r in EFFORT_MENU])
check("codex 菜单：选项、光标、底部按键", m and m["cursor"] == 3 and len(m["items"]) == 5 and m["items"][3] == (4, "Extra high")
      and ("s", "s：只用于这个会话") in m["keys"], m)
check("点选项时按 s（只改这个会话，不改默认）", crcore.pick_action(m)[0] == "s")
check("普通屏幕不是菜单", crcodex.menu([r.strip() for r in APPROVAL[:2]] + ["› Ask Codex to do anything", "GPT-6.1-Sol high",
                                                                           "← for agents · ? for shortcuts"]) is None)
check("批准框也能当菜单用遥控键盘", crcodex.menu(rows)["keys"] == [("Enter", "回车：确认"), ("Escape", "Esc：取消")])
UPDATE = ["Update available · 0.164.0 → 0.165.0", "› 1. Update now (runs `npm install -g @openai/codex`)", "2. Skip",
          "3. Skip until next version", "enter continue · esc skip"]
check("启动时「有新版本」的框算菜单（不往里打字）", crcodex.menu(UPDATE) is not None)
_real_rows = crcodex.pane_rows
FRESH = ["", ">_ OpenAI Codex (v0.164.0)", "", "Resuming session…", "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything", "? for shortcuts"] + [""] * 40
crcodex.pane_rows = lambda s, ansi=False: FRESH if ansi else [re.sub(r"\x1b\[[0-9;]*m", "", r) for r in FRESH]
check("刚起来、内容在屏幕上半截时，还在 Resuming session 不算能打字", not crcodex.ready({}))
FRESH[3] = ""
check("接好了就能打字", crcodex.ready({}))
GOAL = [" " * 76 + "Resume paused goal?", " " * 76 + "Goal: 只回复一个字：好", "",
        " " * 74 + "\x1b[1;7m› 1. Resume goal   \x1b[0;7mMark it active and continue when idle",
        " " * 76 + "2. Leave paused  \x1b[2mKeep it paused; use /goal resume later", "", " " * 76 + "enter select · esc back"] + [""] * 17 + \
       ["\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything", "  GPT-6.1-Sol high", "  ? for shortcuts"]
crcodex.pane_rows = lambda s, ansi=False: GOAL if ansi else [re.sub(r"\x1b\[[0-9;]*m", "", r).strip() for r in GOAL]
m = crcodex.menu(crcodex.pane_rows({}))
check("屏幕中间弹的框（输入框还在下面）也算菜单", m and m["items"] == [(1, "Resume goal"), (2, "Leave paused")] and m["cursor"] == 1
      and ("Escape", "Esc：返回") in m["keys"], m)
check("弹着框时不算能打字（回车会替你选第一项）", not crcodex.ready({}) and crcodex.box_text({}) == "")
LEFT = [("• 现在跑的是上一轮审稿后新增的机制对照" + " " * 30 + r.strip()) if r.strip() else "• 合计 1024 条新视频" for r in GOAL[:7]]
m = crcodex.menu([re.sub(r"\x1b\[[0-9;]*m", "", r).strip() for r in LEFT + GOAL[7:]])
check("框左边露着对话内容也认得出", m and [n for n, _ in m["items"]] == [1, 2] and m["cursor"] == 1, m)
crcodex.pane_rows = _real_rows
POPUP =["› /model         choose what model and reasoning effort to use", "/fast          2x speed, increased usage",
         "/init - create an AGENTS.md file with instructions for Codex", "› /"]
check("斜杠补全：只认列表里的，不认开场提示", crcodex.slash_items(POPUP) == [("/model", "choose what model and reasoning effort to use"),
                                                             ("/fast", "2x speed, increased usage")])
check("在干活时那一行", crcodex.working(["• Working (20m 32s • esc to interrupt)", "", "› Ask Codex to do anything"]) == "20m 32s")
check("窗格标题：会话 id、等你批准、转圈", crcodex._title_info("[ . ] Action Required | 01a176fa-1524-7173-b0b9-818d0... | 执行 | x")
      == ("01a176fa-1524-7173-b0b9-818d0", True, False)
      and crcodex._title_info("01a1765b-e775-78b2-89c9-22007... ⠹ 联系 | speculative")[2])

# ---------------------------------------------------------------- 一次问你几道题（request_user_input 提问框）
QFOLD = """• Working (24s • esc to interrupt) · 1 background terminal running · /ps to view · /stop to close
• Queued follow-up inputs
  ? 3 questions · 19s
    shift+← to answer
› Ask Codex to do anything
  GPT-6.1-Sol high""".splitlines()
QOPEN = """• Queued follow-up inputs
  1 of 3
  测试题一：选哪种颜色？
  › 1. 红色
    2. 绿色
    3. 蓝色
    4. Other
  enter submit   ^] skip   shift+→ main prompt   shift+← next question""".splitlines()
QLAST = """› > 测试题二：选哪个数字？
  自定义七
• Queued follow-up inputs
  测试题三：要继续吗？
  › 1. 继续
    2. 停止
    3. Other
  enter submit   ^] skip   shift+→ main prompt""".splitlines()
b = crcodex.question_box([r.strip() for r in QFOLD])
check("提问框收起时：认出还剩几道", b == {"open": False, "left": 3}, b)
b = crcodex.question_box([r.strip() for r in QOPEN])
check("提问框展开时：题目、选项、光标、能往后翻", b and b["text"] == "测试题一：选哪种颜色？" and b["options"][-1] == (4, "Other")
      and b["cursor"] == 1 and b["first"] and b["next"] and (b["pos"], b["left"]) == (1, 3), b)
b = crcodex.question_box([r.strip() for r in QLAST])
check("只剩一道：没有「1 of 1」，上面的回显不算题目", b and b["text"] == "测试题三：要继续吗？" and b["left"] == 1 and not b["next"], b)
check("提问框不是菜单、不是批准框", crcodex.menu([r.strip() for r in QOPEN]) is None and crcodex.dialog([r.strip() for r in QOPEN]) is None)
wrap = [r.strip() for r in QOPEN[:3]] + ["› 1. 有公网服务器，可以中转，而且这一项写得特别长特别长", "折到第二行", "2. 都没有", "3. Other",
                                         QOPEN[-1].strip()]
check("选项太长折行：接到那一项后面", crcodex.question_box(wrap)["options"][0] == (1, "有公网服务器，可以中转，而且这一项写得特别长特别长 折到第二行"))
check("普通屏幕没有提问框", crcodex.question_box(["• Working (3s • esc to interrupt)", "› Ask Codex to do anything"]) is None)

QR = "<send_user_message_question_reply>\n{}\n</send_user_message_question_reply>"
reply = lambda call, i, q, a: {"timestamp": "2026-10-31T02:12:41.522Z", "type": "response_item", "payload": {
    "type": "message", "role": "user", "content": [{"type": "input_text", "text": QR.format(json.dumps(
        [{"answer": a, "question": q, "questionItemId": json.dumps(["request_user_input_async", call, i])}], ensure_ascii=False))}]}}
ask = lambda call, qs, name="request_user_input_async": {"timestamp": "2026-10-31T02:07:38.527Z", "type": "response_item", "payload": {
    "type": "function_call", "name": name, "call_id": call, "arguments": json.dumps({"questions": qs}, ensure_ascii=False)}}
QPATH = os.path.join(TMP, "rollout-q.jsonl")
with open(QPATH, "w", encoding="utf-8") as f:
    for rec in [ask("call_old", [{"title": "测试题二：选哪个数字？", "options": ["一", "二"]}, {"title": "旧的跳过了", "options": ["是", "否"]}]),
                reply("call_old", 0, "测试题二：选哪个数字？", "二"),
                ask("call_sync", [{"question": "计划模式的题", "options": [{"label": "A", "description": "说明 A"}]}], "request_user_input"),
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call_sync", "output": "{}"}},
                ask("call_new", [{"title": "测试题一：选哪种颜色？", "options": ["红色", "绿色", "蓝色"]},
                                 {"title": "测试题二：选哪个数字？", "options": ["一", "二", "三", "四", "五", "六"]},
                                 {"title": "测试题三：要继续吗？", "options": ["继续", "停止"]}]),
                reply("call_new", 0, "测试题一：选哪种颜色？", "蓝色")]:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
todo = crcodex.asked_questions(QPATH)
check("对话记录里没答的题：回答过的、计划模式答完的去掉，跳过的还在（记录里没痕迹）",
      [(q["call"], q["idx"]) for q in todo] == [("call_old", 1), ("call_new", 1), ("call_new", 2)], todo)
check("回答在卡片上显示成人话", crcodex.readable(reply("c", 0, "要继续吗？", "继续")["payload"]["content"][0]["text"]) == "回答「要继续吗？」：继续")
screen = ["› > 测试题二：选哪个数字？", "二", "› > 测试题一：选哪种颜色？", "蓝色", "• Queued follow-up inputs", "? 2 questions", "shift+← to answer"]
oq = crcodex.open_questions({"transcript": QPATH}, screen)
check("还开着的题：同名的旧回答不算新回答，跳过的旧题按屏幕上的数目丢掉", [q["title"] for q in oq["questions"]] == ["测试题二：选哪个数字？", "测试题三：要继续吗？"], oq)
screen2 = ["↳ > 测试题二：选哪个数字？", "五"] + screen
oq = crcodex.open_questions({"transcript": QPATH}, screen2[:2] + screen[:4] + ["? 1 question", "shift+← to answer"])
check("刚在框里答了、排着队还没写进记录的（↳ > 题目）也去掉", [q["title"] for q in oq["questions"]] == ["测试题三：要继续吗？"], oq)
pt, _ = crcodex.waiting_on({"transcript": QPATH}, screen)
check("waiting_on：提问框算「在问你」，带上题目", pt["name"] == "CodexQuestions" and pt["input"]["left"] == 2 and pt["id"] == "qcall_new"[-9:])


class FakeBox:
    """模拟 codex 的提问框：收起 / 展开、shift+← / shift+→ 翻题、数字键选上就交、Other 打字、回车。"""
    def __init__(self, qs, open_at=None):
        self.qs, self.open, self.pos, self.cursor, self.typed, self.answers, self.keys = [list(q) for q in qs], open_at is not None, open_at or 0, 1, "", [], []

    def rows(self, s=None, ansi=False):
        if not self.qs:
            return ["• Working (1m 2s • esc to interrupt)", "› Ask Codex to do anything"]
        if not self.open:
            return ["• Queued follow-up inputs", f"? {len(self.qs)} questions · 5s", "shift+← to answer", "› Ask Codex to do anything"]
        t, opts = self.qs[self.pos]
        out = ["• Queued follow-up inputs"] + ([f"{self.pos + 1} of {len(self.qs)}"] if len(self.qs) > 1 else []) + [t]
        out += [("› " if k == self.cursor else "") + f"{k}. {o}" for k, o in enumerate(opts + [self.typed or "Other"], 1)]
        return out + ["enter submit   ^] skip   " + ("shift+→ main prompt" if self.pos == 0 else "shift+→ prev question")
                      + ("   shift+← next question" if self.pos < len(self.qs) - 1 else "")]

    def submit(self, k):
        t, opts = self.qs.pop(self.pos)
        self.answers.append((t, (opts + [self.typed])[k - 1]))
        self.typed, self.cursor, self.pos = "", 1, min(self.pos, max(len(self.qs) - 1, 0))
        self.open = self.open and bool(self.qs)

    def key(self, s, name):
        self.keys.append(name)
        n = len(self.qs[self.pos][1]) + 1 if self.qs and self.open else 0
        if name == "S-Left":
            if not self.open:
                self.open, self.pos, self.cursor = True, 0, 1
            elif self.pos < len(self.qs) - 1:
                self.pos, self.cursor = self.pos + 1, 1
        elif name == "S-Right":
            self.open, self.pos, self.cursor = (self.open and self.pos > 0), max(self.pos - 1, 0), 1
        elif name in ("Down", "Up"):
            self.cursor = max(1, min(n, self.cursor + (1 if name == "Down" else -1)))
        elif name == "Enter":
            self.submit(self.cursor)

    def literal(self, s, text):
        self.keys.append(text)
        n = len(self.qs[self.pos][1]) + 1
        if len(text) == 1 and text.isdigit():
            if int(text) < n:
                self.submit(int(text))
            elif int(text) == n:
                self.cursor = n
        elif self.cursor == n:
            self.typed += text


def run_box(box, *a, **kw):
    saved = crcodex.pane_rows, crcodex.crterm.key, crcodex.crterm.literal
    crcodex.pane_rows, crcodex.crterm.key, crcodex.crterm.literal = box.rows, box.key, box.literal
    try:
        return crcodex.answer_question({}, *a, wait=lambda: None, **kw)
    finally:
        crcodex.pane_rows, crcodex.crterm.key, crcodex.crterm.literal = saved


THREE = [("测试题一：选哪种颜色？", ["红色", "绿色", "蓝色"]), ("测试题二：选哪个数字？", ["一", "二", "三", "四", "五", "六"]),
         ("测试题三：要继续吗？", ["继续", "停止"])]
box = FakeBox(THREE)
check("答第二道：打开 → 下一道 → 数字键 → 收回主输入框", run_box(box, "测试题二：选哪个数字？", 5, "五") == (True, "")
      and box.answers == [("测试题二：选哪个数字？", "五")] and box.keys == ["S-Left", "S-Left", "5", "S-Right", "S-Right"] and not box.open, box.keys)
box = FakeBox(THREE)
check("打字回答：挪到 Other、打字、回车", run_box(box, "测试题一：选哪种颜色？", text="都 不要") == (True, "")
      and box.answers == [("测试题一：选哪种颜色？", "都 不要")] and box.keys[:5] == ["S-Left", "Down", "Down", "Down", "都 不要"] and not box.open, box.keys)
box = FakeBox(THREE)
check("选项对不上就不按", run_box(box, "测试题一：选哪种颜色？", 2, "紫色")[0] is False and box.answers == [] and "2" not in box.keys)
box = FakeBox(THREE)
check("题已经不在框里就不按", run_box(box, "测试题四：没有这道", 1, "x")[0] is False and box.answers == []
      and not any(k.isdigit() for k in box.keys))
box = FakeBox(THREE, open_at=2)
check("你在电脑上已经翻到第三道：往前翻到第一道再答，不替你收起", run_box(box, "测试题一：选哪种颜色？", 1, "红色") == (True, "")
      and box.answers == [("测试题一：选哪种颜色？", "红色")] and box.keys[:2] == ["S-Right", "S-Right"] and box.open, box.keys)
box = FakeBox([("十个选项", [str(i) for i in range(1, 11)])])
check("第 10 项以上：挪光标再回车", run_box(box, "十个选项", 10, "10") == (True, "") and box.answers == [("十个选项", "10")]
      and box.keys[-1] == "Enter" and box.keys.count("Down") == 9, box.keys)


# 卡片：题目一行粗体 + 选项按钮（短的两三个一行、长的一个一行、多的下拉），不贴屏幕
QS15 = [{"call": "call_demo1234abcd", "idx": 0, "title": "测试题一：有没有能用来中转的公网服务器？",
         "options": ["有公网服务器，可以中转", "有域名或 Cloudflare，但没有公网服务器", "都没有，希望你推荐最省事的方案"], "notes": ["", "", ""]},
        {"call": "call_demo1234abcd", "idx": 1, "title": "你平时从什么设备连接这台服务器？", "options": ["Mac", "Windows", "Linux"], "notes": ["", "", ""]},
        {"call": "call_demo1234abcd", "idx": 2, "title": "选哪个数字？", "options": list("一二三四五六"), "notes": [""] * 6}]
QB = {"id": "q47e57b10", "name": "CodexQuestions", "input": {"questions": QS15, "left": 3}}
rq = crcore.Remote()
QS_E = {"agent": "codex", "waiting": "questions", "status": "waiting", "statusAt": time.time() - 30, "sessionId": "", "alive": True}
rq.state.pop("cxq", None)
qcard = rq.codex_q_card("cx-crew", {"agent": "codex"}, QB)
els = qcard.card["elements"]
acts = [e for e in els if e["tag"] == "action"]
check("提问卡片：长选项一个一行、短的三个一行、六个的下拉", [a.get("layout") for a in acts[:5]] == ["default", "default", "default", "trisection", "flow"]
      and acts[4]["actions"][0]["tag"] == "select_static" and len(acts[4]["actions"][0]["options"]) == 6, [a.get("layout") for a in acts])
check("提问卡片：按钮命令是「第几道题选第几项」", acts[3]["actions"][1]["value"]["cmd"] == "#cxq cx-crew 1234abcd 1 2"
      and acts[4]["actions"][0]["options"][4]["value"] == "#cxq cx-crew 1234abcd 2 5")
check("提问卡片：不贴屏幕，标题写几道题", "Exited" not in json.dumps(els, ensure_ascii=False) and "3 道题" in qcard.card["header"]["title"]["content"]
      and qcard.ask_id == "q47e57b10")
saved = crcore.waiting_on, crcodex.answer_question, crcore.note_sent
pressed, open_now = [], list(QS15)
crcore.waiting_on = lambda s, pt=None: ({"id": "q47e57b10", "name": "CodexQuestions", "input": {"questions": list(open_now), "left": len(open_now)}}, []) if open_now else None


def fake_answer(s, title, k=None, label="", text=None):
    pressed.append((title, k, label, text))
    open_now[:] = [q for q in open_now if q["title"] != title]
    return True, ""


crcodex.answer_question = fake_answer
crcore.note_sent = lambda s, text, files=0: pressed.append(("sent", text))
try:
    out = rq.codex_pick("cx-crew", QS_E, "1234abcd", "1", "2")
    body = json.dumps(out.card, ensure_ascii=False)
    win = [b for e in out.card["elements"] if e["tag"] == "action" for b in e["actions"] if b.get("text", {}).get("content") == "Windows"][0]
    check("点了 Windows：只在卡片上选上，不按键；按钮蓝色实心、题目后面「已选」", not pressed and out.in_place and win["type"] == "primary_filled"
          and "已选：Windows" in body and "提交已选的 1 道" in body, pressed)
    out = rq.codex_pick("cx-crew", QS_E, "1234abcd", "1", "3")
    body = json.dumps(out.card, ensure_ascii=False)
    check("选错了再点别的：改成 Linux", "已选：Linux" in body and "已选：Windows" not in body and not pressed)
    out = rq.codex_pick("cx-crew", QS_E, "1234abcd", "2", "5")
    sel = [a for e in out.card["elements"] if e["tag"] == "action" for a in e["actions"] if a["tag"] == "select_static"][0]
    check("下拉选完：框里显示选中的", sel.get("initial_option") == "#cxq cx-crew 1234abcd 2 5" and "已选：10022" not in json.dumps(out.card, ensure_ascii=False)
          and "已选：五" in json.dumps(out.card, ensure_ascii=False))
    out = rq.codex_submit("cx-crew", QS_E)
    body = json.dumps(out.card, ensure_ascii=False)
    check("〔提交〕：按顺序交选好的两道，交了的收成「你的回答」，没选的还在", [x[:2] for x in pressed if x[0] != "sent"] == [(QS15[1]["title"], 3), (QS15[2]["title"], 5)]
          and ("sent", "回答「你平时从什么设备连接这台服务器？」：Linux") in pressed and "你的回答：Linux" in body and "你的回答：五" in body
          and "有公网服务器，可以中转" in body and "提交" not in body.split("你的回答：五")[1].split("看屏幕")[0] and out.in_place, pressed)
    check("还剩一道：标题写在问你", "在问你" in out.card["header"]["title"]["content"])
    pressed.clear()
    stale = rq.codex_pick("cx-crew", QS_E, "deadbeef", "0", "1")
    check("旧卡片上的题已经过去了：不按键", not pressed and "过去了" in stale.card["header"]["title"]["content"])
    out = rq.codex_pick("cx-crew", QS_E, None, None, None, text="  先 别 中转 ")
    body = json.dumps(out.card, ensure_ascii=False)
    check("打字：直接交给还开着的第一道（填进 Other）", pressed[0] == (QS15[0]["title"], None, "", "  先 别 中转 ") and pressed[1][1].endswith("：先 别 中转"), pressed)
    check("全交完：标题「已回答」，每道题一行「你的回答」，没有选项按钮", "已回答" in out.card["header"]["title"]["content"]
          and body.count("你的回答：") == 3 and "Windows" not in body and out.card["header"]["template"] == "grey", out.card["header"])
finally:
    crcore.waiting_on, crcodex.answer_question, crcore.note_sent = saved


# 打字：你在飞书上打的当回答；秘书 / 同事用 cr 转来的照常发进主输入框（提问框展开着就先不发）
sent_txt, picked = [], []
saved = crcore.pane_rows, crcore.send_text, rq.resolve, rq.codex_pick, rq.live_card, rq.ask_card
crcore.send_text = lambda s, text, files=(): sent_txt.append(text)
rq.resolve = lambda target, reg, want_alive=True: ("cx-crew", dict(QS_E, pid=1), None)
rq.codex_pick = lambda name, s, call, idx, k, text=None: picked.append(text) or crcore.Card({}, "picked")
rq.live_card = lambda name, s, since: crcore.Card({}, "live")
rq.ask_card = lambda name, s, *a, **kw: crcore.Card({}, "ask:" + kw.get("note", ""))
try:
    crcore.pane_rows = lambda s: [r.strip() for r in QFOLD]
    crcore.VIA.update(via="feishu", by="")
    rq.deliver("cx-crew", "都没有", {})
    check("飞书上打的字：当第一道题的回答，不往主输入框里发", picked == ["都没有"] and not sent_txt, (picked, sent_txt))
    crcore.VIA.update(via="", by="")
    rq.deliver("cx-crew", "秘书转来的：先看日志", {})
    check("cr 转来的话、提问框收起着：照常发进主输入框，不当回答", sent_txt == ["秘书转来的：先看日志"] and picked == ["都没有"], (picked, sent_txt))
    crcore.pane_rows = lambda s: [r.strip() for r in QOPEN]
    out = rq.deliver("cx-crew", "又一句", {})
    check("cr 转来的话、提问框正开着：先不发", sent_txt == ["秘书转来的：先看日志"] and "正开着" in out.text, out.text)
finally:
    crcore.VIA.update(via="", by="")
    crcore.pane_rows, crcore.send_text = saved[:2]
    del rq.resolve, rq.codex_pick, rq.live_card, rq.ask_card


# ---------------------------------------------------------------- 启动参数
check("认出非交互的子命令", crcodex._first_arg(["app-server", "--listen", "unix://"]) == "app-server"
      and crcodex._first_arg(["-s", "read-only", "-a", "on-request"]) is None)
check("上班时沿用的参数：去掉 resume、会话 id、开场白", crcodex.keep_args(["resume", "01a1", "-m", "x", "--yolo", "fix it"]) == ["-m", "x", "--yolo"]
      and crcodex.keep_args(["-s", "read-only", "-a", "on-request"]) == ["-s", "read-only", "-a", "on-request"])
check("选哪一套 codex", crcodex.pick_home("codex")["label"] == "官方" and crcodex.pick_home("codex-b")["label"] == "备用"
      and crcodex.pick_home("codex:备用")["home"] == HOME_B and crcodex.pick_home("codex-xyz") is None)
argv = crcodex.launch_argv(HOME_B, "/w", ["resume", "id", "--no-daemon"], "127.0.0.1:1")
check("启动命令带上那一套的环境脚本和代理，不用共用的后台服务", argv[1:] == ["--proxy", "http://127.0.0.1:1", "--home", HOME_B, "/nonexistent-env.sh", "/w"] + crcodex.OWN_ARGS + ["resume", "id"], argv)
check("上班沿用的参数里不重复带机器人自己加的", crcodex.keep_args(["--no-daemon", "-c", "check_for_update_on_startup=false", "resume", "x", "-m", "m"]) == ["-m", "m"])
check("环境脚本不在就不启动", "找不到" in crcodex.check(HOME_B))

# ---------------------------------------------------------------- rollout
SID = "01a17700-0000-7000-8000-000000000001"
day = os.path.join(HOME_B, "sessions", "2026", "10", "26")
os.makedirs(day)
path = os.path.join(day, f"rollout-2026-10-26T10-00-00-{SID}.jsonl")


def rec(t, typ, payload):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"
    return json.dumps({"timestamp": stamp, "type": typ, "payload": payload}, ensure_ascii=False, separators=(",", ":"))


def item(kind, **kw):
    return {"type": "item_completed", "item": dict(type=kind, **kw)}


T0 = 1792990000
lines = [rec(T0, "session_meta", {"id": SID, "cwd": "/w"}),
         rec(T0 + 1, "event_msg", {"type": "task_started", "turn_id": "t1", "turn_attribution": {"turn_trigger": "user"}}),
         rec(T0 + 1, "turn_context", {"model": "gpt-6.1-sol", "effort": "high"}),
         rec(T0 + 2, "event_msg", item("UserMessage", content=[{"type": "text", "text": "跑一下测试"}])),
         rec(T0 + 3, "event_msg", item("AgentMessage", content=[{"type": "Text", "text": "我先跑测试。"}])),
         rec(T0 + 4, "response_item", {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                                       "input": 'text(await tools.exec_command({cmd:"pytest -q",workdir:"/w"}))'}),
         rec(T0 + 9, "event_msg", item("CommandExecution", command=["/bin/bash", "-lc", "pytest -q"])),
         rec(T0 + 9, "response_item", {"type": "custom_tool_call_output", "call_id": "c1", "output": []}),
         rec(T0 + 10, "event_msg", {"type": "token_count", "info": {"last_token_usage": {"total_tokens": 25840},
                                                                     "model_context_window": 258400},
                                    "rate_limits": {"primary": {"used_percent": 12.0, "window_minutes": 300},
                                                    "secondary": {"used_percent": 51.0, "window_minutes": 10080}}}),
         rec(T0 + 11, "event_msg", item("AgentMessage", content=[{"type": "Text", "text": "全部通过。要我顺手提交吗？"}])),
         rec(T0 + 12, "event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "全部通过。要我顺手提交吗？"})]
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
check("按会话 id 找到 rollout（也认截短的前缀）", crcodex.rollout_path(SID) == path and crcodex.rollout_path(SID[:29]) == path
      and crcore.transcript_path(SID) == path)
check("一轮做完了：停下", crcodex.turn_state(path)[:2] == ("idle", T0 + 12))
crcore.mark_sent_start(T0 - 100)               # 守护进程第一次跑时记下：从这以后机器人替谁打的字都有记录
turn = crcore.last_turn(SID)
check("最后一轮：你叫它干的、它说的话、做了几步", {k: turn[k] for k in ("id", "t", "by", "texts", "tools")} == {
    "id": "t1", "t": T0 + 1, "by": "you", "texts": ["我先跑测试。", "全部通过。要我顺手提交吗？"], "tools": 1}, turn)
check("这一轮你说的话：没经过机器人，算你在电脑上亲手打的", [(x["kind"], x["src"], x["text"]) for x in turn["said"]]
      == [("human", "terminal", "跑一下测试")], turn["said"])
check("结论", crcore.last_reply(SID, final_only=True) == "全部通过。要我顺手提交吗？")
asked, steps, stopped = crcore.turn_steps(SID, T0)
check("过程：你说的话、它说的、跑的命令", asked == "跑一下测试" and ("tool", ("⚙️ 运行命令", "pytest -q", None), True) in steps and not stopped, steps)
check("拍板的判断跟 Claude 一样", crcore.is_decision(crcore.last_turn(SID)["texts"][-1]))
st = crcodex.session_status({"transcript": path, "home": HOME_B})
check("模型、思考程度、上下文、额度", st["model"] == "gpt-6.1-sol" and st["effort"] == "high" and st["ctx"] == 10
      and st["5h"] == 12 and st["7d"] == 51, st)
check("卡片上的用量是它自己账号的", crcore.usage_text(st) == "上下文 10% · 5 小时额度 12% · 周额度 51%", crcore.usage_text(st))
check("你最后说的话 / 之后说过没有", crcore.last_human_text(SID) == "跑一下测试" and crcore.user_spoke_since(SID, T0)
      and not crcore.user_spoke_since(SID, T0 + 5))

with open(path, "a", encoding="utf-8") as f:   # 第二轮：还在跑一条命令
    f.write("\n".join([rec(T0 + 20, "event_msg", {"type": "task_started", "turn_id": "t2", "turn_attribution": {"turn_trigger": "goal"}}),
                       rec(T0 + 21, "response_item", {"type": "custom_tool_call", "call_id": "c2", "name": "exec",
                                                      "input": 'text(await tools.exec_command({cmd:"python train.py"}))'})]) + "\n")
check("新的一轮开始了：在干活（增量读到）", crcodex.turn_state(path)[:2] == ("busy", T0 + 20))
check("/goal 自己接着干的一轮算后台", crcore.last_turn(SID)["by"] == "background")
run = crcore.turn_steps(SID, T0 + 20)[1]
check("还在跑的那一步（跑了多久算到现在）", [(k, t[:2], d) for k, t, d in run] == [("tool", ("⚙️ 运行命令", "python train.py"), False)]
      and run[0][1][2] > 0, run)
check("codex 每一步的耗时：item_completed 里的开始、结束时间", crcodex._took({"started_at_ms": 1000, "completed_at_ms": 13500}) == 12.5)
with open(path, "a", encoding="utf-8") as f:   # 出错结束
    f.write(rec(T0 + 30, "event_msg", {"type": "task_complete", "turn_id": "t2", "last_agent_message": None, "error": {
        "message": json.dumps({"type": "error", "status": 400, "error": {"message": "The 'x' model is not supported"}})}}) + "\n")
check("出错的一轮：错误当成它说的话显示", crcore.last_reply(SID, final_only=True) == "⚠️ 这一轮出错了：The 'x' model is not supported")

# ---------------------------------------------------------------- 类型标记
check("类型标记看程序和配置目录，不看名字",
      crcore.mark({"agent": "claude", "cfg": crcore.CLAUDE_DIR}) == "CC"
      and crcore.mark({}) == "CC"
      and crcore.mark({"agent": "claude", "cfg": "/x/claude-work"}) == "CC·WORK"
      and crcore.mark({"agent": "codex", "home": HOME_A}) == "Codex·官方"
      and crcore.mark({"agent": "codex", "home": HOME_B}) == "Codex·备用")
crcore.save_json(crcore.REGISTRY, {"cx-a": {"agent": "codex", "home": HOME_B, "alive": True}, "exp": {"agent": "claude", "alive": True}})
crcore.save_json(crcore.BADGES, {"cx-a": 8, "exp": 2})
crcore._reg_cache["t"] = 0
check("卡片标题里每个员工都带上工号和类型", crcore.tag_names("💬 2号【exp】→ 【cx-a】") == "💬 2号 CC【exp】→ 8号 Codex·备用【cx-a】",
      crcore.tag_names("💬 2号【exp】→ 【cx-a】"))
check("名单里的写法", crcore.label_name("cx-a") == "8号 Codex·备用 cx-a")
head = crcore.alert("📝【cx-a】：汇报", "x", "orange").card["header"]
check("卡片标题：工号和名字，类型做成右边的小标签（Codex 紫色）", head["title"]["content"] == "8号 cx-a · 汇报"
      and head["text_tag_list"][0]["text"]["content"] == "Codex·备用" and head["text_tag_list"][0]["color"] == "violet", head)
head = crcore.alert("💬 2号【exp】→ 【cx-a】", "", "grey").card["header"]
check("两个员工的标题（同事传话）", head["title"]["content"] == "2号 exp → 8号 cx-a", head)

print(f"\n全部通过：{len(passed)} 项")
