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
import sys
import time

import auto_reply
import executor
import file_read
import image_cache
import live_history
import scheduler
import watch

# 允许发送的图片后缀
_IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# 一次对话最多发几次查库请求的**硬夹**。
#
# 为什么要有硬夹：hook 不支持并发，这个 hook 已经把微信搞崩过 6 次，而
# `agent.max_queries` 是用户在 config.yaml 里手填的数字，以前**完全不设上限**
# ——写 `max_queries: 99999` 就是一串不间断的查库，没有任何东西拦它。
#
# 上限取 20 的理由：
#   * 默认值是 6，20 已经是它的 3 倍多，正常一轮工具调用（读历史 + 搜关键词 +
#     查图片 + 列文件 + 未读）根本用不到；调大只是让模型多绕几圈。
#   * 单个工具最多扣 1 次预算，而 `live_history` 里带选择性过滤的查询实测
#     0.001~0.41 秒（见 CLAUDE.md）。20 次串行查询在最坏情况下约 8 秒，
#     还是挂在**收消息那条线程**上的串行调用，不会和轮询叠在一起。
#   * 上限再抬（几十上百）的唯一效果就是「模型卡住时把微信压在查询里更久」，
#     而这正是历次崩溃的现场特征（慢查询 → hook HTTP 500 → 微信进程没了）。
#
# 注意：这是**上限**，不是建议值；越界只钳制并告警，不报错、不静默放行。
_MAX_QUERIES_DEFAULT = 6
_MAX_QUERIES_MIN = 1
_MAX_QUERIES_MAX = 20

# 「发送类」工具：它们会**真的把东西发出去**，所以异常时不能只说一句
# 「工具 X 执行出错」——那样模型会以为一条都没发，转头跟用户说「没发出去」，
# 而实际上可能已经发出去好几条了。见 ToolBox.run() 的异常分支。
_SEND_TOOLS = ("send_text", "send_image", "send_images", "forward_message")


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

    ⚠️ **查不到显示名时绝不把原始 id 当名字返回**：以前这里是
    `names.get(sender) or sender`，于是查不到就回一串 `wxid_xxx` 给模型，
    模型照抄给你（正是 CLAUDE.md 里「看不到真正的名字」那个根因的另一面）。
    现在查不到就退回「群成员 / 会话名 / 对方」，宁可不精确，也不喂 id。
    """
    if m.get("is_self"):
        return "我"
    named = str(m.get("sender_name") or "").strip()
    if named:
        return named
    sender = str(m.get("sender") or "")
    if sender:
        hit = (names or {}).get(sender)
        # 兜底再挡一道：**映射表里存的是原始 id 也不能当名字用**。
        # （contact_names 已经不填 wxid 了，但别处仍可能构造出这种表；
        # 这一层是渲染统一出口，谁传进来都得过。）
        if hit and not (looks_like_id(hit) or str(hit).isdigit()):
            return hit
        # 不是 id 形状的才当名字用（防御：万一将来某条路塞进来的就是真名字）
        if not (looks_like_id(sender) or sender.isdigit()):
            return sender
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


def _auto_ok_hit(cmd, auto_ok):
    """这条命令在不在 shell.auto_ok 免确认名单里。

    **只做整条命令字符串精确相等匹配**（两边 strip 后比）。绝不做前缀、子串或
    通配符匹配：命令原文是模型写的，任何模糊匹配都等于给模型留了绕过确认的
    注入面（比如名单里写 `dir`，模型就能写 `dir & del ...` 蹭过去）。

    名单不是 list 时当**空**处理；list 里非字符串的项（写错了的 YAML）直接
    跳过，不做 str() 强转——`auto_ok: [5]` 不该让命令「5」免确认。
    两条都是 fail-safe：配置写坏了只能变成"全都要确认"，不能变成"全都免确认"。
    """
    if not isinstance(auto_ok, (list, tuple)):
        return False
    want = str(cmd or "").strip()
    if not want:
        return False
    return any(isinstance(x, str) and x.strip() and want == x.strip()
               for x in auto_ok)


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
    {
        "name": "run_command",
        "description": (
            "在用户本机执行**一条命令行命令**（cmd / PowerShell 都能用）。"
            "用户说「帮我跑一下…」「执行个命令…」「看下这个目录里有什么」时用这个。\n"
            "**本工具绝不会立即执行任何命令**：它只是把 command **原文**登记成一条"
            "待确认动作，由用户回「确认」之后才真的跑。所以：\n"
            "  * 调用后要把命令**原样**复述给用户，请他回「确认」；\n"
            "  * 用户没确认之前，**绝不许说「已经跑了」「正在跑」或者编造输出**；\n"
            "  * 命令到底跑没跑、跑出什么，以工具返回为准，别自己替它下结论。\n"
            "真跑起来之后，超时、报错、输出被截断这些都要**如实**转述给用户。\n"
            "（唯一例外：用户自己在配置里把某条命令列进了免确认名单，那种命令会"
            "直接执行——这种情况工具返回里会写明「已直接执行」，没写就是没跑。）"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "要执行的命令**原文**，一行；别改写、别加解释"},
                "timeout": {"type": "integer",
                            "description": "超时秒数，可不填（默认用 config.yaml 的 shell.timeout）"},
            },
            "required": ["command"],
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

# 发图白名单「兜底放宽」的告警只打一次：这不是会重复的噪音，
# 而是一条**必须让用户看见**的事实（默认白名单被放宽到整个微信数据根目录）。
_WARNED_IMAGE_ROOT = [False]
# 用户配了 send_image_dirs 时那条「白名单 = 你配的 + 默认缓存根」的告警只打一次
_WARNED_IMAGE_DIRS = [False]


def _warn(msg):
    """打一条必须被看见的告警。走 stderr：bot.log / 控制台都收得到。

    这里故意不用 logging——项目里全是 print 到 stdout/stderr 的，
    引入 logging 配置会改变现有日志形状。重点是**不能静默**。
    """
    print(f"⚠️ {msg}", file=sys.stderr)


def allowed_image_dirs(cfg):
    """解析出「发图允许的根目录」列表。**登记和发送两处都走它**，避免两套判定。

    规则（**这是安全边界，改之前先读 CLAUDE.md**）：
      1. **默认**（用户没配）放行**真正的图片缓存根** `<账号>/cache` ——
         也就是「聊天里已有的图」，**不是**整个微信数据目录；
      2. 用户在 `agent.send_image_dirs` 里配的目录 **加在默认之上**：
         CLAUDE.md 的原话是「要加目录让用户改 `agent.send_image_dirs`」——是**加**，
         不是「换一份名单」。**以前这里是「配了就顶掉默认」**，后果是真机自检里
         撞出来的：用户为了自测写了个 `test_images`，就**再也发不出聊天里的图**了，
         而且它是静默的（只是发图被拒）。所以改成并集，并在配了目录时打一条明说
         「两处都能发」的告警——边界可以宽，但**用户必须知道它宽在哪**。
      3. 推不出图片缓存根（没找到 `<账号>/cache`）才退回 `image_cache.data_root()`
         （整个微信数据目录），并打一条明确告警。

    第 3 条是兜底，不是默认姿势：宁可放宽并**说清楚**，也不许静默放宽，
    更不许因为推不出来就变成「什么都不许发」把功能弄坏（那是最坏的一种
    「安全」——用户以为在用，实际全被拒）。
    """
    agent_cfg = (cfg or {}).get("agent") or {}
    raw = agent_cfg.get("send_image_dirs") or []
    if isinstance(raw, str):        # 写成单个字符串的 YAML 不算错，按一个目录处理
        raw = [raw]
    user_dirs = [os.path.abspath(os.path.expanduser(str(d)))
                 for d in raw if str(d).strip()]

    real = [d for d in image_cache.image_cache_dirs() if d]
    if real:
        out = list(user_dirs)
        for d in real:
            if d not in out:
                out.append(d)
        if user_dirs and not _WARNED_IMAGE_DIRS[0]:
            _WARNED_IMAGE_DIRS[0] = True
            _warn("agent.send_image_dirs 已配置：发图白名单 = 你配的目录（"
                  + "、".join(user_dirs) + "）**再加上**默认的微信图片缓存根"
                  "（聊天里已有的图）——两处都能发。")
        return out

    root = image_cache.data_root()
    if root:
        if not _WARNED_IMAGE_ROOT[0]:
            _WARNED_IMAGE_ROOT[0] = True
            _warn("推不出微信图片缓存目录（没找到 <账号>/cache），"
                  f"发图白名单被放宽到整个微信数据根目录：{root}。"
                  "要收紧就在 config.yaml 的 agent.send_image_dirs 里写明目录。")
        out = list(user_dirs)
        if os.path.abspath(root) not in out:
            out.append(os.path.abspath(root))
        return out
    # 连数据根都找不到：至少尊重用户自己配的目录（空列表 = 什么都不许发，调用方如实拒绝）
    return user_dirs


def _is_under(path, root):
    """绝对路径 path 是否在 root 里（**先 realpath 再比**）。

    必须 realpath：不然一个指到别处的 junction / 符号链接放在允许目录里，
    就能把任意路径伪装成「在允许目录内」。realpath 之后链接已经解开，
    比的是它**真正**待在哪儿。
    commonpath 跨盘符会抛 ValueError，那种情况按「不在」处理（跳过这个根）。
    """
    try:
        p = os.path.realpath(path)
        r = os.path.realpath(root)
    except OSError:
        return False
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:
        return False        # 不同盘符，commonpath 会抛——不匹配


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
                image=None, xml=None, cmd=None, timeout=None):
    """登记一条待确认发送。kind 区分来源：agent（用户让助手发的）/ auto（自动回复草稿）。

    bot 对两者要求不一样：自动回复草稿只认明确的中文确认词，避免用户在控制
    会话里随口一句「ok」就把草稿发给别人。

    count 是用户回「确认」后连发的次数；连发节奏由 agent.max_send_count /
    agent.send_interval 兜着，别指望调用方自觉。

    image / xml 用来表示「这条待确认要发的不是文本」：image 是本地图片路径，
    xml 是要转发的原始消息 XML。bot 的确认分支据此选发法（都只发一次，
    count/连发只对文本有意义）。

    kind="shell" 表示「待确认执行的一条本地命令」：cmd 是**模型给的命令原文**，
    text 也存同一份原文（bot 复述给用户用）。它没有收件人，to_wxid / to_name
    留空，bot 的确认分支**不会**走 send_pending。timeout 是模型可选的超时秒数。
    """
    _PENDING.setdefault(str(chat), []).append(
        {"to_wxid": to_wxid, "to_name": to_name, "text": text,
         "image": image, "xml": xml,
         "cmd": cmd, "timeout": timeout,
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


def list_pending(chat, ttl=300):
    """该会话**未过期**的待确认项，顺序就是 FIFO 顺序。**不出队。**

    为什么需要它：待确认队列是「待发送(agent/auto)」和「待执行本地命令(shell)」
    混在一条 FIFO 里的。bot 的确认分支以前是「看队头 → 判队头那条 → 取出队头
    来执行」——队列里同时压着两条时，用户看到的是他刚触发的那条提示，
    实际执行的却是更早入队的另一条（executor-review 的 R5-3）。

    有了这个列表，bot 就能在混合 kind 时改回**按编号的菜单**：
    让用户选「第几条」，而不是糊里糊涂地确认了另一条。
    """
    return list(_alive(chat, ttl))


def pop_pending(chat, ttl=300, index=None):
    """取出并清除一条待确认动作；没有/全过期/越界返回 None。

    * `index is None`：**保持原有行为**——取队头（最早的）。
    * `index` 是 **1 起的序号**（对应 list_pending 返回的顺序）：取第 index 项
      并把它从队列里移除，**其余项顺序不变**。
    * 越界（<=0、超过条数、不是整数）返回 None 且**不改动队列**——
      绝不能因为用户手滑选了个不存在的编号就把整条队列清掉。
    """
    items = _alive(chat, ttl)
    if not items:
        return None
    if index is None:
        # 老行为：队头。pop 的是 _alive 给的新列表，不是 _PENDING 里那份，
        # 所以下面必须显式写回。
        item = items.pop(0)
    else:
        try:
            i = int(index)
        except (TypeError, ValueError):
            return None
        if i < 1 or i > len(items):
            return None
        item = items.pop(i - 1)
    if items:
        _PENDING[str(chat)] = items
    else:
        _PENDING.pop(str(chat), None)
    return item


def _mask_name(name):
    """显示名兜底：万一是 wxid / roomid，就别原样写进给模型或用户看的文本。

    渲染「谁说的、发给谁」一律用显示名（CLAUDE.md 的硬规矩）。正常情况
    `to_name` 就是备注/昵称，这里只是最后一道闸：早期登记过、或者配置写歪了
    留下一条 id 当名字时，宁可说「对方」也不能把那串 id 送到模型面前
    ——模型会照抄给用户。
    """
    n = str(name or "").strip()
    if not n or looks_like_id(n) or "@" in n:
        return "对方"
    return n


def _clip(text, limit):
    """截断并返回 (文本, 截断说明)。**截了必须明说**，不许看着像原文。"""
    s = str(text or "")
    if len(s) <= limit:
        return s, ""
    return s[:limit], f"（原文 {len(s)} 字，上面只显示前 {limit} 字，已截断）"


def describe_pending(item):
    """给编号菜单用的一行人类描述。**不带编号**——编号由菜单按位置自己加。

    为什么不含编号：这个函数只拿得到 item，拿不到它在队列里的位置；
    在这里编一个序号只会和真实顺序对不上。bot 拼菜单时用
    `enumerate(list_pending(...), 1)` 加序号最稳。

    硬规矩：**绝不出现 wxid / roomid**。收件人只认 `to_name`（必要时兜底成
    「对方」）；`shell` 一律显示命令**原文**——用户审的就是这条真命令，
    中间任何转述/改写都等于把确认闸门做废（必要时截断，但一定标注截断）。
    """
    if not isinstance(item, dict):
        return "一条无法识别的待确认动作"

    kind = str(item.get("kind") or "agent")
    to_name = _mask_name(item.get("to_name"))

    # 1) 待确认执行的本地命令：显示原文（防提示词注入的关键）
    if kind == "shell":
        raw = item.get("cmd")
        if raw in (None, ""):
            raw = item.get("text")
        cmd, note = _clip(str(raw or ""), 200)
        return f"本机命令「{cmd}」{(' ' + note) if note else ''}"

    # 2) 待确认发送的图片（单张或一串路径）
    img = item.get("image")
    if img:
        paths = [img] if isinstance(img, str) else list(img)
        names = [os.path.basename(str(p)) for p in paths if str(p or "").strip()]
        if len(names) == 1:
            return f"发给 {to_name} 一张图片（{names[0]}）"
        head = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
        return f"发给 {to_name} {len(names)} 张图片（{head}）"

    # 3) 待确认转发的一条消息
    if item.get("xml"):
        return f"转发一条消息给 {to_name}"

    text, note = _clip(item.get("text"), 120)
    tail = f" {note}" if note else ""

    # 4) 自动回复草稿：正文已经原样发给用户看过了，这里不重复（省 token）
    if kind == "auto":
        return f"自动回复草稿 → 发给 {to_name}：{text}{tail}"

    # 5) 普通的待确认发送
    try:
        count = int(item.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    if count > 1:
        return f"发给 {to_name}「{text}」（连发 {count} 次）{tail}"
    return f"发给 {to_name}「{text}」{tail}"


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


def send_pending(client, item, interval=0.0, allowed_dirs=None):
    """执行一条待确认动作，返回 (真正发出的条数, 错误)。**同步、串行。**

    文本可以连发；转发只发一次。图片可以是一个路径或**一串路径**（群发照片），
    多个之间按 interval 停顿——连发期间轮询会暂停，这是有意为之（hook 不支持并发）。

    `allowed_dirs`：**发送时的二次校验**。登记（工具）时校验过一次，但从登记到
    用户回「确认」之间隔着时间，配置可能变了、文件可能被换成链接指到别处——
    所以真发之前再判一次目录归属。给 None = 保持原有行为不变（调用方自己负责），
    这是为了不破坏还在按老姿势调用它的地方。

    `allowed_dirs` 非 None 且这条待确认带 image 时：逐个路径 realpath 后判归属，
    有任何一个不通过就**一条都不发**并如实返回错误——绝不允许"先发几张再说"，
    也绝不静默跳过那一张（那等于偷偷改用户确认过的内容）。
    """
    wxid = item.get("to_wxid")
    if item.get("image"):
        imgs = item["image"]
        if isinstance(imgs, str):
            imgs = [imgs]

        if allowed_dirs is not None:
            if not allowed_dirs:
                return 0, ("发图白名单是空的（既没配 agent.send_image_dirs，"
                           "也找不到微信图片缓存目录），所以**一张都没发**。")
            for p in imgs:
                if not any(_is_under(str(p), d) for d in allowed_dirs):
                    return 0, (f"发送前复核不通过：{p} 不在允许发送的目录里"
                               f"（允许：{'；'.join(str(d) for d in allowed_dirs)}），"
                               f"**一张都没发**。要换位置只能由**用户自己**去 "
                               f"config.yaml 的 agent.send_image_dirs 里加。")

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

    # 精确相等必须单独一遍——否则「张三」会把「张三丰」也带出来。
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
        # 查库预算：**必须有硬上限**。以前这里直接 int(...) 接用户填的数，
        # 写 9999 就是 9999 次串行查库——hook 不支持并发、已经搞崩微信 6 次，
        # 所以这里越界一律**钳制并告警**（既不报错中断，也绝不放行）。
        # 上下限的理由见模块顶部的 _MAX_QUERIES_MIN / _MAX_QUERIES_MAX 注释。
        try:
            want = int(agent_cfg.get("max_queries", _MAX_QUERIES_DEFAULT))
        except (TypeError, ValueError):
            want = _MAX_QUERIES_DEFAULT
        self.max_queries = max(_MAX_QUERIES_MIN, min(want, _MAX_QUERIES_MAX))
        if want != self.max_queries:
            _warn(f"agent.max_queries={want} 越界，已钳制为 {self.max_queries}"
                  f"（允许 {_MAX_QUERIES_MIN}~{_MAX_QUERIES_MAX}；"
                  f"hook 不支持并发，不能放开查库次数）。")
        self.budget = _Budget(self.max_queries)
        # 连发的两条闸：单次请求的条数上限，以及每条之间的间隔。
        # 都是防「一口气刷屏把 hook 打崩」，不是给模型参考的建议值。
        self.max_send_count = max(1, int(agent_cfg.get("max_send_count", 20)))
        self.send_interval = max(0.0, float(agent_cfg.get("send_interval", 1.5)))
        # 允许发图的目录。**这是安全边界，不是便利设置**：模型自己填 path，
        # 不设边界就等于让它从你硬盘上挑任意文件发出去。留空 = 只放行微信
        # 自己的图片缓存目录（也就是「聊天里已有的图」）。
        # 白名单解析集中到模块级 allowed_image_dirs()：bot 在**发送时**也要用
        # 同一套（那边只拿得到 cfg，拿不到 ToolBox 实例），两处必须一致。
        self.send_image_dirs = allowed_image_dirs(self.cfg)
        # 历史行截断长度。以前写死 200，长消息被截得看不懂，模型答非所问。
        self.line_chars = max(80, int(agent_cfg.get("line_chars", 400)))
        # wxid -> 显示名，群里标发言人用（构造时算一次，别每条消息重算）
        self._names = auto_reply.contact_names(self.contacts)
        self.sent = []          # 本轮真正发出去的 [(name, text)]
        # 本轮真正发出去的**条数**（单独的计数器，不从 self.sent 的长度推：
        # self.sent 里一条可能代表「连发 5 次」也可能代表「一张图」，语义不齐，
        # 拿它当条数一定算错）。run() 的异常分支靠它算出「已经发出去几条」。
        self._sent_count = 0
        self._img_cache = {}    # wxid -> 图片列表（本轮复用，见 _images）
        self._file_cache = {}   # wxid -> 文件列表（同上，见 _files）
        # 本轮**是否真的登记过**一条待确认的本地命令（见 t_run_command）。
        #
        # 这是给 bot.py 当**事实依据**用的：真机上抓到过模型不调工具、自己演一段
        # 「我已经把命令提交上去了，等你回确认」——用户回「确认」时什么都不会发生
        # （没有任何待确认项）。bot 那边靠这个标记核对「说的」和「做的」是否一致。
        self.shell_queued = False

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
        """允许发送的根目录。**解析逻辑集中在模块级 allowed_image_dirs()**。

        以前这里自己拼一遍、bot 那边又拼一遍，两套判定迟早会不一致——
        而这是个安全边界，两套判定等于两个洞。

        默认放行的是**真正的图片缓存目录**（`<账号>/cache`，也就是「聊天里
        已经出现过的图」），不是整个 `~/Documents/xwechat_files`（那里面还有
        配置、db_storage、收来的文件）。推不出缓存目录时才退回 data_root 并告警，
        见 allowed_image_dirs()。
        """
        return allowed_image_dirs(self.cfg)

    def _in_allowed_dirs(self, p):
        """绝对路径 p 在不在允许目录里。返回 (p, 错误文本)。

        用 `_is_under()`：**先 realpath 再 commonpath**——不然允许目录里放一个
        指到别处的 junction/符号链接，就等于把任意路径伪装成「在允许目录内」。
        """
        dirs = self._allowed_dirs()
        if not dirs:
            return "", ("没配可发文件的目录（agent.send_image_dirs），"
                        "也找不到微信图片缓存目录，所以不让发。")
        for d in dirs:
            if _is_under(p, d):
                return p, None
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
            # 先记「真发出去几条」再往下：中途失败时 n 就是已经发出去的条数，
            # self.sent 那条记录是给人看的摘要，两者用途不同（见 _sent_count）。
            self._sent_count += n
            self.sent.append((nm, text if count == 1 else f"{text} ×{n}"))
            if err is not None:
                self._send_fail(f"发给 {nm} 时失败（已发出 {n}/{count} 条）：{err}")
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
                self._send_fail(f"给 {nm} 发图片失败：{e}")
            self._sent_count += 1
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

        # **实时读配置**（和 t_run_command 一致）：self.cfg 是构造时的快照，
        # 而 bot.py 那边是实时读盘的。同一轮会话里如果用户用 /命令 改过
        # max_send_count / send_interval，快照就会让两处对不上——
        # 「一次最多发几张」这种事绝不能用过期数字。
        agent_cfg = (self.cfg_provider() or self.cfg).get("agent") or {}
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
                    # 已经发出去几张先记进计数器，再抛给 run() 统一汇总说明
                    # （见 _send_fail）：少说一张就等于让模型对用户说「一张都没发」。
                    self._sent_count += i
                    self._send_fail(f"发到第 {i + 1} 张失败（前面 {i} 张已发出）：{e}")
            self._sent_count += len(picked)
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
                self._send_fail(f"转发给 {nm} 失败：{e}")
            self._sent_count += 1
            self.sent.append((nm, desc))
            return f"已把「{snm}」里那条消息转发给 {nm}。"
        set_pending(self.chat, wxid, nm, desc, xml=xml)
        return (f"「{nm}」不在自动发送名单里，转发**尚未发出**。"
                f"请告诉用户：准备把「{snm}」里那条消息转给 {nm}，"
                f"让他回复「确认」后再发。")

    def t_run_command(self, args):
        """登记一条「待确认执行」的本地命令。**这里一个字都不执行。**

        微信消息是远程执行入口，等于把本机 shell 的口子开在聊天里，所以规矩只有
        一条：不管模型多想跑，都必须**先把命令原文给用户看、等用户回「确认」**。
        本方法只 set_pending(kind="shell")，真正的执行在 bot.py 的确认分支里
        （那里才有「用户确实回了确认」这个事实）。

        绝不在这里 import/调用 executor.run_command——那等于「模型说跑就跑」。
        """
        raw = str(args.get("command") or "").strip()
        if not raw:
            return "参数不全：需要 command（要执行的命令原文）。"

        cfg = self.cfg_provider() or self.cfg
        shell_cfg = cfg.get("shell") or {}
        if not shell_cfg.get("enabled", False):
            # 如实报错，绝不偷偷换个动作糊弄过去
            return ("本地执行没开（config.yaml 的 shell.enabled）。"
                    "这条命令**没有**登记、也**没有**执行；"
                    "请如实告诉用户「本地命令执行是关着的」，"
                    "让用户自己去 config.yaml 把 shell.enabled 打开。")

        timeout = args.get("timeout")
        if timeout is not None:
            try:
                timeout = int(timeout)
            except (TypeError, ValueError):
                return f"timeout 得是秒数，收到的是「{args.get('timeout')}」。"
            if timeout <= 0:
                return "timeout 得是正数秒。"

        # shell.auto_ok：**用户**在 config.yaml 里逐条写死的命令，命中就直接跑、免确认。
        #
        # 这是用户自己指定的旁路，**不是模型能触发的东西**——模型只能写 command
        # 字符串，写不出"让这条命中白名单"的效果（匹配规则见 _auto_ok_hit：整条
        # 精确相等，没有前缀/子串/通配符）。**以后别把它当成"忘了加确认"删掉**：
        # 默认空列表 = 每条都确认，这才是推荐的姿势。
        if _auto_ok_hit(raw, shell_cfg.get("auto_ok")):
            print(f"[bot] 命中 shell.auto_ok，免确认执行: {raw}")
            kw = {"cfg": cfg}
            if timeout is not None:
                kw["timeout"] = timeout
            try:
                _ok, text = executor.run_command_text(raw, **kw)
            except Exception as e:
                return f"执行「{raw}」时出错：{e}"
            return (f"这条命令在用户的 shell.auto_ok 名单里，**已直接执行**"
                    f"（不用确认——是用户自己在 config.yaml 里指定的）。\n{text}")

        # text 也存命令原文：bot 复述给用户用，且保证**显示模型给的原话**，
        # 不是模型事后转述的版本（转述会把命令改掉，用户确认的就不是真跑的那条）。
        set_pending(self.chat, "", "", text=raw, kind="shell", cmd=raw,
                    timeout=timeout)
        # 真的登记上了才置位（auto_ok 那支直接执行、不走这里，也就不置位）。
        # bot 用它核对回答里说的「已提交、等你确认」是不是真的。
        self.shell_queued = True
        return (f"命令**尚未执行**。请把下面这条命令**原文**发给用户看一眼，"
                f"请他回复「确认」之后才会真的在他本机执行：\n"
                f"{raw}\n"
                f"（用户没回「确认」之前，**不许说已经跑了**，也不许编造执行结果。）")

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

    @staticmethod
    def _send_fail(msg):
        """发送类工具中途失败的**统一出口**：包成异常抛给 run()。

        为什么不让工具自己拼一句错误文本返回：以前 t_send_image / t_send_images
        出错时各自 return 一句话，异常根本没冒到 run() 那里，于是
        「已经发出去几张」这个事实**没人汇总**，模型只能看到一句「发图片失败」
        ——它会转头跟用户说「没发出去」，而对方其实已经收到两张了。

        现在四个发送类工具所有失败路径都走这里：真正发出去几张由 run() 用
        `_sent_count` 统一算（工具自己数容易漏、容易和 self.sent 的语义对不上），
        再配一句「该怎么办」，风格同 _db_fail()。
        """
        raise RuntimeError(msg)

    def run(self, name, args):
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return f"没有名为 {name} 的工具。"
        before = self._sent_count
        try:
            return str(fn(args or {}))
        except Exception as e:
            # 发送类工具的异常**必须带上「已经发出去几条」这个事实**。
            #
            # 真机上踩过：t_send_images 发到第 3 张才出错，工具只回一句
            # 「发图片失败」，模型就去跟用户说「没发出去」——而对方实际已经
            # 收到两张了。发消息是不可逆动作，假装没发等于把用户放在
            # 「以为没发、其实发了」的错误位置上（跟 run_command 那条
            # 「没跑就是没跑」是同一类规矩，只是这里反过来了：发了就是发了）。
            n = self._sent_count - before
            if name in _SEND_TOOLS:
                if n > 0:
                    return (f"工具 {name} 执行到一半失败：{e}\n"
                            f"**已经成功发出 {n} 条/张**（发消息不可逆，"
                            f"这部分收不回来，不要在用户面前说「都没发出去」）。"
                            f"请如实告诉用户：已经发出 {n} 条/张，剩下的**没有发**，"
                            f"并说清失败原因；要补发就**先问用户**，不要自己重发"
                            f"（重发会让对方收到重复消息）。")
                return (f"工具 {name} 执行失败：{e}\n"
                        f"**这条还没有发出去**（失败的时机在发送之前，"
                        f"或者第一条就失败了）。请如实告诉用户没发成、原因是什么，"
                        f"**不要说自己重发过了**。")
            return f"工具 {name} 执行出错：{e}"
