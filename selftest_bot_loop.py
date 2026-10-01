"""bot.py 侧改动的回归自测（不需要微信、不碰 hook、不联网）。

覆盖这次改动的**判据本身**，而不是靠人肉复读代码：
  1. pending_index_of —— 「确认 2 / 第2条 / 2」认，句子里夹带数字**不认**（宁可多问一次）
  2. 确认词表边界：agent 宽、auto/shell 严（shell 只认「确认/确定/确认发送」）
  3. _probe_login —— 分诊「是不是掉登录了」，探测本身炸了要保守当不在线
  4. 落盘状态：原子写、坏文件不挡住启动、读写往返
  5. 待确认队列落盘/恢复（含过期项**不许**恢复）
  6. build_user_prompt 的脱敏接线：开着才打码、关着一个字不动
  7. /用量 命令（usage.py 在就回文本，不在就如实说不可用）

跑法：.venv/Scripts/python.exe selftest_bot_loop.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import agent_tools                      # noqa: E402
import aixed_api                        # noqa: E402
import auto_reply                       # noqa: E402
import bot                              # noqa: E402
import image_cache                      # noqa: E402

_OK = 0
_FAIL = 0
_FAILED = []


def chk(cond, what):
    global _OK, _FAIL
    if cond:
        _OK += 1
        print(f"  ✅ {what}")
    else:
        _FAIL += 1
        _FAILED.append(what)
        print(f"  ❌ {what}")


def sec(title):
    print(f"\n── {title} ──")


# ============================================================
#  1) pending_index_of：只认「整句就是选号」
# ============================================================
def t_index_of():
    sec("待确认选号解析（绝不在句子里乱认数字）")
    f = bot.pending_index_of
    for text, want in (("确认 2", 2), ("确认2", 2), ("第2条", 2), ("2", 2),
                       ("执行 3", 3), ("确认 1", 1), ("第 12 条", 12)):
        chk(f(text) == want, f"「{text}」→ 第 {want} 条")
    for text in ("确认", "ok", "不发", "确认下 2 点的会", "2 是谁",
                 "确认一下", "看第 2 条了吗", "", "   "):
        chk(f(text) is None, f"「{text}」→ 不当成选号（返回 None）")
    # 越界由调用方判：解析层只负责「这句在点号」
    chk(f("0") == 0, "「0」解析出 0（越界判断交给调用方，不在解析层吞掉）")


# ============================================================
#  2) 确认词表：agent 宽 / auto·shell 严
# ============================================================
def t_confirm_words():
    sec("确认词表边界（本地执行只认严格词）")
    for w in ("确认", "确定", "确认发送", "可以发", "发吧", "发送", "ok", "yes", "y"):
        chk(bot.is_confirm(w), f"is_confirm 认「{w}」")
    for w in ("ok", "y", "yes", "发送", "发吧", "可以发"):
        chk(not bot.is_strict_confirm(w), f"严格词**不**认「{w}」（本地命令/草稿不因随口一句就跑）")
    for w in ("确认", "确定", "确认发送", "确认。", "确认！"):
        chk(bot.is_strict_confirm(w), f"严格词认「{w}」")
    for w in ("不发", "取消", "算了", "别发", "不发送"):
        chk(bot.is_cancel(w), f"取消词认「{w}」")
    chk(not bot.is_cancel("ok"), "「ok」不是取消词")


# ============================================================
#  3) _probe_login：分诊掉登录
# ============================================================
class _LoginStub:
    def __init__(self, val=None, boom=False):
        self.val, self.boom = val, boom

    def is_login(self):
        if self.boom:
            raise RuntimeError("hook 死了")
        return self.val


def t_probe_login():
    sec("登录态分诊（探测本身炸了要保守判「不在线」）")
    ok, detail = bot._probe_login(_LoginStub(True))
    chk(ok is True and detail == "", "在线 → (True, '')")
    ok, detail = bot._probe_login(_LoginStub(False))
    chk(ok is False and "登录" in detail, "IsLogin=0 → (False, 提示里含「登录」)")
    ok, detail = bot._probe_login(_LoginStub(boom=True))
    chk(ok is False and "失败" in detail, "探测抛异常 → 保守判不在线，且说明原因")


# ============================================================
#  4) 落盘状态：原子写 / 坏文件容错 / 往返
# ============================================================
def t_state(tmp):
    sec("落盘状态（原子写、坏文件不挡住启动）")
    old_path, old_state = bot.STATE_PATH, bot._STATE
    try:
        bot.STATE_PATH = os.path.join(tmp, "state.json")
        bot._STATE = None
        bot.state_set("cursor", {"message_fts_v4_0": 42, "__time__": 1790000000})
        bot.state_set("note", "你好")
        chk(not os.path.exists(bot.STATE_PATH + ".tmp"), "写盘后没留下 .tmp（原子替换干净）")
        # 重新从盘读（模拟重启）
        bot._STATE = None
        cur = bot.state_get("cursor")
        chk(isinstance(cur, dict) and cur.get("message_fts_v4_0") == 42, "游标往返读回正确")
        chk(bot.state_get("note") == "你好", "中文值往返不乱码")
        chk(bot.state_get("missing", "默认") == "默认", "缺键返回默认值")
        # 坏文件
        with open(bot.STATE_PATH, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        bot._STATE = None
        chk(bot.state_get("cursor", None) is None, "坏 JSON → 当空，不抛异常（不挡住启动）")
    finally:
        bot.STATE_PATH, bot._STATE = old_path, old_state


# ============================================================
#  5) 待确认队列落盘 / 恢复
# ============================================================
def t_pending_persist(tmp):
    sec("待确认队列落盘与恢复（重启后回「确认」不再白等）")
    old_path, old_state = bot.STATE_PATH, bot._STATE
    chat = "filehelper"
    saved_pending = agent_tools._PENDING.get(chat)
    try:
        bot.STATE_PATH = os.path.join(tmp, "state.json")
        bot._STATE = None
        agent_tools._PENDING.pop(chat, None)

        ttl = 300
        agent_tools.set_pending(chat, "wxid_a", "张三", "晚上一起吃饭", kind="agent")
        agent_tools.set_pending(chat, "", "", "dir /b", kind="shell", cmd="dir /b")
        bot.save_pending([chat], {"agent": {"confirm_ttl": ttl}})
        snap = bot.state_get("pending")
        chk(isinstance(snap, dict) and len(snap.get(chat) or []) == 2,
            "队列落盘：两条都在（文本 + 本机命令）")

        # 模拟重启：内存队列清空，然后从盘恢复
        agent_tools._PENDING.pop(chat, None)
        n = bot.restore_pending([chat], {"agent": {"confirm_ttl": ttl}})
        chk(n == 2, f"恢复回 2 条（实际 {n}）")
        items = agent_tools.list_pending(chat, ttl)
        chk(len(items) == 2, "恢复后队列里确实有 2 条")
        chk(items[1].get("kind") == "shell" and items[1].get("cmd") == "dir /b",
            "本机命令的**原文**一字不差地恢复了（用户审的就是它）")
        chk(not any("wxid" in str(agent_tools.describe_pending(i)) for i in items),
            "编号菜单里不出现 wxid（显示名约定）")

        # 队列变了立刻落盘：弹掉一条后，盘上不该还留着它
        agent_tools.pop_pending(chat, ttl)
        bot.save_pending([chat], {"agent": {"confirm_ttl": ttl}})
        after = bot.state_get("pending").get(chat) or []
        chk(len(after) == 1, "已执行的条目不会留在盘上（防崩溃后重复执行）")

        # 过期项不许恢复
        agent_tools._PENDING.pop(chat, None)
        bot.state_set("pending", {chat: [
            {"to_wxid": "wxid_b", "to_name": "李四", "text": "过期了", "kind": "agent",
             "count": 1, "ts": time.time() - 99999}]})
        n2 = bot.restore_pending([chat], {"agent": {"confirm_ttl": ttl}})
        chk(n2 == 0, "过期条目**不**恢复（免得用户对着一句早就没意义的提示回确认）")

        # 不在允许会话里的不许恢复
        agent_tools._PENDING.pop(chat, None)
        bot.state_set("pending", {"别人的会话": [
            {"to_wxid": "x", "to_name": "x", "text": "x", "kind": "agent",
             "count": 1, "ts": time.time()}]})
        chk(bot.restore_pending([chat], {"agent": {"confirm_ttl": ttl}}) == 0,
            "不在控制会话里的条目不恢复（不跨会话串台）")
    finally:
        bot.STATE_PATH, bot._STATE = old_path, old_state
        agent_tools._PENDING.pop(chat, None)
        if saved_pending is not None:
            agent_tools._PENDING[chat] = saved_pending


# ============================================================
#  6) build_user_prompt 的脱敏接线
# ============================================================
class _HistStub:
    def search(self, query, k=8):
        return [{"time": "2026-10-01 12:00", "sender": "张三",
                 "content": "他的手机号是 13812345678，身份证 110101199001011234"}]


def t_redact_wiring():
    sec("送云端前脱敏：开着才打码，关着一个字不动")
    if bot.redact is None:
        chk(False, "redact.py 没导入成功（这次改造的交付物之一）")
        return
    cfg_off = {"search_topk": 8, "privacy": {"redact": False}}
    cfg_on = {"search_topk": 8, "privacy": {"redact": True}}
    other = {"search_topk": 8}

    off = bot.build_user_prompt("他的手机号", None, [], cfg_off, _HistStub(), False)
    chk("13812345678" in off, "默认关闭：原文原样送出去（不偷偷改内容）")

    on = bot.build_user_prompt("他的手机号", None, [], cfg_on, _HistStub(), False)
    chk("13812345678" not in on, "打开后：手机号不再出现在送出的文本里")
    chk("138****5678" in on, "打码保留了可读性（138****5678）")
    chk("110101199001011234" not in on, "身份证也不再原文出现")

    nod = bot.build_user_prompt("他的手机号", None, [], other, _HistStub(), False)
    chk("13812345678" in nod, "配置里没有 privacy 段：按关闭处理（fail-safe）")


# ============================================================
#  7) /用量 命令
# ============================================================
def t_usage_cmd():
    sec("/用量 命令")
    reply, changed = bot.handle_command("/用量", None, {}, False, [])
    if bot.usage is None:
        chk(reply is not None and "不可用" in reply,
            "usage.py 缺失时如实说不可用（不假装有数据）")
    else:
        chk(reply is not None and not changed, "/用量 回文本且不改配置")
        chk("用量" in reply or "没有" in reply or "记录" in reply, "回的是人话（含统计或「还没有记录」）")
    bad, _ = bot.handle_command("/用量 不是数字", None, {}, False, [])
    chk(bad is not None and "用法" in bad, "非法天数给用法提示，不崩")


def t_speaker_no_id_leak():
    sec("渲染「谁说的」：查不到显示名也绝不外泄 id 给模型")
    so = agent_tools.speaker_of
    chk(so({"is_self": 1, "sender": "wxid_x"}, {"wxid_x": "张三"}) == "我",
        "自己发的 → 「我」")
    chk(so({"is_self": 0, "sender_name": "老张", "sender": "wxid_x"}, {}) == "老张",
        "有 sender_name 就用它（微信算好的群昵称最准）")
    chk(so({"is_self": 0, "sender": "wxid_x"}, {"wxid_x": "张三"}) == "张三",
        "查得到联系人表 → 显示名")
    chk(so({"is_self": 0, "sender": "wxid_x"}, {}) == "对方",
        "**查不到 → 「对方」，不是裸 wxid**（模型会照抄 wxid）")
    chk(so({"is_self": 0, "sender": "wxid_abc"}, {}, is_group=True) == "群成员",
        "群聊 + 查不到 → 「群成员」")
    chk(so({"is_self": 0, "sender": "123456"}, {}) == "对方",
        "fts 路的纯数字 id 也不外泄")
    chk(so({"is_self": 0, "sender": "老张"}, {}) == "老张",
        "非 id 形状的值仍当名字用（防御：将来某条路可能塞真名字）")
    chk(so({"is_self": 0}, {}, "某个群") == "某个群", "没有 sender 时退回会话名")
    lines = agent_tools.format_history_lines(
        [{"time": "t", "sender": "wxid_secret", "content": "你好", "is_self": 0}],
        {}, "群", True)
    chk(bool(lines) and all("wxid" not in ln for ln in lines),
        "整行渲染（format_history_lines）里也不出现 wxid")
    # names 表里**存的是 id** 时同样不许当名字用（别处可能构造出这种表）
    lines2 = agent_tools.format_history_lines(
        [{"time": "t", "sender": "wxid_a", "content": "你好", "is_self": 0}],
        {"wxid_a": "wxid_a"}, "群", True)
    chk(bool(lines2) and all("wxid" not in ln for ln in lines2),
        "names 表里存的是 wxid 时也不外泄（渲染统一出口再挡一道）")
    # contact_names 不拿 wxid 顶名字
    nm = auto_reply.contact_names([{"wxid": "wxid_noname"}, {"wxid": "wxid_ok", "name": "张三"}])
    chk("wxid_noname" not in nm, "contact_names 不给无名字的联系人塞 wxid")
    chk(nm.get("wxid_ok") == "张三", "contact_names 照常给出真显示名")
    # bot._msg_speaker：会话名查不到时不许把 talker 当名字（含 Msg_<md5> 那种表名兜底路）
    chk(bot._msg_speaker({"talker": "wxid_zzz", "is_self": 0, "sender": ""}, {}) == "对方",
        "会话名查不到 → 「对方」，不是裸 wxid")
    chk(bot._msg_speaker({"talker": "Msg_abc123", "is_self": 0, "sender": ""}, {}) == "对方",
        "无 fts 兜底路的 Msg_<md5> 表名也不外泄")
    chk(bot._msg_speaker({"talker": "wxid_zzz", "is_self": 0, "sender": ""},
                         {"wxid_zzz": "张三"}) == "张三",
        "查得到就照常显示名字（没把功能改坏）")


class _BatchMsg:
    """够 iter_aixed_messages 用就行（它只负责把 poll 结果 yield 出去）。"""

    def __init__(self, n):
        self.n = n
        self.talker = "wxid_t"
        self.content = f"msg{n}"
        self.is_self = 0
        self.create_time = int(time.time())

    @property
    def type(self):
        return 1

    @property
    def sender(self):
        return self.talker


class _OneBatchClient:
    """第一次 poll 返回一批 N 条，之后永远返回空批。"""

    def __init__(self, n=3):
        self._batch = [_BatchMsg(i) for i in range(1, n + 1)]
        self._first = True

    def prime(self):
        return {"t": 0}, {}

    def poll_messages(self, since=None, seen=None):
        if self._first:
            self._first = False
            return list(self._batch), since, seen
        time.sleep(0.005)
        return [], since, seen


def t_batch_survives_consumer_error(tmp):
    """消费者抛异常**不会**丢掉本批剩余消息。

    复核者曾把这条报成「理论丢消息」：说异常会在 yield 点倒灌回生成器、把它终止。
    实测不成立——挂起的生成器只要还被引用就活着，下一次 next() 会继续把本批发完。
    这个性质是「一条消息处理失败不至于连累同批其它消息」的前提，值得钉住。
    """
    sec("收消息通道：一条消息处理失败不连累同批其它消息")
    old_path, old_state = bot.STATE_PATH, bot._STATE
    try:
        bot.STATE_PATH = os.path.join(tmp, "state.json")
        bot._STATE = None
        src = bot.iter_aixed_messages(_OneBatchClient(3), 5, tick=None)
        got = [next(src).n, next(src).n]          # 1, 2
        # 模拟消费者在处理第 2 条时抛异常（会冒到主循环的外层守护）
        try:
            raise RuntimeError("消费者处理这一条时炸了")
        except RuntimeError:
            pass
        got.append(next(src).n)                   # 第 3 条必须还在
        chk(got == [1, 2, 3],
            f"消费者抛异常后本批剩余消息仍在（期望 [1,2,3]，实际 {got}）")
    finally:
        bot.STATE_PATH, bot._STATE = old_path, old_state


def t_poll_failure_throttled(tmp):
    """轮询持续失败**不许刷屏**（实测过 2 分钟刷 5.4MB 的 traceback）。"""
    sec("轮询失败节流：不静默、也不刷屏")

    class _BoomThenMsg:
        """前 fails 次 poll 抛异常，之后返回一条消息（让 next() 能返回，测试有界）。"""

        def __init__(self, fails=300):
            self.fails = fails
            self.calls = 0

        def prime(self):
            return {"t": 0}, {}

        def poll_messages(self, since=None, seen=None):
            self.calls += 1
            if self.calls <= self.fails:
                raise ValueError("模拟每次查询都炸")
            return [_BatchMsg(9)], since, seen

    old_path, old_state = bot.STATE_PATH, bot._STATE
    try:
        bot.STATE_PATH = os.path.join(tmp, "state.json")
        bot._STATE = None
        c = _BoomThenMsg(fails=300)
        src = bot.iter_aixed_messages(c, 0.001, tick=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            got = next(src).n          # 300 次失败之后才拿到这条
        text = buf.getvalue()
        tb = text.count("Traceback (most recent call last)")
        warns = text.count("轮询第")
        chk(got == 9, "失败恢复后消息照常送达")
        chk(c.calls >= 300, f"确实连续失败了 300 轮（{c.calls} 次轮询）")
        chk(1 <= tb <= 12,
            f"traceback 被节流：300 轮失败只打了 {tb} 条（不节流会是 300 条）")
        chk(1 <= warns <= 12,
            f"失败告警被节流：{warns} 条（首次仍会如实记录，之后按 10/50/每 50 次）")
    finally:
        bot.STATE_PATH, bot._STATE = old_path, old_state


def t_image_dirs_union(tmp):
    """白名单是**并集**：用户配的目录 + 默认的图片缓存根（不是顶掉默认）。

    真机自检撞出来的：用户为了自测加了 `test_images`，旧实现（配了就顶掉默认）
    就让他**静默地**再也发不出聊天里的图。改成并集，并要求打一条告警说明「两处都能发」。
    """
    sec("发图白名单：用户配的目录加在默认之上（并集）")
    user_dir = os.path.join(tmp, "user_images")
    cache_root = os.path.join(tmp, "acct", "cache")
    data_root = os.path.join(tmp, "xwechat_files")
    for d in (user_dir, cache_root, data_root):
        os.makedirs(d, exist_ok=True)

    orig_cache = image_cache.image_cache_dirs
    orig_root = image_cache.data_root
    orig_w = agent_tools._WARNED_IMAGE_DIRS[0]
    try:
        image_cache.image_cache_dirs = lambda: [cache_root]
        # 配了目录 → 两个都在（这就是「并集」）
        dirs = agent_tools.allowed_image_dirs({"agent": {"send_image_dirs": [user_dir]}})
        chk(user_dir in dirs, "用户配的目录在允许列表里")
        chk(cache_root in dirs, "**默认的图片缓存根也在**（旧实现会把它顶掉）")
        # 没配 → 只有默认
        chk(agent_tools.allowed_image_dirs({}) == [cache_root],
            "没配 send_image_dirs 时只放默认的缓存根")
        # 配了目录要有一条明说「两处都能发」的告警
        agent_tools._WARNED_IMAGE_DIRS[0] = False
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            agent_tools.allowed_image_dirs({"agent": {"send_image_dirs": [user_dir]}})
        chk("两处都能发" in buf.getvalue(),
            "配了目录时明确告警「白名单 = 你配的 + 默认缓存根」（边界要让人知道）")
        # 推不出缓存根 → 退回 data_root 并告警
        image_cache.image_cache_dirs = lambda: []
        image_cache.data_root = lambda: data_root
        agent_tools._WARNED_IMAGE_ROOT[0] = False
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            dirs2 = agent_tools.allowed_image_dirs({"agent": {"send_image_dirs": [user_dir]}})
        chk(data_root in dirs2, "推不出缓存根时退回 data_root")
        chk("放宽到整个微信数据根目录" in buf2.getvalue(), "放宽必须告警（不许静默）")
        # 连数据根都没有 → 至少尊重用户配的目录（空列表 = 什么都不许发）
        image_cache.data_root = lambda: None
        chk(agent_tools.allowed_image_dirs({"agent": {"send_image_dirs": [user_dir]}}) == [user_dir],
            "连数据根都推不出时，仍尊重用户配的目录")
        chk(agent_tools.allowed_image_dirs({}) == [], "全推不出时返回空（调用方如实拒绝，不放行）")
    finally:
        image_cache.image_cache_dirs = orig_cache
        image_cache.data_root = orig_root
        agent_tools._WARNED_IMAGE_DIRS[0] = orig_w


def t_check_ret():
    """失败标记两种都要认：`status`（查库）和 `ret`（发送类）。

    hook 的发送接口成功时回 `{"ret":0,"retmsg":"success"}`、JSON 坏掉回 `{"ret":-1}`，
    而旧 `_check` **只认 status** → `ret:-1` 被当成功放过去（静默）。
    """
    sec("失败标记：status 与 ret 都要认")
    ck = aixed_api.AixedClient._check
    chk(ck({"ret": 0, "retmsg": "success"}) is not None, "ret:0 当成功放行")
    for bad in ({"ret": -1, "msg": "invalid json"}, {"status": -1, "desc": "x"},
                {"ret": "-1"}, {"status": "-1"}):
        try:
            ck(bad, "发送 ")
            chk(False, f"{bad} 应当被认成失败")
        except aixed_api.AixedError:
            chk(True, f"{bad} 被认成失败（旧代码对 ret 是静默的）")
    chk(ck(None) is None, "非 dict 原样返回（不误伤）")
    chk(ck({}) is not None, "空 dict 当成功（没有失败标记）")
    chk(ck({"ret": True}) is not None, "ret 是布尔 True 不算负数（别把 bool 当 int）")


def t_own_image():
    """自己刚发出去的图片：不能被下一轮轮询当成新消息再答一遍。

    图片补上「非文本补漏」之后（图片不在 fts 里），**我们自己发的图也会回显**。
    文本有 is_own_reply 兜着，图片没有，所以用「会话 + 时间窗」认。
    """
    sec("自己刚发出的图片：不误当成新消息，也不误伤对方发来的图")
    agent_tools._SENT_IMAGE.clear()
    now = time.time()
    chk(not agent_tools.is_own_image("filehelper", now),
        "没记过 → 不是自己的（**对方发来的图绝不能被误判丢掉**）")
    agent_tools.remember_sent_image("filehelper")
    chk(agent_tools.is_own_image("filehelper", now), "刚记过 + 时间吻合 → 认成自己的回显")
    chk(not agent_tools.is_own_image("wxid_other", now), "别的会话不算")
    chk(not agent_tools.is_own_image("filehelper", 0),
        "取不到消息时间 → 不当成自己的（宁可漏判多答一句，也不误判丢图）")
    agent_tools._SENT_IMAGE["filehelper"] = now - agent_tools._SENT_IMAGE_TTL - 5
    chk(not agent_tools.is_own_image("filehelper", now), "超过时间窗 → 不再当成自己的")
    agent_tools._SENT_IMAGE.clear()


def main():
    print("=" * 60)
    print("bot.py 改动回归自测（无微信 / 不碰 hook / 不联网）")
    print("=" * 60)
    t_index_of()
    t_confirm_words()
    t_probe_login()
    t_speaker_no_id_leak()
    tmp = tempfile.mkdtemp(prefix="bot_loop_selftest_")
    try:
        t_state(tmp)
        t_pending_persist(tmp)
        t_batch_survives_consumer_error(tmp)
        t_poll_failure_throttled(tmp)
        t_image_dirs_union(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    t_redact_wiring()
    t_usage_cmd()
    t_check_ret()
    t_own_image()

    print("\n" + "=" * 60)
    if _FAIL:
        print(f"有失败项 ❌  通过 {_OK} 项，失败 {_FAIL} 项")
        for w in _FAILED:
            print(f"  - {w}")
        print("=" * 60)
        return 1
    print(f"全部通过 ✅  共 {_OK} 项")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
