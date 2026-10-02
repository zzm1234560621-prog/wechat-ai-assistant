"""live_history.py 的回归自测：兜底路径必须留痕 + appmsg 渲染 + LIKE 转义。

不联网、不碰 hook（30001 端口）、不需要微信：
  * 正常兜底那条路复用 selftest_aixed.py 里现成的假服务桩（_V4StaleFtsStub，真 HTTP 往返）；
  * 失败路径用一个**进程内**假客户端，按 (库名, SQL) 路由，能精确地让某一个库抛异常。
    故意不起线程、不打真端口——这批用例考的是 live_history 内部行为，不是 HTTP 层。

selftest_aixed.py 是 hook 层的回归基线，**本文件只 import 它、绝不修改它**。

用法：python selftest_live_history.py
"""
import contextlib
import io
import sqlite3
import sys
import threading
from http.server import ThreadingHTTPServer

import aixed_api
import auto_reply
import live_history

# 复用基线里的桩与常量，避免两份假数据各写各的、慢慢漂移。
from selftest_aixed import (SELF_WXID, V4_SESSION_SUMMARY, V4_SESSION_TS,
                            _V4StaleFtsStub)

NOW = 1_700_000_000


# ---------- 进程内假客户端 ----------

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
                 "last_timestamp": V4_SESSION_TS, "last_msg_sender": SELF_WXID}]
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


# ---------- 小工具 ----------

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

    # ---------------------------------------------------------------
    print("── T3：appmsg 标签不再是内部数字 ──")
    ok &= check("先钉住成因：_type_label(4|(49<<0)) 就是「类型53」",
                live_history._type_label(4 | (49 << 0)) == "类型53")
    out = live_history.render_appmsg("<appmsg><title>一条链接</title></appmsg>")
    ok &= check("非引用的 appmsg 不再产出 类型53",
                "类型53" not in out and out == "[消息] 一条链接", out)
    ok &= check("按 _type_label 的约定传 49 会落到「消息」",
                live_history._type_label(49) == "消息")

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
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

    # ---------------------------------------------------------------
    print("\n── fts 主路径的历史字段：非文本要带 local_type ──")
    ok &= _t_fts_history_fields()

    print("\n── 非文本补漏：图片不在 fts 里，得靠 SessionTable 的信号捞回来 ──")
    ok &= _t_nonttext_pickup()

    print("\n── 微信自带的「标签」：成员藏在 contact_fts 的 search_key 第 4 段 ──")
    ok &= _t_labels()

    print("\n" + "=" * 50)
    print("全部通过 ✅" if ok else "有失败项 ❌")
    print("=" * 50)
    return 0 if ok else 1


_IMG_TS = 1790000100


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
    """

    def __init__(self, last_ts=_IMG_TS, has_image=True):
        self.last_ts = last_ts
        self.has_image = has_image
        self.sql_log = []

    def query_sql(self, db, sql):
        self.sql_log.append((db, sql))
        if db == "session.db":
            return [{"username": "filehelper", "last_timestamp": str(self.last_ts)}]
        if db.startswith("message_"):
            if "sqlite_master" in sql:
                return [{"x": 1}]
            if "FROM Msg_" in sql:
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


def _t_nonttext_pickup():
    """非文本补漏的三条硬要求：不报历史、不重复报、稳态零开销。"""
    ok = True
    _clear_poll_errors()
    c = _PickupStub()
    cursors = {"__time__": _IMG_TS - 100}

    out1 = live_history._v4_pickup_nontext(c, cursors, [], limit=5)
    ok &= check("第一次见到某会话**也要报**（不然「你在某会话发的第一张图」永远报不上来）",
                len(out1) == 1 and out1[0].get("local_type") == 3
                and "[图片]" in str(out1[0].get("content")),
                out1)
    ok &= check("报到之后水位线记下这个会话",
                (cursors.get("__nonttext__") or {}).get("filehelper") == _IMG_TS,
                cursors.get("__nonttext__"))

    c.last_ts = _IMG_TS + 5
    out2 = live_history._v4_pickup_nontext(c, cursors, [], limit=5)
    ok &= check("水位线前进之后，把新的那张图片消息报上来",
                len(out2) == 1 and out2[0].get("local_type") == 3
                and "[图片]" in str(out2[0].get("content")),
                out2)

    out3 = live_history._v4_pickup_nontext(c, cursors, [], limit=5)
    ok &= check("同一张图不会每轮重复报（水位线生效）", out3 == [], out3)

    # 图片消息要自己说清「能不能看」+ 带上 local_id（模型据此直接调 read_image）
    body = str(out1[0].get("content") or "")
    ok &= check("图片内容带 local_id（模型能直接调 read_image，不用先 find_images）",
                "local_id=7" in body, body)
    ok &= check("没有可解码缩略图时如实说明，不假装能看",
                "看不了内容" in body, body)

    # 已经在 fts 结果里的那条不许重复报（去重）
    c.last_ts = _IMG_TS + 9
    dup = [{"talker": "filehelper", "content": out2[0]["content"], "_ts": _IMG_TS + 9}]
    out4 = live_history._v4_pickup_nontext(c, cursors, dup, limit=5)
    ok &= check("已经在 fts 结果里的那条不重复报", out4 == [], out4)

    # 稳态：summary 非空（最后一条是文本）→ 假 client 返回空 → 一次消息表都不查
    class _NoNontext(_PickupStub):
        def query_sql(self, db, sql):
            self.sql_log.append((db, sql))
            if db == "session.db":
                return []            # 真实 SQL 已经用 summary='' 过滤掉了
            return []

    c2 = _NoNontext()
    cur2 = {"__time__": _IMG_TS}
    out5 = live_history._v4_pickup_nontext(c2, cur2, [], limit=5)
    ok &= check("最后一条是文本时：零额外查询、零输出（稳态不加负担）",
                out5 == [] and c2.msg_queries() == [], (out5, c2.msg_queries()))

    # 坏掉的游标形状不许把轮询挡住
    cur3 = {"__time__": _IMG_TS, "__nonttext__": "垃圾"}
    c3 = _PickupStub(last_ts=_IMG_TS + 1)
    live_history._v4_pickup_nontext(c3, cur3, [], limit=5)
    ok &= check("__nonttext__ 形状不对时自动重置，不抛异常",
                isinstance(cur3.get("__nonttext__"), dict), cur3.get("__nonttext__"))
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
