"""cc-crew 守护进程：定时监控 + 飞书长连接收消息/按钮 + 把通知发给你。

飞书凭证在安装目录的 config.env（老版本叫 feishu.env）：
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_ALLOW_OPEN_ID（只认这一个人）
没配好时只跑监控，通知留在 data/outbox.jsonl。
"""
import json
import logging
import re
import os
import queue
import threading
import time

import crcore

log = logging.getLogger("cc-crew")
ENV_FILE = f"{crcore.ROOT}/feishu.env"
REJECTED = f"{crcore.DATA}/rejected.log"
MSGMAP = f"{crcore.DATA}/msgmap.json"
SEEN = f"{crcore.DATA}/seen_msgs.json"       # 处理过的消息 id（重启后也记得，补收时不会重复执行）
CHAT = f"{crcore.DATA}/feishu_chat.json"      # 你和机器人单聊的 id：只存在本地，补收消息时用
SLOTS = f"{crcore.DATA}/slots.json"          # 固定卡片（〔📋 后台进度〕）的消息 id，和它下面又来了几条消息
HEARTBEAT = f"{crcore.DATA}/heartbeat"        # 守护进程每隔几秒写一下时间；看门狗靠它判断是不是挂了
WATCHDOG_NOTE = f"{crcore.DATA}/watchdog.json"  # 看门狗重启过：启动后告诉你停了多久
BURY = 6                         # 固定卡片下面来了这么多条新消息，就算被刷上去了：下次更新挪到最下面
GROUP_NAME = "我的Claude员工们-1群"  # 本机的员工群；别的机器在自己的 feishu.env 里写 FEISHU_GROUP_NAME=…-2群
GROUP_RETRY_SECS = 300           # 建群失败（比如还没开权限）隔多久再试
MEMBER_CHECK_SECS = 300          # 多久看一次群里有没有被拉进别人
PENDING = crcore.PENDING
CATCHUP_SECS = 30                # 多久翻一次聊天记录，补收没推过来的消息（机器人重启、断线时飞书不会重发）
CATCHUP_WINDOW = 30 * 60         # 只补收这么久以内的，太久的不再执行
STALE = f"{crcore.DATA}/stale_msgs.json"   # 关机期间你发的、超过半小时没执行的消息：等你点〔重发〕
STALE_DAYS = 3                   # 最多往回翻这么多天
TICK_SECS = 3                    # 多久检查一次有没有会话停下
LIVE_SECS = 3                    # “思考中”卡片多久刷新一次（久了放慢，见 live_interval）
MSG_LIMIT = 3800
DM, GROUP = "dm", "group"         # 消息在哪边：你和机器人的单聊（助手自己的对话框）/ 员工群

lock = threading.Lock()          # 命令和监控共用一个 Remote，串行执行
remote = crcore.Remote()


def read_env():
    env = {}
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def preview(mtype, raw):
    """消息的一行预览：文字消息取文字，其他写类型。"""
    try:
        body = json.loads(raw or "{}")
    except ValueError:
        body = {}
    if mtype == "text":
        return re.sub(r"@_user_\d+\s*", "", body.get("text", "")).strip() or "（空）"
    if mtype == "post":
        parts = [x.get("text", "") for para in (body.get("content") or []) for x in para if isinstance(x, dict)]
        return (body.get("title", "") + " " + "".join(parts)).strip() or "［富文本］"
    return {"image": "［图片］", "file": "［文件］", "audio": "［语音］", "media": "［视频］"}.get(mtype, f"［{mtype}］")


def remember(message_id, name):
    mp = crcore.load_json(MSGMAP, {})
    mp[message_id] = name
    if len(mp) > 1000:
        mp = dict(list(mp.items())[-800:])
    crcore.save_json(MSGMAP, mp)


def owner(reply):
    """这条消息是哪个员工的：实时卡片、回复、汇报记着名字，别的看标题里第一个「【名字】」。"""
    for name in (getattr(reply, "pending_for", None), getattr(reply, "reply_for", None),
                 (getattr(reply, "merge", None) or {}).get("name")):
        if name:
            return name
    head = (reply.text if isinstance(reply, crcore.Card) else str(reply)).split("\n", 1)[0]
    m = re.search(r"【([^】]+)】", head)
    return m.group(1) if m else None


class Feishu:
    def __init__(self, app_id, app_secret, allow, group_name=GROUP_NAME):
        import lark_oapi as lark
        self.lark, self.allow = lark, allow
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        self.app_id, self.app_secret = app_id, app_secret
        seen = crcore.load_json(SEEN, {})
        self.seen_since = seen.get("since") or time.time()    # 第一次运行：只补收这之后的
        self.seen = seen.get("ids", {})
        if not seen:
            crcore.save_json(SEEN, {"since": self.seen_since, "ids": {}})
        self.seen_lock = threading.Lock()
        chat = crcore.load_json(CHAT, {})
        self.chat_id = chat.get("chat_id")          # 你和机器人的单聊（建群前用）
        self.group = chat.get("group")              # 员工群：建好以后所有消息都在群里
        self.group_name = group_name
        self.next_group_try = self.next_member_check = 0
        self.group_err = None
        self.offline_since = None                   # 停了很久（关机）：从这个时间起的旧消息列成卡片等你点重发
        self.jobs = queue.Queue()
        self.slot_lock = threading.Lock()
        self.after = {}                              # “思考中”卡片 id -> 它下面又来了几条消息（被刷上去多少）
        self.merges = {}                             # 员工 -> 它这 10 分钟的汇报卡片 {"mid", "t0", "after", "entries", "where"}
        self.chat_of = {}                            # 发出去的消息 id -> 在单聊还是群里：被刷上去只数同一边的新消息
        self.catchup_errs = {}

    def bump(self, where=GROUP):
        """那边（单聊 / 群）又多了一条消息：那边的固定卡片又往上被刷了一格。"""
        with self.slot_lock:
            for k in self.after:
                if self.chat_of.get(k, GROUP) == where:
                    self.after[k] += 1
            for v in list(self.merges.values()):   # 监控线程同时在改：先拷一份再数
                if v.get("where", GROUP) == where:
                    v["after"] += 1
            slots = crcore.load_json(SLOTS, {}) if where == self.where_for(None) else {}   # 看板在群里
            if slots:
                for v in slots.values():
                    v["after"] = v.get("after", 0) + 1
                crcore.save_json(SLOTS, slots)

    def put_slot(self, slot, card, minor=False, move=False):
        """固定卡片（看板）：原地更新（飞书不响）。在群里置顶了就一直原地改；
        没置顶时，被新消息刷上去太多、又有人停下说了话，就挪到最下面，旧的那张撤回。"""
        cur = crcore.load_json(SLOTS, {}).get(slot)
        if not move and cur and (cur.get("pinned") or minor or cur.get("after", 0) < BURY) and self.patch(cur["mid"], card):
            return
        mid = self.send(card, self.where_for(None))
        if not mid:
            return
        pinned = bool(self.group) and self.pin(mid)
        with self.slot_lock:
            slots = crcore.load_json(SLOTS, {})
            slots[slot] = {"mid": mid, "after": 0, "pinned": pinned}
            crcore.save_json(SLOTS, slots)
        if cur:
            self.recall(cur["mid"])

    def merge(self, card):
        """汇报 / 进展：同一个员工 10 分钟内的并进它上一张（原地更新，飞书不响）；上一张已经被两条以上的消息
        刷上去了、并了 8 条了、或者这条太长要分几张发，就照常发新的。"""
        info, cur = card.merge, self.merges.get(card.merge["name"])
        if (cur and time.time() - cur["t0"] < crcore.MERGE_SECS and cur["after"] <= 2 and len(cur["entries"]) < 8
                and not card.more and self.patch(cur["mid"], crcore.merged_card(cur["entries"], card))):
            cur["entries"].append(info)
            return
        where = self.route(card)
        mid = self.send(card, where)
        if mid:
            self.merges[info["name"]] = {"mid": mid, "t0": time.time(), "after": 0, "entries": [info], "where": where}

    def recall(self, mid):
        from lark_oapi.api.im.v1 import DeleteMessageRequest
        resp = self.call(self.client.im.v1.message.delete, DeleteMessageRequest.builder().message_id(mid).build())
        if not resp.success():                       # 撤回不了（比如太久了）：缩成一行，指到下面
            self.patch(mid, crcore.alert("📋 后台进度已挪到下面 ⬇️", "", "grey"))

    def retire(self, old, card):
        """上一句还没等到回复又说了一句：新卡片已经接着上一张显示（连发的几句、已有的进度都在），旧的撤回。
        上一句其实已经答完、卡片还没来得及换的：旧卡片原地换成那张回复（换不了就发出来），不撤回。"""
        self.after.pop(old, None)
        close = getattr(card, "close_old", None)
        if close and self.patch(old, close):
            return
        if close:
            self.send(close)
        self.recall(old)

    def claim(self, mid):
        """这条消息还没处理过就记下来并返回 True（飞书推送和补收同时碰到同一条时，只处理一次）。"""
        with self.seen_lock:
            if mid in self.seen:
                return False
            cut = time.time() - 2 * 86400
            self.seen = {k: t for k, t in self.seen.items() if t > cut}
            self.seen[mid] = time.time()
            crcore.save_json(SEEN, {"since": self.seen_since, "ids": self.seen})
            return True

    def set_chat(self, chat_id):
        if chat_id and chat_id != self.chat_id:
            self.chat_id = chat_id
            crcore.save_json(CHAT, dict(crcore.load_json(CHAT, {}), chat_id=chat_id))

    def where_for(self, name):
        """员工 name 的消息发到哪：助手的发到你和机器人的单聊（它自己的对话框，不会被员工们的消息冲掉），别人的发到员工群。"""
        return DM if not self.group or (name and crcore.is_assistant(name)) else GROUP

    def route(self, reply, origin=None):
        """这条消息发到哪：属于哪个员工的按 where_for；不属于谁的（帮助、出错、看板）回到你发命令的那边，监控自己发的去群里。"""
        name = owner(reply)
        return self.where_for(name) if name or not self.group else origin or GROUP

    def chats(self):
        """补收要翻的聊天：员工群、你和机器人的单聊。"""
        return [(w, c) for w, c in ((GROUP, self.group), (DM, self.chat_id)) if c]

    def dm_intro(self):
        """助手搬进单聊后，在单聊里说一次：以后助手在这儿（也让这个对话框出现在你的消息列表上面）。"""
        chat = crcore.load_json(CHAT, {})
        if not self.group or chat.get("dm_intro"):
            return
        body = (f"以后助手的进度、回复和提问都发在这个单聊里，不会被员工们的消息冲掉。\n"
                f"在这里直接打字就是跟助手说；「看看」「@3 内容」这些照样能用。员工们的卡片和看板还在「{self.group_name}」群里。")
        if self.send(crcore.alert("👋 助手搬到这里了", body, "turquoise"), DM):
            crcore.save_json(CHAT, dict(crcore.load_json(CHAT, {}), dm_intro=time.time()))

    # ---------------- 员工群
    def make_group(self):
        """建员工群：你是群主，机器人是管理员（能把看板置顶）。建好后所有消息都发到群里，单聊不再用。"""
        from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody
        body = (CreateChatRequestBody.builder().name(self.group_name)
                .description("这台机器上的 Claude Code：你是老板（群主），每个会话是一个员工，助手是秘书。机器人只听你一个人的。")
                .owner_id(self.allow).user_id_list([self.allow]).chat_mode("group").chat_type("private").build())
        resp = self.call(self.client.im.v1.chat.create, CreateChatRequest.builder().user_id_type("open_id")
                         .set_bot_manager(True).request_body(body).build())
        if not resp.success():
            self.next_group_try = time.time() + GROUP_RETRY_SECS
            if resp.code != self.group_err:          # 同一个原因只记一次（比如还没开 im:chat 权限）
                log.warning("建员工群失败 code=%s msg=%s", resp.code, resp.msg[:160])
                self.group_err = resp.code
            return False
        self.group = resp.data.chat_id
        crcore.save_json(CHAT, dict(crcore.load_json(CHAT, {}), group=self.group))
        crcore.save_json(SLOTS, {})                  # 看板重新发到群里（并置顶）
        log.info("建好了员工群「%s」", self.group_name)
        self.create("text", json.dumps({"text": f"员工们以后在「{self.group_name}」群里；这个单聊留给助手，在这里直接打字就是跟助手说。"},
                                       ensure_ascii=False), p2p=True)
        crcore.save_json(CHAT, dict(crcore.load_json(CHAT, {}), dm_intro=time.time()))
        self.jobs.put(crcore.welcome_card(self.group_name))
        with lock:
            remote.state = crcore.load_json(crcore.STATE, remote.state)
            crcore.notify(remote.status_card(), "status")
            remote.save()
        return True

    def check_members(self):
        """群里被拉进了别人：他们说的话不理，但他们看得到群里的内容，提醒你一次。"""
        from lark_oapi.api.im.v1 import GetChatMembersRequest
        resp = self.call(self.client.im.v1.chat_members.get, GetChatMembersRequest.builder().chat_id(self.group)
                         .member_id_type("open_id").page_size(50).build())
        if not resp.success():
            log.warning("查群成员失败 code=%s msg=%s", resp.code, resp.msg[:160])
            return
        others = sorted(m.name or "某人" for m in resp.data.items or [] if m.member_id != self.allow)
        chat = crcore.load_json(CHAT, {})
        if others and "、".join(others) != chat.get("warned"):
            self.jobs.put(f"⚠️ 群里多了 {len(others)} 个人：{'、'.join(others)}。我只听你一个人的，他们发的话不会理；"
                          "但他们能看到群里所有内容（包括 Claude 的回复和代码），建议把他们移出群。")
        crcore.save_json(CHAT, dict(chat, warned="、".join(others)))

    def pin(self, mid):
        """把看板设成群置顶：一直在群顶上，不会被刷上去。"""
        from lark_oapi.api.im.v1 import (ChatTopNotice, PutTopNoticeChatTopNoticeRequest,
                                         PutTopNoticeChatTopNoticeRequestBody)
        resp = self.call(self.client.im.v1.chat_top_notice.put_top_notice, PutTopNoticeChatTopNoticeRequest.builder()
                         .chat_id(self.group).request_body(PutTopNoticeChatTopNoticeRequestBody.builder().chat_top_notice(
                             [ChatTopNotice.builder().action_type("1").message_id(mid).build()]).build()).build())
        if not resp.success():
            log.warning("置顶看板失败 code=%s msg=%s", resp.code, resp.msg[:160])
        return resp.success()

    # ---------------- 发消息
    def send(self, reply, where=None):
        """发一条消息；where 不写就按 route 定（助手的去单聊，别的员工的去群里）。"""
        if not self.allow:
            return False
        where = where or self.route(reply)
        if isinstance(reply, crcore.Card):
            items = [("interactive", json.dumps(reply.card, ensure_ascii=False))]
        else:
            text = str(reply)
            items = [("text", json.dumps({"text": text[i:i + MSG_LIMIT]}, ensure_ascii=False))
                     for i in range(0, max(len(text), 1), MSG_LIMIT)]
        first_id = None
        m = re.search(r"【([^】]+)】", str(reply)[:60])
        for msg_type, content in items:
            mid = self.create(msg_type, content, p2p=where == DM)
            first_id = first_id or mid
            if mid:
                self.chat_of[mid] = where
                self.bump(where)
            if m and mid:
                remember(mid, m.group(1))
                self.merges.pop(m.group(1), None)    # 它有别的卡片发在后面了：下一条汇报另起一张，不往上面的旧卡片里塞
        for extra in getattr(reply, "more", []):      # 太长的回复：接着发「续」卡片
            self.send(extra, where)
        if len(self.chat_of) > 500:                  # 只留最近的，和还在数「被刷上去几格」的
            keep = set(self.after) | {v["mid"] for v in list(self.merges.values())}
            self.chat_of = {k: v for i, (k, v) in enumerate(list(self.chat_of.items())) if k in keep or i >= 200}
        return first_id

    def call(self, fn, req):
        """调飞书接口；网络抖一下就重试，别让回复丢了。"""
        for wait in (2, 5, 0):
            try:
                return fn(req)
            except Exception as e:  # noqa: BLE001 —— 超时、断连
                if not wait:
                    raise
                log.warning("飞书接口网络出错，%s 秒后重试：%s", wait, type(e).__name__)
                time.sleep(wait)

    def create(self, msg_type, content, p2p=False):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        to_group = bool(self.group) and not p2p
        req = (CreateMessageRequest.builder().receive_id_type("chat_id" if to_group else "open_id")
               .request_body(CreateMessageRequestBody.builder().receive_id(self.group if to_group else self.allow)
                             .msg_type(msg_type).content(content).build()).build())
        resp = self.call(self.client.im.v1.message.create, req)
        if not resp.success():
            log.error("发送失败 code=%s msg=%s", resp.code, resp.msg)
            if to_group:                             # 群没了、机器人被移出群…：这条先发到单聊，别丢
                return self.create(msg_type, content, p2p=True)
            return None
        if not to_group:
            self.set_chat(getattr(resp.data, "chat_id", None))
        return resp.data.message_id if resp.data else None

    def send_file(self, path, where=None):
        """把回复里提到的图 / PDF 发到你手机上：图片直接显示，其他是文件。where：你在哪边点的。"""
        from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody,
                                         CreateImageRequest, CreateImageRequestBody)
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            if ext in crcore.IMAGE_EXTS and os.path.getsize(path) <= 10 << 20:
                resp = self.client.im.v1.image.create(CreateImageRequest.builder().request_body(
                    CreateImageRequestBody.builder().image_type("message").image(f).build()).build())
                msg_type, key = "image", {"image_key": resp.data.image_key} if resp.success() else None
            else:
                resp = self.client.im.v1.file.create(CreateFileRequest.builder().request_body(
                    CreateFileRequestBody.builder().file_type("pdf" if ext == ".pdf" else "stream")
                    .file_name(os.path.basename(path)).file(f).build()).build())
                msg_type, key = "file", {"file_key": resp.data.file_key} if resp.success() else None
        if not key:
            log.error("上传文件失败 code=%s msg=%s", resp.code, resp.msg)
            return self.send(f"📎 {os.path.basename(path)} 没发出去（飞书返回 {resp.code}：{resp.msg}）。"
                             "如果是没有权限，要在飞书开放平台给应用开「获取与上传图片或文件资源」(im:resource)。", where)
        return self.create(msg_type, json.dumps(key, ensure_ascii=False), p2p=where == DM)

    def download(self, message_id, kind, key, name):
        """你发来的图片 / 文件：下载到 data/uploads/ 下，返回 (路径, 出错原因)。"""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest
        resp = self.call(self.client.im.v1.message_resource.get, GetMessageResourceRequest.builder()
                         .message_id(message_id).file_key(key).type(kind).build())
        if not resp.success():
            log.error("下载你发的%s失败 code=%s msg=%s", kind, resp.code, resp.msg)
            return None, f"{resp.code}：{resp.msg}"
        return crcore.save_upload(resp.file.read(), name or getattr(resp, "file_name", "") or "", kind), None

    def patch(self, message_id, reply):
        """把已发出的卡片原地换成新内容（一问一答只占一张卡片）。"""
        from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody
        req = (PatchMessageRequest.builder().message_id(message_id)
               .request_body(PatchMessageRequestBody.builder()
                             .content(json.dumps(reply.card, ensure_ascii=False)).build()).build())
        resp = self.call(self.client.im.v1.message.patch, req)
        if not resp.success():
            log.error("更新卡片失败 code=%s msg=%s", resp.code, resp.msg)
            return False
        for extra in getattr(reply, "more", []):
            self.send(extra, self.chat_of.get(message_id))
        return True

    # ---------------- 收消息 / 按钮
    def authorized(self, open_id):
        if open_id and open_id == self.allow:
            return True
        with open(REJECTED, "a", encoding="utf-8") as f:     # 只记 open_id，不记内容
            f.write(f"{time.strftime('%F %T')}\t{open_id}\n")
        log.warning("拒绝了一个未授权的飞书用户（open_id 已记到 rejected.log）")
        return False

    def on_message(self, data):
        try:
            self._on_message(data)
        except Exception:  # noqa: BLE001 —— 收消息出错要留下记录，不然你发的东西会悄悄丢掉
            log.exception("收消息出错")
            self.jobs.put("（这条消息没处理成功，已记在日志里）")

    def _on_message(self, data):
        ev, m = data.event, data.event.message
        if ev.sender and ev.sender.sender_type and ev.sender.sender_type != "user":
            return                                   # 机器人（包括它自己）发的消息：不用管，也不算「未授权的人」
        self.receive(m.message_id, m.chat_type, m.chat_id, ev.sender.sender_id.open_id, m.message_type, m.content,
                     m.parent_id or m.root_id)
        if m.chat_type == "p2p" and ev.sender.sender_id.open_id == self.allow:
            self.set_chat(m.chat_id)

    def receive(self, mid, chat_type, chat_id, open_id, mtype, raw, quoted):
        """处理你发来的一条消息（飞书推送来的，或补收的）。认员工群，和你跟机器人的单聊（助手的对话框：在这里说的都给助手）。"""
        where = DM if chat_type == "p2p" else GROUP
        ok_chat = where == DM or (self.group and chat_id == self.group)
        if not ok_chat or not self.authorized(open_id) or not self.claim(mid):
            return
        try:
            content = json.loads(raw or "{}")
        except ValueError:
            content = {}
        files = []                                   # (image/file, key, 文件名)：像在电脑上贴截图
        log.info("收到飞书消息：%s%s", mtype, "（单聊）" if where == DM and self.group else "")   # 只记类型，不记内容
        self.bump(where)
        if mtype == "text":
            text = content.get("text", "")
        elif mtype == "post":                        # 图文一起发
            if "content" not in content and len(content) == 1 and isinstance(next(iter(content.values())), dict):
                content = next(iter(content.values()))   # 有的版本外面多包一层语言：{"zh_cn": {...}}
            rows = content.get("content") or []
            text = "\n".join("".join(seg.get("text", "") for seg in row if seg.get("tag") in ("text", "a"))
                             for row in rows).strip()
            files = [("image", seg["image_key"], "") for row in rows for seg in row
                     if seg.get("tag") == "img" and seg.get("image_key")]
        elif mtype == "image":
            text, files = "", [("image", content.get("image_key"), "")]
        elif mtype == "file":
            text, files = "", [("file", content.get("file_key"), content.get("file_name", ""))]
        else:
            self.jobs.put(("note", "（目前支持文字、图片和文件；语音请用输入法转成文字再发）", where))
            return
        text = re.sub(r"@_user_\d+\s*", "", text).strip()   # 群里 @机器人 的那一截去掉
        name = crcore.load_json(MSGMAP, {}).get(quoted) if quoted else None   # 引用了哪个员工的卡片：直接对它说
        text = text.replace("@_all", "@所有人")      # 从 @ 列表点的「所有人」
        self.jobs.put(("files", mid, files, text, name, where) if files else ("typed", text, name, where))

    def catch_up_loop(self):
        """补收：机器人重启或断线的那一会儿你发的消息，飞书不会再推过来。
        每隔一会儿翻一下这个单聊最近的记录，没处理过的按顺序补上，并告诉你是补收的。"""
        from lark_oapi.api.im.v1 import ListMessageRequest
        while True:
            try:
                if self.offline_since and self.chats():
                    since, self.offline_since = self.offline_since, None
                    self.report_stale(since)
                if not self.group and time.time() >= self.next_group_try:
                    self.make_group()
                if self.group and time.time() >= self.next_member_check:
                    self.next_member_check = time.time() + MEMBER_CHECK_SECS
                    self.check_members()
                self.dm_intro()
                for where, chat in self.chats():
                    start = max(time.time() - CATCHUP_WINDOW, self.seen_since)
                    resp = self.client.im.v1.message.list(
                        ListMessageRequest.builder().container_id_type("chat").container_id(chat)
                        .start_time(str(int(start))).sort_type("ByCreateTimeAsc").page_size(50).build())
                    if not resp.success() and resp.code != self.catchup_errs.get(where):
                        log.warning("补收消息：读聊天记录失败 code=%s msg=%s", resp.code, resp.msg)   # 同一个原因只记一次
                    self.catchup_errs[where] = None if resp.success() else resp.code
                    for it in (resp.data.items or []) if resp.success() else []:
                        t = int(it.create_time) / 1000
                        if (it.sender and it.sender.sender_type == "user" and it.sender.id == self.allow
                                and it.message_id not in self.seen and start <= t < time.time() - 20):       # 刚发的还在路上，留给推送
                            log.info("补收一条消息（%s 发的）", time.strftime("%H:%M", time.localtime(t)))
                            self.jobs.put(("note", f"📥 补收到你 {time.strftime('%H:%M', time.localtime(t))} 发的一条消息"
                                                   "（当时机器人在重启或断线），现在处理。", where))
                            self.receive(it.message_id, "p2p" if where == DM else "group", chat, it.sender.id, it.msg_type,
                                         it.body.content if it.body else "", it.parent_id or it.root_id)
            except Exception:  # noqa: BLE001
                log.exception("补收消息出错")
            time.sleep(CATCHUP_SECS)

    def report_stale(self, since):
        """电脑关机 / 机器人停了很久：那段时间你发的、已经超过半小时的消息不自动执行（免得旧指令突然跑起来），
        列成一张卡片，每条一个〔重发〕按钮。群里和单聊各翻各的，卡片发在那一边。"""
        for where, chat in self.chats():
            self._report_stale(since, where, chat)

    def _report_stale(self, since, side, chat):
        from lark_oapi.api.im.v1 import ListMessageRequest
        start = max(since, time.time() - STALE_DAYS * 86400, self.seen_since)
        end = time.time() - CATCHUP_WINDOW
        if end <= start:
            return
        found, token = [], None
        for _ in range(5):                               # 最多翻 5 页（250 条）
            req = (ListMessageRequest.builder().container_id_type("chat").container_id(chat)
                   .start_time(str(int(start))).end_time(str(int(end))).sort_type("ByCreateTimeAsc").page_size(50))
            resp = self.client.im.v1.message.list((req.page_token(token) if token else req).build())
            if not resp.success():
                log.warning("翻关机期间的消息失败 code=%s msg=%s", resp.code, resp.msg)
                return
            for it in resp.data.items or []:
                if it.sender and it.sender.sender_type == "user" and it.sender.id == self.allow and it.message_id not in self.seen:
                    found.append(it)
            token = resp.data.page_token if resp.data.has_more else None
            if not token:
                break
        if not found:
            return
        store = crcore.load_json(STALE, {})
        lines, buttons = [], []
        for it in found:
            raw = it.body.content if it.body else ""
            store[it.message_id] = {"chat_type": "p2p" if side == DM else "group", "chat_id": chat, "open_id": it.sender.id,
                                    "mtype": it.msg_type, "raw": raw, "quoted": it.parent_id or it.root_id}
            when = time.strftime("%m-%d %H:%M", time.localtime(int(it.create_time) / 1000))
            lines.append(f"· {when}　{crcore.one_line(preview(it.msg_type, raw), 60)}")
            if len(buttons) < 12:
                buttons.append(crcore._button(f"重发 {when[6:]}", f"#resend {it.message_id}"))
            self.claim(it.message_id)                    # 以后补收不再碰它，只能点〔重发〕
        crcore.save_json(STALE, store)
        body = ("这段时间机器人没在运行（电脑关机、休眠或断网）。下面这些超过半小时了，**没有自动执行**，"
                "免得旧指令突然跑起来。还要做的点〔重发〕：\n\n" + "\n".join(lines[-30:]))
        self.send(crcore.alert(f"📭 关机期间你发的 {len(found)} 条消息没执行", body, "orange", buttons), side)

    def resend(self, mid):
        """〔重发〕：把关机期间那条消息当成刚发的处理。"""
        store = crcore.load_json(STALE, {})
        m = store.pop(mid, None)
        crcore.save_json(STALE, store)
        if not m:
            return "这条已经重发过了。"
        self.receive(mid + ":resend", m["chat_type"], m["chat_id"], m["open_id"], m["mtype"], m["raw"], m["quoted"])
        return None

    def on_card(self, data):
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        ev = data.event
        if self.authorized(ev.operator.open_id):
            cmd = (ev.action.value or {}).get("cmd", "") or ev.action.option or ""   # 按钮带 value；「···」菜单里点的是 option
            if cmd:
                chat = getattr(ev.context, "open_chat_id", None) if ev.context else None
                where = DM if not self.group or (chat and chat != self.group) else GROUP   # 点的是单聊里的卡片还是群里的
                self.jobs.put(("cmd", cmd, ev.context.open_message_id if ev.context else None, where))
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "收到"}})

    def worker(self):
        while True:
            job = self.jobs.get()
            where = job[-1] if isinstance(job, tuple) else None   # 你在哪边发的 / 点的：不属于哪个员工的回复回到那边
            try:
                if isinstance(job, tuple):            # 你发了消息 / 点了按钮：你醒着，夜里也照常推
                    with lock:
                        remote.saw_you()
                if isinstance(job, tuple) and job[0] == "files":
                    _, mid, files, text, name, _ = job
                    got = [self.download(mid, kind, key, fname) for kind, key, fname in files]
                    bad = [err for path, err in got if not path]
                    if bad:
                        self.send(f"📎 图片 / 文件没收下来（飞书返回 {bad[0]}）。如果是没有权限，要在飞书开放平台给应用开"
                                  "「获取与上传图片或文件资源」(im:resource)，发布新版本后再试。", where)
                        continue
                    with lock:
                        reply = remote.handle_files([path for path, _ in got], text, name, dm=where == DM)
                elif isinstance(job, tuple) and job[0] == "typed":   # 你手打的：机器人不猜，交给助手 / 引用的那个员工
                    with lock:
                        reply = remote.handle(job[1], typed=True, to=job[2], dm=where == DM)
                elif isinstance(job, tuple) and job[0] == "note":
                    reply = job[1]
                elif isinstance(job, tuple) and job[1].startswith("#resend "):
                    reply = self.resend(job[1].split()[1])
                elif isinstance(job, tuple):
                    with lock:
                        reply = remote.handle(job[1])
                else:
                    reply = job
                if reply is None:                     # 比如〔⏹ 停止〕：卡片自己会变，不用再发消息
                    continue
                if getattr(reply, "summon", False) and where != DM:   # 你在群里发「看看」：看板挪到最下面，旧的撤回、置顶跟着换
                    self.put_slot(reply.slot, reply, move=True)
                    continue
                if isinstance(reply, crcore.Files):  # 〔📎 文件名〕：把图 / PDF 发到手机上
                    for path in reply.paths:
                        self.send_file(path, where)
                    continue
                clicked = job[2] if isinstance(job, tuple) and job[0] == "cmd" else None
                if clicked and getattr(reply, "in_place", False) and self.patch(clicked, reply):
                    mid = clicked                     # 比如点了问题的选项：这张卡片原地变成实时进度
                else:
                    mid = self.send(reply, self.route(reply, where))   # 在单聊里发「看看」：看板就发在单聊里
                if mid and getattr(reply, "pending_for", None):
                    with lock:
                        pend = crcore.load_json(PENDING, {})
                        old = pend.get(reply.pending_for)
                        pend[reply.pending_for] = mid
                        crcore.save_json(PENDING, pend)
                    self.after[mid] = 0
                    if old and old != mid:            # 上一句还没等到回复又发了一句：旧的进度卡片撤回，几句话、进度和回复都在新卡片里
                        self.retire(old, reply)
            except Exception as e:  # noqa: BLE001 —— 单条命令出错不影响守护进程
                log.exception("处理命令出错")
                try:
                    self.send(f"出错了：{e}", where)
                except Exception:  # noqa: BLE001 —— 连「出错了」都发不出去（比如断网）：也不能让这个线程死掉
                    log.exception("发送出错提示也失败了")

    def run(self):
        lark = self.lark
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self.on_message)
                   .register_p2_card_action_trigger(self.on_card).build())
        threading.Thread(target=self.worker, name="worker", daemon=True).start()
        threading.Thread(target=self.catch_up_loop, name="catchup", daemon=True).start()
        lark.ws.Client(self.app_id, self.app_secret, event_handler=handler,
                       log_level=lark.LogLevel.INFO).start()


def live_interval(age):
    return LIVE_SECS if age < 600 else 6           # 前 10 分钟每 3 秒刷新，像看着它写；之后 6 秒


def live_loop(feishu):
    """像 App 一样：“思考中”卡片每几秒刷新它想了什么、做到哪一步；做完原地换成回复。"""
    shown = {}                                       # 消息 id -> (上次刷新时间, 上次内容)
    started, moved = {}, {}                          # 消息 id -> 开始干活的时间；会话名 -> 上次挪到最底下的时间
    while True:
        time.sleep(LIVE_SECS)
        t0 = time.time()
        try:
            pend = crcore.load_json(PENDING, {})
            if not pend:
                continue
            with lock:
                t1 = time.time()
                updates = remote.live_updates(list(pend))
            t2 = time.time()
            for name, card, final in updates:
                mid = pend.get(name)
                started.setdefault(mid, remote.state["remote_pending"].get(name, time.time()))
                if not final:
                    last_t, last_body = shown.get(mid, (0, None))
                    since = remote.state["remote_pending"].get(name, time.time())
                    if feishu.after.get(mid, 0) >= BURY and time.time() - moved.get(name, 0) > 120:
                        new = feishu.send(card, feishu.where_for(name))     # 进度卡片被别的消息刷上去了：挪到最底下，旧的撤回（最多 2 分钟挪一次）
                        if new:
                            with lock:
                                p = crcore.load_json(PENDING, {})
                                if p.get(name) == mid:
                                    p[name] = new
                                    crcore.save_json(PENDING, p)
                            feishu.after.pop(mid, None)
                            feishu.after[new] = 0
                            started[new] = started.pop(mid)
                            feishu.recall(mid)
                            shown[new], moved[name] = (time.time(), None), time.time()
                        continue
                    body = json.dumps(card.card["elements"][:-1], ensure_ascii=False)   # 不比较底部时间
                    if time.time() - last_t < live_interval(time.time() - since) or (
                            body == last_body and time.time() - last_t < 2 * LIVE_SECS):   # 内容没变（在等模型）也隔几秒刷一次：标题的秒数、底栏时间在走，看得出没卡
                        continue
                    if feishu.patch(mid, card):
                        shown[mid] = (time.time(), body)
                    continue
                with lock:
                    p = crcore.load_json(PENDING, {})
                    if p.get(name) == mid:
                        p.pop(name)
                        crcore.save_json(PENDING, p)
                shown.pop(mid, None)
                took, below = time.time() - started.pop(mid, time.time()), feishu.after.pop(mid, 0)
                if card is None:
                    continue
                if took > 60 or below >= 1:          # 干了一阵子，或者下面已经有别的消息：回复发在最底下（会提醒你），旧进度卡片撤回
                    if feishu.send(card, feishu.where_for(name)):
                        feishu.recall(mid)
                        continue
                if not feishu.patch(mid, card):       # 很快就答完、下面也没别的消息：原地换成回复
                    feishu.send(card, feishu.where_for(name))
        except Exception:  # noqa: BLE001
            log.exception("刷新思考中卡片出错")
        else:
            if time.time() - t0 > 10:                 # 卡片该几秒刷一次：慢了记下来是卡在哪（等锁 / 读状态 / 调飞书）
                log.warning("刷新思考中卡片慢了：等锁 %.1f 秒，读状态 %.1f 秒，调飞书 %.1f 秒",
                            t1 - t0, t2 - t1, time.time() - t2)


UPGRADE_SECS = 300   # 多久看一次有没有员工该换新版本


def upgrade_loop():
    """客户端自己更新过：闲着的员工换上新版本（见 crcore.Remote.upgrade_tick）。重开一个要半分钟，不占着锁，
    跟命令行「cr 下班 / 上班」一样用自己的 Remote，不耽误飞书消息和别的通知。"""
    while True:
        time.sleep(UPGRADE_SECS)
        try:
            crcore.Remote().upgrade_tick()
        except Exception:
            log.exception("换新版本出错")


def monitor_loop(feishu):
    pos = os.path.getsize(crcore.OUTBOX) if os.path.exists(crcore.OUTBOX) else 0
    while True:
        try:
            with lock:
                remote.monitor_tick()
            if os.path.exists(crcore.OUTBOX):
                with open(crcore.OUTBOX, encoding="utf-8") as f:
                    f.seek(pos)
                    new = f.readlines()
                    pos = f.tell()
                for line in new:
                    item = json.loads(line)
                    log.info("通知 [%s]", item["kind"])
                    if not feishu:
                        continue
                    msg = crcore.as_card(item) if item.get("card") else item["text"]
                    if item.get("pending_for"):          # 你在电脑上叫员工干活：这张实时卡片跟飞书发起的一样登记、刷新
                        mid = feishu.send(msg, feishu.where_for(item["pending_for"]))
                        if mid:
                            with lock:
                                pend = crcore.load_json(PENDING, {})
                                old = pend.get(item["pending_for"])
                                pend[item["pending_for"]] = mid
                                crcore.save_json(PENDING, pend)
                            feishu.after[mid] = 0
                            if old and old != mid:
                                feishu.retire(old, msg)
                        continue
                    if item.get("merge"):
                        feishu.merge(msg)
                        continue
                    if item.get("slot"):
                        feishu.put_slot(item["slot"], msg, item.get("minor", False))
                        continue
                    pend = crcore.load_json(PENDING, {})
                    mid = pend.pop(item.get("for"), None) if item.get("for") else None
                    if mid:
                        crcore.save_json(PENDING, pend)
                    if not (mid and feishu.patch(mid, msg)):
                        feishu.send(msg, feishu.where_for(item["for"]) if item.get("for") else None)
        except Exception:  # noqa: BLE001
            log.exception("监控出错")
        names = {t.name for t in threading.enumerate() if t.is_alive()}
        if not feishu or {"worker", "live", "catchup"} <= names:   # 有一个线程死了就不写心跳，让看门狗整个重启
            try:
                with open(HEARTBEAT + ".tmp", "w") as f:    # 先写临时文件再换上：看门狗不会读到写了一半的空文件
                    f.write(f"{time.time():.0f}\n")
                os.replace(HEARTBEAT + ".tmp", HEARTBEAT)
            except OSError:
                pass
        time.sleep(TICK_SECS)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # 飞书是国内服务，直连（长连接本来就是直连的）；走 127.0.0.1:10000 代理经常超时，回复会丢。代理只留给 claude 用。
    for k in ("NO_PROXY", "no_proxy"):
        os.environ[k] = ",".join(x for x in (os.environ.get(k), ".feishu.cn", ".feishucdn.com", ".larksuite.com") if x)
    crcore.VIA.update(via="feishu", by="")          # 守护进程替你打的字都是飞书来的（别的进程默认算 cli）
    env = {**read_env(), **{k: v for k, v in os.environ.items() if k.startswith("FEISHU_")}}   # cr 已经把 config.env 读进环境变量了
    app_id, secret, allow = env.get("FEISHU_APP_ID"), env.get("FEISHU_APP_SECRET"), env.get("FEISHU_ALLOW_OPEN_ID")
    feishu = Feishu(app_id, secret, allow, env.get("FEISHU_GROUP_NAME") or GROUP_NAME) if app_id and secret else None
    if not feishu:
        log.warning("飞书未配置（feishu.env），只运行监控，通知写在 data/outbox.jsonl")
    elif not allow:
        log.warning("还没设 FEISHU_ALLOW_OPEN_ID：谁的消息都不处理。给机器人发一条消息后运行「cr 授权」")
    were_on = [n for n, e in crcore.load_json(crcore.REGISTRY, {}).items() if e.get("alive")]   # 上次还在上班的
    lost = remote.lost_card(were_on)                     # 开机后它们的进程没了：电脑关机 / 重启过
    threading.Thread(target=monitor_loop, args=(feishu,), daemon=True).start()
    threading.Thread(target=upgrade_loop, name="upgrade", daemon=True).start()
    note = crcore.load_json(WATCHDOG_NOTE, None)
    if note:
        os.remove(WATCHDOG_NOTE)
        since = note.get("down_since") or note["t"]
        down = time.strftime("%m-%d %H:%M" if time.time() - since > 20 * 3600 else "%H:%M", time.localtime(since))
        mins = max(1, round((time.time() - since) / 60))
        long = f"{mins // 60} 小时 {mins % 60} 分钟" if mins >= 60 else f"{mins} 分钟"
        log.warning("机器人从 %s 起停了约 %s（%s）", down, long, "开机" if note.get("boot") else "看门狗重启")
        if feishu:
            head = (f"💻 电脑开机了：监控机器人从 {down} 起停了大约 {long}（关机、休眠或断网）。" if note.get("boot") else
                    f"⚠️ 监控机器人从 {down} 起停了大约 {long}，看门狗已经自动把它重启了。")
            feishu.jobs.put(head + "半小时内你发的消息会自动补收" +
                            ("；更早的列在下一张卡片里，要不要重发你来定。" if mins > CATCHUP_WINDOW / 60 else "。"))
            if mins > CATCHUP_WINDOW / 60:
                feishu.offline_since = since
    if lost:
        feishu.jobs.put(lost) if feishu else crcore.notify(lost)
    if feishu:
        threading.Thread(target=live_loop, args=(feishu,), name="live", daemon=True).start()
        feishu.run()
    else:
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
