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

        # 群发批次与素材指代必须**整条恢复**：只恢复 8 个基础字段的话，
        # 群发批次会变成「没有 items」，然后按文本分支把**给人看的预览**
        # 往一个**空 wxid** 发出去（真机上最难查的那种错）。
        agent_tools._PENDING.pop(chat, None)
        batch_items = [{"wxid": "wxid_a", "name": "张三", "text": "老张，节日快乐"},
                       {"wxid": "wxid_b", "name": "李四", "text": "李四，节日快乐"}]
        agent_tools.set_pending(chat, "", "", "给 2 个人群发…", kind="broadcast",
                                items=batch_items, label="群发内容")
        agent_tools.set_pending(chat, "wxid_a", "张三", "那张图", kind="agent",
                                xml="<xml/>", label="那张图")
        bot.save_pending([chat], {"agent": {"confirm_ttl": ttl}})
        agent_tools._PENDING.pop(chat, None)
        n3 = bot.restore_pending([chat], {"agent": {"confirm_ttl": ttl}})
        chk(n3 == 2, f"群发批次 + 素材项都恢复了（实际 {n3}）")
        back = agent_tools.list_pending(chat, ttl)
        bcast = [i for i in back if i.get("kind") == "broadcast"]
        chk(len(bcast) == 1 and [x["text"] for x in (bcast[0].get("items") or [])]
            == ["老张，节日快乐", "李四，节日快乐"],
            f"群发的逐条内容一字不差地恢复了（实际 {bcast[0].get('items') if bcast else None}）")
        mat = [i for i in back if i.get("label") == "那张图"]
        chk(len(mat) == 1, "素材项的用户指代（label）也恢复了——不然用户认不出是哪一条")
        agent_tools._PENDING.pop(chat, None)
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
#  6.5) 预取的历史窗口必须自曝范围
# ============================================================
_HIST_WIN_CONTACTS = [{"wxid": "wxid_zhangsan", "name": "张三", "remark": "张三"}]


def t_history_window_label():
    """提示词里那段历史是**按条数**截的窗口——它必须自己说清这一点。

    2026-10-01 真机：用户问「我跟张三最近 10 天说了什么」，模型拿的就是这段
    （30 条、实际只覆盖 9/30–10/1），却答成「最近 10 天（9/30–10/1）」——
    而那条会话从 2026-01 起有 6486 条。修复后这段的标题带条数、带实际覆盖范围、
    带「取满上限」的警告；结尾还有一条硬规矩：问时间范围必须带 days 去查库。
    """
    sec("预取历史：自曝「只有多少条 / 覆盖到什么时候」")
    real_q = bot.query_contact_history
    real_s = bot.search_history
    real_r = bot.recent_messages
    msgs = [{"time": "2026-09-30 16:02:02", "content": "考完了", "is_self": 1,
             "talker": "wxid_zhangsan", "local_type": 1},
            {"time": "2026-10-01 20:28:33", "content": "认真说下 muse", "is_self": 1,
             "talker": "wxid_zhangsan", "local_type": 1}]

    def fake_q(client, talker, limit=50, keyword=None, since=None):
        return (msgs * 50)[:int(limit)]        # 永远填满上限（最坏情况）

    try:
        bot.query_contact_history = fake_q
        bot.search_history = lambda *a, **k: []
        bot.recent_messages = lambda *a, **k: []
        cfg = {"recent_messages": 4, "target_chats": [], "search_topk": 8}
        p = bot.build_user_prompt("我跟张三最近10天说了什么", None,
                                  _HIST_WIN_CONTACTS, cfg, None, True)
    finally:
        bot.query_contact_history = real_q
        bot.search_history = real_s
        bot.recent_messages = real_r

    chk("按**条数**取的窗口" in p, "标题写明这是按条数截的窗口（不是时间范围）")
    chk("不是全部历史" in p, "标题写明这不是全部历史")
    chk("实际覆盖" in p and "2026-09-30" in p, "带上这批实际覆盖的时间跨度")
    chk("更早的没有取" in p, "取满上限时明说「更早的没有取」")
    chk("read_history" in p and "days" in p,
        "结尾钉了「问时间范围必须调 read_history 并带 days」")


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


def t_stash_media(tmp):
    """控制会话来了图片/表情 -> 自动暂存 + 回执（素材暂存区那条**确定性**链路）。

    为什么不交给模型：素材是「刚才那张」，模型看不到图片内容、也拿不到 local_id。
    这条链路的判据全在这里锁住：只收自己发的、只收能转发的类型、取不到原始 XML 就
    如实说「暂存失败」（**绝不留一个发不出去的空壳**）、关掉开关就完全不动作。
    """
    sec("素材暂存：控制会话收到图片/表情就记下来，并回一句回执")
    import assets
    import live_history

    old_path = assets.PATH
    old_latest = live_history.latest_media
    old_xml = live_history.message_xml
    # ⚠️ 副本目录也必须指到临时目录：这条链路里会 stash/clear，而 assets 现在会
    # 「清掉没人引用的副本」——指向真目录的话会把你真机上刚收下来的那张明文图删掉。
    old_stash = assets.STASH_DIR
    assets.STASH_DIR = os.path.join(tmp, "stash_dir")
    assets.PATH = os.path.join(tmp, "stash_assets.json")
    sent = []

    def _send(text, chat):
        sent.append((text, chat))

    def _msg(local_type=3, is_self=1, ts=1000):
        return aixed_api.Msg("filehelper", "[图片]", is_self, ts, local_type)

    def _row(kind="图片", local_type=3, is_self=1, ts=1000, lid="42", image=None):
        return {"talker": "filehelper", "local_id": lid, "local_type": local_type,
                "kind": kind, "is_self": is_self, "image": image, "time": "", "_ts": ts}

    try:
        live_history.latest_media = lambda client, talker, limit=3: [_row()]
        live_history.message_xml = lambda client, talker, lid: "<msg><img/></msg>"

        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper", _msg(), _send) is True,
            "图片：返回 True（这轮到此为止，不再当成提问丢给模型）")
        chk(len(sent) == 1 and "已暂存" in sent[0][0] and "发不出去" in sent[0][0],
            "只有消息引用时：回执**如实说这张发不出去**（转发接口在本版微信上会崩，"
            "已禁用），而不是让用户以为「已暂存 = 随时能发」")
        items = assets.load()
        chk(len(items) == 1 and items[0]["xml"] == "<msg><img/></msg>",
            "原始消息引用照样存下来（hook 将来修好还能用）")

        # 有明文（微信缓存的缩略图）-> 存明文，回执告诉用户「说发给谁我就发」
        thumb = os.path.join(tmp, "明文缩略图.jpg")
        with open(thumb, "wb") as f:
            f.write(b"\xff\xd8\xff\xd9")
        assets.clear()
        live_history.latest_media = lambda client, talker, limit=3: [_row(image=thumb)]
        sent.clear()
        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper", _msg(), _send) is True
            and "发给谁" in sent[-1][0]
            and assets.plaintext_of(assets.load()[-1]) == thumb,
            "有明文时：存的是明文，回执告诉用户「说发给谁我就发」")
        live_history.latest_media = lambda client, talker, limit=3: [_row()]

        bot.stash_control_media(None, {"assets": {}}, "filehelper", _msg(), _send)
        chk(len(assets.load()) == 1 and "已经存过了" in sent[-1][0],
            "同一条重复报上来：不叠两条，回执改说「已经存过了」")

        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper",
                                    _msg(local_type=1), _send) is False,
            "文本消息不拦（照旧走命令/问答）")
        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper",
                                    _msg(is_self=0), _send) is False,
            "**别人发来的图不暂存**（否则「发给谁」会把对方刚发的又发出去）")
        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper",
                                    _msg(local_type=34), _send) is False,
            "语音这类转发不了的类型不暂存")

        live_history.latest_media = lambda client, talker, limit=3: [_row(is_self=None)]
        n_before = len(assets.load())
        sent.clear()
        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper", _msg(), _send) is True
            and "暂存失败" in sent[-1][0] and len(assets.load()) == n_before,
            "认不出是不是自己发的 → 如实说暂存失败，不留空壳")

        live_history.latest_media = lambda client, talker, limit=3: [_row()]
        live_history.message_xml = lambda client, talker, lid: ""
        sent.clear()
        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper", _msg(), _send) is True
            and "暂存失败" in sent[-1][0] and len(assets.load()) == n_before,
            "取不到原始 XML → 如实说「转发不了」，不留空壳")

        # 时间戳有秒级差异、只有一个候选：认下来（否则真机上会经常「暂存失败」）
        live_history.message_xml = lambda client, talker, lid: "<msg><img/></msg>"
        live_history.latest_media = lambda client, talker, limit=3: [_row(ts=1005, lid="43")]
        sent.clear()
        chk(bot.stash_control_media(None, {"assets": {}}, "filehelper", _msg(ts=1000), _send)
            is True and len(assets.load()) == n_before + 1,
            "时间戳差几秒且只有一个候选 → 认下来（别动不动就报暂存失败）")

        # 关掉开关：什么都不做（连查库都不去）
        def _boom(*a, **k):
            raise AssertionError("开关关着就不该查库")
        live_history.latest_media = _boom
        sent.clear()
        chk(bot.stash_control_media(None, {"assets": {"enabled": False}}, "filehelper",
                                    _msg(), _send) is False and sent == [],
            "assets.enabled=false：不暂存、不回执、不查库")

        # ★ 用户在微信界面里发图时，微信在 temp\RWTemp 留的**明文原图**：
        #   这是「自己在微信里发的图」唯一能拿到的明文（正式落盘只有加密 .dat），
        #   必须优先收它、而且立刻复制走（那个临时目录会被清理）。
        import image_cache
        real_accounts = image_cache.account_dirs
        real_stash = assets.STASH_DIR
        acct = os.path.join(tmp, "acct_rw")
        rw = os.path.join(acct, "temp", "RWTemp", "2026-10", "aaa")
        os.makedirs(rw, exist_ok=True)
        with open(os.path.join(rw, "abcdef0123456789.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"y" * 32)
        assets.STASH_DIR = os.path.join(tmp, "stash_rw")
        image_cache.account_dirs = lambda: [acct]
        try:
            now = int(time.time())
            live_history.latest_media = lambda client, talker, limit=3: [
                _row(ts=now, lid="523")]
            assets.clear()
            sent.clear()
            chk(bot.stash_control_media(None, {"assets": {}}, "filehelper",
                                        _msg(ts=now), _send) is True
                and "明文原图" in sent[-1][0] and "发给谁" in sent[-1][0],
                "发图时微信留的**明文原图**被收下，回执说明是明文原图")
            _it = assets.load()[-1]
            chk(assets.plaintext_of(_it)
                and str(_it.get("path") or "").startswith(assets.STASH_DIR),
                "存的是**复制进来**的明文副本（微信那个临时目录会被清理）")
        finally:
            image_cache.account_dirs = real_accounts
            assets.STASH_DIR = real_stash
    finally:
        assets.PATH = old_path
        assets.STASH_DIR = old_stash
        live_history.latest_media = old_latest
        live_history.message_xml = old_xml


def t_broadcast_preview_note():
    """群发预览必须**原样**补在答复后面（不让模型转述）。

    模型转述十条正文必然走样：漏一条、改一个字，用户就在**没看清内容**的情况下
    回「确认」把消息发给一群人。所以这段由 bot 拿 ToolBox 记下的事实直接拼上去。
    """
    sec("群发预览原样直发（模型转述必走样）")
    out = bot.with_broadcast_preview("已经准备好了，回确认即可",
                                     "给 2 个人群发：\n1. 张三：A\n2. 李四：B")
    chk("预览被补在答复后面", out.endswith("2. 李四：B") and out.startswith("已经准备好了"))
    chk("逐条内容一字不差", "1. 张三：A" in out and "2. 李四：B" in out)
    chk("没有预览时**原样返回**、不加空行",
        bot.with_broadcast_preview("就一句话", "") == "就一句话")
    chk("没有预览且答复为空 -> 空串（别造出一条空消息）",
        bot.with_broadcast_preview("", "") == "")


def _tiny_png(path):
    """造一张 1×1 的真 PNG（纯标准库：不依赖 Pillow，也不依赖系统程序）。"""
    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return path


def t_inline_image_round(tmp):
    """`image.mode=inline`：原图**附给模型当次调用**，而且（关键）**不进对话记忆**。

    三条是这条链的全部价值，缺一条就变成"烧钱"或"撒谎"：
      ① 图真的进了模型的这一次请求（含 base64 的 image_url）；
      ② 只附一次 —— 后续轮次的 messages 里**不再有**这张图（否则每轮重发，token 翻倍）；
      ③ 超过 `image.max_per_round` 的图**说不出来**（不静默少给模型几张）。
    """
    sec("inline 图：进模型当次调用、不进对话记忆")
    import json as _json

    import file_read
    import llm as llm_mod

    sub = os.path.join(tmp, "2026-10")          # locate/search 都按 <root>/<月>/ 找
    os.makedirs(sub, exist_ok=True)
    png1 = _tiny_png(os.path.join(sub, "图一.png"))
    png2 = _tiny_png(os.path.join(sub, "图二.png"))
    with open(os.path.join(sub, "说明.txt"), "w", encoding="utf-8", newline="") as f:
        f.write("这是一份纯文本说明，内容足够长以通过乱码判据，用来占位。")
    old_roots = file_read.files_roots
    file_read.files_roots = lambda: [tmp]

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError(f"不该查库，却调了 {name}")

    cfg = {"agent": {"max_rounds": 4},
           "image": {"mode": "inline", "ocr_first": False, "downscale": 0,
                     "max_per_round": 1},
           "file": {"inline_bytes": 10 ** 7}}
    seen = []
    rounds = [
        # 第一轮：一口气读两张图（第二张会撞 max_per_round=1 的上限）
        [llm_mod.ToolCall("c1", "read_file", {"name": "图一.png"}),
         llm_mod.ToolCall("c2", "read_file", {"name": "图二.png"})],
        # 第二轮：改读纯文本（不再收图）
        [llm_mod.ToolCall("c3", "read_file", {"name": "说明.txt"})],
        # 第三轮：给结论
        [],
    ]

    class _LLM:
        def chat_with_tools(self, system, messages, tools):
            seen.append(messages)
            calls = rounds[min(len(seen) - 1, len(rounds) - 1)]
            return llm_mod.ChatResult("" if calls else "看过了：图里是一块红色。", calls)

    state = {}
    try:
        answer, _changed = bot.run_agent(
            _LLM(), "你是助手", "看看我刚发的那两张图", _Boom(), [], cfg, "filehelper",
            "", cfg_provider=lambda: cfg, history=[], state=state)
    finally:
        file_read.files_roots = old_roots

    def images_in(msgs):
        out = []
        for m in msgs:
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "image_url":
                        out.append(b)
        return out

    chk(len(seen) >= 2, f"模型至少被调了两轮（实际 {len(seen)}）")
    chk(not images_in(seen[0]), "第一轮没有图（工具还没读）")
    n2 = len(images_in(seen[1]))
    chk(n2 == 1, f"第二轮**图真的附上了**（含 base64 的 image_url），实际 {n2} 张")
    if n2:
        url = images_in(seen[1])[0]["image_url"]["url"]
        chk(url.startswith("data:image/") and ";base64," in url,
            f"附的是 data URL + base64（{url[:40]}…）")
    chk(any("没给" in str(n) for n in state.get("image_notes", [])),
        f"超过 max_per_round 的那张**如实说出来**了：{state.get('image_notes')}")
    # 工具回给模型的话也**不许**说"已经给模型看了"（那张明明没给）——自测抓出来的撒谎点
    over_cap_tool_text = ""
    for m in seen[1]:
        if m.get("role") == "tool" and isinstance(m.get("content"), str) \
                and "图二" in m["content"]:
            over_cap_tool_text = m["content"]
    chk("没能" in over_cap_tool_text and "已把原图交给模型看" not in over_cap_tool_text,
        "没给成的那张，工具如实说「没能交给模型看」")
    chk(len(seen) < 3 or not images_in(seen[2]),
        f"第三轮**不再带图**（只附一次，不每轮重发），共 {len(seen)} 轮")
    chk("base64" not in _json.dumps(bot.dialog_history("filehelper", cfg), ensure_ascii=False),
        "对话记忆里没有 base64（图绝不进 dialog）")
    chk("红色" in answer, f"答复正常返回：{answer[:40]}")

    # attach_images 不许就地改调用方传进来的列表（否则图会被留在 messages 里）
    base = [{"role": "user", "content": "原消息"}]
    out = bot.attach_images(base, [(png1, "图一")])
    chk(base == [{"role": "user", "content": "原消息"}]
        and len(out) == 2 and isinstance(out[-1]["content"], list),
        f"attach_images 返回新列表、不改原列表（返回 {len(out)} 条）")
    chk(bot.attach_images(base, []) is base, "没有图时原样返回（不白加一条消息）")

    # 如实说明由 bot 补在答复后面
    noted = bot.with_image_notes("答复正文", ["⚠️ 有一张没给模型看"])
    chk(noted.startswith("答复正文") and "没给模型看" in noted,
        f"with_image_notes 把说明补在后面：{noted[:40]}")
    chk(bot.with_image_notes("答复正文", []) == "答复正文", "没有说明时原样返回")


def t_executed_once(tmp):
    sec("已执行指纹：同一条不执行两次（落盘、扛重启），但同样内容的第二条要放行")
    old_path, old_state = bot.STATE_PATH, bot._STATE
    try:
        bot.STATE_PATH = os.path.join(tmp, "state_exec.json")
        bot._STATE = None

        def item(text="好的", ts=1790000000.0):
            return {"kind": "agent", "to_wxid": "wxid_a", "to_name": "张三",
                    "text": text, "count": 1, "ts": ts}

        it = item()
        dup, _ = bot.already_executed(it)
        chk(not dup, "没执行过 → 放行")

        bot.remember_executed(it, {})
        dup, ago = bot.already_executed(it)
        chk(dup and ago is not None and ago < 5, "执行过 → 拦住，并给出「多久以前」")

        # ★ 关键：模拟崩溃重启（清内存、从盘重新读）
        bot._STATE = None
        dup, _ = bot.already_executed(it)
        chk(dup, "**重启后仍然拦得住**（这正是加它的理由：恢复出来的那条已经发过了）")

        # ★ 反向关键：用户重说一遍会生成**新的 ts**，那是新的一条，必须放行
        dup, _ = bot.already_executed(item(ts=1790000001.0))
        chk(not dup, "同样内容但新的 ts → **放行**（不拦用户的正当重发）")

        # 内容变了也要放行
        dup, _ = bot.already_executed(item(text="另一句"))
        chk(not dup, "内容不同 → 放行")

        # 指纹要跟字段顺序无关（否则同一件事换个构造顺序就绕过了闸门）
        a = {"kind": "agent", "text": "x", "ts": 1.0}
        b = {"ts": 1.0, "text": "x", "kind": "agent"}
        chk(bot.item_fingerprint(a) == bot.item_fingerprint(b),
            "指纹与 dict 字段顺序无关")

        # 过期就不认了
        bot._STATE = None
        dup, _ = bot.already_executed(it, {"state": {"executed_ttl": 0}})
        chk(not dup, "超过 executed_ttl → 不再拦（否则 state.json 会无限长）")

        # 记新条目时顺手清过期的（种子一条很老的，再记一条新的）
        bot._STATE = None
        bot.state_set("executed", {"old_fp": {"ts": time.time() - 99999, "what": "老的"}})
        bot.remember_executed(item(text="新的", ts=3.0), {})
        book = bot.state_get("executed") or {}
        chk("old_fp" not in book and len(book) == 1,
            "记新条目时清掉过期项（账本不会越积越多）")

        # 坏账面数据不许抛
        bot.state_set("executed", "这不是字典")
        try:
            dup, _ = bot.already_executed(it)
            chk(not dup, "账面结构坏掉 → 当没执行过，不抛异常")
        except Exception as e:
            chk(False, f"账面坏掉时不该抛：{e!r}")
    finally:
        bot.STATE_PATH, bot._STATE = old_path, old_state


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
        t_executed_once(tmp)
        t_batch_survives_consumer_error(tmp)
        t_poll_failure_throttled(tmp)
        t_image_dirs_union(tmp)
        t_stash_media(tmp)
        t_inline_image_round(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    t_redact_wiring()
    t_history_window_label()
    t_usage_cmd()
    t_check_ret()
    t_own_image()
    t_broadcast_preview_note()

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
