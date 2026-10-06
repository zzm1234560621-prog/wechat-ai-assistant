"""live_history.py 的回归自测：兜底路径必须留痕 + appmsg 渲染 + LIKE 转义。

不联网、不碰 hook（30001 端口）、不需要微信：
  * 正常兜底那条路复用 selftest_aixed.py 里现成的假服务桩（_V4StaleFtsStub，真 HTTP 往返）；
  * 失败路径用一个**进程内**假客户端，按 (库名, SQL) 路由，能精确地让某一个库抛异常。
    故意不起线程、不打真端口——这批用例考的是 live_history 内部行为，不是 HTTP 层。

selftest_aixed.py 是 hook 层的回归基线，**本文件只 import 它、绝不修改它**。

用法：python selftest_live_history.py
"""
import contextlib
import inspect
import io
import sqlite3
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import aixed_api
import auto_reply
import live_history

# 复用基线里的桩与常量，避免两份假数据各写各的、慢慢漂移。
from selftest_aixed import (SELF_WXID, V4_SESSION_SUMMARY, V4_SESSION_TS,
                            _V4StaleFtsStub)

NOW = 1_700_000_000


class _FakeClient:
    """把每条 (库名, SQL) 交给路由函数的假客户端。

    用途只有一个：**让指定的库一定抛异常**，好验证兜底路径失败时到底留没留痕。
    不开线程、不开端口、不联网——这条正好顺着「hook 不支持并发」的铁律，
    这批用例本来也不需要并发。
    """

    def __init__(self, router):
        self._router = router
        self.sql_log = []
        # force_rescan 的时间戳属性：留 0 让它照常走限流分支（get_dbs 由本类兜住）
        self._lh_last_rescan = 0.0

    def query_sql(self, db, sql):
        self.sql_log.append((db, sql))
        return self._router(db, sql) or []

    def get_dbs(self):
        return []

    def take_sql(self):
        """取走并清空记录：断言只看某个调用点自己发出去的那几条 SQL。"""
        out, self.sql_log = self.sql_log, []
        return out


def _dead_db(db):
    """真实形状：拿不到句柄时 aixed 回 status<0，客户端把它抛成 AixedError。"""
    raise aixed_api.AixedError(f"get database handle which named {db} failed")


def _all_ok(db, sql):
    """什么都查得动、但一行都没有——用来观察各调用点拼出来的 SQL。"""
    if "name LIKE 'Msg" in sql:
        return [{"name": "Msg_abc"}]      # 让「逐表 LIKE」那条兜底路真的走下去
    return []


def _v3_only(db, sql):
    """只有 3.9.x 的 MicroMsg.db 可用：把 is_wechat4 判成 False，走 v3 分支。"""
    if db == "MicroMsg.db":
        return []
    _dead_db(db)


def _three_layers_dead(db, sql):
    """三层全废的形状。

    contact.db 的探针必须通，否则会被判成 3.9.x，根本走不到 v4 这条链。
    另外两层照着真实故障复刻：
      * message_fts.db：句柄失效的样子是**查询成功但 0 行**（连 sqlite_master 都列不出来）
      * message_N.db / session.db：拿不到句柄（session.db 这一条正是本次要补的：
        它以前静默 return，日志里一个字都没有）
    """
    if db == "contact.db":
        return [{"x": 1}]
    if db == "message_fts.db":
        return []
    _dead_db(db)          # 一定抛异常


def _session_idle(db, sql):
    """fts 与 Msg_ 都不行，但 session.db 查得动、确实没有新消息（正常空闲）。"""
    if db == "contact.db":
        return [{"x": 1}]
    if db == "message_fts.db":
        return []
    if db == "session.db":
        return []
    _dead_db(db)


def _session_alive(db, sql):
    """session.db 恢复正常——用来验证旧错误会被清掉。"""
    if db == "session.db":
        return [{"username": "filehelper", "summary": "兜底恢复了",
                 "last_timestamp": V4_SESSION_TS, "last_msg_sender": SELF_WXID,
                 "last_msg_type": 1}]
    _dead_db(db)


def _session_voice(db, sql):
    """最后一道兜底路上来的**语音条**：英文界面 summary 是 `[Audio] 8"`、中文是 `1"`。"""
    if db == "session.db":
        return [{"username": "filehelper", "summary": '[Audio] 8"',
                 "last_timestamp": V4_SESSION_TS, "last_msg_sender": SELF_WXID,
                 "last_msg_type": 34}]
    _dead_db(db)


def _session_no_type(db, sql):
    """老库/结构变了：`last_msg_type` 取不到（NULL）——行为必须和以前**完全一致**。"""
    if db == "session.db":
        return [{"username": "filehelper", "summary": "老库没有这一列",
                 "last_timestamp": V4_SESSION_TS, "last_msg_sender": SELF_WXID,
                 "last_msg_type": None}]
    _dead_db(db)


def _fts_shard_dead(db, sql):
    """fts 分片表还列得出来（句柄表没重建），但查分片本身报错。"""
    if db == "contact.db":
        return [{"x": 1}]        # v4 探针要通，否则会被判成 3.9.x，走不到 fts 这条链
    if db == "message_fts.db":
        if "CREATE VIRTUAL TABLE" in sql:
            return [{"name": "message_fts_v4_0"}]
        _dead_db(db)
    _dead_db(db)


def _fts_ok(db, sql):
    """fts 分片恢复正常（本轮只是没有新消息）。"""
    if db == "message_fts.db":
        if "CREATE VIRTUAL TABLE" in sql:
            return [{"name": "message_fts_v4_0"}]
        return []
    return []


def _fts_history(db, sql):
    """4.x 的 **fts 主路径**：某个会话的历史从 message_fts 分片里取。

    这条是生产里查历史的主路径（`query_contact_history` 先走它、命中就返回），
    以前**一个用例都没有**。这里要钉住的是：**非文本消息也必须带 `local_type`**——
    它的 content 已经被渲染成 `[图片]` 这类标签，光看 content 分不出
    「一句话」和「一张图的标签」，而 `auto_reply` 的「从历史学语气」正靠这个字段
    挑出用户自己发的**文本**。fts 那条路以前把这个字段算完就丢了。
    """
    if db == "contact.db":
        return [{"x": 1}]        # v4 探针要通，否则会被判成 3.9.x，走不到 fts 这条链
    if db == "message_fts.db":
        if "CREATE VIRTUAL TABLE" in sql:
            return [{"name": "message_fts_v4_0"}]
        if "SELECT rowid, username FROM Name2Id" in sql:
            # fts 库自己的 id 空间：100=对方会话，55=我自己
            return [{"rowid": 100, "username": "wxid_friend"},
                    {"rowid": 55, "username": SELF_WXID}]
        if "SELECT rowid FROM Name2Id" in sql:
            if SELF_WXID in sql:
                return [{"rowid": 55}]
            return [{"rowid": 100}]
        if "FROM message_fts_v4_0" in sql:
            return [
                {"acontent": "我发的一句话", "session_id": 100, "sender_id": 55,
                 "create_time": NOW + 1, "local_type": 1, "message_local_id": 1},
                {"acontent": "对方回的一句", "session_id": 100, "sender_id": 100,
                 "create_time": NOW + 2, "local_type": 1, "message_local_id": 2},
                # 我发的一张图：fts 里没有正文，content 会被渲染成 `[图片]`
                {"acontent": "", "session_id": 100, "sender_id": 55,
                 "create_time": NOW + 3, "local_type": 3, "message_local_id": 7},
            ]
        return []
    _dead_db(db)


def _msg_path_alive(db, sql):
    """fts 拿不到、但 Msg_ 表这条路自己捞到了消息（session.db 不在链路上）。"""
    if db == "session.db":
        if "SELECT username FROM SessionTable" in sql:
            return [{"username": "wxid_a"}]
        return []
    if "FROM Msg_" in sql:
        return [{"local_id": 5, "local_type": 1, "real_sender_id": 0,
                 "create_time": NOW + 5, "message_content": "你好"}]
    return []


# ---------- Msg_ 兜底路的发言人解析 ----------
#
# 真实形状：**每个 message_N.db 各有一份 Name2Id，rowid 不通用**。
# 所以下面这几个桩都只让 message_0.db 可用（其余分片老实报「拿不到句柄」）——
# _v4_msg_dbs 探到几个分片就用几个，全放行的话同一批行会在每个分片里各查出一遍，
# 「两个发言人」的断言就分不清是解析对了还是重复了。

def _msg_two_speakers(db, sql):
    """一个群里两个发言人：两行不同的 real_sender_id，本分片 Name2Id 各对应一个 wxid。"""
    if db != "message_0.db":
        _dead_db(db)
    if "FROM Name2Id" in sql:
        if "rowid IN" in sql:
            return [{"rowid": 7, "user_name": "wxid_alice"},
                    {"rowid": 8, "user_name": "wxid_bob"}]
        return []                      # 查「我自己」：不在这个分片里（is_self 全 0）
    if "FROM Msg_" in sql:
        return [{"local_id": 11, "local_type": 1, "real_sender_id": 8,
                 "create_time": NOW + 11, "message_content": "我是 bob"},
                {"local_id": 10, "local_type": 1, "real_sender_id": 7,
                 "create_time": NOW + 10, "message_content": "我是 alice"}]
    return []


def _msg_self_row(db, sql):
    """自己发的那条：real_sender_id 等于本分片里我自己的 rowid。"""
    if db != "message_0.db":
        _dead_db(db)
    if "FROM Name2Id" in sql:
        if "rowid IN" in sql:
            return [{"rowid": 9, "user_name": SELF_WXID}]
        return [{"rowid": 9}]          # 查自己：解出来
    if "FROM Msg_" in sql:
        return [{"local_id": 3, "local_type": 1, "real_sender_id": 9,
                 "create_time": NOW + 3, "message_content": "我发的"}]
    return []


def _msg_names_missing(db, sql):
    """Msg_ 表读得动，但本分片 Name2Id 里**根本没有**这些 real_sender_id。"""
    if db != "message_0.db":
        _dead_db(db)
    if "FROM Name2Id" in sql:
        return []                      # 查得动、里面什么都没有
    if "FROM Msg_" in sql:
        return [{"local_id": 5, "local_type": 1, "real_sender_id": 42,
                 "create_time": NOW + 5, "message_content": "谁啊"}]
    return []


def _msg_names_dead(db, sql):
    """Name2Id 查不动（hook 层面的故障），但 Msg_ 表本身读得动。"""
    if db != "message_0.db":
        _dead_db(db)
    if "FROM Name2Id" in sql:
        _dead_db(db)
    if "FROM Msg_" in sql:
        return [{"local_id": 5, "local_type": 1, "real_sender_id": 42,
                 "create_time": NOW + 5, "message_content": "谁啊"}]
    return []


def _raw_id_fields(rows):
    """行里除 talker / sender 之外的字段值，凡是「像原始 id」的都挑出来。

    talker（会话 id）和 sender（wxid）本来就是原始 id，是**给上层查表用**的；
    除此之外任何字段（尤其 sender_name）出现原始 id 就是把 id 当名字用了。
    """
    bad = []
    for m in rows:
        for k, v in m.items():
            if k in ("talker", "sender"):
                continue
            s = str(v or "")
            if s.startswith("wxid_") or s.endswith("@chatroom"):
                bad.append((k, s))
    return bad


def _clear_poll_errors():
    """每段用例都从「没有任何轮询失败」开始，断言才精确。

    这里确实要碰私有全局：本文件考的就是「失败有没有写进 _POLL_ERRORS」。
    """
    live_history._POLL_ERRORS.clear()


def _like_hits(condition, values):
    """把 live_history 拼出来的 LIKE 条件丢进真 SQLite 跑一遍。

    用真引擎验「按字面匹配」比字符串比对更能说明问题（转义符对不对、ESCAPE 生效没）。
    """
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE TABLE t (v TEXT)")
        con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
        return sorted(r[0] for r in con.execute(f"SELECT v FROM t WHERE {condition}"))
    finally:
        con.close()


def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return cond


def main():
    ok = True
    live_history.set_self_wxid(SELF_WXID)

    print("── T3：appmsg 标签不再是内部数字 ──")
    ok &= check("先钉住成因：_type_label(4|(49<<0)) 就是「类型53」",
                live_history._type_label(4 | (49 << 0)) == "类型53")
    out = live_history.render_appmsg("<appmsg><title>一条链接</title></appmsg>")
    ok &= check("非引用的 appmsg 不再产出 类型53",
                "类型53" not in out and out == "[消息] 一条链接", out)
    ok &= check("按 _type_label 的约定传 49 会落到「消息」",
                live_history._type_label(49) == "消息")

    print("\n── T5：只有 <des> 的消息不能白读 ──")
    out = live_history.render_appmsg("<appmsg><des>只有描述</des></appmsg>", "无用的摘要")
    ok &= check("<des> 能单独成词，且保住 [标签] 风格（摘要不许顶掉它）",
                out == "[消息] 只有描述", out)
    out = live_history.render_appmsg("<appmsg><content>正文</content></appmsg>")
    ok &= check("只有 <content> 的同理", out == "[消息] 正文", out)
    out = live_history.render_appmsg("<appmsg></appmsg>", "摘要")
    ok &= check("什么都抠不出来时退回「标签 + 摘要」，不是 类型0",
                out == "[消息] 摘要", out)
    out = live_history.render_appmsg("", "摘要")
    ok &= check("XML 解码失败（空串）时同样是 [消息]，不再是 [类型0]",
                out == "[消息] 摘要", out)
    ok &= check("只有 <title> 时仍是「标签 + 标题」",
                live_history.render_appmsg(
                    "<appmsg><title>甲</title><des>说明</des></appmsg>") == "[消息] 甲")
    ref = ("<appmsg><refermsg><displayname>张三</displayname>"
           "<content>被引用的原文</content></refermsg><title>我打的字</title></appmsg>")
    ok &= check("引用消息的既有格式一个字都没变",
                live_history.render_appmsg(ref) == "[引用 张三：被引用的原文] 我打的字",
                live_history.render_appmsg(ref))

    print("\n── T4：LIKE 通配符按字面匹配（真 SQLite 验语义）──")
    vals = ["进度50%完成", "进度5012完成", "50%", "5012"]
    hit = _like_hits(live_history._like("v", "50%"), vals)
    ok &= check("搜「50%」只命中真的含 50% 的", hit == ["50%", "进度50%完成"], hit)
    old = _like_hits("v LIKE '%50%%'", vals)      # 旧写法：% 当通配符使
    ok &= check("对照：旧写法确实会多命中（这就是要修的坑）", set(old) > set(hit), old)
    vals2 = ["A_B 你好", "AXB 你好", "A_B"]
    hit2 = _like_hits(live_history._like("v", "A_B"), vals2)
    ok &= check("搜「A_B」不再连 AXB 一起命中（_ 也是通配符）",
                hit2 == ["A_B", "A_B 你好"], hit2)
    vals3 = ["a\\b", "ab", "axb", "a%b"]
    hit3 = _like_hits(live_history._like("v", "a\\b"), vals3)
    ok &= check("转义符本身（反斜杠）也按字面匹配", hit3 == ["a\\b"], hit3)
    hit4 = _like_hits(live_history._like("v", "o'b"), ["o'b", "oxb", "ob"])
    ok &= check("单引号照旧转义，值没被拆坏", hit4 == ["o'b"], hit4)

    print("\n── T4：每个 LIKE 调用点都带 ESCAPE（看真发出去的 SQL）──")

    def likes(sqls):
        return [s for _, s in sqls if "LIKE" in s]

    def like_ok(sqls, needle):
        ls = likes(sqls)
        return bool(ls) and all("ESCAPE '\\'" in s for s in ls) \
            and any(needle in s for s in ls), ls

    rec = _FakeClient(_all_ok)
    live_history._v3_search(rec, "50%")
    good, ls = like_ok(rec.take_sql(), "50\\%")
    ok &= check("_v3_search（跨库关键词检索）", good, ls[:1])

    live_history._v3_query_history(rec, "wxid_friendA", keyword="A_B")
    good, ls = like_ok(rec.take_sql(), "A\\_B")
    ok &= check("_v3_query_history（某一会话里按关键词翻）", good, ls[:1])

    live_history._v4_history_from_tables(rec, "wxid_friendA", keyword="50%")
    good, ls = like_ok(rec.take_sql(), "50\\%")
    ok &= check("_v4_history_from_tables（Msg_ 表按关键词翻）", good, ls[:1])

    live_history._v4_search_by_scan(rec, "A_B", max_tables=1)
    good, ls = like_ok(rec.take_sql(), "A\\_B")
    ok &= check("_v4_search_by_scan（没有 fts 时的逐表兜底）", good, ls[:1])

    live_history.resolve_contact(rec, "50%")
    ls = likes(rec.take_sql())
    ok &= check("resolve_contact（4.x）四个字段都带 ESCAPE",
                len(ls) == 1 and ls[0].count("ESCAPE '\\'") == 4 and "50\\%" in ls[0], ls)

    rec3 = _FakeClient(_v3_only)
    live_history.resolve_contact(rec3, "A_B")
    ls = likes(rec3.take_sql())
    ok &= check("resolve_contact（3.9.x）四个字段都带 ESCAPE",
                len(ls) == 1 and ls[0].count("ESCAPE '\\'") == 4 and "A\\_B" in ls[0], ls)

    # 顺手钉住「= 精确匹配」那条路没被 LIKE 转义污染
    rec4 = _FakeClient(_all_ok)
    live_history._v4_fts_session_id(rec4, "o'brien")
    s = rec4.take_sql()[0][1]
    ok &= check("精确相等仍走 _q（引号加倍、没有 LIKE 转义、没有 ESCAPE）",
                "= 'o''brien'" in s and "ESCAPE" not in s and "\\" not in s, s)

    print("\n── T1（前半）：fts 分片自己失败时也要留痕（键是分片名）──")
    _clear_poll_errors()
    shard = _FakeClient(_fts_shard_dead)
    errf = io.StringIO()
    with contextlib.redirect_stderr(errf):
        msgs_f, _ = live_history.new_messages(shard, {"message_fts_v4_0": 0})
    ok &= check("分片失败写进 _POLL_ERRORS",
                "message_fts_v4_0" in live_history.poll_errors(),
                live_history.poll_errors())
    ok &= check("stderr 有分片告警（带异常原文）",
                "轮询 message_fts_v4_0 连续失败 1 次" in errf.getvalue()
                and "failed" in errf.getvalue(), repr(errf.getvalue()))
    ok &= check("分片失败时不返回消息", msgs_f == [], msgs_f)

    print("\n── T1：fts / Msg_ / session.db 三层全废，必须报错不许静默 ──")
    _clear_poll_errors()
    dead = _FakeClient(_three_layers_dead)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        msgs, _cur = live_history.new_messages(dead, {"__time__": NOW})
    warn = err.getvalue()
    errs = live_history.poll_errors()
    ok &= check("三层全废时不返回假消息", msgs == [], msgs)
    ok &= check("失败写进了 _POLL_ERRORS，键是独立的 session.db",
                "session.db" in errs, errs)
    ok &= check("_POLL_ERRORS 里带异常原文",
                "session.db failed" in str(errs.get("session.db", ("", 0))[0]),
                errs.get("session.db"))
    ok &= check("poll_errors() 能被 bot 心跳读出来（次数在涨）",
                errs.get("session.db", ("", 0))[1] == 1, errs)
    ok &= check("stderr 有明确告警（带异常原文）",
                "轮询 session.db 连续失败" in warn and "session.db failed" in warn,
                repr(warn))
    ok &= check("stderr 说清是三层的最后一道也废了",
                "三层都不可用" in warn, repr(warn))

    # 恢复后旧错误必须清掉，否则心跳会一直挂着一条假故障
    alive = _FakeClient(_session_alive)
    msgs2, _ = live_history._v4_new_messages_session(alive, {"__time__": NOW})
    ok &= check("session.db 恢复后错误被清掉、消息照常拿到",
                live_history.poll_errors() == {} and len(msgs2) == 1,
                (live_history.poll_errors(), msgs2))
    ok &= check("文本消息的 local_type=1 也带上了（不破坏文本那条路）",
                len(msgs2) == 1 and msgs2[0].get("local_type") == 1, msgs2)

    # ⚠️ **兜底路必须带 `local_type`**（2026-10-03 晚）：以前这条路的产出没有它，
    # 于是语音条进不去语音分支 —— 英文界面 summary 是 `[Audio] 8"`，被下游当
    # "只有类型标签"**静默丢掉**（连一句失败提示都没有）；中文界面 summary 是 `1"`，
    # 更糟：那串时长会被当成**用户说的话**送进模型。
    voice = _FakeClient(_session_voice)
    vm, _ = live_history._v4_new_messages_session(voice, {"__time__": NOW})
    ok &= check("兜底路上的语音带 local_type=34（下游才进得去语音分支）",
                len(vm) == 1 and vm[0].get("local_type") == 34, vm)
    ok &= check("……summary 原样带着（下游据此如实说读不出来，不编）",
                bool(vm) and "[Audio]" in vm[0].get("content", ""), vm)
    # 取不到 `last_msg_type` → **不加这个键**，行为与改动前一字不差
    notype = _FakeClient(_session_no_type)
    nm, _ = live_history._v4_new_messages_session(notype, {"__time__": NOW})
    ok &= check("取不到 last_msg_type → **不加 local_type 键**（老库行为不变）",
                len(nm) == 1 and "local_type" not in nm[0], nm)

    # 「我在这条路上认不认得出来」：这层比的是 `sender == _SELF_WXID`，所以只有
    # `last_msg_sender` 真的是 wxid 形状时才算认得出。有的行/有的构建给的是数字 id，
    # 那种行 is_self 必然是 0（= 我发的会被当成对方发的，bot 会去答自己刚发的回复），
    # 这时 fail-safe 闸门必须打开（见 live_history.self_identity_ok）。
    ok &= check("★ 兜底路的 sender 是 wxid → 认得自己（兜底闸门不打开）",
                live_history.self_identity_ok() is True,
                live_history.self_identity_ok())

    def _session_numeric_sender(db, sql):
        if db == "session.db":
            return [{"username": "filehelper", "summary": "数字 id 的说话人",
                     "last_timestamp": V4_SESSION_TS, "last_msg_sender": 49,
                     "last_msg_type": 1}]
        _dead_db(db)

    _num, _ = live_history._v4_new_messages_session(
        _FakeClient(_session_numeric_sender), {"__time__": NOW})
    ok &= check("★ 兜底路的 sender 不是 wxid（数字 id）→ 如实判「认不出自己」，闸门打开",
                live_history.self_identity_ok() is False
                and bool(_num) and _num[0].get("is_self") == 0,
                (live_history.self_identity_ok(), _num))
    live_history._SELF_ID_OK = None                       # 别影响后面的用例

    # 接线钉子：**三条**收消息的路各自都要把「认不认得自己」记下来，否则 fail-safe
    # 闸门在某条路上永远是「不知道」＝不生效（那正是它要防的那条路）。
    _wiring = (inspect.getsource(live_history._v4_new_messages)
               + inspect.getsource(live_history._v4_history_from_tables)
               + inspect.getsource(live_history._v4_new_messages_session))
    ok &= check("★ 三条路（fts / 会话表 / session 兜底）都记了「认不认得自己」",
                _wiring.count("_note_self_id(") >= 3, _wiring.count("_note_self_id("))

    # 「认不出自己」必须**留一行痕**（以前一个字都不说，用户只看得到「重复回复」）；
    # 但要等连续 3 轮 —— 单轮查不到也可能只是 hook 那一瞬间不接，一抖就喊
    # 「认不出自己」会把排查方向指错（本项目反复踩过「日志骗人」）。
    _save_note, _save_fails = live_history._SELF_ID_NOTE_ONCE[0], live_history._SELF_ID_FAILS[0]
    try:
        live_history._SELF_ID_NOTE_ONCE[0] = False
        live_history._SELF_ID_FAILS[0] = 0
        _err = io.StringIO()
        with contextlib.redirect_stderr(_err):
            live_history._note_self_id(False, "自测：对不上")
            live_history._note_self_id(False, "自测：对不上")
            _early = _err.getvalue()
            live_history._note_self_id(False, "自测：对不上")
            live_history._note_self_id(False, "自测：对不上")
        _txt = _err.getvalue()
        ok &= check("★ 第 1~2 轮认不出**先不喊**（可能只是 hook 抖了一下）",
                    _early == "", repr(_early))
        ok &= check("★ 连续 3 轮认不出 → 留一行痕，写清原因与修法，且不刷屏",
                    _txt.count("认不出「我自己」") == 1
                    and "自测：对不上" in _txt and "find_self_wxid" in _txt,
                    repr(_txt[:160]))
        _err2 = io.StringIO()
        with contextlib.redirect_stderr(_err2):
            live_history._note_self_id(True)
            live_history._note_self_id(False, "又一次")
        ok &= check("认得出之后计数清零（下次真的要连续 3 轮才再报）",
                    _err2.getvalue() == "" and live_history._SELF_ID_FAILS[0] == 1,
                    (repr(_err2.getvalue()), live_history._SELF_ID_FAILS[0]))
    finally:
        live_history._SELF_ID_NOTE_ONCE[0], live_history._SELF_ID_FAILS[0] = _save_note, _save_fails
        live_history._SELF_ID_OK = None

    # 正常空闲**不许**报故障（否则每 5 秒刷一行吓人的日志）
    _clear_poll_errors()
    idle = _FakeClient(_session_idle)
    err2 = io.StringIO()
    with contextlib.redirect_stderr(err2):
        msgs3, _ = live_history.new_messages(idle, {"__time__": NOW})
    ok &= check("session.db 查得动、只是没新消息时，不报错也不刷日志",
                msgs3 == [] and live_history.poll_errors() == {}
                and "session.db" not in err2.getvalue(),
                (msgs3, live_history.poll_errors(), repr(err2.getvalue())))

    # 旧错误不许永远挂着：fts 修好后它就不再是「当前链路」上的失败了
    live_history._POLL_ERRORS["session.db"] = ("上一轮的旧错误", 3)
    live_history.new_messages(_FakeClient(_fts_ok), {"message_fts_v4_0": 0})
    ok &= check("fts 恢复后再走 fts 那条路，旧 session.db 告警被清掉",
                "session.db" not in live_history.poll_errors(),
                live_history.poll_errors())

    _clear_poll_errors()
    live_history._POLL_ERRORS["session.db"] = ("上一轮的旧错误", 3)
    msgs_m, _ = live_history._v4_new_messages_tables(
        _FakeClient(_msg_path_alive), {"__time__": NOW})
    ok &= check("Msg_ 这条路自己捞到消息时，同样清掉旧的 session.db 告警",
                bool(msgs_m) and "session.db" not in live_history.poll_errors(),
                (msgs_m, live_history.poll_errors()))

    print("\n── 复用 selftest_aixed 的 _V4StaleFtsStub：真兜底路径不许误报 ──")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _V4StaleFtsStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = aixed_api.AixedClient(base_url=f"http://127.0.0.1:{srv.server_address[1]}")
        live_history.set_rescan_interval(0)     # 本用例不触发 GetAllDBName
        _clear_poll_errors()
        err3 = io.StringIO()
        with contextlib.redirect_stderr(err3):
            msgs4, cur4 = live_history.new_messages(c, {"__time__": NOW})
        ok &= check("fts + Msg_ 双掉线仍能从 session.db 拿到消息",
                    len(msgs4) == 1 and msgs4[0]["content"] == V4_SESSION_SUMMARY, msgs4)
        ok &= check("游标推进到最新", cur4.get("__time__") == V4_SESSION_TS, cur4)
        ok &= check("这条正常路不许写 _POLL_ERRORS、也不许报 session.db",
                    live_history.poll_errors() == {}
                    and "session.db" not in err3.getvalue(),
                    (live_history.poll_errors(), repr(err3.getvalue())))
    finally:
        live_history.set_rescan_interval(300)
        srv.shutdown()

    print("\n── Msg_ 兜底路：同一群里两个发言人必须能区分 ──")

    def _fresh_v4():
        """每段用例都从干净状态开始：轮询失败表和「缺失 id」限流表都要清。"""
        _clear_poll_errors()
        live_history._N2ID_MISS_NOTE.clear()

    _fresh_v4()
    two = live_history._v4_history_from_tables(
        _FakeClient(_msg_two_speakers), "1234@chatroom", limit=10)
    by_content = {m["content"]: m for m in two}
    ok &= check("两行都在，content 没被改",
                sorted(by_content) == ["我是 alice", "我是 bob"], two)
    ok &= check("两个人的 sender 都解出来了、而且互不相同",
                {m["sender"] for m in two} == {"wxid_alice", "wxid_bob"},
                [m["sender"] for m in two])
    ok &= check("wxid 和各自那行对得上（没有张冠李戴）",
                by_content["我是 alice"]["sender"] == "wxid_alice"
                and by_content["我是 bob"]["sender"] == "wxid_bob",
                {k: v["sender"] for k, v in by_content.items()})
    ok &= check("sender_name 一律留空（这条路上没有真显示名，不许拿 wxid 顶）",
                all(m.get("sender_name") == "" for m in two), two)
    ok &= check("老字段一个字没变（content/is_self/time/_ts）",
                all(m["is_self"] == 0 and m["_ts"] in (NOW + 10, NOW + 11)
                    and m["time"] == live_history._fmt_time(m["_ts"]) for m in two), two)
    ok &= check("解析成功就不许写 _POLL_ERRORS（那会是假故障）",
                live_history.poll_errors() == {}, live_history.poll_errors())
    ok &= check("除 talker / sender 外没有任何字段装原始 id",
                _raw_id_fields(two) == [], _raw_id_fields(two))

    # 交给上层真正渲染一遍：文本里绝不许出现裸 wxid
    note = []
    text = auto_reply.build_transcript(two, {}, unknown_note=note)
    ok &= check("给模型的文本里没有一个裸 wxid",
                "wxid_alice" not in text and "wxid_bob" not in text, text)
    ok &= check("上层拿不到显示名时退回编号，且两个人是两个编号",
                "对方1" in text and "对方2" in text and bool(note), (text, note))

    _fresh_v4()
    mine = live_history._v4_history_from_tables(
        _FakeClient(_msg_self_row), "1234@chatroom", limit=10)
    ok &= check("自己发的仍然认成 is_self=1（语义没动）",
                len(mine) == 1 and mine[0]["is_self"] == 1, mine)
    ok &= check("自己那行的 sender 也是 wxid（和 fts 那条路保持一致）",
                mine and mine[0]["sender"] == SELF_WXID, mine)

    print("\n── Msg_ 兜底路：解析不出时不许编名字 ──")
    _fresh_v4()
    err_u = io.StringIO()
    with contextlib.redirect_stderr(err_u):
        unk = live_history._v4_history_from_tables(
            _FakeClient(_msg_names_missing), "1234@chatroom", limit=10)
    ok &= check("Name2Id 里没有这个 id：sender 留空，不编",
                len(unk) == 1 and unk[0]["sender"] == "" and unk[0]["sender_name"] == "",
                unk)
    ok &= check("消息本身照样返回（不许因为解不出发言人就丢消息）",
                len(unk) == 1 and unk[0]["content"] == "谁啊", unk)
    ok &= check("行里没有一个字段把 wxid 当名字", _raw_id_fields(unk) == [],
                _raw_id_fields(unk))
    ok &= check("stderr 如实说明「这些 real_sender_id 查不到」",
                "Name2Id 里查不到这些 real_sender_id" in err_u.getvalue()
                and "42" in err_u.getvalue(), repr(err_u.getvalue()))
    ok &= check("这类数据缺失**不进** _POLL_ERRORS"
                "（否则 health._healthy 会把 bot 永远判成不健康）",
                live_history.poll_errors() == {}, live_history.poll_errors())
    text_u = auto_reply.build_transcript(unk, {}, unknown_note=[])
    ok &= check("退到编号渲染，文本里没有裸 wxid、也没有那个数字 id",
                "wxid" not in text_u and "42" not in text_u, text_u)

    print("\n── Msg_ 兜底路：Name2Id 查不动要留痕，恢复后要自己清掉 ──")
    _fresh_v4()
    err_d = io.StringIO()
    with contextlib.redirect_stderr(err_d):
        dead_names = live_history._v4_history_from_tables(
            _FakeClient(_msg_names_dead), "1234@chatroom", limit=10)
    key = "message_0.db Name2Id"
    ok &= check("查不动时消息仍然返回（只是没有发言人）",
                len(dead_names) == 1 and dead_names[0]["sender"] == "",
                dead_names)
    ok &= check("查不动写进 _POLL_ERRORS，键标明是哪个分片的 Name2Id",
                key in live_history.poll_errors(), live_history.poll_errors())
    ok &= check("stderr 有明确告警（带异常原文）",
                key in err_d.getvalue() and "failed" in err_d.getvalue(),
                repr(err_d.getvalue()))
    ok &= check("这个键不许把别的分片故障挤掉/混用",
                set(live_history.poll_errors()) == {key}, live_history.poll_errors())

    _fresh_v4()
    live_history._v4_history_from_tables(_FakeClient(_msg_names_dead),
                                        "1234@chatroom", limit=10)
    live_history._v4_history_from_tables(_FakeClient(_msg_two_speakers),
                                        "1234@chatroom", limit=10)
    ok &= check("Name2Id 恢复后旧告警被清掉，不许永远挂在心跳上",
                key not in live_history.poll_errors(), live_history.poll_errors())

    # 桩里的 real_sender_id=0（真实数据里就是「这行没写谁发的」）：
    # 没有可解析的东西，**不许**当成解析失败刷告警——那会把真故障淹掉。
    _fresh_v4()
    err_z = io.StringIO()
    with contextlib.redirect_stderr(err_z):
        live_history._v4_history_from_tables(
            _FakeClient(_msg_path_alive), "wxid_a", limit=10)
    ok &= check("real_sender_id=0 不当成解析失败：不报警、不刷日志",
                live_history.poll_errors() == {}
                and "Name2Id" not in err_z.getvalue(),
                (live_history.poll_errors(), repr(err_z.getvalue())))

    print("\n── Msg_ 兜底路：绝不能用 fts 的 Name2Id 解本分片的 id ──")
    _fresh_v4()
    sent_sql = _FakeClient(_msg_two_speakers)
    live_history._v4_history_from_tables(sent_sql, "1234@chatroom", limit=10)
    n2id = [(db, sql) for db, sql in sent_sql.sql_log if "Name2Id" in sql]
    mapped = [(db, sql) for db, sql in n2id if "rowid IN" in sql]
    ok &= check("所有 Name2Id 查询都打在 message_N.db 上（一次都没碰 message_fts.db）",
                bool(n2id) and all(db.startswith("message_") for db, _ in n2id), n2id)
    ok &= check("解 real_sender_id 那条用的是本分片的 user_name 列",
                bool(mapped) and all("user_name" in s for _, s in mapped), mapped)
    ok &= check("带选择性过滤（rowid IN）、不做排序",
                all("rowid IN" in s and "ORDER BY" not in s for _, s in mapped), mapped)

    print("\n── fts 主路径的历史字段：非文本要带 local_type ──")
    ok &= _t_fts_history_fields()
    ok &= _t_appmsg_breaker()
    ok &= _t_align_stale_cursor()
    ok &= _t_skip_far_behind()
    ok &= _t_talker_miss()
    ok &= _t_fts_batch()

    print("\n── 非文本补漏：图片不在 fts 里，得靠 SessionTable 的信号捞回来 ──")
    ok &= _t_nonttext_pickup()

    print("\n── 非文本补漏的窗口：不许被本批更晚的消息推走（语音条就是这么丢的）──")
    ok &= _t_poll_window_keeps_nontext()

    print("\n── fts 与 session.db 同时失效：靠 sqlite_sequence 照样收得到 ──")
    ok &= _t_fallback_survives_dead_session_db()

    print("\n── 微信自带的「标签」：成员藏在 contact_fts 的 search_key 第 4 段 ──")
    ok &= _t_labels()

    print("\n" + "=" * 50)
    print("全部通过 ✅" if ok else "有失败项 ❌")
    print("=" * 50)
    return 0 if ok else 1


# 非文本补漏的用例时间戳**必须是「刚刚」**：还没打过基线的会话要过
# `_NONTEXT_BOOTSTRAP_WINDOW`（默认 1800 秒）那道兜底闸，用固定的远古时刻会被它挡掉。
_IMG_TS = int(time.time()) - 100



def _t_fts_history_fields():
    """fts 主路径的历史必须带 `local_type`——「学语气」靠它挑出用户自己发的文本。

    真踩过（2026-10-01）：`_v4_fts_rows` 把 local_type 读出来渲染完就**丢了**，
    返回的字典里没有它。后果是 `auto_reply._learn_messages` 里那条
    「非文本不算语气样本」的过滤**在生产主路径上根本不生效**：图片会被渲染成
    `[图片]` 混进学习样本。而当时的自测用的是带 local_type 的假数据，
    所以**测试是绿的、生产是漏的**——这条用例就是把这两边钉在一起。
    """
    ok = True
    c = _FakeClient(_fts_history)
    hist = live_history.query_contact_history(c, "wxid_friend")
    ok &= check("fts 主路径拿得到历史（3 条：我一句、对方一句、我一张图）",
                len(hist) == 3, hist)
    ok &= check("每条都带 local_type（和 Msg_ 那条路的形状一致）",
                all("local_type" in m for m in hist), hist)
    img = [m for m in hist if "[图片]" in str(m.get("content"))]
    ok &= check("图片渲染成 [图片]、但 local_type 仍是 3（不许塌成 1）",
                len(img) == 1 and img[0]["local_type"] == 3, img)
    ok &= check("is_self 认得对（我发的两条、对方一条）",
                [m["is_self"] for m in hist] == [1, 0, 1],
                [m["is_self"] for m in hist])

    # 跨模块契约：学语气只该拿「我发的 + 文本」
    samples = auto_reply._learn_messages(hist)
    ok &= check("学语气只取我发的文本：图片标签和对方的话都被挡掉",
                len(samples) == 1 and "我发的一句话" in samples[0]
                and all("[图片]" not in s for s in samples), samples)
    return ok


class _PickupStub:
    """只回答 _v4_pickup_nontext 需要的那几个查询。

    真实故障的形状（2026-10-01 实测）：**图片不在 fts 里**（四个分片
    `local_type=3` 全是 0 条），而 SessionTable 里那个会话 `summary` 是空串。

    2026-10-04 晚起，新鲜度信号换成了 `message_0.db.sqlite_sequence`
    （每个 `Msg_<hash>` 一行的最大 `local_id`）—— 所以这个桩要同时回答它。
    """

    def __init__(self, last_ts=_IMG_TS, has_image=True, last_type=3, rows=None):
        self.last_ts = last_ts
        self.has_image = has_image
        self.last_type = last_type
        # rows = 显式给这个会话的消息表内容（用来演「图后面跟了一句话」）
        self.rows = rows
        self.sql_log = []

    @property
    def seq(self):
        """这个会话的**最大 local_id** —— 新的那条新鲜度信号。"""
        if self.rows is None:
            return 7 if self.has_image else 0
        best = 0
        for r in self.rows:
            try:
                best = max(best, int(r.get("local_id") or 0))
            except (TypeError, ValueError):
                pass
        return best

    def query_sql(self, db, sql):
        self.sql_log.append((db, sql))
        if db == "session.db":
            # 反查表名 → 会话名（`SELECT username FROM SessionTable`）也走这里
            return [{"username": "filehelper", "last_timestamp": str(self.last_ts),
                     "last_msg_type": self.last_type}]
        if db.startswith("message_"):
            # 真机上该会话的表**只在一个分片里**（其余分片探测就该失败）——
            # 不过这一条，`_v4_msg_dbs` 会返回 8 个库、同一批行被复制 8 份，
            # 最后 `rows[-limit:]` 只留下时间最新的那几条（自测会假绿/假红）。
            if db != "message_0.db":
                return []
            if "sqlite_sequence" in sql:
                return ([{"name": live_history._v4_table_for("filehelper"),
                          "seq": str(self.seq)}] if self.seq else [])
            if "sqlite_master" in sql:
                return [{"x": 1}]
            if "FROM Msg_" in sql:
                if self.rows is not None:
                    return self.rows
                if self.has_image:
                    return [{"local_id": "7", "local_type": "3",
                             "create_time": str(self.last_ts),
                             "real_sender_id": "0", "message_content": ""}]
                return []
            return []
        raise aixed_api.AixedError(db)

    def get_dbs(self):
        return []

    def msg_queries(self):
        return [s for db, s in self.sql_log if "FROM Msg_" in s]


class _PickupMultiStub:
    """多会话版：每个会话各有自己的 last_timestamp / last_msg_type / 消息行。

    按 `Msg_<md5(会话)>` 表名定位到会话（`_v4_table_for` 就是那条映射），
    所以「哪个会话被查了」是**真的**由 SQL 决定的，不是靠调用顺序猜的。
    """

    def __init__(self, sessions):
        # sessions = {talker: {"last_ts": int, "last_type": int, "rows": [...]}}
        self.sessions = sessions
        self._by_table = {live_history._v4_table_for(t): s
                          for t, s in sessions.items()}
        self.sql_log = []

    def query_sql(self, db, sql):
        self.sql_log.append((db, sql))
        if db == "session.db":
            since = 0
            if "last_timestamp >= " in sql:
                try:
                    since = int(sql.split("last_timestamp >= ")[1].split()[0])
                except (IndexError, ValueError):
                    since = 0
            return [{"username": t, "last_timestamp": str(s["last_ts"]),
                     "last_msg_type": s.get("last_type", 1)}
                    for t, s in self.sessions.items() if s["last_ts"] >= since]
        if db.startswith("message_"):
            if db != "message_0.db":
                return []                # 同上：一个会话的表只在一个分片里
            if "sqlite_sequence" in sql:
                out = []
                for s in self.sessions.values():
                    best = max([int(r.get("local_id") or 0)
                                for r in (s.get("rows") or [])] or [0])
                    if best:
                        tbl = live_history._v4_table_for(
                            [t for t, x in self.sessions.items() if x is s][0])
                        out.append({"name": tbl, "seq": str(best)})
                return out
            if "sqlite_master" in sql:
                return [{"x": 1}]
            if "FROM Msg_" in sql:
                for tbl, s in self._by_table.items():
                    if tbl in sql:
                        return list(s.get("rows") or [])
                return []
            return []
        raise aixed_api.AixedError(db)

    def get_dbs(self):
        return []

    def msg_queries(self):
        return [s for db, s in self.sql_log if "FROM Msg_" in s]


def _t_appmsg_breaker():
    """appmsg 原文回查：坏库时**不许按行砸**，而且取不到要如实说（2026-10-05 真机）。

    真机现场：追赶积压时每条 appmsg 都回查一次 message_N.db，而句柄坏掉时
    `_v4_msg_dbs` 的空结果**不缓存** → 每条最多再打 8 次探测查询；日志里就是
    `⚠️ 慢查询 1.59s db=message_0.db` + `get database handle … failed` 一行行刷。
    """
    print("\n── appmsg 原文回查：熔断 + 如实标注（2026-10-05 真机）──")
    ok = True
    live_history.reset_shard_breakers()
    _clear_poll_errors()

    def good_client():
        c = _FakeClient(lambda db, sql: [{"message_content":
                                          "<appmsg><title>一条链接</title></appmsg>"}]
                        if "message_content" in sql else [])
        c._lh_v4_msgdbs = (time.time(), ["message_0.db"])   # 直接给分片缓存，省掉探测
        return c

    # ① 正常：取到原文 -> 用原文，**不贴**标注
    txt = live_history._appmsg_text(good_client(), "filehelper", 7, "摘要")
    ok &= check("原文取到时用原文（不贴标注）",
                "一条链接" in txt and live_history.APPMSG_NO_XML_NOTE not in txt, txt)
    ok &= check("取到原文会清掉这条库的失败记账",
                "message_0.db" not in live_history.poll_errors(),
                live_history.poll_errors())

    # ② 熔断中：一个查询都不发，但**如实标注**
    live_history._shard_blocked_until["message_0.db"] = time.monotonic() + 30
    bad = _FakeClient(_dead_db)
    bad._lh_v4_msgdbs = (time.time(), ["message_0.db"])
    n_before = len(bad.sql_log)
    txt2 = live_history._appmsg_text(bad, "filehelper", 8, "摘要")
    ok &= check("★ 熔断中：一个查询都不发（不再按行砸坏库）",
                len(bad.sql_log) == n_before, bad.sql_log[n_before:])
    ok &= check("★ 熔断中：如实说「原始内容没取到」，且摘要还在",
                live_history.APPMSG_NO_XML_NOTE in txt2 and "摘要" in txt2, txt2)
    live_history.reset_shard_breakers()

    # ③ 回查失败要计入熔断（以前这条路的失败根本不计数，永远打不破）
    _clear_poll_errors()
    for i in range(live_history.SHARD_FAIL_LIMIT):
        c = _FakeClient(_dead_db)
        c._lh_v4_msgdbs = (time.time(), ["message_0.db"])
        ok &= check(f"第 {i + 1} 次回查失败会留痕",
                    live_history._fetch_message_xml(c, "filehelper", 9) == "")
    ok &= check("★ 连错到阈值就把这条库打成熔断",
                live_history.shard_blocked("message_0.db"), live_history.poll_errors())

    # ④ 冷却到点（半开）后查得动 -> 立刻解除并清账
    live_history._shard_blocked_until["message_0.db"] = time.monotonic() - 1
    live_history._fetch_message_xml(good_client(), "filehelper", 10)
    ok &= check("★ 查得动就立刻解除熔断 + 清掉告警（不留假故障）",
                not live_history.shard_blocked("message_0.db")
                and "message_0.db" not in live_history.poll_errors())
    live_history.reset_shard_breakers()
    _clear_poll_errors()
    return ok


def _t_align_stale_cursor():
    """分片被重建 → 旧游标比新头部还大 = 这条分片永远读不到新行（静默失效）。

    2026-10-05 用户拍板只做这一半：对齐**不会丢任何东西**（比新头部更大的 rowid 本来
    就不存在）。另一半（落后很多就跳到头部，会少一批旧通知）故意不做。
    """
    print("\n── 死游标对齐：游标 > 头部（分片被重建，2026-10-05）──")
    ok = True
    HEADS = {"message_fts_v4_0": 100, "message_fts_v4_1": 900}

    def mk(tabs):
        state = {"head": dict(HEADS)}      # 用例可以改它，模拟「这张表又长了」

        def router(db, sql):
            if "MAX(rowid)" in sql:
                tab = sql.split("FROM ")[-1].strip()
                return [{"m": state["head"][tab]}] if tab in state["head"] else []
            if "WHERE rowid >" in sql:
                # ⚠️ 别假设「一条 SQL 只有一个分片」：2026-10-06 起收消息那条路把
                # **4 个分片合并成一条 `UNION ALL`**（每片一个子查询、各自游标）。
                # 这里按段扫，两种形状都认。
                out = []
                for chunk in sql.split("FROM ")[1:]:
                    parts = chunk.split()
                    tab = parts[0] if parts else ""
                    if not tab or tab.startswith("(") or "WHERE rowid >" not in chunk:
                        continue
                    bound = int(chunk.split("WHERE rowid >")[1].split()[0])
                    if tab in state["head"] and bound < state["head"][tab]:
                        out.append({"shard": tab, "rowid": state["head"][tab],
                                    "acontent": "重建后第一条",
                                    "session_id": 1, "sender_id": 1, "create_time": 1,
                                    "local_type": 1, "message_local_id": 1})
                return out
            return []
        c = _FakeClient(router)
        c._lh_fts_tables = (time.time(), list(tabs))
        c._lh_fts_smap = (time.time(), {1: "filehelper"})
        c._lh_fts_selfid = (time.time(), 0)
        c._align_state = state
        return c

    # ① 语义：超头部的对齐；只是落后的**一个字都不动**；相等不误报
    c = mk(["message_fts_v4_0", "message_fts_v4_1"])
    cur = {"message_fts_v4_0": 200524, "message_fts_v4_1": 500, "__time__": 1}
    new, fixed = live_history.align_stale_cursors(c, cur)
    ok &= check("★ 游标超过头部的分片被对齐到头部",
                new.get("message_fts_v4_0") == 100
                and fixed.get("message_fts_v4_0") == (200524, 100), (new, fixed))
    ok &= check("★ 只是落后的分片一个字都不动（那是没做的那一半）",
                new.get("message_fts_v4_1") == 500 and "message_fts_v4_1" not in fixed, new)
    ok &= check("★ 游标 == 头部（正常空闲）不误报",
                live_history.align_stale_cursors(
                    c, {"message_fts_v4_0": 100, "message_fts_v4_1": 900})[1] == {})
    ok &= check("只改该改的键，别的原样", set(new) == set(cur), sorted(new))

    # ②③④ 一条死游标分片：对齐前**连新消息都收不到**，对齐后立刻收得到
    c1 = mk(["message_fts_v4_0"])
    stale = {"message_fts_v4_0": 200524}
    msgs_before, _ = live_history._v4_new_messages(c1, dict(stale))
    ok &= check("★ 对齐前：重建后的新消息一条都收不到（而且不报错 = 静默失效）",
                msgs_before == [], msgs_before)
    c1._align_state["head"]["message_fts_v4_0"] = 101      # 模拟「重建后来了第一条新消息」
    msgs_still, _ = live_history._v4_new_messages(c1, dict(stale))
    ok &= check("★ 死游标即使来了新消息也照样收不到（这正是不对齐的代价）",
                msgs_still == [], msgs_still)
    aligned, _ = live_history.align_stale_cursors(c1, stale)
    ok &= check("★ 对齐后游标落在**当时**的头部（不是它自己记的旧值）",
                aligned.get("message_fts_v4_0") == 101, aligned)
    c1._align_state["head"]["message_fts_v4_0"] = 102      # 又来一条新消息
    msgs_after, _ = live_history._v4_new_messages(c1, aligned)
    ok &= check("★ 对齐后：这条分片立刻又能收到新消息",
                [m["content"] for m in msgs_after] == ["重建后第一条"], msgs_after)

    # ⑤ 头部查不到 -> 保守不动，绝不猜
    bad = _FakeClient(_dead_db)
    bad._lh_fts_tables = (time.time(), ["message_fts_v4_0"])
    new3, fixed3 = live_history.align_stale_cursors(bad, {"message_fts_v4_0": 200524})
    ok &= check("★ 头部查不到时保守不动（不许猜）",
                new3 == {"message_fts_v4_0": 200524} and fixed3 == {}, (new3, fixed3))
    return ok


def _t_fts_batch():
    """4 个 fts 分片**一次查完**：请求数 5 → 2、行为不变（2026-10-06）。

    ## 为什么有它

    hook **每收到一个请求都要遍历校验所有库句柄**（见 CLAUDE.md「hook 使用铁律」），
    所以「一个分片一条 SQL」= 4 倍这份固定开销。合并成一条 `UNION ALL`（每个分片带
    **自己那个 rowid 游标**、各自 `LIMIT`）之后，一轮从 5 个请求降到 2 个 ——
    空闲期的常态压力砍掉六成，而**延迟一点没动**。
    （为什么不去做「写入驱动轮询」：本机实测库一直在被写，那条已被否，见
    `docs/poll-reliability-notes.md` §12。）

    ## 这一条钉什么

    * **一次请求**取回多个分片的新行；**游标按分片各自推进**；
    * 熔断中的分片**不进**这条 SQL（照旧不查它）；
    * 后端不给 `shard` 列时：消息照样返回、**不推进分片游标**（靠 `seen` 去重 ⇒
      只会重复读、不会丢），并留一行痕（限流）。
    """
    print("\n── fts 分片合并成一条查询：请求数 5 → 2（2026-10-06）──")
    ok = True
    live_history.reset_shard_breakers()
    _clear_poll_errors()
    live_history._SHARD_COL_MISS_AT[0] = 0.0

    def mk(rows_by_shard, with_shard=True):
        def router(db, sql):
            if "Name2Id" in sql and "username" in sql:
                return [{"rowid": 100, "username": "wxid_friend"},
                        {"rowid": 55, "username": SELF_WXID}]
            if "SELECT rowid FROM Name2Id" in sql:
                return [{"rowid": 55}]
            if "WHERE rowid >" in sql:
                out = []
                for tab, rows in rows_by_shard.items():
                    for rid, txt, ts in rows:
                        r = {"rowid": rid, "acontent": txt, "session_id": 100,
                             "sender_id": 100, "create_time": ts, "local_type": 1,
                             "message_local_id": 1}
                        if with_shard:
                            r["shard"] = tab
                        out.append(r)
                return out
            return []
        c = _FakeClient(router)
        c._lh_fts_tables = (time.time(), sorted(rows_by_shard))
        c._lh_fts_selfid = (time.time(), 55)
        return c

    rows = {"message_fts_v4_0": [(11, "a0", 1000), (12, "a1", 1001)],
            "message_fts_v4_1": [(7, "b0", 1002)]}
    c = mk(rows)
    msgs, cur = live_history._v4_new_messages(
        c, {"message_fts_v4_0": 10, "message_fts_v4_1": 0})
    fts_sqls = [s for d, s in c.sql_log if d == "message_fts.db" and "WHERE rowid >" in s]
    ok &= check("★ 两个分片只发**一条** fts 查询", len(fts_sqls) == 1, len(fts_sqls))
    ok &= check("★ 一条 SQL 里两个分片都在（UNION ALL + 各自那个游标）",
                bool(fts_sqls) and "UNION ALL" in fts_sqls[0]
                and "message_fts_v4_0 WHERE rowid > 10" in fts_sqls[0]
                and "message_fts_v4_1 WHERE rowid > 0" in fts_sqls[0],
                (fts_sqls or [""])[0][:170])
    ok &= check("每个分片各自 LIMIT（不是整条共用一个）",
                bool(fts_sqls)
                and fts_sqls[0].count(f"LIMIT {live_history.POLL_ROWS_PER_SHARD}") == 2,
                (fts_sqls or [""])[0][:170])
    ok &= check("三条消息都回来了",
                sorted(m["content"] for m in msgs) == ["a0", "a1", "b0"], msgs)
    ok &= check("★ 游标按**分片各自**推进",
                cur.get("message_fts_v4_0") == 12 and cur.get("message_fts_v4_1") == 7, cur)
    ok &= check("时间游标也在维护（fts 掉线要退回按会话表查）",
                cur.get("__time__") == 1002, cur.get("__time__"))
    ok &= check("这次成功了 → 熔断/失败账清掉",
                live_history.poll_errors() == {}, live_history.poll_errors())

    live_history.reset_shard_breakers()
    _clear_poll_errors()
    live_history._shard_blocked_until["message_fts_v4_1"] = time.monotonic() + 60
    c2 = mk(rows)
    live_history._v4_new_messages(c2, {"message_fts_v4_0": 10, "message_fts_v4_1": 0})
    s2 = [s for d, s in c2.sql_log if d == "message_fts.db" and "WHERE rowid >" in s]
    ok &= check("★ 熔断中的分片**不进**这条 SQL（只查没熔断的那个）",
                bool(s2) and "message_fts_v4_0" in s2[0] and "message_fts_v4_1" not in s2[0],
                (s2 or [""])[0][:150])
    live_history.reset_shard_breakers()

    ok &= check("分片全在熔断里 → 一条 fts 查询都不发（拼出来是空串）",
                live_history._v4_shard_batch_sql([], {}) == "")

    c3 = mk(rows, with_shard=False)
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        msgs3, cur3 = live_history._v4_new_messages(
            c3, {"message_fts_v4_0": 0, "message_fts_v4_1": 0})
        live_history._SHARD_COL_MISS_AT[0] = 0.0        # 放开限流，再触发一次
        live_history._v4_new_messages(c3, {"message_fts_v4_0": 0, "message_fts_v4_1": 0})
        live_history._v4_new_messages(c3, {"message_fts_v4_0": 0, "message_fts_v4_1": 0})
    ok &= check("★ 后端不给 shard 列 → 消息照样返回（不吞消息）", len(msgs3) == 3, len(msgs3))
    ok &= check("★ 这时**不推进分片游标**（靠 seen 去重：只会重复读、不会丢）",
                cur3.get("message_fts_v4_0") in (0, None)
                and cur3.get("message_fts_v4_1") in (0, None)
                and cur3.get("__time__") == 1002, cur3)
    ok &= check("★ 并留一行痕，且**限流**（同一句话不刷屏）",
                buf.getvalue().count("没带 `shard` 列") == 2, buf.getvalue()[:80])

    live_history.reset_shard_breakers()
    _clear_poll_errors()
    return ok


def _t_talker_miss():
    """会话名查不到：**先刷新一次映射**，刷新还查不到就留痕。

    2026-10-05 真机：映射是微信重建索引的中途建的（10 分钟缓存）→ filehelper 被认成
    `session_297` → 上层按「非目标会话」**静默丢掉**「你好」，日志里一行都没有。
    """
    print("\n── 会话名拿不到：刷新一次 + 如实留痕（2026-10-05 真机）──")
    ok = True
    live_history.reset_shard_breakers()
    _clear_poll_errors()

    def mk(seen_maps):
        state = {"n": 0}

        def router(db, sql):
            if "Name2Id" in sql and "username" in sql:
                state["n"] += 1
                seen_maps.append(state["n"])
                if state["n"] == 1:            # 第一次（旧缓存）：故意缺 297
                    return [{"rowid": 1, "username": "2598@openim"}]
                return [{"rowid": 1, "username": "2598@openim"},
                        {"rowid": 297, "username": "filehelper"}]
            if "WHERE rowid >" in sql:
                return [{"rowid": 10, "acontent": "你好", "session_id": 297,
                         "sender_id": 3, "create_time": 1000, "local_type": 1,
                         "message_local_id": 5}]
            return []
        c = _FakeClient(router)
        c._lh_fts_tables = (time.time(), ["message_fts_v4_0"])
        c._lh_fts_selfid = (time.time(), 99)
        return c

    maps = []
    msgs, _ = live_history._v4_new_messages(mk(maps), {"message_fts_v4_0": 0})
    ok &= check("★ 查不到会话名时**先刷新一次映射**（不是拿旧缓存硬判）",
                len(maps) >= 2, maps)
    ok &= check("★ 刷新拿到名字 → talker 就是 filehelper（消息不再被当非目标丢掉）",
                [m["talker"] for m in msgs] == ["filehelper"], msgs)

    def mk_missing():
        def router(db, sql):
            if "Name2Id" in sql and "username" in sql:
                return [{"rowid": 1, "username": "2598@openim"}]
            if "WHERE rowid >" in sql:
                return [{"rowid": 11, "acontent": "你好", "session_id": 555,
                         "sender_id": 3, "create_time": 1001, "local_type": 1,
                         "message_local_id": 6}]
            return []
        c = _FakeClient(router)
        c._lh_fts_tables = (time.time(), ["message_fts_v4_0"])
        c._lh_fts_selfid = (time.time(), 99)
        return c

    live_history._TALKER_MISS_AT[0] = 0.0
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        msgs2, _ = live_history._v4_new_messages(mk_missing(), {"message_fts_v4_0": 0})
        first_txt = buf.getvalue()
        live_history._v4_new_messages(mk_missing(), {"message_fts_v4_0": 0})
        second_txt = buf.getvalue()
    ok &= check("★ 刷新后仍查不到 → 退回编号，并**留一行痕**",
                [m["talker"] for m in msgs2] == ["session_555"]
                and "session_id" in first_txt, (msgs2, first_txt[:90]))
    ok &= check("★ 留痕有限流（60 秒内不重复刷）",
                second_txt == first_txt, second_txt[len(first_txt):][:90])
    live_history.reset_shard_breakers()
    _clear_poll_errors()
    return ok


def _t_skip_far_behind():
    """落后太多 → 跳到头部（**会丢那批旧通知**，用户 2026-10-05 明确要，带如实通知）。

    真机：v4_2 从 3400 追到 135421 ≈ 13 万行；按每轮 20 行要追几小时，期间新消息全被压在后面。
    """
    print("\n── 落后太多就跳头部（会丢旧通知，阈值可关）──")
    ok = True
    HEADS = {"message_fts_v4_0": 100000, "message_fts_v4_1": 500}

    def mk():
        def router(db, sql):
            if "MAX(rowid)" in sql:
                tab = sql.split("FROM ")[-1].strip()
                return [{"m": HEADS[tab]}] if tab in HEADS else []
            return []
        c = _FakeClient(router)
        c._lh_fts_tables = (time.time(), ["message_fts_v4_0", "message_fts_v4_1"])
        return c

    # ① 落后在阈值以内 → 一个字都不动（不许为了省事就跳）
    cur = {"message_fts_v4_0": 99000, "message_fts_v4_1": 100}    # 差 1000 / 400
    new, jumped = live_history.skip_far_behind(mk(), cur, max_gap=5000)
    ok &= check("★ 落后在阈值内 → 一个字都不动（继续慢慢追、通知照发）",
                new == cur and jumped == {}, (new, jumped))

    # ② 落后超过阈值 → 跳到头部，并如实报出「跳了多少行」
    cur2 = {"message_fts_v4_0": 3400, "message_fts_v4_1": 100}
    new2, jumped2 = live_history.skip_far_behind(mk(), cur2, max_gap=5000)
    ok &= check("★ 落后超过阈值 → 该分片跳到头部",
                new2.get("message_fts_v4_0") == 100000, new2)
    ok &= check("★ 如实报出跳过的行数（上层要照这个数通知用户）",
                jumped2.get("message_fts_v4_0") == (3400, 100000, 96600), jumped2)
    ok &= check("没超阈值的那个分片照样不动",
                new2.get("message_fts_v4_1") == 100, new2)

    # ③ max_gap=0 = 关掉这条闸（永远慢慢追）
    new3, jumped3 = live_history.skip_far_behind(mk(), cur2, max_gap=0)
    ok &= check("★ poll_max_catchup=0 → 关掉这条闸（不跳）",
                new3 == cur2 and jumped3 == {}, (new3, jumped3))

    # ④ 阈值读不出来 → 回默认，**不许静默变成 0（= 关闸）**
    new4, jumped4 = live_history.skip_far_behind(mk(), cur2, max_gap="abc")
    ok &= check("★ 阈值是垃圾值 → 回默认阈值，而不是静默关闸",
                new4.get("message_fts_v4_0") == 100000, (new4, jumped4))

    # ⑤ 头部查不到 → 保守不动
    bad = _FakeClient(_dead_db)
    bad._lh_fts_tables = (time.time(), ["message_fts_v4_0"])
    new5, jumped5 = live_history.skip_far_behind(bad, {"message_fts_v4_0": 3400})
    ok &= check("★ 头部查不到时保守不动（不许猜）",
                new5 == {"message_fts_v4_0": 3400} and jumped5 == {}, (new5, jumped5))
    return ok


def _t_nonttext_pickup():
    """非文本补漏的硬要求：不报历史、不重复报、稳态零开销、**图后面跟一句话也要报**。

    2026-10-04 晚换判据：新鲜度信号从 `SessionTable.last_timestamp` 换成
    `message_0.db.sqlite_sequence`（每会话最大 `local_id`）。真机实测 `session.db`
    能 17 小时不落盘，那个信号会让**一个会话都匹配不到**（语音全丢、无日志）。
    所以水位线也换成 local_id 语义：`cursors["__nonttext_seq__"]`。
    """
    ok = True
    _clear_poll_errors()
    c = _PickupStub()
    cursors = {"__time__": _IMG_TS - 100}

    out1 = live_history._v4_pickup_nontext(c, cursors, [], limit=5)
    ok &= check("第一次见到某会话**也要报**（不然「你在某会话发的第一张图」永远报不上来）",
                len(out1) == 1 and out1[0].get("local_type") == 3
                and "[图片]" in str(out1[0].get("content")),
                out1)
    ok &= check("报到之后水位线 = 这个会话的**最大 local_id**",
                (cursors.get("__nonttext_seq__") or {}).get("filehelper") == 7,
                cursors.get("__nonttext_seq__"))
    ok &= check("旧的水位线（会话时钟语义）退役并删掉，不留两个 owner",
                "__nonttext__" not in cursors, list(cursors))

    # 会话里又来了新的（local_id 比水位线大）→ 必须报
    c2 = _PickupStub(rows=[{"local_id": "8", "local_type": "3",
                            "create_time": str(_IMG_TS),
                            "real_sender_id": "0", "message_content": ""}])
    out2 = live_history._v4_pickup_nontext(c2, cursors, [], limit=5)
    ok &= check("local_id 前进之后，把新的那张图片消息报上来",
                len(out2) == 1 and out2[0].get("local_type") == 3
                and "[图片]" in str(out2[0].get("content")),
                out2)

    out3 = live_history._v4_pickup_nontext(c2, cursors, [], limit=5)
    ok &= check("同一张图不会每轮重复报（local_id 水位线生效）", out3 == [], out3)

    # 图片消息要自己说清「能不能看」+ 带上 local_id（模型据此直接调 read_image）
    body = str(out1[0].get("content") or "")
    ok &= check("图片内容带 local_id（模型能直接调 read_image，不用先 find_images）",
                "local_id=7" in body, body)
    ok &= check("没有可解码缩略图时如实说明，不假装能看",
                "看不了内容" in body, body)

    # 已经在 fts 结果里的那条不许重复报（去重）
    img8 = {"local_id": "8", "local_type": "3", "create_time": str(_IMG_TS),
            "real_sender_id": "0", "message_content": ""}
    cur_d = {"__time__": _IMG_TS - 100, "__nonttext_seq__": {"filehelper": 7}}
    got = live_history._v4_pickup_nontext(_PickupStub(rows=[img8]), cur_d, [], limit=5)
    cur_d2 = {"__time__": _IMG_TS - 100, "__nonttext_seq__": {"filehelper": 7}}
    out4 = live_history._v4_pickup_nontext(_PickupStub(rows=[img8]), cur_d2, got, limit=5)
    ok &= check("已经在 fts 结果里的那条不重复报",
                len(got) == 1 and out4 == [], (got, out4))

    # ⚠️ 2026-10-04 真机：**图后面紧跟一句话**（用户发完图马上打字提问）。
    # 那时的 `last_msg_type` 已经是 1（文本），旧实现按它当闸 → 这张图永久消失。
    # 现在判据是「这个会话有动静」，图必须照报，而那句话（文本）不许重复报。
    img = {"local_id": "8", "local_type": "3", "create_time": str(_IMG_TS + 20),
           "real_sender_id": "0", "message_content": ""}
    txt = {"local_id": "9", "local_type": "1", "create_time": str(_IMG_TS + 21),
           "real_sender_id": "0", "message_content": "图片里的价格怎么样"}
    c_img_then_text = _PickupStub(last_ts=_IMG_TS + 21, last_type=1,
                                  rows=[img, txt])
    cur_it = {"__time__": _IMG_TS + 15}
    out6 = live_history._v4_pickup_nontext(c_img_then_text, cur_it, [], limit=5)
    ok &= check("图后面跟了一句话：那张图**照样报上来**（旧实现会永久丢掉）",
                len(out6) == 1 and out6[0].get("local_type") == 3
                and "[图片]" in str(out6[0].get("content")), out6)
    ok &= check("同一批里的**文本不许重复报**（那条是 fts 的活）",
                all(live_history._as_int(x.get("local_type")) != 1 for x in out6), out6)

    # 稳态：会话没有新动静 → 一次消息表都不查
    class _NoChange(_PickupStub):
        def query_sql(self, db, sql):
            self.sql_log.append((db, sql))
            if db == "session.db":
                return []            # 没有会话 `last_timestamp >= since`
            return []

    c2 = _NoChange()
    cur2 = {"__time__": _IMG_TS}
    out5 = live_history._v4_pickup_nontext(c2, cur2, [], limit=5)
    ok &= check("没有会话有动静时：零额外查询、零输出（稳态不加负担）",
                out5 == [] and c2.msg_queries() == [], (out5, c2.msg_queries()))

    # 闸门：一轮最多扫 8 个会话，超出的进 pending，**下一轮不看 since 也照样扫**
    many = {}
    for i in range(11):
        many["room%d@chatroom" % i] = {
            "last_ts": _IMG_TS + i, "last_type": 1,
            "rows": [{"local_id": str(100 + i), "local_type": "3",
                      "create_time": str(_IMG_TS + i),
                      "real_sender_id": "0", "message_content": ""}],
        }
    cur_m = {"__time__": _IMG_TS - 100}
    out_m = live_history._v4_pickup_nontext(_PickupMultiStub(many), cur_m, [],
                                           limit=5)
    ok &= check("洪水时一轮只扫 8 个会话（不一次压几十条查询给 hook）",
                len(out_m) == 8, len(out_m))
    ok &= check("超出的 3 个记进 pending",
                len(cur_m.get("__nonttext_pending__") or {}) == 3,
                cur_m.get("__nonttext_pending__"))
    # 第二轮：since 已经被推到很后面（模拟 __time__ 被后续消息推走），
    # 但 pending 里的会话**照样要扫**——这正是「只靠 since 会丢消息」那个坑。
    cur_m["__time__"] = _IMG_TS + 10_000
    out_m2 = live_history._v4_pickup_nontext(_PickupMultiStub(many), cur_m, [],
                                             limit=5)
    ok &= check("pending 里的会话下一轮照样扫（不被 since 挡掉）",
                len(out_m2) == 3, len(out_m2))
    ok &= check("pending 清空", (cur_m.get("__nonttext_pending__") or {}) == {},
                cur_m.get("__nonttext_pending__"))

    # 坏掉的游标形状不许把轮询挡住
    cur3 = {"__time__": _IMG_TS, "__nonttext_seq__": "垃圾"}
    c3 = _PickupStub(last_ts=_IMG_TS + 1)
    live_history._v4_pickup_nontext(c3, cur3, [], limit=5)
    ok &= check("__nonttext_seq__ 形状不对时自动重置，不抛异常",
                isinstance(cur3.get("__nonttext_seq__"), dict), cur3.get("__nonttext_seq__"))
    return ok


class _DeadSessionStub:
    """`session.db` 与 `message_fts.db` 句柄**同时失效**，只有 message_0.db 能用。

    2026-10-04 晚真机就是这形状：这两个库的查询一直报
    `get database handle which named … failed`，而 `message_0.db` / `message_resource.db`
    好好的 —— 旧实现只拿 SessionTable 当「谁有新消息」的信号，于是候选为空，
    bot **完全收不到消息**（连文本都收不到）。
    """

    def __init__(self, rows, seq):
        self.rows = rows
        self.seq = seq
        self._ft = live_history._v4_table_for("filehelper")

    def query_sql(self, db, sql):
        if db in ("session.db", "message_fts.db"):
            raise aixed_api.AixedError(
                f"查库 {db} 失败：get database handle which named {db} failed")
        if db == "message_resource.db":
            if "ChatName2Id" in sql:
                return [{"rowid": "6", "user_name": "filehelper"}]
            return []
        if db == "message_0.db":
            if "sqlite_sequence" in sql:
                return [{"name": self._ft, "seq": str(self.seq)}]
            if "sqlite_master" in sql:
                return [{"x": 1}]
            if ("FROM " + self._ft) in sql:
                return self.rows
            return []
        return []

    def get_dbs(self):
        return []


def _t_fallback_survives_dead_session_db():
    """fts 与 session.db 都失效时，靠 `sqlite_sequence` 照样收到消息（含语音条）。

    这是 2026-10-04 晚真机的形状：那两个库的句柄一直取不到，语音/文本**全收不到**。
    只要 `message_0.db` 还在，就有救 —— 而它是新判据唯一依赖的库。
    """
    ok = True
    _clear_poll_errors()
    now = int(time.time())
    old = [
        {"local_id": "4", "local_type": "1", "create_time": str(now - 600),
         "real_sender_id": "0", "message_content": "更早的一句话"},
        {"local_id": "5", "local_type": "34", "create_time": str(now - 599),
         "real_sender_id": "0", "message_content": ""},
    ]
    # 第一次：只打基线，不回放历史
    out0, cur = live_history._v4_new_messages(_DeadSessionStub(old, seq=5), {})
    ok &= check("第一次只打 local_id 基线、不回放历史（否则约 290 个会话同轮各查一次）",
                out0 == [] and (cur.get("__msg_seq__") or {}).get("filehelper") == 5,
                (out0, cur.get("__msg_seq__")))

    # 之后来了新的：一句文本 + 一条语音条
    rows = old + [
        {"local_id": "6", "local_type": "1", "create_time": str(now - 60),
         "real_sender_id": "0", "message_content": "搜索"},
        {"local_id": "7", "local_type": "34", "create_time": str(now - 59),
         "real_sender_id": "0", "message_content": ""},
    ]
    out, cur2 = live_history._v4_new_messages(_DeadSessionStub(rows, seq=7), cur)
    ok &= check("session.db / message_fts.db 全失效时**照样收到消息**",
                len(out) == 2, out)
    ok &= check("语音条（local_type=34）也在里面，并且渲染成可读的一行",
                any(live_history._as_int(m.get("local_type")) == 34
                    and "语音条" in str(m.get("content")) for m in out), out)
    ok &= check("文本也在里面（这条路上 fts 已经不干活了）",
                any(str(m.get("content")) == "搜索" for m in out), out)
    ok &= check("水位线按 local_id 记下来",
                (cur2.get("__msg_seq__") or {}).get("filehelper") == 7,
                cur2.get("__msg_seq__"))
    out2, _ = live_history._v4_new_messages(_DeadSessionStub(rows, seq=7), cur2)
    ok &= check("同一批不会每轮重复报", out2 == [], out2)
    return ok


class _PollOneRoundStub:
    """**一整轮轮询**的假库（考 `_v4_new_messages`，不是单个函数）。

    复刻真机 2026-10-04 16:42 那一轮（用户发的 4 秒语音**一行日志都没有**）：
      * 语音条在 filehelper 里（`local_type=34`），**不进 fts**（结构性事实）；
      * 同一批里**另一个会话来了一条更晚的文本**（`other_ts`）——它会把 fts 游标
        `__time__` 推过那条语音；
      * filehelper 自己最后一条是紧跟语音后面的那句文本（`last_msg_type=1`）。

    真机的数据形状就是这样：`_v4_pickup_nontext` 拿的是「会话有动静」这个信号，
    而**这个会话的 last_timestamp 是那句文本**，不是语音。
    """

    def __init__(self, voice_ts, text_ts, other_ts, voice_type=34):
        self.voice_ts = voice_ts
        self.text_ts = text_ts
        self.other_ts = other_ts
        self.voice_type = voice_type
        self.sql_log = []
        self._ft = live_history._v4_table_for("filehelper")
        # 会话表查询里那个 `since`（能不能捞到 filehelper 全看它）
        self.session_since = []

    def query_sql(self, db, sql):
        self.sql_log.append((db, sql))
        if db == "message_fts.db":
            if "sqlite_master" in sql:
                return [{"name": "message_fts_v4_0"}]
            if "FROM Name2Id" in sql:
                if "username =" in sql:            # 自己那个 id：拿不到就算了
                    return []
                return [{"rowid": "5", "username": "other_friend"},
                        {"rowid": "7", "username": "filehelper"}]
            if "message_fts_v4_" in sql:
                # 本批唯一进 fts 的一条：**别的会话**、比语音更晚
                return [{"rowid": "11", "acontent": "别的会话的一句话",
                         "session_id": "5", "sender_id": "2",
                         "create_time": str(self.other_ts),
                         "local_type": "1", "message_local_id": "1"}]
            return []
        if db == "session.db":
            if "FROM SessionTable" in sql:
                since = 0
                if "last_timestamp >= " in sql:
                    try:
                        since = int(sql.split("last_timestamp >= ")[1].split()[0])
                    except (IndexError, ValueError):
                        since = 0
                self.session_since.append(since)
                rows = [{"username": "filehelper",
                         "last_timestamp": str(self.text_ts), "last_msg_type": 1},
                        {"username": "other_friend",
                         "last_timestamp": str(self.other_ts), "last_msg_type": 1}]
                return [r for r in rows
                        if int(r["last_timestamp"]) >= since]
            return []
        if db.startswith("message_"):
            if db != "message_0.db":
                return []                          # 同上：一个会话的表只在一个分片里
            if "sqlite_sequence" in sql:
                return [{"name": self._ft, "seq": "43"}]
            if "sqlite_master" in sql:
                return [{"x": 1}]
            if "FROM Name2Id" in sql:
                return []
            if ("FROM " + self._ft) in sql:
                return [
                    {"local_id": "42", "local_type": str(self.voice_type),
                     "create_time": str(self.voice_ts), "real_sender_id": "0",
                     "message_content": "", "packed_info_data": ""},
                    {"local_id": "43", "local_type": "1",
                     "create_time": str(self.text_ts), "real_sender_id": "0",
                     "message_content": "1", "packed_info_data": ""},
                ]
            return []
        return []

    def get_dbs(self):
        return []


def _t_poll_window_keeps_nontext():
    """轮询窗口**不许被本批更晚的消息推走**——「语音条一行日志都没有」的真正成因。

    真机现场（2026-10-04 16:42，控制会话）：用户发了一条 4 秒语音，紧接着一句「1」，
    同一批里别的会话也在动。`bot.log` 里**没有任何一行**跟这条语音有关
    —— 不是「读不出来」，是**根本没送到语音分支**，bot 只回了一句无关的话。

    成因：`_v4_new_messages` 先把 `cursors["__time__"]` 推到**本批最新**
    （`other_ts`，来自别的会话），再拿这个已经被推走的值当非文本补捞的窗口。
    而 filehelper 的 `last_timestamp` 是那句文本（`text_ts < other_ts`）
    —— 于是 `WHERE last_timestamp >= since` 直接把这个会话排除掉，
    **整条会话连候选都不是**，里面的语音永久消失（水位线也没记，下一轮 since 更晚）。

    契约：补捞窗口必须是**轮询前**的水位（「上一轮看到的时刻」），
    因为真正防重复的是每个会话自己的 `__nonttext__` 水位线。
    """
    ok = True
    _clear_poll_errors()
    voice_ts, text_ts = _IMG_TS + 10, _IMG_TS + 11
    # 本批里**别的会话**那条比语音晚 1 小时：它会把 fts 游标 `__time__` 推得老远。
    # 拿推走之后的值当窗口，这条语音就够不着了（真机就是这么丢的）。
    other_ts = voice_ts + 3600
    c = _PollOneRoundStub(voice_ts, text_ts, other_ts)
    cursors = {"__time__": _IMG_TS}

    out, cur2 = live_history._v4_new_messages(c, cursors)

    ok &= check("前提：本批最新那条比语音晚很多（足够把窗口推出去）",
                other_ts - voice_ts > live_history._NONTEXT_BOOTSTRAP_WINDOW,
                (voice_ts, other_ts))
    ok &= check("语音条**送到上层**了（有内容、带 local_type=34）",
                any(live_history._as_int(m.get("local_type")) == 34 for m in out),
                out)
    ok &= check("语音渲染成可读的一行（不是空串走 `if not query: continue` 静默丢掉）",
                any("语音条" in str(m.get("content")) for m in out)
                and all(str(m.get("content")).strip() for m in out), out)
    ok &= check("同一批里别的会话那条文本照常上报（补捞不许顶掉 fts 的活）",
                any(str(m.get("talker")) == "other_friend" for m in out), out)
    ok &= check("语音报过之后记下这个会话的水位线（下一轮不会重复报）",
                (cur2.get("__nonttext_seq__") or {}).get("filehelper") == 43,
                cur2.get("__nonttext_seq__"))
    return ok


def _label_router(db, sql):
    """微信自带「标签」的假库：contact_label 给标签名，contact_fts 给成员。

    刻意混进两条**假命中**（真实数据里这是常态，不是假想）：
      * `gzh001` —— 微信号里有数字「1」，但标签段是空的；
      * 「亲人小卖部」—— 备注里有「亲人」两个字，但标签段是空的。
    实测标签「1」LIKE 命中 412 行、真成员只有 1 个。只看 LIKE 不看第 4 段，
    就会把 411 个无关的人当成收件人——而**群发是不可逆的**。
    """
    if db == "contact.db":
        if "contact_label" in sql:
            return [{"label_id_": "2", "label_name_": "亲人"},
                    {"label_id_": "3", "label_name_": "1"},
                    {"label_id_": "4", "label_name_": "家"}]
        return [{"x": 1}]        # v4 探针要通，否则会被判成 3.9.x
    if db == "contact_fts.db" and "contact_fts_v5" in sql:
        def key(remark, nick, labels, alias, region):
            # 真实布局：备注 \x08 '' \x08 昵称 \x08 标签 \x08 微信号 \x08 地区 \x08 ''
            return "\x08".join([remark, "", nick, labels, alias, region, ""])
        return [
            {"u": "wxid_a", "k": key("", "张三", "亲人", "zs001", "北京 朝阳")},
            {"u": "wxid_b", "k": key("王五", "五哥:岩", "亲人,家", "", "中国大陆 ")},
            {"u": "wxid_c", "k": key("", "公众号君", "", "gzh001", "某省 某市")},
            {"u": "wxid_d", "k": key("亲人小卖部", "小卖部", "", "shop1", "")},
            {"u": "wxid_e", "k": key("", "李四", "亲人,家", "", "")},
        ]
    return []


def _t_labels():
    """微信自带标签：**成员藏在 contact_fts 的 search_key 第 4 段**。

    契约是 2026-10-01 对着真实数据反解确认的（和 contact.db 的 remark/nick/alias
    逐行对拍）。钉住三件事：
      1. 第 4 段才是标签（布局错一位就会把备注/昵称/微信号当标签）；
      2. **LIKE 命中不算数**，必须精确核对第 4 段；
      3. 读不到就返回空，**不猜**（上层会如实说读不到，而不是发错人）。
    """
    ok = True
    f = live_history.labels_of_search_key

    ok &= check("7 段：第 4 段是标签",
                f("张三\x08\x08张三丰\x08亲人\x08zs001\x08北京 朝阳\x08") == ["亲人"])
    ok &= check("多个标签用逗号分开",
                f("王五\x08\x08五哥:岩\x08亲人,家\x08\x08中国大陆 \x08") == ["亲人", "家"])
    ok &= check("第 4 段为空 = 这个人没有标签",
                f("赵六  小学同学\x08\x08六哥\x08\x08zhangsan\x08某国 \x08") == [])
    ok &= check("备注里出现标签名**不算**标签（位置不对就是不算）",
                f("亲人小卖部\x08\x08小卖部\x08\x08shop1\x08") == [])
    ok &= check("段数不足 4（布局对不上）-> 返回空，不猜", f("a\x08b\x08c") == [])
    ok &= check("空输入不炸", f("") == [] and f(None) == [])

    client = _FakeClient(_label_router)
    labs = live_history.label_names(client)
    ok &= check("读出 3 个标签名", [l["name"] for l in labs] == ["亲人", "1", "家"], labs)

    got = live_history.contacts_in_label(client, "亲人")
    ok &= check("「亲人」的真实成员是 3 个（a/b/e），**两条假命中被剔掉**",
                got == ["wxid_a", "wxid_b", "wxid_e"], got)
    ok &= check("gzh001 和「亲人小卖部」都不在里面",
                "wxid_c" not in got and "wxid_d" not in got, got)

    ok &= check("标签「1」：LIKE 会命中 gzh001，但真成员一个都没有",
                live_history.contacts_in_label(client, "1") == [])
    ok &= check("不存在的标签返回空（不瞎给）",
                live_history.contacts_in_label(client, "没这个标签") == [])

    dead = _FakeClient(_dead_db)
    ok &= check("库坏了返回 None（**和「没有」区分开**：上层要如实说「读不到」）",
                live_history.label_names(dead) is None
                and live_history.contacts_in_label(dead, "亲人") is None)
    ok &= check("真没有这个标签时返回空列表（不是 None）",
                live_history.contacts_in_label(client, "没这个标签") == []
                and live_history.label_names(_FakeClient(
                    lambda db, sql: [{"x": 1}] if db == "contact.db" else [])) == [])
    return ok


if __name__ == "__main__":
    sys.exit(main())
