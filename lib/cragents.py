"""统一的员工数据层：不管员工是 Claude Code 还是 Codex（以后还能加别的），都整理成同一套字段。

思路参考 t3code 的多 agent 支持：每种 agent 一个适配器（Claude 的在 crcore 里，Codex 的在 crcodex.py），
对外只有统一的状态、待处理请求和能力（caps），用的人不用知道是哪种 agent：
- 状态 state：working 在干活 / waiting_approval 等你批准 / waiting_answer 等你回答选择题 /
  waiting_decision 停下来问你拿主意（它写在回复里的问题）/ background 等后台任务 / idle 停下 / offline 下班了；
- 待处理请求 pending：{"kind": approval / question / screen, "title", "detail", "options": [{"label", "cmd"}]}；
- 操作：每个员工的 cmds、每个选项的 cmd 都是一条普通命令，交给 `cr`（或 crcore.Remote().handle）执行，
  跟飞书卡片上的按钮走同一条路；App 发话时设环境变量 CR_VIA=app，员工那边就记成「📱 你在 App 上说」；
- 听到的话 heard：最近有人跟它说的话，每条 {"t", "kind": human / slash / peer / background, "text", "images",
  "src": feishu / cli / app / terminal（你在电脑上亲手打的）/ peer（同事）/ background / unknown, "by": 哪个员工}。
  全部记在 data/heard.jsonl（守护进程边看对话记录边记，多一个 "name"、"sid"），Claude 和 Codex 一个格式。

飞书以外的客户端（比如以后的手机 App）读 data/crew.json（守护进程有变化就更新）或运行 `cr json`。
"""
import json
import os
import socket
import time

import crcodex
import crcore

SNAPSHOT = f"{crcore.DATA}/crew.json"

CAPS = {   # 各种 agent 能做什么（客户端据此决定显示哪些按钮）
    "claude": {"effort": [lv for lv, _ in crcore.EFFORTS], "slash": [c for c, _, _ in crcore.SLASH], "interrupt": "Esc",
               "exit": "/exit", "resume": "claude --resume <session_id>", "quota": "claude-account",
               "peer_messages": True, "multi_select_questions": True},
    "codex": {"effort": list(crcodex.EFFORTS), "slash": [c for c, _, _ in crcodex.SLASH], "interrupt": "Esc",
              "exit": "/quit（先 Esc 停下手上的活）", "resume": "codex resume <session_id>（用原来那套 CODEX_HOME）",
              "quota": "codex-home", "peer_messages": False, "multi_select_questions": False},
}


def agent_of(e):
    return (e or {}).get("agent") or "claude"


def label(e):
    """类型标记，跟卡片、看板上的一样：CC / CC·WORK / Codex·官方 / Codex·备用（按实际跑的程序和配置目录，不看名字）。"""
    return crcore.mark(e)


def state_of(name, e, asking):
    if not e.get("alive"):
        return "offline"
    if e.get("status") == "busy":
        return "working"
    if e.get("waiting"):
        return "waiting_approval" if "permission" in e["waiting"] else "waiting_answer"
    if name in asking:
        return "waiting_decision"
    if e.get("status") == "shell":
        return "background"
    return "idle"


HEARD_SHOW = 8      # 快照里每个员工带最近几句听到的话（一天内）


def employee(remote, name, e, badges, with_pending=True, heard=()):
    st = remote.state
    alive = bool(e.get("alive"))
    bar = crcore.session_status(e) if alive else {}
    latest = (st.get("bg") or {}).get(name)
    asking = (st.get("asking") or {}).get(name)
    out = {
        "name": name, "badge": badges.get(name), "role": "secretary" if crcore.is_assistant(name) else "staff",
        "agent": agent_of(e), "flavor": e.get("flavor") or "", "label": label(e),
        "home": e.get("home") or e.get("cfg") or "",
        "state": state_of(name, e, st.get("asking") or {}), "alive": alive,
        "since": e.get("statusAt"), "started": e.get("startedAt"), "offline_since": None if alive else e.get("died"),
        "cwd": e.get("cwd"), "topic": remote.topic(name, e), "session_id": e.get("sessionId"),
        "model": bar.get("model") or "", "effort": bar.get("effort") or "", "ctx": bar.get("ctx"),
        "quota": {k: bar.get(k) for k in ("5h", "7d", "30d") if bar.get(k) is not None},
        "latest": {"t": latest["t"], "text": latest["text"]} if latest else None,
        "decision": {"t": asking["t"], "q": asking["q"]} if asking else None,
        "pending": None,
        "heard": [{k: x.get(k) for k in crcore.SAID_KEYS} for x in heard][-HEARD_SHOW:],
        "caps": CAPS[agent_of(e)],
        "cmds": {"say": f"@{name} <内容>", "stop": f"#stop {name}", "screen": f"屏 {name}", "latest": f"看 {name}",
                 "full": f"看 {name} 全部", "keys": f"键 @{name} <键…>", "off": f"下班 {name}", "on": f"上班 {name}",
                 "effort": f"#effort {name} <档位>", "slash": f"#slash {name} </命令>"},
    }
    if with_pending and alive and e.get("waiting"):
        try:
            out["pending"] = remote.pending_request(name, e)
        except Exception as ex:                          # 读屏失败也别耽误别的
            out["pending"] = {"kind": "error", "title": str(ex), "options": []}
    return out


def snapshot(remote, reg=None, with_pending=True):
    """整个员工队伍的快照（JSON 能序列化的 dict）。"""
    if reg is None:
        reg = remote.refresh()[1]
    badges = crcore.load_json(crcore.BADGES, {})
    default = remote.state.get("default")
    heard = {}
    for x in crcore.heard_log(since=time.time() - 86400):       # 读一遍，按员工分
        heard.setdefault(x.get("name"), []).append(x)
    rows = []
    for name, e in reg.items():
        if crcore.is_quiet(name) and not e.get("alive"):
            continue                                     # 每日资讯跑完自己退出，不算
        if crcore.is_assistant(name) and name != default:
            continue                                     # 以前的助手对话只在「对话记录」里
        if not e.get("alive") and time.time() - e.get("died", 0) > 7 * 86400:
            continue
        rows.append(employee(remote, name, e, badges, with_pending, heard.get(name, ())))
    rows.sort(key=lambda r: (r["role"] != "secretary", not r["alive"], r["badge"] or 999, r["name"]))
    codex_quota = {}
    for h in crcodex.homes():
        q = crcodex.quota_windows(crcodex.latest_quota(h["home"]))
        if q:
            codex_quota[h["label"] or h["home"]] = q
    return {"version": 1, "t": time.time(), "host": socket.gethostname(), "default": default,
            "talking_to": remote.state.get("current"), "night": remote.quiet_now(), "held": remote.state.get("held", 0),
            "quota": {"claude": crcore.latest_quota(), "codex": codex_quota},
            "agents": {k: {"caps": v} for k, v in CAPS.items()},
            "employees": rows}


def write_snapshot(remote, reg=None):
    """内容变了才写 data/crew.json（时间戳不算变化）。返回写没写。"""
    snap = snapshot(remote, reg)
    body = json.dumps({k: v for k, v in snap.items() if k != "t"}, ensure_ascii=False, sort_keys=True, default=str)
    old = crcore.load_json(SNAPSHOT, None)
    if old is not None and json.dumps({k: v for k, v in old.items() if k != "t"}, ensure_ascii=False, sort_keys=True, default=str) == body:
        return False
    crcore.save_json(SNAPSHOT, snap)
    return True
