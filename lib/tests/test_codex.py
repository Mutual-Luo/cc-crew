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
