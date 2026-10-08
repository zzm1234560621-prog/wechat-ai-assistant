"""自动回复：在指定会话里代替用户本人回消息。

和 bot.py 的分工：
  * bot.py 管「什么时候该回」——判定会话、防自回环、min_gap 冷却、发送或转审核；
  * 本模块只管「回什么」——拼上下文 → 让模型生成 → 清洗成能直接发出去的文本。

三条硬约束：
  * **只用 llm.chat()，不带工具**。自动回复里模型绝不该自己去给别人发消息。
  * 群聊里模型要能选择「不回复」，用严格 JSON 表达；解析不出来一律当静默——
    宁可少回一句，也不要在群里刷屏或发一句没头没尾的话。
  * 输出必须清洗成「能直接发进微信」的形态：单行、无 markdown、长度可控。
    清洗后为空**一律不发**（send_text 发空串在微信里是条空消息）。
"""
import ast
import json
import re
import time

import live_history
import settings

# 单聊必回；群聊靠 SILENT 表达「这条我不该插话」
SILENT = None

_MODE_WORDS = ("self", "assistant")

_DEFAULT_SELF = (
    "你正在代替我本人回复微信消息。用我的口吻、第一人称、简短自然，"
    "就像平时打微信那样。不要暴露你是 AI，不要用书面语、列表或 markdown。"
    "不确定的事别编，用你自己的话如实说（别每次套同一句）。不要复述对方刚说过的话。"
)
_DEFAULT_ASSISTANT = (
    "你是我的微信 AI 助手，现在代我回复消息。礼貌、简洁，"
    "必要时说明自己是助手。不要用 markdown。"
)

# ⚠️「不许替我承诺」为什么必须写在这一段里，而不是写进 _DEFAULT_SELF 或某个人的人设：
#   人设是**整体替换**的（`persona_for`：`rec["persona"]` 非空就整段用它），
#   用户给某人设过或学过人设之后，_DEFAULT_SELF 一个字都不进 prompt；
#   只有 _COMMON_RULES 对**所有**自动回复无条件追加（单聊、群聊同一条路）。
#   2026-10-02 真机踩过：给「老师」学过人设之后，助手以用户本人的口吻把饭约了
#   （「老师，SKP米其林可不便宜啊[捂脸] 行，明天就明天…」），再往前还主动加了
#   「修好了我请您吃一顿」——当时人设里只有「不确定的事别编」，那条管的是**事实**，
#   管不住**替我表态**（约时间、答应赴约、承诺请客花钱）。改人设那一侧时别把它挪回去。
#
# ⚠️⚠️ 这一条里**故意不写「我回头确认下」那个现成句子**，别再好心加回去（2026-10-02 实测）：
#   原文是「一律不接，只回一句「我回头确认下」（或「我看下时间哈」）」。对会先判断
#   「这条到底算不算要我承诺」的模型（deepseek-flash），它是个兜底文案；但换成本地模型
#   （Ollama qwen3:14b）就变成了一份**可直接照抄的成品答案**——它不再判断，凡拿不准就整句抄，
#   于是「老师」那一路连着几条自动回复都是「老师，我回头确认下[捂脸]」
#   （人设给「老师」+[捂脸]，这句给正文）。
#   实测（同一模型、同一条历史）：把现成句子拿掉、只留「按对方这次具体说的话临场组织、
#   不要每次都用同一句」之后，它不再输出那句固定文案，改成自己的话。
#   **要保住的是「不许替我承诺」这个约束，不是那句文案。**
#
# ⚠️「表情要写成 `[捂脸]` 这种方括号」是同一条道理（2026-10-08 真机）：
#   微信**只把方括号的写法渲染成黄脸表情**——裸写「捂脸」到对方那儿就是两个汉字。
#   那天 11:49 给一位朋友学出来的那份人设写的是常用“捂脸/旺柴”（**裸词**），
#   11:53 起对方收到的每条带表情的回复都成了「… 捂脸」；而 10-02 那份人设写的是
#   `[呲牙][捂脸][强]`，同一天的回复就真是 `[捂脸]`（两天都是 deepseek-flash，
#   模型没换，变的只是人设里的写法）。人设是整体替换注入的，所以这条规矩也必须
#   落在 _COMMON_RULES，并且**要明说「人设里只写了表情名也照样补方括号」**——
#   已经存下的那些人设不会自己变好。回归：selftest_sched_auto.t19_emoji_brackets。
_COMMON_RULES = """
【硬性要求】
- 只输出要发出去的那条消息本身。不要解释、不要前缀、不要引号、不要 markdown。
- 像真人发微信：口语、简短（一两句），不要长篇大论，不要分点列表。
- 直接回应对方，不要复述对方刚说过的话。
- **表情要写成方括号的形式**（例如 `[捂脸]`、`[发呆]`、`[呲牙]`）：微信只认这一种写法，
  裸写「捂脸」到对方那儿就是两个汉字，等于没发。人设或口头语里如果只写了表情名，
  **也照样补上方括号再发**，不许照抄成裸词。
- **先判断最后一条消息属于哪一类**（寒暄闲聊 / 问事实 / 要我替他表态），再照下面这条走。
- **绝不替我做承诺、约定或决定**：约时间/约地点、答应赴约、承诺请客或花钱、
  答应帮人办事或传话、替我担保或表态——一律不接，把决定留给我本人。
  这时按对方**这次具体说的话**临场组织，只表达「这事我做不了主、得先问过我本人」；
  **不要每次都用同一句**，不许把某句话当成万能回复反复发（这里故意不给你现成句子，
  就是为了不让你照抄）。对方再追问、甚至说「你不是答应了吗」，也不许改口。
  普通聊天、寒暄、聊天气照常回，不受这条影响——**别拿这条挡正常的回应**。
"""

_GROUP_RULES = """
【当前是群聊】
先判断最后一条消息是否在跟我说话、或是否需要我回应。
- 需要回应：只输出 {"reply": "你要发的内容"}
- 不需要回应（与我无关、别人之间的闲聊、别人之间道谢寒暄等）：只输出 {"reply": null}
- 除了这个 JSON，什么都不要输出。
例子：{"reply": "好的，我下午过去"}
例子：{"reply": null}
"""

# 「称呼」——我平时怎么叫这个人。**单独一份、独立注入**，不塞进人设正文里：
# 人设是散文，一旦用户手写或重写，夹在里面的称呼就跟着没了。
_ADDRESS_MAX = 20          # 称呼必须是个词（「老张」「张哥」），不是一句话
# 「清空 / 学习」这两个魔法词和人设**共用同一套写法**（_PERSONA_CLEAR / _PERSONA_LEARN）：
# 同一句话在两个字段上意思一样，没必要各写一份、以后慢慢漂移。

# 称呼在**回复时**的注入文案。独立成段，因为它是给模型的一条硬约束：
# 叫错人比语气不对严重得多。
_ADDRESS_RULE = """
【怎么称呼对方】
你平时叫对方「{addr}」——回消息时自然这么叫（比如开口或句中带上）。
但只在自然的时候用：对方刚发来的话里不适合套称呼、或者你拿不准，就直接说事，
**不要硬塞称呼、更不要叫成别的名字或「亲爱的」这类你没用过的叫法**。
"""

_FENCE = re.compile(r"```[a-zA-Z0-9]*\r?\n?(.*?)```", re.S)
# 只有真出现 reply 键才按 JSON 解析（见 _extract_group_reply）。
# 用 "reply"（带引号）而不是裸单词 reply：模型在自然语言里提一句 reply
# 不该把它自己的回复变成静默。冒号前后不一定有空格（JSON 里 "a":1 很常见），
# 所以两处空白都是可选的。
_REPLY_KEY = re.compile(r"""["']reply["']\s*:""", re.I)
_LEAD = re.compile(r"^(回复|答复|reply|我)\s*[:：]\s*", re.I)
_MD = re.compile(r"\*\*|__|`")


def is_group(chat_id):
    """群会话 id 以 @chatroom 结尾（aixed 的 Msg.roomid 就是这么判的）。"""
    return str(chat_id or "").endswith("@chatroom")


def contact_names(contacts):
    """wxid -> **显示名**（备注优先），群里标发言人用。

    ⚠️ 只认真显示名：`remark` / `name` 都为空的联系人**不进这张表**，
    绝不拿 wxid 顶上——那会让渲染层把一串 wxid 交给模型（CLAUDE.md 明令禁止）。
    查不到就交给渲染层退回「对方 / 群成员」，宁可不精确也不喂 id。
    """
    out = {}
    for c in contacts or []:
        wxid = str(c.get("wxid") or "")
        if not wxid:
            continue
        disp = str(c.get("remark") or c.get("name") or "").strip()
        if disp:
            out[wxid] = disp
    return out


def _is_wxid(s):
    """看起来像原始 id（wxid_xxx / 群 roomid）的字符串。给模型看的文本里绝不许出现它。"""
    t = str(s or "").strip()
    return t.startswith("wxid_") or t.endswith("@chatroom")


def build_transcript(msgs, names, limit=20, unknown_note=None):
    """把历史渲染成带说话人的文本。时间从早到晚，最后一条是刚收到的。

    **为什么不能拿不到 sender 就统统写「对方」**：群聊里那条静默判定的依据
    就是「这话是谁说的」。全部塌成一个「对方」，模型看到的就是「对方」「对方」
    接不上话的碎句子，判断等于塌了；单聊里「对方」还说得通，群聊里就是错的。
    几种真实来源的字段不一样，所以这里按可靠性依次取：
      1. sender_name / last_sender_display_name —— 微信自己算好的发言人显示名
         （群里是群昵称，比拿 wxid 去查联系人表准），SessionTable 那条路带这个字段；
      2. sender（wxid）→ names 里查显示名 —— fts 那条路带这个字段；
      3. 都取不到：按顺序编号「对方1/对方2…」。编号是**可区分**的兜底，
         至少不会把两个人当成同一个人；原始 wxid 一个都不许进文本（CLAUDE.md）。
    出现第 3 种情况时把说明写进 `unknown_note`（一个 list），由调用方放在
    **聊天记录之外**——那段说明是给模型看的格式提示，不是群里谁说的话，
    插进记录中间会变成一条不存在的「消息」。返回拼接好的历史文本（无内容返回 ""）。
    """
    if unknown_note is None:
        unknown_note = []
    lines = []
    unknown = 0
    for m in (msgs or [])[-int(limit):]:
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if m.get("is_self"):
            who = "我"
        else:
            disp = str(m.get("sender_name") or m.get("last_sender_display_name")
                       or "").strip()
            if not disp or _is_wxid(disp):     # 显示名缺失，或字段里塞的是原始 id
                disp = str(names.get(str(m.get("sender") or "")) or "").strip()
            if not disp or _is_wxid(disp):
                unknown += 1
                who = f"对方{unknown}"
            else:
                who = disp
        lines.append(f"[{m.get('time', '?')}] {who}: {content}")
    if unknown and not unknown_note:
        unknown_note.append(
            f"（注意：上面有 {unknown} 条消息查不到发言人的显示名，只能按先后顺序"
            f"编成「对方1/对方2」。编号只代表第几个说话的人，"
            f"**同一个编号不代表同一个人**。）")
    return "\n".join(lines)


def sanitize(text, max_chars=200):
    """把模型输出洗成能直接发进微信的一条消息。洗空了返回 ""（调用方据此静默）。"""
    if text is None:
        return ""
    t = str(text).strip()
    if not t:
        return ""
    t = _FENCE.sub(lambda m: m.group(1), t)      # 去代码围栏，保留围栏里的字
    t = _LEAD.sub("", t.strip())
    t = t.replace("\r", " ").replace("\n", " ")
    t = _MD.sub("", t)
    t = t.strip().strip('"').strip("'").strip("“”").strip("「」")
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    try:
        limit = max(1, int(max_chars))
    except (TypeError, ValueError):
        limit = 200
    if len(t) > limit:
        t = t[:limit].rstrip() + "…"
    return t


def _json_reply(raw):
    """按 JSON 取值。返回 (是否取值成功, 值)；值 None = 模型明确说「不回」。

    只认**整段就是一个 JSON 对象**的形态（可以套 ``` 围栏、前后带空白）：
    模型真按协议输出时就是这么给的。前后还夹着人话的（「看这个 {"a":1} 的例子」）
    不算——那种情况调用方会把整段当直接回复，不能把中间的字典当成协议。
    """
    t = _FENCE.sub(lambda m: m.group(1), str(raw)).strip().lstrip("\ufeff")
    if not (t.startswith("{") and t.endswith("}")):
        return False, None
    try:
        data = json.loads(t)
    except (ValueError, TypeError):
        # 有些模型会吐 Python 字面量（单引号：{'reply': '好'}）。那也是一次
        # **明确的 JSON 协议回复**——解析失败就静默的话，用户这句话又被白吞了，
        # 正是这次要修的毛病。literal_eval 只认字面量、不执行代码，安全。
        try:
            data = ast.literal_eval(t)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return False, None
    if not isinstance(data, dict):
        return False, None
    return True, data.get("reply")


def _extract_group_reply(raw):
    """从群聊回复里取出内容。返回 None 表示静默。

    **「模型明确说不回」和「格式没按 JSON 走」是两件事**，不能都当静默：
      * 模型确实按协议给了 JSON 对象（整段一个对象，可套 ``` 围栏）时：
        `reply` 有内容 → 就发它；`reply` 是 null/空 → 明确静默；
      * 模型没按 JSON 走（`价格是 {100} 元，回头聊`、`看这个 {"a":1} 的例子`、
        干脆一段白话）→ 整段当直接回复，交给上层 sanitize 清洗后照发。
    以前是「见到花括号就 json.loads，失败即 SILENT」，于是模型只要没按 JSON 输出，
    该回的话就被吞掉（实测 `价格是 {100} 元，回头聊` 直接静默）。CLAUDE.md 说的
    「宁可少回」指的是**判断不该接话**，不是**解析失败**——这两者不能混。
    """
    if not raw:
        return SILENT
    t = _FENCE.sub(lambda m: m.group(1), str(raw)).strip()
    if not t:
        return SILENT
    shaped = t.startswith("{") and t.endswith("}")
    if shaped or _REPLY_KEY.search(t):
        ok_json, val = _json_reply(t)
        if ok_json:
            if val is None or not str(val).strip():
                return SILENT      # reply 为 null / 空串 = 明确不回，也别发空消息
            return str(val)
        if shaped:
            # 整段就是一个对象、却不是我们认的 reply 协议（比如 {"other":1}
            # 或者压根没解析出来）。这时**不能**把原始 JSON 当回复发出去——
            # 对方会收到一串花括号。这是「模型在走协议但走歪了」，按原设计静默。
            return SILENT
        if t.find("{") < 0:
            # 提到了 reply 字段、却连左花括号都没有（模型没打算走 JSON，
            # 只是话里带了 reply 这个词）。按「没按 JSON 走」处理：整段照发。
            return t
        # 有花括号、却不是「整段一个对象」、也解析不出来（`{"reply": ` 这种半截）。
        # 这种半截文本发出去只会让人看不懂，按 fail-safe 静默。
        return SILENT
    # 没有 reply 字段：这不是协议回复，是模型直接说了一句话
    return t


# 人设（语气）文本的硬上限。这段会被**整段**塞进每次自动回复的 system prompt，
# 所以太长既贵、又会压过 _COMMON_RULES 里「简短、只输出消息」那些要求。
# 超了**如实拒绝**，绝不静默截断：截断一段描述行为的话，可能正好把
# 「不确定的事别编」那半句切掉，而用户以为自己说的话全生效了。
PERSONA_MAX = 300

# 「清空人设」的写法：整段文本**等于**其中之一才算（不做子串匹配，
# 否则「别用默认那种语气」会被当成清空指令）。
_PERSONA_CLEAR = ("清空", "清除", "重置", "恢复默认", "默认", "clear", "reset", "none")

# 全局范围词。和 agent_tools.t_auto_reply 里 review 用的是同一套写法——
# 「全局」在这个项目里一向是**范围词**，不是某个人的昵称。
_GLOBAL_WORDS = ("全局", "所有", "全部", "默认", "all", "*")

# 「学语气」的写法：整段文本**等于**其中之一才算（和 _PERSONA_CLEAR 同一姿势：
# 不做子串匹配，否则「别学我那么客气」会被当成学习指令）。
_PERSONA_LEARN = ("学习", "重新学习", "再学习", "学一下", "重新学", "learn", "relearn")

# ---- 从历史对话里学语气 ----
LEARN_SAMPLE = 200        # 默认取多少条**自己发出去的**话当样本（config: auto_reply.learn_sample）
LEARN_MIN_MINE = 5        # 少于这么多条就学不出语气，**如实拒绝**、不硬编一段
_LEARN_TARGET = 150       # 让模型往多少字写（软目标）；PERSONA_MAX 仍是硬上限

_LEARN_SYSTEM = (
    "你是语气与称呼分析师。下面给你的是「我」在微信里跟**某一个人**聊天时、"
    "我自己发出去的消息。请从这些样本里总结两件事，并按要求输出。\n"
    "要求输出**严格 JSON**，只有这两个字段：\n"
    '{"address": "我平时对 TA 的称呼", "persona": "一整段人设"}\n'
    "· address：我**叫**对方的那个词（「老张」「张哥」「宝贝」这类）。"
    "**只填称呼本身**——不要填备注、全名、关系说明（不写「朋友」「同事」），"
    "也不要填对方怎么叫我。样本里看不出固定称呼、或者我压根不叫称呼"
    "（直接说事），就写空串 \"\"。\n"
    f"· persona：一段**可以直接当人设使用**的角色设定，{_LEARN_TARGET} 字以内。"
    "用第二人称写给一个要代替我回消息的 AI（「你正在代替我…」这种口吻），"
    "**必须把这些底线一并写进去**：用第一人称、口语简短、像平时打微信那样、"
    "不要用书面语/列表/markdown、不要暴露自己是 AI、不确定的事别编、"
    "不要复述对方刚说过的话。在这之上把语气特征写清楚：正式还是随便、"
    "句子长短、爱不爱开玩笑、常用口头语等。\n"
    "⚠️ 常用口头语里的表情**必须保留微信的方括号写法**（写「[捂脸]」这种，"
    "不要写成裸词「捂脸」——裸词发到对方那儿只是两个汉字，表情等于没发）。\n"
    "除了这个 JSON 什么都不要输出，不要解释、不要 markdown 围栏。\n"
    "样本里只有我单方面发出去的话（没有对方的消息），据此推断即可，"
    "不要编造事实、不要凭空猜人名或关系。\n"
    '例子：{"address": "老张", "persona": "你正在代替我本人回复微信消息…"}'
)

# 模型有时会自己加个「人设：」的前缀，剥掉。
_LEAD_LABEL = re.compile(r"^(人设|设定|语气设定|语气|persona)\s*[:：]\s*", re.I)
_SENT_END = "。！？；.!?;"


def _clean_persona(text):
    """人设描述压成一行：换行/连续空格折成一个空格。

    微信消息里可以有换行，而这段会进 system prompt；折成一行既好存也好显示，
    不影响语义（人设是散文，不是格式）。
    """
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _persona_too_long(text):
    """太长就返回一句**如实拒绝**的话，否则返回 None。"""
    n = len(_clean_persona(text))
    if n <= PERSONA_MAX:
        return None
    return (f"人设太长了（{n} 字，上限 {PERSONA_MAX} 字）。这段会被整段塞进每次"
            f"自动回复的提示里，太长会压过「简短口语」那些要求——请压缩后再发。")


def _short(text, n=24):
    """状态页里的一行摘要，长了截断（只影响显示，不动存的原文）。"""
    t = _clean_persona(text)
    return t if len(t) <= n else t[:n] + "…"


def persona_for(rec, auto_cfg):
    """这个人设：聊天单独配的 > 全局对应 mode 的 > 代码里的兜底。

    **单条是「整体替换」，不是叠加**（用户 2026-10-01 定的）：给某人设了 persona，
    就整段用它，不再拼全局那份。原因是叠加会让「你写的话」和「代码写的话」
    优先级纠缠不清，出问题极难查；替换至少行为是确定的。

    代价必须说清：替换之后，_DEFAULT_SELF 里那句「不要暴露你是 AI」也没了。
    所以**工具层要求模型把一句大白话补成一段完整人设**（含该有的底线），
    而不是只把「随便点」三个字塞进来——见 agent_tools 里 auto_reply 的说明。
    """
    own = str(rec.get("persona") or "").strip()
    if own:
        return own
    mode = str(rec.get("mode") or "self").lower()
    if mode == "assistant":
        return str(auto_cfg.get("persona_assistant") or "").strip() or _DEFAULT_ASSISTANT
    return str(auto_cfg.get("persona_self") or "").strip() or _DEFAULT_SELF


def _clean_address(text):
    """把称呼洗成一个「词」。

    称呼是**要叫出口**的东西，所以必须短、单行、不带引号书名号——
    这些都可能在拼进提示词时把结构搞乱，或者让模型照抄一堆符号叫出来。
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = t.strip().strip('"').strip("'").strip("“”").strip("「」").strip("《》")
    t = t.strip("[]【】()（）").strip()
    return t


def _address_too_long(text):
    """称呼太长就返回一句如实拒绝的话，否则 None。"""
    t = _clean_address(text)
    if len(t) <= _ADDRESS_MAX:
        return None
    return (f"称呼太长了（{len(t)} 字，上限 {_ADDRESS_MAX} 字）。称呼是**要叫出口的那个词**"
            f"（像「老张」「张哥」），不是一句话——请只给称呼本身。")


def address_for(rec):
    """这个人我平时怎么叫。空串 = 没有固定称呼（回复时就不提称呼）。"""
    return _clean_address((rec or {}).get("address"))


def address_rule(addr):
    """把称呼渲染成给模型的一段约束（独立成段，见 _ADDRESS_RULE 的注释）。"""
    return _ADDRESS_RULE.format(addr=addr)


# 称呼的独立存储（2026-10-04：和自动回复名单解绑）
#
# 用户 2026-10-04 拍的：**称呼和自动回复必须分开**。
# 以前称呼借住在 `auto_reply.chats[].address` 里，而 `chats` 就是「自动回复名单」，
# 于是「给某人设/学称呼」先得把他加进名单——名单外的人（比如群友）根本做不到，
# 顺手加人又等于替用户决定要不要让 AI 代他回话（CLAUDE.md 明令禁止）。
#
# 现在称呼有自己的家：`settings.json` 顶层 `addresses`
#     {"wxid_xxx": {"address": "老张", "name": "张三",
#                   "source": "manual|learned", "at": 1234567890}}
# 规矩：
#   * **读**只走 `address_of()`（唯一出口）；**写**只走 `set_address()` / `clear_address()`；
#   * 旧记录里的 `chats[].address` **只作只读兜底**（2026-10-04 之前写进去的），
#     任何一次写入都会把它清掉，所以它只会越来越少、不会和称呼表打架；
#   * 人设（persona）**仍然只对名单里的人生效**——那是「替你回话」的语气，
#     和「你平时怎么叫他」是两件事，别把这条也一起放开。

def address_book(cfg=None):
    """`{wxid: {"address","name","source","at"}}`。cfg 里没有就回落到 settings.json。"""
    src = (cfg or {}).get("addresses")
    if not isinstance(src, dict):
        src = settings.load().get("addresses")
    if not isinstance(src, dict):
        return {}
    out = {}
    for wxid, rec in src.items():
        if not isinstance(rec, dict):
            continue
        a = _clean_address(rec.get("address"))
        if not a:
            continue
        out[str(wxid)] = {"address": a,
                          "name": str(rec.get("name") or "").strip(),
                          "source": str(rec.get("source") or "manual"),
                          "at": rec.get("at")}
    return out


def address_record(cfg, wxid):
    """这个人在称呼表里的那一条（没有就 None）。"""
    return address_book(cfg).get(str(wxid or ""))


def address_of(cfg, wxid, rec=None):
    """这个人我平时怎么叫。**唯一读取出口**。

    顺序：独立称呼表 > 旧记录 `chats[].address`（只读兜底）。
    传 `rec` 只是为了兜住旧数据；新代码不必传。
    """
    hit = address_record(cfg, wxid)
    if hit:
        return hit["address"]
    return address_for(rec)


def set_address(wxid, addr, source="manual", name=""):
    """写称呼（唯一写入口）。`addr` 空串 = 删掉这一条（= 没称呼）。

    基准取自**磁盘现值**（不是传进来的 cfg）：cfg 可能是上一条命令之前的快照。
    """
    wxid = str(wxid or "").strip()
    if not wxid:
        return False
    book = settings.load().get("addresses")
    data = dict(book) if isinstance(book, dict) else {}
    a = _clean_address(addr)
    if not a:
        data.pop(wxid, None)
    else:
        old = data.get(wxid) if isinstance(data.get(wxid), dict) else {}
        data[wxid] = {"address": a,
                      "name": str(name or old.get("name") or "").strip(),
                      "source": str(source or "manual"),
                      "at": int(time.time())}
    settings.set_value("addresses", data or None)
    return True


def clear_address(wxid):
    """删掉这个人的称呼（不会碰到自动回复名单）。"""
    return set_address(wxid, "")


def address_aliases(cfg):
    """`{称呼: [候选, ...]}` —— 给联系人解析当别名用（`resolve_contacts`）。

    **一个称呼可能对上多个人**（两个人都被叫「老张」），所以值是候选列表，
    调用方必须照旧走重名保护，**绝不能静默取第一个**（CLAUDE.md 铁律）。

    两个来源，**称呼表为准**：
      1. `settings.json` 的 `addresses`（2026-10-04 起唯一的写入目标）；
      2. 旧记录 `auto_reply.chats[].address`（只读兜底，同一 wxid 已被称呼表
         覆盖时就不再看它）。
    """
    out = {}

    def add(a, wxid, name):
        if not a:
            return
        out.setdefault(a, []).append({
            "wxid": wxid,
            "name": name or wxid,
            "remark": name or "",
            "alias": "",
            "_by": "称呼",
        })

    book = address_book(cfg)
    for wxid, item in book.items():
        add(item["address"], wxid, item.get("name"))
    for r in chat_list(cfg):
        wxid = str(r.get("wxid") or "")
        if wxid in book:
            continue
        add(address_for(r), wxid, r.get("name"))
    return out


def make_reply(llm, rec, msgs, names, cfg, group=False):
    """生成一条可直接发送的回复。返回 "" 表示不该发（静默）。"""
    auto_cfg = cfg.get("auto_reply") or {}
    notes = []
    transcript = build_transcript(msgs, names, auto_cfg.get("context_messages", 20),
                                 unknown_note=notes)
    if not transcript:
        return ""

    system = persona_for(rec, auto_cfg) + "\n" + _COMMON_RULES
    # 称呼**独立于 persona** 注入：用户重写人设、或者手写一份时，称呼不该跟着丢。
    # 它也不要求这个人在自动回复名单里（2026-10-04 解绑），所以走 address_of。
    addr = address_of(cfg, (rec or {}).get("wxid"), rec)
    if addr:
        system += address_rule(addr)
    if group:
        system += _GROUP_RULES
        ask = "按要求的 JSON 输出，判断最后一条消息是否需要我回应。"
    else:
        ask = "请回复最后一条消息。"

    # 发言人缺失的说明放在聊天记录**之外**（记录之后、提问之前）：
    # 它是给模型的格式提示，不是群里谁说的话，混进记录会变成一条假消息。
    note = ("\n".join(notes) + "\n\n") if notes else ""
    prompt = ("【最近的聊天记录】（时间从早到晚，最后一条是刚收到的）\n"
              f"{transcript}\n\n{note}{ask}")
    raw = llm.chat(system, [{"role": "user", "content": prompt}])
    picked = _extract_group_reply(raw) if group else raw
    return sanitize(picked, auto_cfg.get("max_reply_chars", 200))


# 配置读写（/auto 命令）

def section(cfg):
    return dict(cfg.get("auto_reply") or {})


def chats(cfg):
    """{wxid: 条目}。条目按引用返回，命令里改完再 _save。"""
    out = {}
    for c in (section(cfg).get("chats") or []):
        if isinstance(c, dict) and str(c.get("wxid") or "").strip():
            out[str(c["wxid"])] = c
    return out


def chat_list(cfg):
    return list(chats(cfg).values())


def enabled(cfg):
    return bool(section(cfg).get("enabled"))


def review_on(rec, cfg):
    """这个人设用不用审核：聊天单独配的优先，否则看全局。"""
    own = rec.get("review")
    return bool(section(cfg).get("review") if own is None else own)


# 只有这几个键由命令管理、也**只把**它们写进 settings.json。
# min_gap / max_reply_chars 留在 config.yaml：settings.json 里
# 存一份副本的话，用户之后改 config.yaml 就再也不生效了。
#
# persona_self / persona_assistant 是 2026-10-01 加进来的（「/auto persona 全局」）：
# 用户用大白话改默认语气时，**只能**落到 settings.json——总不能替他去改
# config.yaml（那里面有注释，程序回写会把注释冲掉）。代价同样是「存过一次之后
# 改 config.yaml 就不再生效」，所以配置注释和 CLAUDE.md 里都写明了这一点。
_MANAGED = ("enabled", "review", "chats", "context_messages",
            "persona_self", "persona_assistant")


def _save(_unset=(), **changes):
    """把改动写进 settings.json 的 auto_reply 段（只写命令管的键）。

    settings.effective() 对 dict 做一层深合并，所以这里不需要写整段——
    config.yaml 里的 min_gap / max_reply_chars 等默认值仍然会生效。

    基准取自磁盘上的现值（不是传进来的 cfg）：cfg 可能是上一条命令之前的
    快照，用它当基准会把上一条命令的改动丢掉。

    `_unset` 是**删键**（不是写空串）：全局人设写 "" 会盖住 config.yaml 里那份，
    于是「恢复默认」反而变成「什么都不生效、只剩代码兜底」。删掉键才能真的
    退回 config.yaml。
    """
    saved = settings.load().get("auto_reply")
    data = dict(saved) if isinstance(saved, dict) else {}
    data.update(changes)
    for k in _unset:
        data.pop(k, None)
    settings.set_value("auto_reply", {k: v for k, v in data.items() if k in _MANAGED})


def _split_target(rest):
    """'张三 self' / '张三' / 'wxid_xxx' -> (目标, mode 或 None)。

    昵称可能带空格，所以只把**最后一个词**是 self/assistant 时才当 mode 剥掉。
    """
    parts = str(rest or "").split()
    mode = None
    if len(parts) >= 2 and parts[-1].lower() in _MODE_WORDS:
        mode = parts[-1].lower()
        parts = parts[:-1]
    return " ".join(parts).strip(), mode


def _resolve(client, who, can_lookup=True, cfg=None):
    """昵称/备注/**称呼**/wxid/roomid -> (wxid, 显示名, 候选列表)。

    学到的「称呼」也是一种名字，所以先查自己的称呼表。它和库里查到的结果是
    **合并**（不是谁覆盖谁）：万一另一个人备注真叫「老张」，两边都要摆出来
    让调用方按既有的重名规则去问，**绝不能静默挑一个**（那会写错人）。
    这也让「没连上库」（can_lookup=False）时靠称呼仍然解析得出来。
    """
    who = str(who or "").strip()
    if not who:
        return None, None, []
    if who.endswith("@chatroom"):
        return who, who, [{"wxid": who}]
    if who.lower().startswith("wxid_") or who == "filehelper":
        return who, who, [{"wxid": who}]

    cands = list((address_aliases(cfg) if cfg is not None else {}).get(who) or [])
    seen = {str(c.get("wxid")) for c in cands}
    if can_lookup:
        try:
            for c in live_history.resolve_contact(client, who, limit=5):
                if str(c.get("wxid")) not in seen:
                    seen.add(str(c.get("wxid")))
                    cands.append(c)
        except Exception:
            pass
    if not cands:
        return None, None, []
    c = cands[0]
    return str(c.get("wxid")), str(c.get("remark") or c.get("name") or c.get("wxid")), cands


def _find(recs, who, cfg=None):
    """按 wxid 精确、显示名精确、称呼精确、最后名字包含，找一条记录。

    **称呼也要认**：用户心里那个人就叫「老张」，让他为了改审核/身份先想起
    这个人在微信里备注成「张三」，是没道理的。
    称呼重名（两个人都被叫「老张」）时返回 None、**不静默挑一个**——
    和下面「名字包含」那条的既有规矩一致，宁可让用户说清楚。
    """
    who = str(who or "").strip()
    if not who:
        return None
    for r in recs:
        if str(r.get("wxid")) == who:
            return r
    for r in recs:
        if str(r.get("name") or "") == who:
            return r
    addr_of = (lambda r: address_of(cfg, r.get("wxid"), r)) if cfg is not None \
        else address_for
    by_addr = [r for r in recs if addr_of(r) and addr_of(r) == who]
    if len(by_addr) == 1:
        return by_addr[0]
    if len(by_addr) > 1:
        return None
    hits = [r for r in recs if who in str(r.get("name") or "")]
    return hits[0] if len(hits) == 1 else None


def _persona_disk_override(key):
    """settings.json 里存过这个人设键没有（用来在状态里说清来源）。"""
    saved = settings.load().get("auto_reply")
    if not isinstance(saved, dict):
        return False
    return key in saved


def _split_rec_target(rest, recs, cfg=None):
    """把 `<谁> <内容>` 的 rest 拆成 (记录, 是否全局, mode, 内容)。

    persona 和 address 两条命令共用它——两边的「谁」是同一套名字。
    返回的**内容为 None = 目标没认出来**（调用方据此报「名单里没有…」）；
    内容为空串 = 没给内容（调用方按「查看当前值」处理）。

    为什么不用 `_split_target`：人设描述 / 称呼后面**必然带空格**
    （「跟张三说话随便点」），根本没法用一个规则判断名字到哪儿结束。
    但这两样只对**已在名单里的人**有意义，而名单是已知的——于是「名字」这一侧
    可以穷举，改成**最长前缀匹配**：先试最长的，`张三` 和 `张三丰` 同时存在时
    也不会认错人。匹配要求 key 后面**跟着空格**（或整段就是 key），
    所以「张三丰」不会被「张三」吃掉。
    **`address` 也算名字**：用户心里那个人就叫「老张」，让他为了改称呼
    先想起这个人在微信里备注成「张三」，是没道理的。

    全局那一支：`<命令> 全局 [self|assistant] <内容>`。名单里真有个人的
    显示名就叫「全局」时**他优先**（显式对象胜过范围词），和 review 那条一致。
    """
    rest = str(rest or "").strip()
    if not rest:
        return None, False, None, ""

    best = None
    for r in recs:
        for key in (str(r.get("name") or "").strip(),
                    str(r.get("wxid") or "").strip(),
                    address_of(cfg, r.get("wxid"), r) if cfg is not None
                    else address_for(r)):
            if not key:
                continue
            if rest == key or rest.startswith(key + " "):
                if best is None or len(key) > len(best[1]):
                    best = (r, key)
    if best is not None:
        return best[0], False, None, rest[len(best[1]):].strip()

    parts = rest.split(maxsplit=1)
    if parts[0].strip().lower() in _GLOBAL_WORDS:
        tail = parts[1].strip() if len(parts) > 1 else ""
        mode = None
        if tail:
            bits = tail.split(maxsplit=1)
            if bits[0].lower() in _MODE_WORDS:
                mode = bits[0].lower()
                tail = bits[1].strip() if len(bits) > 1 else ""
        return None, True, mode, tail
    return None, False, None, None


def _split_address_target(rest, cfg, client, can_lookup, name_hint=None):
    """`<谁> <称呼>` 里，**谁可能不在自动回复名单里**（2026-10-04 解绑）。

    名单里的人有 `_split_rec_target` 那张可枚举的名字表（名单是已知的）；
    名单外的人没有，所以改用「**最长前缀能唯一认出一个联系人**」来切：

        李四 阿四            → 前缀「李四」认出来 → 称呼=「阿四」
        Johny 黄 儿子 阿黄    → 前缀「Johny 黄 儿子」→ 称呼=「阿黄」
        wxid_xxx 阿四        → 前缀「wxid_xxx」（id 直接透传）

    探针有上限（最多试 5 个词的前缀），因为每探一次就是一次联系人查询——
    这条路是给人手打命令用的，不该因为它把 hook 压上去。
    `can_lookup=False`（没连上库、也没在称呼表里）时只能认 wxid/roomid。

    返回 `(wxid, 显示名, 内容, 错误文本)`；错误文本非空 = 调用方原样回给用户。
    前缀**匹配到多个人**时如实报重名、**绝不挑一个**（CLAUDE.md 铁律）。
    """
    words = str(rest or "").split()
    if not words:
        return None, "", "", "用法：/auto address <昵称|wxid> [称呼]"
    for k in range(min(len(words) - 1, 5), 0, -1):
        prefix = " ".join(words[:k])
        tail = " ".join(words[k:])
        wxid, disp, cands = _resolve(client, prefix, can_lookup, cfg=cfg)
        if not wxid:
            continue
        if len(cands) > 1:
            names = "；".join(f"{c.get('remark') or c.get('name')}({c.get('wxid')})"
                              for c in cands[:5])
            return None, "", "", (f"「{prefix}」匹配到多个人，请用更全的名字、"
                                  f"或者直接给 wxid：\n{names}")
        return wxid, str(name_hint or disp or wxid), tail, ""

    # 整串就是一个人的名字 = 不带称呼的「查看 / 学 / 清空」用法
    wxid, disp, cands = _resolve(client, rest, can_lookup, cfg=cfg)
    if wxid and len(cands) == 1:
        return wxid, str(name_hint or disp or wxid), "", ""
    return (None, "", "",
            (f"没认出「{rest}」里哪一段是人名。用法：/auto address <昵称|wxid> [称呼]；"
             f"名字带空格也行，例：/auto address Johny 黄 儿子 阿黄"))


def _persona_clear(text):
    """这段文本是不是「清空人设」指令（**整段相等**，不做子串匹配）。"""
    t = str(text or "").strip().lower()
    return t in {c.lower() for c in _PERSONA_CLEAR}


def _persona_learn(text):
    """这段文本是不是「学语气」指令（同样整段相等）。"""
    t = str(text or "").strip().lower()
    return t in {c.lower() for c in _PERSONA_LEARN}


# 从历史对话里学语气

def _is_text_msg(m):
    """这条历史是不是一条**文本**消息。

    图片/文件/表情在 live_history 里会被渲染成 `[图片]` 这类标签，**内容看上去
    和真文本没区别**，拿它去学语气就是喂噪声。判据只能用 `local_type`：
    `_v4_history_from_tables` 一直带这个字段，`_v4_fts_rows`（生产主路径）
    2026-10-01 才补上——**别按 `content` 是不是以 `[` 开头来判**，手打的
    「[呲牙]」也是真文本，那样会误删。

    取不到 local_type 时**当文本**：宁可多带一句，也比整段学不到强。
    """
    lt = (m or {}).get("local_type")
    if lt is None:
        return True
    try:
        return int(lt) == 1
    except (TypeError, ValueError):
        return True


def _learn_messages(msgs):
    """挑出**我自己发出去的**文本——要复制的是「我对这个人怎么说话」。

    **只送我自己那一侧**：对方的话对「我的语气」没有信息量，还白多送一份隐私
    给模型服务商（用户 2026-10-01 定的）。
    """
    out = []
    for m in msgs or []:
        if not m.get("is_self"):
            continue
        if not _is_text_msg(m):
            continue
        t = str(m.get("content") or "").strip()
        if t:
            out.append(f"[{m.get('time', '?')}] {t}")
    return out


def _clean_learned(raw):
    """把模型返回的整段文字洗成人设文本（**不是**消息，别用 sanitize）。"""
    t = _FENCE.sub(lambda m: m.group(1), str(raw or "")).strip()
    t = _LEAD_LABEL.sub("", t).strip()
    t = t.strip().strip('"').strip("'").strip("“”").strip("「」")
    return _clean_persona(t)


def _cut_at_sentence(text, limit):
    """超长时按句末标点截到 limit 以内（截不到就在 limit 处硬截）。"""
    head = text[:limit]
    cut = max(head.rfind(ch) for ch in _SENT_END)
    return (head[:cut + 1] if cut >= limit // 2 else head).rstrip()


def _learn_result(raw):
    """解析学习输出，返回 `(address, persona)`。

    * 认出 JSON（有 `persona` 键）→ `persona` 取正文，`address` 取值：
      拿到了就是字符串（可能是空串 = 模型明确说「没有固定称呼」），
      **字段缺失/是 null 时给 `None`**（意思是「这次别动已有的称呼」）；
    * 没按 JSON 走（模型还是只给了一段散文）→ **整段当人设**、`address=None`。

    **绝不能因为解析不出来就把整次学习判成失败**：人设那条路在加称呼之前一直是好的，
    加个字段不该让它变脆——所以 JSON 认不出就退回「原文当人设」。
    也**不许**从散文里猜一个称呼（那等于编），猜错比没有严重得多（会叫错人）。
    """
    t = _FENCE.sub(lambda m: m.group(1), str(raw or "")).strip().lstrip("\ufeff")
    if t.startswith("{") and t.endswith("}"):
        data = None
        try:
            data = json.loads(t)
        except (ValueError, TypeError):
            # 有些模型吐 Python 字面量（单引号）。literal_eval 只认字面量、不执行代码。
            try:
                data = ast.literal_eval(t)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                data = None
        if isinstance(data, dict) and "persona" in data:
            addr = data.get("address")
            addr = None if addr is None else str(addr).strip()
            return addr, _clean_learned(data.get("persona"))
    return None, _clean_learned(t)


def learn_persona(client, llm, rec, cfg, sample=None):
    """从我和这个人的历史对话里学出**语气 + 称呼**。

    返回 `(ok, data, message)`：

    * `ok=False` → `data` 只有 `n`（用了几条样本），`message` 是**如实说明为什么没学成**；
    * `ok=True`  → `data = {"persona": str, "address": str|None, "n": int}`；
      `message` 只放需要额外告知的话（比如「太长已经压过」）。
      `address=None` 表示**这次没拿到称呼，调用方不许动原来那份**；
      `address=""` 是模型明确说「没有固定称呼」，那是有效的学习结果。

    一次调用同时给两样（用户 2026-10-01 要的），所以**不额外多一次读库/模型调用**。
    成本就是原来的**一次读库 + 一次模型调用**，跑在收消息那条线程上——`do_auto_reply`
    本来就在同一条线程上调模型，不是新引入的并发风险。读库只调一次
    `live_history.query_contact_history`（带会话过滤取 N 条，文档里的快路径），
    绝不裸拼 SQL。
    """
    who = str((rec or {}).get("wxid") or "").strip()
    name = str((rec or {}).get("name") or who or "").strip()
    if not who:
        return False, {"n": 0}, "没指定是谁，学不了。"
    if client is None:
        return False, {"n": 0}, "现在读不了聊天记录（没连上微信），这次没学成。"
    if llm is None:
        return (False, {"n": 0},
                f"没配 API Key，学不了——先发 /api <key> 配好模型，"
                f"再发 /auto persona {name} 学习。")

    try:
        n = int(sample if sample is not None else (section(cfg).get("learn_sample")
                                                   or LEARN_SAMPLE))
    except (TypeError, ValueError):
        n = LEARN_SAMPLE
    n = max(LEARN_MIN_MINE, min(2000, n))

    try:
        msgs = live_history.query_contact_history(client, who, limit=n)
    except Exception as e:
        return False, {"n": 0}, f"读聊天记录失败（{type(e).__name__}: {e}），这次没学成。"

    lines = _learn_messages(msgs)
    if len(lines) < LEARN_MIN_MINE:
        return (False, {"n": len(lines)},
                f"{name} 这里我只找到 {len(lines)} 条**你自己**发的话（少于 "
                f"{LEARN_MIN_MINE} 条学不出语气），先按默认人设回。"
                f"想学的话先多聊几句，再发 /auto persona {name} 学习。")

    prompt = "【我自己发出去的消息】（时间从早到晚）\n" + "\n".join(lines[-n:])
    try:
        raw = llm.chat(_LEARN_SYSTEM, [{"role": "user", "content": prompt}])
    except Exception as e:
        return False, {"n": len(lines)}, f"模型调用失败（{type(e).__name__}: {e}），这次没学成。"

    addr, text = _learn_result(raw)
    if not text:
        return (False, {"n": len(lines)},
                "模型这次没学出可用的语气（返回是空的），没动你的人设。")

    notes = []
    if len(text) > PERSONA_MAX:
        cut = _cut_at_sentence(text, PERSONA_MAX)
        notes.append(f"⚠️ 学出来的人设太长（{len(text)} 字，上限 {PERSONA_MAX}），"
                     f"已按句末标点压到 {len(cut)} 字。")
        text = cut
    if addr is not None:
        addr = _clean_address(addr)
        if len(addr) > _ADDRESS_MAX:
            # 称呼是**要叫出口**的词，长了就不是称呼。宁可当没有（并说出来），
            # 也不能截一半存下去——那会叫出一个用户从没说过的半截称呼。
            notes.append(f"⚠️ 模型给的称呼太长（{len(addr)} 字，上限 {_ADDRESS_MAX}），"
                         f"已按「没有固定称呼」处理，没动称呼。")
            addr = None
    return True, {"persona": text, "address": addr, "n": len(lines)}, "\n".join(notes)


def _store_learned(rec, data, fields, n):
    """把学到的**人设 / 称呼**连同来源一起记进去。

    来源必须记：不然「学到的」和「你手写的」在状态里长得一样，
    用户根本分不清手上这一段是谁写的，也不知道重学会不会盖掉自己的东西。

    **只写 `fields` 里点名的字段**：`/auto address 谁 学习` 只学称呼时，
    绝不能顺手把人设的 `persona_source` 从「你手写的」改成「学到的」——
    那等于把来源记脏，用户以后再也分不清哪段是自己写的。

    `data["address"] is None` = 这次没拿到称呼 → **一个字都不动**（不是清空）。
    """
    if "persona" in fields:
        rec["persona"] = data["persona"]
        rec["persona_source"] = "learned"
        rec["persona_at"] = int(time.time())
        rec["persona_n"] = int(n)
    if "address" in fields and data["address"] is not None:
        # ⚠️ 称呼**不再写回聊天记录**（2026-10-04 解绑）：它有自己那份存储，
        # 写这儿等于又造了第二个真源。顺手把旧字段清掉，旧数据只会越来越少。
        set_address(rec.get("wxid"), data["address"], "learned",
                    name=rec.get("name"))
        rec.pop("address", None)
        rec.pop("address_source", None)


def _maybe_learn(rec, recs, cfg, client, llm_factory, force, fields=None):
    """按规矩决定要不要学，返回 `(是否改了配置, 要追加给用户的一段话)`。

    规矩（用户 2026-10-01 定的）：**只有当前没设过时才自动学**；
    已经有（手写的或上次学的）一律不动，要重学必须用户明说 `force=True`。
    这样加人时自动学一次，但绝不会把你精心写的东西默默冲掉。

    `fields` 是要**写回**的字段（默认人设+称呼）。分开是为了支持
    `/auto address 谁 学习`——只学称呼、**一个字都不动人设**。

    学习失败**绝不影响调用方**（加人照样成功）：拿不到模型、没历史、读库出错
    都只回一句真话。
    """
    fields = tuple(fields or ("persona", "address"))
    name = str(rec.get("name") or rec.get("wxid") or "")

    blocked = []
    if "persona" in fields and str(rec.get("persona") or "").strip() and not force:
        blocked.append("人设")
    if "address" in fields and address_of(cfg, rec.get("wxid"), rec) and not force:
        blocked.append("称呼")
    if blocked:
        return False, (f"\n（{'、'.join(blocked)}没动：{name} 已经设过了。"
                       f"想按最近的聊天重学，发 /auto persona {name} 重新学习；"
                       f"只想重学称呼，发 /auto address {name} 学习）")

    if llm_factory is None:
        return False, ("\n（这条链路没接模型，学不了。）" if force else "")

    try:
        llm = llm_factory()
    except Exception as e:
        return False, (f"\n（学习没成：拿不到模型（{type(e).__name__}: {e}），"
                       f"{name} 先用原来的。）")

    ok, data, msg = learn_persona(client, llm, rec, cfg)
    if not ok:
        return False, f"\n（没学成：{msg}）"

    n = data["n"]
    _store_learned(rec, data, fields, n)
    _save(chats=recs)

    out = f"\n已从你们最近 {n} 条**你自己**发的话里学出对{name}的"
    if "persona" in fields:
        out += f"语气：\n{data['persona']}\n"
    else:
        out += "称呼。\n"
    if "address" in fields:
        addr = data["address"]
        if addr:
            out += f"称呼：{addr}\n"
        elif addr == "":
            out += "称呼：（没有固定称呼，回的时候直接说事）\n"
        else:
            out += "称呼：这次没学到，原来那份**没动**。\n"
    if "persona" in fields:
        out += (f"（学到的人设是**整体替换**默认那份；想自己改就直接发 "
                f"/auto persona {name} <描述>）")
    if msg:
        out += f"\n{msg}"
    return True, out


def status_text(cfg):
    sec = section(cfg)
    recs = chat_list(cfg)
    lines = [
        f"自动回复：{'开启' if sec.get('enabled') else '关闭'}",
        f"审核模式：{'开启（草稿先发给你，回「确认」才发出去）' if sec.get('review') else '关闭（直接发给对方）'}",
        (f"上下文 {sec.get('context_messages', 20)} 条  |  "
         f"冷却 {sec.get('min_gap', 6)} 秒  |  单条上限 {sec.get('max_reply_chars', 200)} 字"),
        "人设：每个人可以单独一份（整体替换默认）；没单独设的用全局默认",
        "     看/改：/auto persona <昵称> [描述]   学语气：/auto persona <昵称> 学习",
        "     称呼：/auto address <昵称> [称呼]    学称呼：/auto address <昵称> 学习",
        "     ⚠️ 称呼和自动回复名单**是分开的**：名单外的人也能设/学/清，不必先 /auto add",
        f"名单（{len(recs)}）：",
    ]
    if not recs:
        lines.append("  （空）发 /auto add <昵称|wxid|roomid> [self|assistant] 添加"
                     "（加进来会顺手学一次语气和称呼）")
    for r in recs:
        rev = sec.get("review") if r.get("review") is None else r.get("review")
        kind = "（群）" if is_group(r.get("wxid")) else ""
        own = str(r.get("persona") or "").strip()
        if not own:
            tone = "人设=（默认）"
        elif r.get("persona_source") == "learned":
            tone = f"人设=学到[{r.get('persona_n') or '?'}条]:{_short(own, 14)}"
        else:
            tone = f"人设=你设的:{_short(own, 14)}"
        addr = address_of(cfg, r.get("wxid"), r)
        if addr:
            hit = address_record(cfg, r.get("wxid"))
            src = "学到" if ((hit or {}).get("source")
                            or r.get("address_source")) == "learned" else "你设的"
            tone += f"  称呼={src}:{addr}"
        else:
            tone += "  称呼=（无）"
        lines.append(f"  · {r.get('name') or r.get('wxid')}{kind}  "
                     f"身份={r.get('mode') or 'self'}  审核={'开' if rev else '关'}  {tone}")

    # 称呼**不要求在名单里**（2026-10-04 解绑），所以名单外那些也要看得到——
    # 否则用户设完了在状态里找不到，会以为没生效。
    book = address_book(cfg)
    names = {str(r.get("wxid") or "") for r in recs}
    extra = [(w, v) for w, v in book.items() if w not in names]
    if extra:
        shown = "、".join(f"{v.get('name') or w}＝{v['address']}" for w, v in extra[:8])
        more = f" 等 {len(extra)} 个" if len(extra) > 8 else ""
        lines.append(f"另外 {len(extra)} 个人的称呼（**不在名单里**，只用来称呼）：{shown}{more}")
    return "\n".join(lines)


_USAGE = (
    "用法：\n"
    "/auto                    看状态和名单\n"
    "/auto on | off           开 / 关（关掉就你自己回）\n"
    "/auto add <昵称|wxid|roomid> [self|assistant]\n"
    "/auto del <昵称|wxid>\n"
    "/auto mode <昵称|wxid> self|assistant   身份（self=假装你本人 / assistant=明说是助手）\n"
    "/auto persona <昵称> [描述]   这个人的语气人设；不带描述=看，清空=恢复默认\n"
    "/auto persona <昵称> learn    从你和他的历史对话里学语气+称呼（覆盖已有的）\n"
    "/auto address <昵称> [称呼]   你平时怎么叫他；不带=看，清空=不套称呼\n"
    "/auto address <昵称> learn    只从历史里学称呼，**一个字都不动人设**\n"
    "（中文参数也还能用：学习 / 清空 / 全局 —— 命令词用 /auto 不变）\n"
    "  ⚠️ 称呼**不要求**他在自动回复名单里（名单外的人也能设/学/清）；\n"
    "     名字带空格也行：/auto address Johny 黄 儿子 阿黄\n"
    "/auto persona 全局 [self|assistant] [描述]   没单独设的人用的默认人设\n"
    "/auto review on|off [昵称|wxid]   不带对象则改全局\n"
    "/auto ctx <1~30>         上下文条数\n"
    "（群只能填 roomid，形如 xxx@chatroom）"
)


def summary_line(cfg):
    """一行话的状态摘要，给 agent 工具复述用。"""
    sec = section(cfg)
    recs = chat_list(cfg)
    if not recs:
        who = "名单是空的"
    else:
        names = "、".join(str(r.get("name") or r.get("wxid")) for r in recs[:6])
        more = "" if len(recs) <= 6 else f" 等 {len(recs)} 个"
        who = f"名单：{names}{more}"
    return (f"{'开' if sec.get('enabled') else '关'}着，{who}，"
            f"审核{'开' if sec.get('review') else '关'}")


def build_arg(action, who="", mode=None, review=None, context=None, persona=None,
              address=None):
    """把 agent 工具的结构化参数拼成 /auto 的子命令串。

    让工具和命令走**同一条**实现（handle_command），省得两套逻辑各自跑偏。
    """
    a = str(action or "").strip().lower()
    who = str(who or "").strip()
    if a in ("on", "off", "status", ""):
        return a
    if a == "add":
        return " ".join(x for x in ("add", who, mode) if x)
    if a == "del":
        return f"del {who}".strip()
    if a == "mode":
        return f"mode {who} {mode or 'self'}".strip()
    if a == "review":
        if review is None:
            return "review"
        return f"review {'on' if review else 'off'} {who}".strip()
    if a == "persona":
        text = _clean_persona(persona)
        # who 为空 = 全局（和上面 review 一样：「空 who」就是改全局默认，
        # t_auto_reply 里已经拦过「模型漏参数」那一头）。
        # 全局人设有 self / assistant 两份，所以全局那支才需要带 mode；
        # 单人那份和身份无关（整体替换），带了反而会把 mode 当成描述的一部分。
        if not who:
            return " ".join(x for x in ("persona", "全局", mode if text else None, text)
                            if x)
        return " ".join(x for x in ("persona", who, text) if x)
    if a == "learn":
        # 学语气**按人**学，没有全局那一支（who 为空时 t_auto_reply 已经拦住）。
        return " ".join(x for x in ("persona", who, "重新学习") if x)
    if a == "address":
        text = _clean_address(address)
        return " ".join(x for x in ("address", who, text) if x)
    if a == "ctx":
        if context is None:
            return "ctx"
        try:
            return f"ctx {int(context)}"
        except (TypeError, ValueError):
            return "ctx"
    return a


def handle_command(arg, cfg, client, can_lookup=True, name_hint=None,
                   llm_factory=None):
    """处理 /auto 系列子命令。返回 (回复文本, 是否改了配置)。

    name_hint 是给 agent 工具用的：工具会先把昵称解析成 wxid（它的精确匹配比
    这里严格），再让 `/auto add` 按 wxid 走，显示名就会被记成 wxid_xxx，
    /auto 列表里看不到人名。带上原名当显示名即可。

    llm_factory 是**可选的、懒调用的**模型工厂（`() -> llm`）：只有真要去学语气
    时才调它，所以普通命令不会因为多一个参数就多建一个模型客户端。
    不传 = 这条链路没有学习能力，`/auto persona 谁 学习` 会如实说学不了。
    """
    parts = str(arg or "").split(maxsplit=1)
    sub = parts[0].strip().lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    sec = section(cfg)
    recs = chat_list(cfg)

    if not sub or sub in ("status", "list", "状态", "名单"):
        return status_text(cfg), False

    if sub in ("on", "开", "开启", "启用"):
        _save(enabled=True)
        return "自动回复已开启：名单里的人发消息，我代你回。", True

    if sub in ("off", "关", "关闭", "停"):
        _save(enabled=False)
        return "自动回复已关闭，这些聊天你自己回。", True

    if sub in ("add", "加", "添加"):
        who, mode = _split_target(rest)
        if not who:
            return _USAGE, False
        wxid, disp, cands = _resolve(client, who, can_lookup, cfg=cfg)
        if not wxid:
            hint = ("当前实时查库不可用，只能直接填 wxid（群填 roomid）。"
                    if not can_lookup else
                    f"没找到「{who}」。群请直接填 roomid（xxx@chatroom）。")
            return hint, False
        if len(cands) > 1:
            names = "；".join(f"{c.get('remark') or c.get('name')}({c.get('wxid')})"
                              for c in cands[:5])
            return f"「{who}」匹配到多个人，请用全名或直接给 wxid：\n{names}", False
        if wxid in {str(t) for t in (cfg.get("target_chats") or [])}:
            return (f"{disp} 已经是控制会话（target_chats）了：控制会话是你跟助手说话的地方，"
                    f"不能同时当自动回复对象。请二选一。"), False
        if wxid == "filehelper" or (str(cfg.get("self_wxid") or "") and wxid == str(cfg.get("self_wxid"))):
            return "文件传输助手 / 你自己不能加进自动回复名单（会自己回自己）。", False

        # 和「盯着」名单互斥（watch.py 加人时也查这边，两个方向都要拦）：
        # 两边都有时 bot 的 watched 分支先命中就 continue，自动回复那条**永远
        # 走不到**——用户以为配好了自动回复，实际上人一个也没回过，还很难查。
        import watch
        if wxid in watch.chats(cfg):
            return (f"{disp} 已经在「盯着」名单里了（只通知、不回他）。自动回复是"
                    f"「代你回」，两边同时开既通知又回复，是互斥的——想自动回复就先发"
                    f" /watch del {disp}。"), False

        rec = next((r for r in recs if str(r.get("wxid")) == wxid), None)
        disp = str(name_hint or "").strip() or disp
        if rec is None:
            rec = {"wxid": wxid, "name": disp, "mode": mode or "self",
                   "review": None, "persona": ""}
            recs.append(rec)
            head = f"已加入自动回复：{disp}"
        else:
            if mode:
                rec["mode"] = mode
            head = f"{disp} 已在名单里"
        _save(chats=recs)

        # 加进名单就顺手学一次语气（用户 2026-10-01 要的：「开启自动回复之后，
        # AI 能通过历史对话学出对这个人的语气」）。
        # **只有当前没设过人设时才会真的学**——已有的一律不动，想重学要明说，
        # 免得你精心写的人设被一次自动学习默默冲掉。
        # 学习要读一次库 + 调一次模型，所以这条命令会慢几秒；失败**绝不影响加人**。
        _learned, learn_note = _maybe_learn(rec, recs, cfg, client, llm_factory,
                                           force=False)

        tail = "" if sec.get("enabled") else "\n总开关还是关着的，记得发 /auto on。"
        return (f"{head}（身份={rec.get('mode')}，"
                f"self=假装你本人 / assistant=明说是助手）。{tail}{learn_note}"), True

    if sub in ("del", "delete", "remove", "删", "删除"):
        who, _ = _split_target(rest)
        rec = _find(recs, who, cfg)
        if rec is None:
            return f"名单里没有「{who}」。发 /auto 看名单。", False
        _save(chats=[r for r in recs if r is not rec])
        return f"已移出自动回复名单：{rec.get('name') or rec.get('wxid')}。", True

    if sub in ("mode", "身份"):
        who, mode = _split_target(rest)
        if not who or not mode:
            return "用法：/auto mode <昵称|wxid> self|assistant", False
        rec = _find(recs, who, cfg)
        if rec is None:
            return f"名单里没有「{who}」。", False
        rec["mode"] = mode
        _save(chats=recs)
        return f"{rec.get('name') or rec.get('wxid')} 的身份已改为 {mode}。", True

    # 称呼：**我平时怎么叫这个人**。和 persona 分开存、也分开注入到提示词里——
    # 理由是 persona 是散文，用户重写人设（或干脆手写一份）时，
    # 夹在里面的称呼会跟着一起丢；称呼要能独立活下来。
    # 它同时被当成联系人解析的别名（见 address_aliases），所以「给老张发消息」也认。
    if sub in ("address", "称呼", "叫法", "称谓"):
        if not rest.strip():
            return _USAGE, False
        rec, glob, _am, text = _split_rec_target(rest, recs, cfg)
        in_list = rec is not None
        if rec is None and not glob:
            # **名单外的人也要能设/学/清称呼**（2026-10-04 解绑，用户拍的）：
            # 不必为了一个称呼先把他加进自动回复名单——那等于替用户决定
            # 要不要让 AI 代他回话。这里只认人，不碰名单。
            wxid, disp, text, err = _split_address_target(rest, cfg, client,
                                                          can_lookup, name_hint)
            if err:
                return err, False
            rec = {"wxid": wxid, "name": disp}

        if text is None:
            who = str(name_hint or "").strip() or (rest.split(maxsplit=1)[0]
                                                   if rest.strip() else "")
            return (f"没找到「{who}」。请用完整的昵称/备注，或者直接给 wxid。"), False
        if glob:
            return ("称呼是**按人**的，没有「全局」那一份（只有语气人设有全局默认）。"
                    "发 /auto address <昵称> <称呼>。"), False

        wxid = str(rec.get("wxid") or "")
        name = rec.get("name") or wxid
        hit = address_record(cfg, wxid)
        cur = address_of(cfg, wxid, rec)
        if not text:                      # 不带内容 = 查看
            if cur:
                src = ("你手写的"
                       if ((hit or {}).get("source")
                           or rec.get("address_source")) == "manual"
                       else "从历史学到的")
                return (f"{name} 的称呼＝{src}：{cur}\n"
                        f"改：/auto address {name} <称呼>   从历史学："
                        f"/auto address {name} 学习   清空：/auto address {name} 清空"), False
            return (f"{name} 还没设称呼（回消息时不套称呼、直接说事）。\n"
                    f"设：/auto address {name} <称呼>（例：老张）   "
                    f"从历史学：/auto address {name} 学习"), False
        # 「学习」= 只学称呼、**一个字都不动人设**（force 只管称呼这一路；
        # 人设不在 fields 里，_store_learned 也就不会碰它）
        if _persona_learn(text):
            changed, note = _maybe_learn(rec, recs, cfg, client, llm_factory,
                                         force=True, fields=("address",))
            if in_list:
                rec.pop("address", None)
                rec.pop("address_source", None)
                _save(chats=recs)
            return note.lstrip("\n"), changed
        if _persona_clear(text):
            if not cur:
                return f"{name} 本来就没称呼，没动。", False
            clear_address(wxid)
            if in_list:
                rec.pop("address", None)
                rec.pop("address_source", None)
                _save(chats=recs)
            return f"{name} 的称呼已清空（回消息时不套称呼）。", True
        bad = _address_too_long(text)
        if bad:
            return bad, False
        addr = _clean_address(text)
        set_address(wxid, addr, "manual", name=name)
        if in_list:
            # 旧字段（2026-10-04 之前存这儿）清掉：称呼只能有一份真源
            rec.pop("address", None)
            rec.pop("address_source", None)
            _save(chats=recs)
        return (f"{name} 的称呼已设为「{addr}」——回复时会自然这么叫，"
                f"你说「给{addr}发消息」也能认出是他"
                f"（**不用**把他加进自动回复名单）。"
                f"想取消发 /auto address {name} 清空"), True

    # ⚠️ 命令词分家（2026-10-01）：「人设」这个别名以前挂在 mode（self/assistant）上，
    # 而真正的人设字段叫 persona —— 两个都叫「人设」，用户一定会改错东西。
    # 现在 mode = **身份**（假装你本人 / 明说是助手），persona = **人设/语气**。
    # `/auto mode` 这个命令词保持不变（不破坏已经记住它的手）；只把中文别名挪对了。
    if sub in ("persona", "人设", "语气", "口吻"):
        if not rest.strip():              # 裸 /auto persona → 用法（别拿空串去查名单）
            return _USAGE, False
        rec, glob, pmode, text = _split_rec_target(rest, recs, cfg)

        # text 为 None = 目标没认出来（不是名单里的人，也不是全局范围词）
        if text is None:
            who = str(name_hint or "").strip() or (rest.split(maxsplit=1)[0]
                                                   if rest.strip() else "")
            return (f"名单里没有「{who}」。人设是**每个人一份**、只对自动回复名单里的人"
                    f"生效——先发 /auto add {who} 把他加进来。\n"
                    f"（只要**称呼**的话不用进名单：/auto address {who} 学习 "
                    f"或 /auto address {who} <称呼>。）"), False

        if glob:
            m = pmode or "self"
            key = f"persona_{m}"
            cur = str(sec.get(key) or "").strip()
            if not text:
                if cur:
                    src = "你设的（存在 settings.json）"
                else:
                    src = "config.yaml / 代码兜底"
                    cur = _DEFAULT_ASSISTANT if m == "assistant" else _DEFAULT_SELF
                return (f"全局默认人设（{m} 模式，没单独设人设的人都用它）：\n{cur}\n"
                        f"（来源：{src}；改法：/auto persona 全局 {m} <描述>）"), False
            # 学语气是**按人**学的：全局那份没有对应的「一个人」的历史可学。
            # 不拦住的话「学习」两个字会被当成人设正文存进去（静默存一段没意义的东西）。
            if _persona_learn(text):
                return ("全局人设没法从历史里学——语气是**按人**学的。"
                        "发 /auto persona <昵称> 学习 学某个人。"), False
            if _persona_clear(text):
                if not _persona_disk_override(key):
                    return f"全局 {m} 的人设本来就没单独设过，没动。", False
                _save(_unset=(key,))
                return "已清掉全局 {} 的人设覆盖，重新用 config.yaml 里的默认人设。".format(m), True
            bad = _persona_too_long(text)
            if bad:
                return bad, False
            val = _clean_persona(text)
            _save(**{key: val})
            return (f"全局默认人设已设（{m} 模式，**整体替换**原默认人设）：\n{val}\n"
                    f"⚠️ 这是**没单独设人设的人**的默认值；单独设过的会话不受影响。"), True

        name = rec.get("name") or rec.get("wxid")
        own = str(rec.get("persona") or "").strip()
        if not text:                      # 不带描述 = 查看当前人设
            cur = persona_for(rec, sec)
            if own:
                src = ("你手写的" if rec.get("persona_source") == "manual"
                       else f"从历史学到的（用了 {rec.get('persona_n') or '?'} 条你自己发的话）")
                src += "，整体替换默认"
            else:
                src = f"默认（{rec.get('mode') or 'self'} 模式那份）"
            return (f"{name} 的人设＝{src}：\n{cur}\n"
                    f"改：/auto persona {name} <描述>   学：/auto persona {name} 学习"
                    f"   恢复默认：/auto persona {name} 清空"), False
        # 「学习 / 重新学习」= 明说 · 这时才允许盖掉已有的人设
        if _persona_learn(text):
            changed, note = _maybe_learn(rec, recs, cfg, client, llm_factory,
                                          force=True)
            return note.lstrip("\n"), changed
        if _persona_clear(text):
            if not own:
                return f"{name} 本来就用默认人设，没动。", False
            rec["persona"] = ""
            for k in ("persona_source", "persona_at", "persona_n"):
                rec.pop(k, None)
            _save(chats=recs)
            return f"{name} 的人设已恢复默认（{rec.get('mode') or 'self'} 模式那份）。", True
        bad = _persona_too_long(text)
        if bad:
            return bad, False
        rec["persona"] = _clean_persona(text)
        # 记下来源：手写的和学习到的在状态里必须能分辨（不然用户分不清手上
        # 这一段是谁写的、也不知道「重新学习」会不会盖掉自己的东西）。
        rec["persona_source"] = "manual"
        for k in ("persona_at", "persona_n"):
            rec.pop(k, None)
        _save(chats=recs)
        return (f"{name} 的人设已设为（**整体替换**默认人设）：\n{rec['persona']}\n"
                f"回复「{name}」时会整段用它。想退回默认，发 /auto persona {name} 清空"), True

    if sub in ("review", "审核"):
        bits = rest.split()
        if not bits:
            return "用法：/auto review on|off [昵称|wxid]（不带对象则改全局）", False
        v = bits[0].lower()
        if v not in ("on", "off", "开", "关"):
            return "只认 on / off。例：/auto review on 张三", False
        on = v in ("on", "开")
        who = " ".join(bits[1:]).strip()
        if not who:
            _save(review=on)
            return (f"全局审核已{'开启' if on else '关闭'}——⚠️ 这是**所有**自动回复会话的"
                    f"默认值（不只某一个），单独设过的会话不受影响。"
                    + ("草稿会先发给你，你回「确认」才发出去。" if on
                       else "自动回复直接发给对方。")), True
        rec = _find(recs, who, cfg)
        if rec is None:
            return f"名单里没有「{who}」。", False
        rec["review"] = on
        _save(chats=recs)
        return f"{rec.get('name') or rec.get('wxid')} 的审核已{'开启' if on else '关闭'}。", True

    if sub in ("ctx", "context", "上下文"):
        try:
            n = max(1, min(int(rest), 30))
        except ValueError:
            return "用法：/auto ctx 20（1~30 之间的整数）", False
        _save(context_messages=n)
        return f"上下文条数已设为 {n}。", True

    return _USAGE, False
