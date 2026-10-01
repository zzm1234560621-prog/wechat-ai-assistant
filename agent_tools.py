"""给大模型用的工具层：让助手能真的「动手」——查联系人、翻历史、给别人发消息。

设计原则：
  * 发消息是**不可逆**的动作，默认不能乱发。config.yaml 的
    `agent.auto_send_whitelist` 列了谁，就往谁那儿直接发；名单外的，
    工具只登记一个「待确认」动作，由 bot 让用户回「确认」才真正发出。
  * hook **不支持并发查询**，查多了会把微信拖崩。所以这里用一个
    ToolBox 实例统计本轮用掉的查询数，超预算就拒绝继续查。

配置（config.yaml）：
  agent:
    enabled: true
    max_rounds: 3              # 一次对话最多几轮工具调用
    max_queries: 6             # 一次对话最多发几次查库请求
    auto_send_whitelist: []    # 允许直接发送的对象（昵称/备注/微信号/wxid）
    confirm_ttl: 300           # 「确认」的有效期（秒）
    line_chars: 400            # 历史行截断长度
"""
import os
import time

import auto_reply
import file_read
import image_cache
import live_history
import scheduler
import watch

# 允许发送的图片后缀
_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def looks_like_id(name):
    """是不是一个能直接用的原始会话 id：wxid / 群 roomid / 文件传输助手。

    群**只能**靠 roomid 定位（昵称匹配不到群），所以这类输入不去查联系人表，
    直接原样透传——顺带省掉一次查库。
    """
    n = str(name or "")
    return bool(n) and (n.endswith("@chatroom") or n == "filehelper"
                        or n.lower().startswith("wxid_"))


def speaker_of(m, names, chat_name="", is_group=False):
    """一条消息的发言人显示名。

    群里优先用 sender_name——那是微信自己算好的显示名（群里就是**群昵称**），
    比拿 wxid 去联系人表里查更准。没有才退回联系人表 / 会话名。
    """
    if m.get("is_self"):
        return "我"
    named = str(m.get("sender_name") or "").strip()
    if named:
        return named
    sender = str(m.get("sender") or "")
    if sender:
        return (names or {}).get(sender) or sender
    if is_group:
        return "群成员"
    return chat_name or "对方"


def format_history_lines(msgs, names, chat_name="", is_group=False, limit=20,
                         line_chars=400):
    """把聊天记录渲染成「[时间] 说话人: 内容」。

    以前这里不看 sender，群聊里所有非自己的消息都标成同一个群名，
    模型根本不知道是谁在说话。做成纯函数是为了能在自测里用合成数据直接验。
    """
    out = []
    for m in (msgs or [])[-int(limit):]:
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        who = speaker_of(m, names, chat_name, is_group)
        text = content[:line_chars] if line_chars else content
        out.append(f"[{m.get('time', '?')}] {who}: {text}")
    return out


# 查库失败的统一说法：告诉模型**该怎么办**，否则它会在原地反复重试，
# 而每次重试都是一次真实的 hook 调用（这会把微信拖垮）。
def _db_fail(what, err):
    return (f"{what}失败：{err}。这多半是 hook 查库出问题了——"
            f"请如实告诉用户暂时查不到，**不要反复重试**。")

# 中立格式的工具描述（llm.py 会转成各家协议要的样子）
TOOLS = [
    {
        "name": "find_contact",
        "description": "按昵称/备注/微信号查联系人，返回 wxid。发消息前不确定对方 wxid 时先用它。",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "昵称、备注或微信号"}},
            "required": ["name"],
        },
    },
    {
        "name": "send_text",
        "description": ("给某个微信联系人发送文本消息，默认只发一次。"
                        "count 用来连发同一条内容多次——只在用户明确说「发 N 次」时才填。"
                        "用户让你给谁发消息时**必须调用本工具**，不要只口头回复"
                        "『我准备发』——要不要用户确认由本工具判断并返回。"
                        "返回里说『尚未发送』时，就是把内容复述给用户、请他回「确认」。"),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人的昵称/备注/微信号/wxid"},
                "text": {"type": "string", "description": "要发送的内容"},
                "count": {"type": "integer",
                          "description": "连发几次，默认 1。只在用户明确要求发多次时填。"},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "read_history",
        "description": "读某个联系人或群最近的聊天记录。",
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "联系人的昵称/备注/wxid"},
                "limit": {"type": "integer", "description": "最多返回几条，默认 20"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "search_history",
        "description": "在所有聊天记录里按关键词搜索。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "description": "默认 10"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "auto_reply",
        "description": (
            "管理「自动回复」——让 AI 代替用户本人回某个聊天的消息。"
            "用户用大白话说这类要求时**必须调用本工具**，不要只口头答应：\n"
            "  「以后张三的消息你帮我回」→ action=add, who=张三（然后 action=on）\n"
            "  「群里的消息也帮我回」→ action=add, who=群的 roomid\n"
            "  「别自动回李四了」「李四的我自己回」→ action=del, who=李四\n"
            "  「发之前先给我看一眼」→ action=review, review=true\n"
            "  「直接发就行不用问我」→ action=review, review=false\n"
            "  「关掉自动回复」→ action=off\n"
            "  「自动回复都配了谁」→ action=status\n"
            "mode：self = 假装用户本人（默认，除非用户说要表明是 AI）；"
            "assistant = 说明自己是助手。"
            "调用后把工具返回的内容**如实复述**给用户，别自己另编一套说法。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["on", "off", "add", "del", "mode", "review",
                                    "ctx", "status"]},
                "who": {"type": "string",
                        "description": "昵称/备注/微信号/wxid；群填 roomid（xxx@chatroom）"},
                "mode": {"type": "string", "enum": ["self", "assistant"]},
                "review": {"type": "boolean",
                           "description": "true=发之前先让用户确认；false=直接发给对方"},
                "context_messages": {"type": "integer", "description": "上下文条数 1~30"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "schedule",
        "description": (
            "管理「定时任务」——到点自动给某人发消息（打电话还没做出来）。"
            "用户提这类要求时**必须调用本工具**，不要只口头答应：\n"
            "  「明天9点提醒我给张三发个消息说带伞」→ action=add, when=明天9:00,"
            " who=张三, text=记得带伞\n"
            "  「每天早上8点给李四发个早安」→ action=add, when=8:00, who=李四, text=早安\n"
            "  「每周一9点给王五发周报提醒」→ action=add, when=每周一 9:00, ...\n"
            "  「每隔半小时给他发一次」→ action=add, when=每30分钟, ...\n"
            "  「每天早8点给我整理一下谁还没回我」→ action=add, mode=ask,"
            " when=每天8:00, text=整理一下谁还没回我、昨天有什么漏的"
            "（mode=ask 是到点让**你**回答这段话，答案发回控制会话，不用填 who）\n"
            "  「把第2个定时删了」→ action=del, target=t2\n"
            "  「定时都先停掉」→ action=off, target=all\n"
            "  「我有哪些定时」→ action=status\n"
            "when 是**时间写法**，照用户原话写：9:00=每天、明天9:00=只一次、"
            "每周一 9:00、每30分钟。别把它换算成别的时间。"
            "打电话（mode=call）目前发不出去，用户要这个时也要照实建、"
            "并告诉他到点只会报错。"
            "调用后把工具返回的内容**如实复述**给用户，别自己另编一套说法。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["add", "del", "on", "off", "status"]},
                "when": {"type": "string",
                         "description": "时间写法：9:00 / 明天9:00 / 每周一 9:00 / 每30分钟"},
                "who": {"type": "string",
                        "description": "昵称/备注/微信号/wxid"},
                "text": {"type": "string", "description": "要发的内容（action=add 且不是通话时必填）"},
                "mode": {"type": "string", "enum": ["text", "call", "ask"],
                         "description": "默认 text=发固定内容；ask=到点让你回答 text 里那段话，"
                                        "答案回控制会话（做每日摘要用）；call=打电话（还没打通）"},
                "target": {"type": "string",
                           "description": "del/on/off 时的任务编号，如 t2；on/off 可用 all"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "watch",
        "description": (
            "管理「盯着」名单——名单里的人一给用户发消息，就**通知用户本人**，"
            "但**一个字都不回复对方**。用户用大白话说这类要求时必须调用本工具：\n"
            "  「张三发消息告诉我一声」「帮我盯着张三」→ action=add, who=张三\n"
            "  「别盯着张三了」→ action=del, who=张三\n"
            "  「盯着谁了」→ action=status\n"
            "  「先别通知了」→ action=off\n"
            "**注意和 auto_reply 的区别**：auto_reply 是「代用户回对方」，"
            "watch 是「只告诉用户他说了啥」。用户说「帮我回」用 auto_reply，"
            "说「告诉我」「盯着」才用这个。两者对同一会话互斥。"
            "调用后把工具返回的内容如实复述给用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["add", "del", "on", "off", "status"]},
                "who": {"type": "string",
                        "description": "昵称/备注/微信号/wxid；群填 roomid"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "find_images",
        "description": (
            "列出某个聊天里最近的**图片**消息，并标明哪几张能真正看到。"
            "用户问「他发的那张图是什么」「最近发图了吗」时用这个。\n"
            "返回里标了『可看』的才是能解读的——微信只把你**滚动看过**的图片"
            "解码缓存在本地，其余的解不开（聊天里的图片文件是加密的）。"
            "拿到后可再用 read_image 读某一张。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "昵称/备注/微信号/wxid"},
                "limit": {"type": "integer", "description": "最多列几张，默认 10"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "read_image",
        "description": (
            "读某张图片的内容（识别图里的文字）。"
            "contact + local_id 从 find_images 的结果里拿。\n"
            "只能读**有本地缓存**的图；没缓存的会明确告诉你读不了，"
            "这时候要如实告诉用户「这张图没缓存，我这边看不到」，不要编内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "昵称/备注/微信号/wxid"},
                "local_id": {"type": "string", "description": "find_images 返回的 local_id"},
            },
            "required": ["contact", "local_id"],
        },
    },
    {
        "name": "find_files",
        "description": (
            "列出某个聊天里**收到的文件**（PDF / Word / Excel / PPT / 文本），"
            "并标明哪些在本地、能不能读。用户问「他发的那份文件」「上次那份资料」"
            "「那个 pdf 里写了什么」时用这个。\n"
            "只有**收过的**文件才在本地；发出去的不在。返回里标了「本地有」的才能读。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "昵称/备注/微信号/wxid/roomid"},
                "limit": {"type": "integer", "description": "默认 10"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "读一份收到的文件的**内容**（把它转成文字）。"
            "contact + local_id 从 find_files 的结果里拿。\n"
            "支持 pdf / docx / xlsx / pptx / txt / csv 等。"
            "**扫描件 PDF（整页是图片）抽不出文字**，这时候要如实告诉用户"
            "「这份读不出文字」，不要编内容。文件很长时只会给前面一部分。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "昵称/备注/微信号/wxid"},
                "local_id": {"type": "string", "description": "find_files 返回的 local_id"},
            },
            "required": ["contact", "local_id"],
        },
    },
    {
        "name": "recent_messages",
        "description": (
            "最近有哪些会话来了消息，以及每个会话的最后一条说了什么。"
            "回答「最近聊了什么」「谁找我了」「有什么新消息」时用这个——"
            "它一次就能看全，比逐个联系人去翻历史快得多。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多列几个会话，默认 20"},
            },
        },
    },
    {
        "name": "search_in_chat",
        "description": (
            "在**某一个**联系人或群的聊天记录里按关键词搜。"
            "比 search_history（全局模糊搜）精确——用户说「我在和张三的聊天里"
            "说过什么关于合同的」时用这个。要用 search_history 还是它，"
            "看用户是限定了一个人还是一句话没提人。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "昵称/备注/微信号/wxid；群填 roomid"},
                "keyword": {"type": "string"},
                "limit": {"type": "integer", "description": "默认 10"},
            },
            "required": ["contact", "keyword"],
        },
    },
    {
        "name": "pending_replies",
        "description": (
            "列出**还有未读消息**的会话（微信自己统计的未读数，不是猜的），"
            "回答「谁找我了」「谁在等我回」「有什么没回的消息」时用这个。\n"
            "注意：未读数要在微信里**点开那个会话**才会清零，所以同一批人会"
            "一直出现——那是微信的语义，别当成新消息反复报给用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多列几个会话，默认 20"},
            },
        },
    },
    {
        "name": "group_members",
        "description": (
            "列出一个**群**的成员（含群昵称，群主已标出）。"
            "用户问「这个群里有谁」「群里那个 XXX 是什么人」时用。\n"
            "群必须用 roomid（形如 xxx@chatroom）指定——昵称匹配不到群。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string", "description": "群的 roomid（xxx@chatroom）"},
                "limit": {"type": "integer", "description": "最多列几个人，默认 80"},
            },
            "required": ["contact"],
        },
    },
    {
        "name": "send_image",
        "description": (
            "给某个微信联系人**发一张本地图片**。\n"
            "path 必须是**允许目录**下的图片文件——默认只放行微信自己的图片缓存"
            "目录，也就是「聊天里已经出现过的图」。发别处的文件会被拒绝，"
            "这时要让用户自己把图放进允许目录、或先用 find_images 找一张已有的图。\n"
            "和 send_text 一样：名单外的人**不会直接发**，工具会登记待确认，"
            "你负责把内容复述给用户请他回「确认」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人的昵称/备注/微信号/wxid"},
                "path": {"type": "string", "description": "本地图片路径（相对或绝对都行）"},
            },
            "required": ["to", "path"],
        },
    },
    {
        "name": "send_images",
        "description": (
            "把**一个目录里的图片批量发给某人**。用户说「把 XX 文件夹里的照片发给他」"
            "「把这个文件夹的图都发过去」时用这个。\n"
            "只发目录**第一层**的图片（不递归子目录），按修改时间从早到晚发——"
            "照片天然就是拍摄顺序。单次最多 agent.max_send_count 张，"
            "目录里更多的话会截断，你**要把截断情况告诉用户**。\n"
            "目录必须在 agent.send_image_dirs 允许的范围内，否则会被拒绝——"
            "这时让用户自己去 config 加目录，**不要绕过**。\n"
            "和 send_image 一样，名单外的人不会直接发，会登记待确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人的昵称/备注/微信号/wxid"},
                "dir": {"type": "string", "description": "本地目录路径（只扫这一层）"},
                "limit": {"type": "integer", "description": "最多发几张，默认按配置上限"},
            },
            "required": ["to", "dir"],
        },
    },
    {
        "name": "forward_message",
        "description": (
            "把**某一条已有的消息转发**给别人（含图片、链接、文件、名片这些非文本消息）。\n"
            "contact + local_id 从 find_images / read_history 的结果里拿。\n"
            "和 send_text 一样受确认机制约束：名单外的收件人要先请用户回「确认」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人的昵称/备注/微信号/wxid"},
                "contact": {"type": "string", "description": "这条消息所在的会话（昵称/备注/wxid/roomid）"},
                "local_id": {"type": "string", "description": "消息的 local_id"},
            },
            "required": ["to", "contact", "local_id"],
        },
    },
]

_AUTO_ACTIONS = ("on", "off", "add", "del", "mode", "review", "ctx", "status")
_SCHED_ACTIONS = ("add", "del", "on", "off", "status")
_WATCH_ACTIONS = ("add", "del", "on", "off", "status")


class _Budget:
    def __init__(self, limit):
        self.left = limit

    def take(self):
        if self.left <= 0:
            return False
        self.left -= 1
        return True


# 待确认的发送动作：{聊天: [{"to_wxid","to_name","text","image","xml","ts"}, ...]}
#
# 用**列表**而不是单个：同一个控制会话里可能同时压着好几条待确认
# （比如你让助手发给 A、同时审核模式下又有一条自动回复给 B），
# 只存一条会互相覆盖，回「确认」时发错人。
_PENDING = {}


def _alive(chat, ttl):
    """该会话里未过期的待确认项，最早的在前。"""
    items = _PENDING.get(str(chat)) or []
    now = time.time()
    items = [i for i in items if now - i["ts"] <= ttl]
    if items:
        _PENDING[str(chat)] = items
    else:
        _PENDING.pop(str(chat), None)
    return items


def set_pending(chat, to_wxid, to_name, text, kind="agent", count=1,
                image=None, xml=None):
    """登记一条待确认发送。kind 区分来源：agent（用户让助手发的）/ auto（自动回复草稿）。

    bot 对两者要求不一样：自动回复草稿只认明确的中文确认词，避免用户在控制
    会话里随口一句「ok」就把草稿发给别人。

    count 是用户回「确认」后连发的次数；连发节奏由 agent.max_send_count /
    agent.send_interval 兜着，别指望调用方自觉。

    image / xml 用来表示「这条待确认要发的不是文本」：image 是本地图片路径，
    xml 是要转发的原始消息 XML。bot 的确认分支据此选发法（都只发一次，
    count/连发只对文本有意义）。
    """
    _PENDING.setdefault(str(chat), []).append(
        {"to_wxid": to_wxid, "to_name": to_name, "text": text,
         "image": image, "xml": xml,
         "kind": kind, "count": int(count or 1), "ts": time.time()})


def pop_pending(chat, ttl=300):
    """取出并清除最早的一条待确认动作；没有或全过期返回 None。"""
    items = _alive(chat, ttl)
    if not items:
        return None
    item = items.pop(0)
    if items:
        _PENDING[str(chat)] = items
    else:
        _PENDING.pop(str(chat), None)
    return item


def peek_pending(chat, ttl=300):
    items = _alive(chat, ttl)
    return items[0] if items else None


def discard_pending(chat):
    """丢掉该会话所有待确认动作（用户回「不发」）。返回丢掉几条。"""
    items = _PENDING.pop(str(chat), None) or []
    return len(items)


def send_repeated(client, wxid, text, count=1, interval=0.0):
    """连发同一条消息 count 次，每次之间等 interval 秒。返回 (真正发出的条数, 错误)。

    **同步、顺序地发，故意不开线程**：hook 不支持并发调用（并发能直接把微信搞崩，
    是踩过的坑）。宁可让主循环多等几秒——轮询期间消息在库里排着，回来照收。

    中途失败立刻停手，把已经发出去几条如实返回，不假装全成功。
    """
    count = max(1, int(count or 1))
    sent = 0
    for i in range(count):
        if i and interval > 0:
            time.sleep(interval)
        try:
            client.send_text(text, wxid)
        except Exception as e:
            return sent, e
        sent += 1
    return sent, None


def send_pending(client, item, interval=0.0):
    """执行一条待确认动作，返回 (真正发出的条数, 错误)。**同步、串行。**

    文本可以连发；转发只发一次。图片可以是一个路径或**一串路径**（群发照片），
    多个之间按 interval 停顿——连发期间轮询会暂停，这是有意为之（hook 不支持并发）。
    """
    wxid = item.get("to_wxid")
    if item.get("image"):
        imgs = item["image"]
        if isinstance(imgs, str):
            imgs = [imgs]
        sent = 0
        for i, p in enumerate(imgs):
            if i and interval:
                time.sleep(interval)
            try:
                client.send_image(p, wxid)
            except Exception as e:
                return sent, e
            sent += 1
        return sent, None
    if item.get("xml"):
        try:
            client.send_xml(item["xml"], wxid)
        except Exception as e:
            return 0, e
        return 1, None
    return send_repeated(client, wxid, item.get("text") or "",
                         item.get("count") or 1, interval)


def resolve_contacts(contacts, name, self_wxid="", client=None, budget=None):
    """昵称/备注/微信号 -> 候选列表。先精确匹配，没有再退到包含匹配。

    抽成模块级是为了让 bot 的 /定时 命令也能用**同一套**解析：重名处理必须一致，
    不能一边要求用户说清楚、另一边静默取第一个（那会发错人）。
    """
    name = str(name or "").strip()
    if not name:
        return []

    # 原始 id 直接透传：群没法用昵称定位（只能给 roomid），而且这样不查库。
    if looks_like_id(name):
        for c in contacts or []:
            if str(c.get("wxid") or "") == name:
                return [c]
        return [{"wxid": name, "name": name}]

    # 精确相等必须单独一遍——否则「李同学」会把「李同学2」也带出来。
    exact = []
    for c in contacts or []:
        for key in (c.get("remark"), c.get("name"), c.get("alias"), c.get("wxid")):
            if key and str(key) == name:
                exact.append(c)
                break
    if exact:
        return exact

    loose = []
    for c in contacts or []:
        for key in (c.get("remark"), c.get("name"), c.get("alias")):
            if key and name in str(key):
                loose.append(c)
                break
    if loose:
        return loose

    if self_wxid and name == self_wxid:
        return [{"wxid": self_wxid, "name": "我自己"}]
    # 退路：交给 live_history 去库里模糊找（会花一次查询预算）
    if client is not None and (budget is None or budget.take()):
        try:
            return live_history.resolve_contact(client, name, limit=5)
        except Exception:
            return []
    return []


def resolve_one(contacts, name, self_wxid="", client=None, budget=None):
    """把「昵称/备注/wxid」解析成唯一候选人。返回 (候选人, 错误文本)。

    重名时**不静默取第一个**——那会读错人、甚至发错人。
    """
    cands = resolve_contacts(contacts, name, self_wxid, client, budget)
    if not cands:
        return None, f"没找到「{name}」。"
    if len(cands) > 1:
        names = "；".join(
            f"{c.get('remark') or c.get('name')}({c.get('wxid')})" for c in cands[:5])
        return None, f"「{name}」匹配到多个人：{names}。请用全名或直接给 wxid。"
    return cands[0], None


class ToolBox:
    """一次对话里执行工具调用的上下文。"""

    def __init__(self, client, cfg, contacts, self_wxid="", chat="", cfg_provider=None):
        self.client = client
        self.cfg = cfg or {}
        self.contacts = contacts or []
        self.self_wxid = str(self_wxid or "")
        self.chat = str(chat or "")
        # 取「当前最新配置」的方式。cfg 是构造时的快照，一轮里连着改两次
        # 第二次就会基于旧快照读-改-写，把第一次的改动丢掉。
        self.cfg_provider = cfg_provider or (lambda: self.cfg)
        # 有工具改动了配置（auto_reply / 定时任务），主循环要据此重建自己的状态
        self.cfg_changed = False
        agent_cfg = self.cfg.get("agent") or {}
        self.whitelist = [str(x).strip() for x in (agent_cfg.get("auto_send_whitelist") or []) if str(x).strip()]
        self.confirm_ttl = int(agent_cfg.get("confirm_ttl", 300))
        self.budget = _Budget(int(agent_cfg.get("max_queries", 6)))
        # 连发的两条闸：单次请求的条数上限，以及每条之间的间隔。
        # 都是防「一口气刷屏把 hook 打崩」，不是给模型参考的建议值。
        self.max_send_count = max(1, int(agent_cfg.get("max_send_count", 20)))
        self.send_interval = max(0.0, float(agent_cfg.get("send_interval", 1.5)))
        # 允许发图的目录。**这是安全边界，不是便利设置**：模型自己填 path，
        # 不设边界就等于让它从你硬盘上挑任意文件发出去。留空 = 只放行微信
        # 自己的图片缓存目录（也就是「聊天里已有的图」）。
        self.send_image_dirs = [str(d).strip()
                                for d in (agent_cfg.get("send_image_dirs") or [])
                                if str(d).strip()]
        # 历史行截断长度。以前写死 200，长消息被截得看不懂，模型答非所问。
        self.line_chars = max(80, int(agent_cfg.get("line_chars", 400)))
        # wxid -> 显示名，群里标发言人用（构造时算一次，别每条消息重算）
        self._names = auto_reply.contact_names(self.contacts)
        self.sent = []          # 本轮真正发出去的 [(name, text)]
        self._img_cache = {}    # wxid -> 图片列表（本轮复用，见 _images）
        self._file_cache = {}   # wxid -> 文件列表（同上，见 _files）

    def _image_path_ok(self, path):
        """校验发图路径。返回 (绝对路径, 错误文本)。

        **这是安全边界**：路径是模型填的，不校验就等于让它从你硬盘上挑任意
        文件发出去。默认只放行微信自己的图片缓存目录（「聊天里已有的图」）。
        """
        raw = str(path or "").strip()
        if not raw:
            return "", "没给图片路径。"
        p = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isfile(p):
            return "", f"找不到这个文件：{p}"
        ext = os.path.splitext(p)[1].lower()
        if ext not in _IMG_EXT:
            return "", (f"「{os.path.basename(p)}」不是图片"
                        f"（只支持 {'/'.join(sorted(_IMG_EXT))}）。")
        p, derr = self._in_allowed_dirs(p)
        if derr:
            return "", derr
        return p, None

    def _allowed_dirs(self):
        """允许发送的根目录。

        **默认只放行微信自己的图片缓存目录**（也就是「聊天里已有的图」）。
        要发别处的文件，必须由**用户**去 config.yaml 的 `agent.send_image_dirs`
        加目录——助手不许自己改配置绕过这条。
        """
        dirs = [os.path.abspath(os.path.expanduser(str(d)))
                for d in self.send_image_dirs if str(d).strip()]
        if not dirs:
            root = image_cache.data_root()
            if root:
                dirs = [os.path.abspath(root)]
        return dirs

    def _in_allowed_dirs(self, p):
        """绝对路径 p 在不在允许目录里。返回 (p, 错误文本)。"""
        dirs = self._allowed_dirs()
        if not dirs:
            return "", ("没配可发文件的目录（agent.send_image_dirs），"
                        "也找不到微信图片缓存目录，所以不让发。")
        target = os.path.normcase(p)
        for d in dirs:
            dd = os.path.normcase(d)
            try:
                if os.path.commonpath([target, dd]) == dd:
                    return p, None
            except ValueError:
                continue        # 不同盘符时 commonpath 会抛，跳过
        return "", (f"这个位置不在允许发送的目录里。允许：{'；'.join(dirs)}。\n"
                    f"（要发别处的，得**用户自己**去 config.yaml 的 "
                    f"agent.send_image_dirs 加目录——你不要改配置绕过。）")

    # 一次取多少张图给缓存用
    _IMG_FETCH = 50
    _FILE_FETCH = 30

    def _images(self, wxid, limit=None):
        """本会话的图片列表，**一轮内只查一次**。

        以前 read_image 每读一张都重新查一遍列表（一次查库），模型一口气读
        8 张就把 max_queries 用光，后面全返回"次数已用完"——实测踩过。
        列表本身一轮内不会变，缓存住就行。
        """
        if wxid not in self._img_cache:
            self._img_cache[wxid] = live_history.v4_images(
                self.client, wxid, limit=self._IMG_FETCH)
        rows = self._img_cache[wxid]
        return rows[-limit:] if limit else rows

    def _files(self, wxid, limit=None):
        """本会话收到的文件列表，**一轮内只查一次**（同 _images 的道理）。"""
        if wxid not in self._file_cache:
            self._file_cache[wxid] = live_history.v4_files(
                self.client, wxid, limit=self._FILE_FETCH)
        rows = self._file_cache[wxid]
        return rows[-limit:] if limit else rows

    # ---------- 工具实现 ----------

    def _resolve(self, name):
        """昵称/备注/微信号 -> 候选列表。实现见模块级 resolve_contacts。"""
        return resolve_contacts(self.contacts, name, self.self_wxid,
                                self.client, self.budget)

    def _in_whitelist(self, wxid, name):
        for w in self.whitelist:
            if w in (str(wxid), str(name)):
                return True
        return False

    def _one(self, who):
        """把「昵称/备注/wxid」解析成唯一候选人。返回 (候选人, 错误文本)。

        重名时**不能静默取第一个**——那会读错人、甚至发错人。让模型回去问用户。
        """
        cands = self._resolve(who)
        if not cands:
            return None, f"没找到「{who}」。"
        if len(cands) > 1:
            names = "；".join(
                f"{c.get('remark') or c.get('name')}({c.get('wxid')})"
                for c in cands[:5])
            return None, (f"「{who}」匹配到多个人：{names}。"
                          f"请问用户要哪一个，拿到明确的名字后再试。")
        return cands[0], None

    def t_find_contact(self, args):
        cands = self._resolve(args.get("name"))
        if not cands:
            return f"没找到叫「{args.get('name')}」的联系人。"
        lines = []
        for c in cands[:5]:
            nm = c.get("remark") or c.get("name") or ""
            lines.append(f"{nm} (wxid={c.get('wxid')})")
        return "找到：" + "；".join(lines)

    def t_send_text(self, args):
        to = str(args.get("to") or "").strip()
        text = str(args.get("text") or "")
        if not to or not text:
            return "参数不全：需要 to 和 text。"
        try:
            count = int(args.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        # 闸在工具里，不信任模型填的数
        count = max(1, min(count, self.max_send_count))
        cands = self._resolve(to)
        if not cands:
            return f"没找到收件人「{to}」，消息没有发送。"
        if len(cands) > 1:
            names = "；".join(f"{c.get('remark') or c.get('name')}({c.get('wxid')})" for c in cands[:5])
            return f"「{to}」匹配到多个人，请指定更精确的名字：{names}"

        wxid = str(cands[0].get("wxid"))
        nm = cands[0].get("remark") or cands[0].get("name") or wxid

        if self._in_whitelist(wxid, nm) or self._in_whitelist(wxid, to):
            n, err = send_repeated(self.client, wxid, text, count, self.send_interval)
            self.sent.append((nm, text if count == 1 else f"{text} ×{n}"))
            if err is not None:
                return f"发给 {nm} 时失败（已发出 {n}/{count} 条）：{err}"
            if count == 1:
                return f"已发送给 {nm}。"
            return f"已给 {nm} 连发 {n} 条「{text}」。"

        # 名单外：只登记待确认，不真发
        set_pending(self.chat, wxid, nm, text, count=count)
        times = f"连发 {count} 次" if count > 1 else "发一条"
        return (f"「{nm}」不在自动发送名单里，消息**尚未发送**。"
                f"请告诉用户：准备{times}给 {nm}，内容是「{text}」，"
                f"让用户回复「确认」后再发。")

    def t_read_history(self, args):
        contact = str(args.get("contact") or "").strip()
        limit = int(args.get("limit") or 20)
        limit = max(1, min(limit, 50))
        cand, err = self._one(contact)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or contact
        if not self.budget.take():
            return "本轮查库次数已用完，请基于已有信息回答。"
        try:
            msgs = live_history.query_contact_history(self.client, wxid, limit=limit)
        except Exception as e:
            return _db_fail(f"读「{nm}」的历史", e)
        lines = format_history_lines(msgs, self._names, nm,
                                     auto_reply.is_group(wxid), limit,
                                     self.line_chars)
        if not lines:
            return f"和「{nm}」没有查到聊天记录。"
        return (f"「{nm}」最近的聊天记录（{len(lines)} 条，时间从早到晚）：\n"
                + "\n".join(lines))

    def t_search_history(self, args):
        kw = str(args.get("keyword") or "").strip()
        if not kw:
            return "关键词为空。"
        limit = int(args.get("limit") or 10)
        limit = max(1, min(limit, 30))
        if not self.budget.take():
            return "本轮查库次数已用完，请基于已有信息回答。"
        try:
            msgs = live_history.search_history(self.client, kw, limit=limit + 5)
        except Exception as e:
            return _db_fail("搜索", e)
        # 排除控制会话自己：否则助手在文件传输助手里的旧回答会被当成
        # 「我的聊天记录」喂回模型，自我指涉（build_user_prompt 里记过这个坑）。
        msgs = [m for m in (msgs or [])
                if str(m.get("talker") or "") != self.chat][-limit:]
        if not msgs:
            return f"没搜到含「{kw}」的记录。"
        out = []
        for m in msgs:
            talker = str(m.get("talker") or "")
            tname = self._names.get(talker) or talker
            is_group = auto_reply.is_group(talker)
            who = speaker_of(m, self._names, tname, is_group)
            # 群里的记录得带上群名，否则不知道是哪儿的对话
            where = f"{tname}/" if is_group else ""
            text = str(m.get("content") or "").strip()[:self.line_chars]
            out.append(f"[{m.get('time', '?')}] {where}{who}: {text}")
        return f"含「{kw}」的记录（{len(out)} 条，时间从早到晚）：\n" + "\n".join(out)

    def t_recent_messages(self, args):
        limit = int(args.get("limit") or 20)
        limit = max(1, min(limit, 50))
        if not self.budget.take():
            return "本轮查库次数已用完，请基于已有信息回答。"
        try:
            msgs = live_history.recent_messages(self.client, limit=limit + 5)
        except Exception as e:
            return _db_fail("查最近消息", e)
        msgs = [m for m in (msgs or [])
                if str(m.get("talker") or "") != self.chat][-limit:]
        if not msgs:
            return "没查到最近的聊天记录。"
        out = []
        for m in msgs:
            talker = str(m.get("talker") or "")
            tname = self._names.get(talker) or talker
            group = auto_reply.is_group(talker)
            who = speaker_of(m, self._names, tname, group)
            text = str(m.get("content") or "").strip()[:self.line_chars]
            if m.get("is_self"):
                tail = "我最后说的是"
            elif group and who not in (tname, "群成员"):
                tail = f"{who} 最后说的是"
            else:
                tail = "最后一条是"
            out.append(f"[{m.get('time', '?')}] {tname}：{tail}「{text}」")
        return ("最近有消息的会话（每个会话只取最后一条，时间从早到晚）：\n"
                + "\n".join(out))

    def t_search_in_chat(self, args):
        contact = str(args.get("contact") or "").strip()
        kw = str(args.get("keyword") or "").strip()
        if not contact or not kw:
            return "参数不全：需要 contact 和 keyword。"
        limit = int(args.get("limit") or 10)
        limit = max(1, min(limit, 30))
        cand, err = self._one(contact)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or contact
        if not self.budget.take():
            return "本轮查库次数已用完，请基于已有信息回答。"
        try:
            msgs = live_history.query_contact_history(self.client, wxid,
                                                      limit=limit, keyword=kw)
        except Exception as e:
            return _db_fail(f"在「{nm}」里搜索", e)
        lines = format_history_lines(msgs, self._names, nm,
                                     auto_reply.is_group(wxid), limit,
                                     self.line_chars)
        if not lines:
            return f"在「{nm}」的聊天记录里没搜到含「{kw}」的。"
        return (f"在「{nm}」里搜到含「{kw}」的 {len(lines)} 条：\n"
                + "\n".join(lines))

    def t_find_images(self, args):
        contact = str(args.get("contact") or "").strip()
        limit = int(args.get("limit") or 10)
        limit = max(1, min(limit, 30))
        cand, err = self._one(contact)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or contact
        try:
            # 只在真的要查库时才扣预算（列表已缓存就不用）
            if wxid not in self._img_cache and not self.budget.take():
                return "本轮查库次数已用完，请基于已有信息回答。"
            imgs = self._images(wxid, limit)
        except Exception as e:
            return _db_fail("查图片", e)
        if not imgs:
            return f"和「{nm}」没查到图片消息。"

        lines = [f"「{nm}」最近的图片（{len(imgs)} 条）："]
        is_group = wxid.endswith("@chatroom")
        for m in imgs:
            # 群聊里没法便宜地拿到「具体谁发的」，别把群名当发言人，会误导
            if m.get("is_self"):
                who = "我"
            elif is_group:
                who = "群成员"
            else:
                who = nm
            tag = "可看" if m.get("image") else "无缓存、看不了"
            lines.append(f"- [{m.get('time')}] {who}  local_id={m.get('local_id')}  {tag}")
        n = sum(1 for m in imgs if m.get("image"))
        lines.append(f"\n其中 {n} 张有本地缓存、能读内容（微信只缓存你滚动看过的图，"
                     f"所以覆盖率有限）。要读某张就调 read_image，传 contact 和 local_id。")
        return "\n".join(lines)

    def t_read_image(self, args):
        contact = str(args.get("contact") or "").strip()
        lid = str(args.get("local_id") or "").strip()
        if not contact or not lid:
            return "参数不全：需要 contact 和 local_id。"
        cand, err = self._one(contact)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        try:
            # 读图本身是本地 OCR，不额外查库；列表也已缓存，所以**不扣预算**——
            # 否则模型多看几张就把预算耗光，后面全白跑。
            if wxid not in self._img_cache and not self.budget.take():
                return "本轮查库次数已用完，请基于已有信息回答。"
            imgs = self._images(wxid)
        except Exception as e:
            return _db_fail("查图片", e)
        hit = next((m for m in imgs if str(m.get("local_id")) == lid), None)
        if hit is None:
            return f"在「{contact}」最近的图片里没找到 local_id={lid}。"

        path = hit.get("image")
        if not path:
            return (f"这张图（local_id={lid}，{hit.get('time')}）**本地没有缓存，读不了**。"
                    f"微信只把你滚动看过的图片解码缓存在本地，这张不在里面。\n"
                    f"请如实告诉用户「这张图我看不到」，**不要编内容**。")

        import image_read
        ok, text = image_read.describe(path, self.cfg)
        if not ok:
            return f"这张图有缓存，但读不出内容：{text}"
        if not text.strip():
            return (f"这张图（{hit.get('time')}）里没识别到文字。"
                    f"当前用的是系统 OCR，只能认『图里的字』；"
                    f"照片、表情包这类画面内容它看不懂。")
        return f"[{hit.get('time')} 的图片，识别出的文字]\n{text.strip()}"

    def t_find_files(self, args):
        contact = str(args.get("contact") or "").strip()
        limit = int(args.get("limit") or 10)
        limit = max(1, min(limit, 30))
        cand, err = self._one(contact)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or contact
        try:
            # 同 _images：列表一轮内只查一次，缓存命中就不扣预算
            if wxid not in self._file_cache and not self.budget.take():
                return "本轮查库次数已用完，请基于已有信息回答。"
            files = self._files(wxid, limit)
        except Exception as e:
            return _db_fail("查文件", e)
        if not files:
            return (f"在「{nm}」最近的聊天里没找到文件消息。\n"
                    f"（只扫最近几百条消息，更早的文件可能没覆盖到。）")

        is_group = wxid.endswith("@chatroom")
        lines = [f"「{nm}」最近的文件（{len(files)} 份）："]
        viewable = 0
        for m in files:
            who = "我" if m.get("is_self") else ("群成员" if is_group else nm)
            # locate 要列目录，每个文件只算一次
            path = file_read.locate(m.get("name") or "")
            viewable += 1 if path else 0
            size = m.get("size") or 0
            sz = (f"{size / 1048576:.1f}MB" if size >= 1048576
                  else f"{max(1, size // 1024)}KB")
            mark = "本地有、能读" if path else "本地没有、读不了"
            lines.append(f"- [{m.get('time')}] {who}  local_id={m.get('local_id')}  "
                         f"{m.get('name')}（{m.get('ext') or '?'}, {sz}）  {mark}")
        lines.append(f"\n其中 {viewable} 份在本地、能读内容。要读哪份就调 read_file，"
                     f"带上它的 local_id。只有**你收过的**文件才在本地，发出去的不在。")
        return "\n".join(lines)

    def t_read_file(self, args):
        contact = str(args.get("contact") or "").strip()
        lid = str(args.get("local_id") or "").strip()
        if not contact or not lid:
            return "参数不全：需要 contact 和 local_id。"
        cand, err = self._one(contact)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        try:
            # 读文件是本地解析、不额外查库；列表也缓存着，所以不扣预算
            if wxid not in self._file_cache and not self.budget.take():
                return "本轮查库次数已用完，请基于已有信息回答。"
            files = self._files(wxid)
        except Exception as e:
            return _db_fail("查文件", e)
        hit = next((m for m in files if str(m.get("local_id")) == lid), None)
        if hit is None:
            return f"在「{contact}」最近的文件里没找到 local_id={lid}。先调 find_files 看列表。"

        name = hit.get("name") or ""
        path = file_read.locate(name)
        if not path:
            return (f"这份文件（{name}）**本地没有，读不了**。微信只把「你收过」的文件"
                    f"存在本地。请如实告诉用户「这份我这边没有」，**不要编内容**。")
        text, err = file_read.extract(path, self.cfg)
        if err:
            return f"「{name}」读不了：{err}\n请如实告诉用户，**不要编内容**。"
        return (f"[{hit.get('time')} 收到的文件：{name}]\n{text}\n"
                f"（文件共 {hit.get('size') or '?'} 字节，上面是提取出的文字）")

    def t_pending_replies(self, args):
        limit = int(args.get("limit") or 20)
        limit = max(1, min(limit, 50))
        if not self.budget.take():
            return "本轮查库次数已用完，请基于已有信息回答。"
        try:
            rows = live_history.pending_replies(self.client, limit=limit + 5)
        except Exception as e:
            return _db_fail("查未读消息", e)
        rows = [m for m in (rows or [])
                if str(m.get("talker") or "") != self.chat][-limit:]
        if not rows:
            return "现在没有未读消息（微信里没有待处理的会话）。"
        out = []
        for m in rows:
            talker = str(m.get("talker") or "")
            tname = self._names.get(talker) or talker
            text = str(m.get("content") or "").strip()[:self.line_chars]
            unread = m.get("unread")
            if auto_reply.is_group(talker):
                who = speaker_of(m, self._names, tname, True)
                head = f"{tname}（群，未读 {unread} 条）{who}："
            else:
                head = f"{tname}（未读 {unread} 条）："
            out.append(f"[{m.get('time', '?')}] {head}{text}")
        return ("还有未读的会话（未读数由微信统计，点开才会清零）：\n" + "\n".join(out))

    def t_group_members(self, args):
        contact = str(args.get("contact") or "").strip()
        if not contact:
            return "参数不全：需要 contact（群的 roomid，形如 xxx@chatroom）。"
        limit = int(args.get("limit") or 80)
        limit = max(1, min(limit, 300))
        cand, err = self._one(contact)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or contact
        if not auto_reply.is_group(wxid):
            return (f"「{nm}」不是群（群 id 要以 @chatroom 结尾）。"
                    f"看单聊历史直接用 read_history 就行。")
        if not self.budget.take():
            return "本轮查库次数已用完，请基于已有信息回答。"
        try:
            mem = live_history.group_members(self.client, wxid)
        except Exception as e:
            return _db_fail(f"查「{nm}」的群成员", e)
        if not mem:
            return (f"没查到「{nm}」的成员名单（这个版本可能拿不到群成员数据）。"
                    f"如实告诉用户查不到，**不要编名单**。")
        shown = mem[:limit]
        lines = []
        for m in shown:
            nick = m.get("name") or self._names.get(m["wxid"]) or m["wxid"]
            lines.append(f"- {nick}{'（群主）' if m.get('is_owner') else ''}")
        head = f"「{nm}」共 {len(mem)} 人"
        if len(mem) > len(shown):
            head += f"，下面只列前 {len(shown)} 个"
        return head + "：\n" + "\n".join(lines)

    def t_send_image(self, args):
        to = str(args.get("to") or "").strip()
        path, perr = self._image_path_ok(args.get("path"))
        if perr:
            return perr
        cand, err = self._one(to)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or to
        base = os.path.basename(path)
        desc = f"一张图片（{base}）"
        if self._in_whitelist(wxid, nm) or self._in_whitelist(wxid, to):
            try:
                self.client.send_image(path, wxid)
            except Exception as e:
                return f"给 {nm} 发图片失败：{e}"
            self.sent.append((nm, desc))
            return f"已把 {base} 发给 {nm}。"
        set_pending(self.chat, wxid, nm, desc, image=path)
        return (f"「{nm}」不在自动发送名单里，图片**尚未发送**。"
                f"请告诉用户：准备把 {base} 发给 {nm}，让他回复「确认」后再发。")

    def t_send_images(self, args):
        to = str(args.get("to") or "").strip()
        raw = str(args.get("dir") or "").strip()
        if not to or not raw:
            return "参数不全：需要 to 和 dir。"
        d = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(d):
            return f"没找到目录：{d}"
        d, derr = self._in_allowed_dirs(d)
        if derr:
            return derr

        # 只扫第一层（不递归），按修改时间从早到晚——照片天然就是拍摄顺序
        try:
            entries = []
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if not os.path.isfile(p):
                    continue
                if os.path.splitext(fn)[1].lower() not in _IMG_EXT:
                    continue
                try:
                    entries.append((os.path.getmtime(p), p))
                except OSError:
                    continue
            entries.sort()
        except OSError as e:
            return f"读不了这个目录：{e}"

        folder = os.path.basename(d.rstrip("\\/")) or d
        if not entries:
            return (f"「{folder}」里第一层没有图片。"
                    f"（只扫一层，子目录不算；支持的格式：{'/'.join(sorted(_IMG_EXT))}）")

        agent_cfg = self.cfg.get("agent") or {}
        cap = max(1, int(agent_cfg.get("max_send_count", 20)))
        want = int(args.get("limit") or cap)
        want = max(1, min(want, cap))
        picked = [p for _t, p in entries[:want]]
        trunc = ""
        if len(entries) > len(picked):
            trunc = (f"\n（「{folder}」里共 {len(entries)} 张，本次只发最前面的 {len(picked)} 张。"
                     f"要发更多就分几次说，或让用户调大 agent.max_send_count）")

        cand, err = self._one(to)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or to
        desc = f"{len(picked)} 张图片（来自「{folder}」）"

        if self._in_whitelist(wxid, nm) or self._in_whitelist(wxid, to):
            interval = max(0.0, float(agent_cfg.get("send_interval", 1.5)))
            for i, p in enumerate(picked):
                if i and interval:
                    # 连发期间轮询会暂停，这是**有意**的：hook 不支持并发
                    time.sleep(interval)
                try:
                    self.client.send_image(p, wxid)
                except Exception as e:
                    return f"发到第 {i + 1} 张失败（前面 {i} 张已发出）：{e}"
            self.sent.append((nm, desc))
            return f"已把 {len(picked)} 张图片发给 {nm}。{trunc}"

        set_pending(self.chat, wxid, nm, "", image=picked)
        return (f"「{nm}」不在自动发送名单里，**一张都还没发**。"
                f"请告诉用户：准备把「{folder}」里的 {len(picked)} 张图发给 {nm}，"
                f"让他回复「确认」后我再发。{trunc}")

    def t_forward_message(self, args):
        to = str(args.get("to") or "").strip()
        contact = str(args.get("contact") or "").strip()
        lid = str(args.get("local_id") or "").strip()
        if not to or not contact or not lid:
            return "参数不全：需要 to / contact / local_id。"
        src, serr = self._one(contact)
        if serr:
            return serr
        snm = src.get("remark") or src.get("name") or contact
        if not self.budget.take():
            return "本轮查库次数已用完，请基于已有信息回答。"
        try:
            xml = live_history.message_xml(self.client, str(src.get("wxid")), lid)
        except Exception as e:
            return _db_fail("取那条消息", e)
        if not xml:
            return (f"取不到「{snm}」里 local_id={lid} 那条消息的原始内容，转发不了。"
                    f"（3.9.x 没有这条路；4.x 上本地也可能没留原文。）"
                    f"请如实告诉用户，**不要编**。")

        cand, err = self._one(to)
        if err:
            return err
        wxid = str(cand.get("wxid"))
        nm = cand.get("remark") or cand.get("name") or to
        desc = f"转发「{snm}」里的一条消息"
        if self._in_whitelist(wxid, nm) or self._in_whitelist(wxid, to):
            try:
                self.client.send_xml(xml, wxid)
            except Exception as e:
                return f"转发给 {nm} 失败：{e}"
            self.sent.append((nm, desc))
            return f"已把「{snm}」里那条消息转发给 {nm}。"
        set_pending(self.chat, wxid, nm, desc, xml=xml)
        return (f"「{nm}」不在自动发送名单里，转发**尚未发出**。"
                f"请告诉用户：准备把「{snm}」里那条消息转给 {nm}，"
                f"让他回复「确认」后再发。")

    def t_auto_reply(self, args):
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        if action not in _AUTO_ACTIONS:
            return f"action 只能是 {' / '.join(_AUTO_ACTIONS)} 之一。"
        who = str(args.get("who") or "").strip()
        given = who            # 用户/模型说的原名，用来当显示名（见下面 name_hint）
        mode = str(args.get("mode") or "").strip().lower() or None
        if mode and mode not in ("self", "assistant"):
            return "mode 只能是 self 或 assistant。"
        review = args.get("review")
        if isinstance(review, str):
            review = review.strip().lower() in ("true", "1", "yes", "on", "是")

        # 昵称换成 wxid 再用（群里让模型直接给 roomid）
        if who and action in ("add", "del", "mode", "review"):
            if not looks_like_id(who):
                cands = self._resolve(who)
                if not cands:
                    return f"没找到「{who}」。群请直接给 roomid（形如 xxx@chatroom）。"
                if len(cands) > 1:
                    names = "；".join(
                        f"{c.get('remark') or c.get('name')}({c.get('wxid')})"
                        for c in cands[:5])
                    return f"「{who}」匹配到多个人：{names}。请问用户要哪一个。"
                who = str(cands[0].get("wxid"))

        arg = auto_reply.build_arg(action, who=who, mode=mode, review=review,
                                   context=args.get("context_messages"))
        # 上面把昵称换成了 wxid，得把原名带进去，否则名单里记的是 wxid_xxx
        hint = given if (action == "add" and given and given != who) else None
        text, changed = auto_reply.handle_command(arg, self.cfg_provider(),
                                                  self.client, can_lookup=True,
                                                  name_hint=hint)
        if changed:
            self.cfg_changed = True
        if action == "status":
            return text          # 状态本身就把名单列全了，不用再补一行摘要
        return f"{text}\n（当前自动回复：{auto_reply.summary_line(self.cfg_provider())}）"

    def t_schedule(self, args):
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        if action not in _SCHED_ACTIONS:
            return f"action 只能是 {' / '.join(_SCHED_ACTIONS)} 之一。"
        who = str(args.get("who") or "").strip()
        # 和 /定时 走**同一条**实现（scheduler.handle_command），
        # 包括重名不静默取第一个这条规矩。
        arg = scheduler.build_arg(
            action, when=args.get("when"), who=who, text=args.get("text"),
            target=args.get("target") or who, mode=args.get("mode"))
        text, changed = scheduler.handle_command(arg, self.cfg_provider(), self._one)
        if changed:
            self.cfg_changed = True
        if action == "status":
            return text
        return f"{text}\n（当前{scheduler.summary_line(self.cfg_provider())}）"

    def t_watch(self, args):
        args = args or {}
        action = str(args.get("action") or "").strip().lower()
        if action not in _WATCH_ACTIONS:
            return f"action 只能是 {' / '.join(_WATCH_ACTIONS)} 之一。"
        who = str(args.get("who") or "").strip()
        # 和 /盯着 走同一条实现（含重名不静默取第一个）
        arg = watch.build_arg(action, who=who)
        text, changed = watch.handle_command(arg, self.cfg_provider(), self._one)
        if changed:
            self.cfg_changed = True
        if action == "status":
            return text
        return f"{text}\n（当前{watch.summary_line(self.cfg_provider())}）"

    # ---------- 分发 ----------

    def run(self, name, args):
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"没有名为 {name} 的工具。"
        try:
            return str(fn(args or {}))
        except Exception as e:
            return f"工具 {name} 执行出错：{e}"
