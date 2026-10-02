"""aixed 后端的自测：起一个假服务模拟 WeChat-Hook 的 HTTP 接口，验证客户端和轮询逻辑。

不用真微信、不用真降级，就能验证：
  1. AixedClient 的各个接口请求格式对不对、返回解析对不对
  2. live_history.py **一行都不用改**就能跑在 AixedClient 上（鸭子类型成立）
  3. 轮询的游标推进与去重是对的

用法：python selftest_aixed.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent_tools
import aixed_api
import bot
import live_history

SENT = []          # 记录发出去的消息，供断言
SELF_WXID = "wxid_self_0001"
NOW = 1_700_000_000

# 假数据：两个会话，两个消息分片库
MSGS = {
    "MSG0.db": [
        {"StrTalker": "wxid_friendA", "StrContent": "上个月那个项目怎么样了", "IsSender": 0, "CreateTime": NOW + 10},
        {"StrTalker": "wxid_friendA", "StrContent": "我这边还在等回复", "IsSender": 0, "CreateTime": NOW + 11},
        {"StrTalker": "wxid_self_0001", "StrContent": "我明天看看", "IsSender": 1, "CreateTime": NOW + 12},
    ],
    "MSG1.db": [
        {"StrTalker": "room123@chatroom", "StrContent": "群里的项目进度同步一下", "IsSender": 0, "CreateTime": NOW + 13},
        {"StrTalker": "wxid_friendB", "StrContent": "关键词：项目 计划书", "IsSender": 0, "CreateTime": NOW + 14},
    ],
}
CONTACTS = [
    {"UserName": "wxid_friendA", "NickName": "张三", "Remark": "老张", "Alias": "zhangsan"},
    {"UserName": "wxid_friendB", "NickName": "李四", "Remark": "", "Alias": "lisi"},
]

DB_NAMES = ["MicroMsg.db", "MSG0.db", "MSG1.db", "MediaMSG0.db"]
# 真实接口返回的是对象数组，字段名是 dbName/dbHandle（踩过一次坑，桩要还原真实形状）
DB_NAMES_RESP = [{"dbHandle": 1000 + i, "dbName": n} for i, n in enumerate(DB_NAMES)]


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音

    def _send(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _filtered_msgs(self, db, sql):
        """按 SQL 里写的条件筛 MSG 行。

        **桩必须真的过滤**：不然游标、时间下界/上界这些逻辑测不出来
        （测试是绿的、生产是漏的——这个项目栽过不止一次）。
        """
        rows = list(MSGS.get(db, []))
        if "StrTalker = '" in sql:
            talker = sql.split("StrTalker = '")[1].split("'")[0]
            rows = [r for r in rows if r["StrTalker"] == talker]
        if "LIKE '%" in sql:
            key = sql.split("LIKE '%")[1].split("%'")[0]
            rows = [r for r in rows if key in r["StrContent"]]
        # 下界（since）/ 上界（until）：两个都得认，少一个都会让
        # 「按时间查」那条路在自测里静默失效
        if "CreateTime >= " in sql:
            floor = int(sql.split("CreateTime >= ")[1].split()[0])
            rows = [r for r in rows if r["CreateTime"] >= floor]
        if "CreateTime <= " in sql:
            ceil = int(sql.split("CreateTime <= ")[1].split()[0])
            rows = [r for r in rows if r["CreateTime"] <= ceil]
        return rows

    def do_GET(self):
        if self.path == "/QueryDB/status":
            self._send({"IsLogin": 1, "hWeixin": 123456})
        else:
            self._send({"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")

        if self.path == "/GetSelfProfile":
            self._send({"wxid": SELF_WXID, "nickName": "我自己"})

        elif self.path == "/QueryDB/GetAllDBName":
            self._send({"data": DB_NAMES_RESP})

        elif self.path == "/QueryDB/execute":
            db, sql = payload.get("optDbName"), payload.get("SQL", "")
            # 这个桩只模拟 3.9.x。查别的库要如实报「拿不到句柄」，
            # 否则 is_wechat4 探 contact.db 时会被误判成 4.x。
            if db not in ("MicroMsg.db", "MSG0.db", "MSG1.db"):
                self._send({"status": -1,
                            "desc": f"get database handle which named {db} failed"})
                return
            # 模拟登录失败/查库失败时的错误返回格式
            if "BADSQL" in sql:
                self._send({"status": -1, "desc": "simulated db handle failure"})
            # 聚合必须先判：`COUNT(*) AS c, MIN(...), MAX(...)` 这种 SQL 里也有
            # `MAX(CreateTime)`，落到下面那个「取最大时间」的分支就会回错形状，
            # 于是 count_history 在自测里静默拿到 0（测试绿的、生产漏的，栽过）。
            elif "COUNT(*)" in sql and "FROM MSG" in sql:
                rows = self._filtered_msgs(db, sql)
                ct = [r["CreateTime"] for r in rows]
                self._send({"data": [{"c": len(ct),
                                      "mn": min(ct) if ct else 0,
                                      "mx": max(ct) if ct else 0}]})
            elif "MAX(CreateTime)" in sql:
                rows = MSGS.get(db, [])
                self._send({"data": [{"m": max([r["CreateTime"] for r in rows], default=0)}]})
            elif "FROM Contact" in sql:
                # 支持 LIKE 过滤，好让 resolve_contact 也能测
                if "LIKE" in sql:
                    key = sql.split("'%")[1].split("%'")[0]
                    hit = [c for c in CONTACTS if key in (c["NickName"] + c["Remark"] + c["Alias"])]
                    self._send({"data": hit[:5]})
                else:
                    self._send({"data": CONTACTS})
            elif "FROM MSG" in sql:
                self._send({"data": self._filtered_msgs(db, sql)})
            else:
                self._send({"data": []})

        elif self.path == "/SendTextMsg":
            SENT.append(payload)
            self._send({"code": 0})

        else:
            self._send({"error": "not found"})


class _NoDbStub(Stub):
    """模拟未登录：查任何库都拿不到句柄。

    真实返回就是这个形状——微信没登录时 aixed 会回
    {"status": -1, "desc": "get database handle which named X failed"}。
    """
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(n)
        if self.path == "/QueryDB/GetAllDBName":
            self._send({"data": []})
        elif self.path == "/GetSelfProfile":
            self._send({"wxid": ""})
        elif self.path == "/QueryDB/execute":
            db = json.loads(payload or b"{}").get("optDbName", "?")
            self._send({"status": -1,
                        "desc": f"get database handle which named {db} failed"})
        else:
            self._send({"data": []})


# ---------- 4.x：fts 句柄失效，只有 session.db 是好的 ----------
#
# 复现 2026-09-30 那次「发消息没反应」：
#   message_fts.db 句柄失效 -> sqlite_master 一行都列不出来（**查询成功、返回 0 行**）
#   message_0.db 也解析不出句柄
#   只有 session.db 正常
# 老代码在这里会静默收不到任何消息；期望修好后能靠 SessionTable.summary 兜住。

V4_SESSION_TS = NOW + 40
V4_SESSION_SUMMARY = "给张三发10次你好"

# 真实抓到的 chat_room.ext_buffer（群 12345678901@chatroom），用来验群成员解码。
# 结构：repeated { 1=wxid  2=群昵称  3=角色(群主=9)  4=邀请人 }
ROOM_BUF = (
    "0A2C0A12777869645F616161616161616161616161611206E88081E5BCA018"
    "09220C7465616368657230303030310A2C0A12777869645F62626262626262"
    "6262626262621881402213777869645F6363636363636363636363636363"
)


class _V4StaleFtsStub(BaseHTTPRequestHandler):
    """fts 与 Msg_ 分片双掉线，只有 session.db 可用。"""

    dbs_called = 0          # GetAllDBName 被调了几次（用来验证重扫的限流）

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/QueryDB/status":
            self._send({"IsLogin": 1, "hWeixin": 123456})
        else:
            self._send({"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")

        if self.path == "/GetSelfProfile":
            self._send({"wxid": SELF_WXID})
            return
        if self.path == "/QueryDB/GetAllDBName":
            type(self).dbs_called += 1
            self._send({"data": [{"dbHandle": 1, "dbName": "session.db"}]})
            return
        if self.path != "/QueryDB/execute":
            self._send({"error": "not found"})
            return

        db, sql = payload.get("optDbName"), payload.get("SQL", "")

        # contact.db 是 is_wechat4 的探针，必须查得通
        if db == "contact.db":
            if "FROM chat_room" in sql:
                self._send({"data": [{"ext_buffer": ROOM_BUF,
                                      "owner": "wxid_aaaaaaaaaaaaa"}]})
            else:
                self._send({"data": [{"x": 1}]})
        elif db == "message_fts.db":
            # 句柄失效的样子：不报错，但什么都查不出来
            self._send({"data": []})
        elif db.startswith("message_"):
            self._send({"status": -1,
                        "desc": f"get database handle which named {db} failed"})
        elif db == "session.db":
            if "MAX(last_timestamp)" in sql:
                self._send({"data": [{"m": V4_SESSION_TS}]})
            elif "FROM SessionTable" in sql:
                since = 0
                if "last_timestamp >= " in sql:
                    since = int(sql.split("last_timestamp >= ")[1].split()[0])
                rows = [{"username": "filehelper", "summary": V4_SESSION_SUMMARY,
                         "last_timestamp": V4_SESSION_TS, "last_msg_sender": SELF_WXID,
                         "unread_count": 3,
                         "last_sender_display_name": "老张"}] \
                    if V4_SESSION_TS >= since else []
                self._send({"data": rows})
            else:
                self._send({"data": [{"x": 1}]})
        else:
            self._send({"status": -1,
                        "desc": f"get database handle which named {db} failed"})


def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return cond


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    c = aixed_api.AixedClient(base_url=f"http://127.0.0.1:{port}")
    ok = True
    print(f"\n假服务跑在 127.0.0.1:{port}\n")

    print("── AixedClient 基本接口 ──")
    ok &= check("get_dbs()", c.get_dbs() == DB_NAMES, c.get_dbs())
    ok &= check("_v3_msg_dbs() 只挑出 MSG 分片", live_history._v3_msg_dbs(c) == ["MSG0.db", "MSG1.db"], live_history._v3_msg_dbs(c))
    ok &= check("get_self_wxid()", c.get_self_wxid() == SELF_WXID, c.get_self_wxid())
    ok &= check("ping() 可用", c.ping() == (True, SELF_WXID), c.ping())
    ok &= check("db_status()", c.db_status() == {"IsLogin": 1, "hWeixin": 123456})
    ok &= check("is_login()", c.is_login() is True)

    print("\n── 错误返回必须显式报错，不能被当成空结果 ──")
    try:
        c.query_sql("MSG0.db", "SELECT BADSQL")
        ok &= check("查库失败应抛 AixedError", False, "没抛异常")
    except aixed_api.AixedError as e:
        ok &= check("查库失败应抛 AixedError", "simulated db handle failure" in str(e), e)

    print("\n── send_text 参数顺序要与 wcferry 一致 (msg, wxid) ──")
    c.send_text("你好", "wxid_friendA")
    ok &= check("发出去的内容/对象正确",
                SENT and SENT[-1] == {"wxidorgid": "wxid_friendA", "msg": "你好"}, SENT[-1:])

    print("\n── live_history.py 不改一行，直接跑在 AixedClient 上 ──")
    contacts = live_history.all_contacts(c)
    ok &= check("all_contacts()", len(contacts) == 2 and contacts[0]["wxid"] == "wxid_friendA", contacts)
    hist = live_history.query_contact_history(c, "wxid_friendA")
    ok &= check("query_contact_history()", len(hist) == 2 and hist[0]["content"].startswith("上个月"), hist)
    # v3 这条 SQL 的 WHERE 里写死 Type = 1，所以每条都该显式带 local_type=1：
    # 下游（「从历史学语气」挑用户自己发的文本）按 local_type 判断，不分后端。
    ok &= check("query_contact_history() 每条都带 local_type=1",
                all(m.get("local_type") == 1 for m in hist), hist)
    # since（epoch 秒）= 只看这个时刻之后的消息。**这是「最近 N 天」唯一能被
    # 真正回答的入口**：不加它，返回的永远是「最新的 limit 条」——按条数不按时间
    # （2026-10-01 真机：张三那条会话 30 条只覆盖 1.4 天，用户问 10 天答不出来）。
    # 假数据里 friendA 的两条是 NOW+10 / NOW+11。
    part = live_history.query_contact_history(c, "wxid_friendA", since=NOW + 11)
    ok &= check("query_contact_history(since=) 只回该时刻之后的",
                len(part) == 1 and part[0]["content"] == "我这边还在等回复", part)
    ok &= check("since 比所有消息都新 → 空结果（不是报错，也不退回全量）",
                live_history.query_contact_history(
                    c, "wxid_friendA", since=NOW + 999) == [])
    ok &= check("since 不影响 keyword 一起用（两个条件串在同一条 SQL 里）",
                [m["content"] for m in live_history.query_contact_history(
                    c, "wxid_friendA", keyword="等回复", since=NOW + 10)]
                == ["我这边还在等回复"],
                live_history.query_contact_history(
                    c, "wxid_friendA", keyword="等回复", since=NOW + 10))
    # until = 上界（含）。**只给 since 翻不到更早**（它锚在「现在」，返回的永远是
    # 最近 limit 条——实测 days=10 与 days=30 拿到同一批），所以往更早看必须给 until。
    ok &= check("query_contact_history(until=) 只回该时刻（含）以前的",
                [m["content"] for m in live_history.query_contact_history(
                    c, "wxid_friendA", until=NOW + 10)]
                == ["上个月那个项目怎么样了"],
                live_history.query_contact_history(
                    c, "wxid_friendA", until=NOW + 10))
    ok &= check("since + until = 两头都夹住（只看这一段）",
                [m["content"] for m in live_history.query_contact_history(
                    c, "wxid_friendA", since=NOW + 10, until=NOW + 11)]
                == ["上个月那个项目怎么样了", "我这边还在等回复"],
                live_history.query_contact_history(
                    c, "wxid_friendA", since=NOW + 10, until=NOW + 11))
    # count_history：只为了把规模如实说给模型听——「9 月一共 1400 条，
    # 这里只给你最新的 200 条」和「只给 200 条」是完全不同的两句话。
    cnt = live_history.count_history(c, "wxid_friendA",
                                     since=NOW + 10, until=NOW + 11)
    ok &= check("count_history 报出范围内条数 + 时间跨度",
                cnt.get("count") == 2 and cnt.get("first") == NOW + 10
                and cnt.get("last") == NOW + 11, cnt)
    ok &= check("count_history 和取数用同一套过滤（只数该时刻之后的）",
                live_history.count_history(
                    c, "wxid_friendA", since=NOW + 11).get("count") == 1)
    ok &= check("范围里一条都没有 → count=0，不是报错",
                live_history.count_history(
                    c, "wxid_friendA", since=NOW + 999).get("count") == 0)
    found = live_history.search_history(c, "项目")
    ok &= check("search_history()", len(found) == 3, [f["content"] for f in found])
    resolved = live_history.resolve_contact(c, "老张")
    ok &= check("resolve_contact()", len(resolved) == 1 and resolved[0]["wxid"] == "wxid_friendA", resolved)

    print("\n── 轮询：游标推进 + 去重 ──")
    # 游标是 dict：4.x 是 {fts分片: rowid}，3.9.x 是 {"__time__": 时间}
    cur0 = c.latest_cursor()
    ok &= check("latest_cursor() 是 dict 游标", isinstance(cur0, dict), type(cur0).__name__)
    ok &= check("latest_cursor() 取到最大 CreateTime", cur0.get("__time__") == NOW + 14, cur0)

    # prime 之后，边界上那条老消息不能被当成新消息（否则重启就重复回复）
    cursor, seen = c.prime()
    msgs, cursor, seen = c.poll_messages(since=cursor, seen=seen)
    ok &= check("prime 之后不重复处理边界消息", msgs == [], len(msgs))

    # 游标往回拨到起点，应拿到全部 5 条
    msgs, cur2, seen = c.poll_messages(since={"__time__": NOW}, seen={})
    ok &= check("从头轮询拿到 5 条", len(msgs) == 5, len(msgs))
    ok &= check("按时间升序", [m.create_time for m in msgs] == sorted(m.create_time for m in msgs))
    ok &= check("游标推进到最新", cur2.get("__time__") == NOW + 14, cur2)

    # 同一批再轮询一次，seen 去重后不应该重复处理
    msgs2, _, seen = c.poll_messages(since={"__time__": NOW}, seen=seen)
    ok &= check("重复轮询被 seen 去重", msgs2 == [], len(msgs2))

    # 来一条新消息，应恰好被拿到一条
    MSGS["MSG0.db"].append(
        {"StrTalker": "wxid_friendA", "StrContent": "刚发的新消息", "IsSender": 0, "CreateTime": NOW + 30}
    )
    msgs3, cur3, seen = c.poll_messages(since=cur2, seen=seen)
    ok &= check("新消息恰好取到 1 条", len(msgs3) == 1 and msgs3[0].content == "刚发的新消息", msgs3)
    ok &= check("游标推进到新消息", cur3.get("__time__") == NOW + 30, cur3)

    print("\n── Msg 冒充 wcferry.Message ──")
    priv = aixed_api.Msg("wxid_friendA", "hi", 0, NOW)
    grp = aixed_api.Msg("room123@chatroom", "hi", 0, NOW)
    mine = aixed_api.Msg("wxid_self_0001", "hi", 1, NOW)
    ok &= check("私聊 roomid 为空、sender 是对方", priv.roomid == "" and priv.sender == "wxid_friendA")
    ok &= check("群聊 roomid 是群 id", grp.roomid == "room123@chatroom")
    ok &= check("自己发的 from_self() 为真", mine.from_self() is True)
    ok &= check("type 恒为 1（文本）", priv.type == 1)

    print("\n── 发言人标注：群聊里必须标出「谁说的」（纯函数，不需要真库）──")
    names = {"wxid_friendA": "老张", "wxid_friendB": "李四"}
    grp_msgs = [
        {"time": "t1", "content": "进度同步一下", "is_self": 0, "sender": "wxid_friendA"},
        {"time": "t2", "content": "收到", "is_self": 1, "sender": ""},
        {"time": "t3", "content": "我再看看", "is_self": 0, "sender": "wxid_friendB"},
        {"time": "t4", "content": "不认识的号", "is_self": 0, "sender": ""},
    ]
    lines = agent_tools.format_history_lines(grp_msgs, names, "项目群", True)
    ok &= check("群里标出真实发言人", "老张: 进度同步一下" in lines[0], lines)
    ok &= check("自己发的标「我」", lines[1].startswith("[t2] 我:"), lines)
    ok &= check("sender 认不出就打群成员，不冒充", lines[3].endswith("群成员: 不认识的号"), lines)
    # 群名不能被当成发言人——这正是修之前的 bug
    ok &= check("不再把群名当发言人", not any("项目群:" in x for x in lines), lines)

    solo = agent_tools.format_history_lines(
        [{"time": "t1", "content": "在吗", "is_self": 0, "sender": ""}], names, "老张", False)
    ok &= check("单聊回退到联系人名", solo[0].endswith("老张: 在吗"), solo)

    cut = agent_tools.format_history_lines(
        [{"time": "t", "content": "x" * 500, "is_self": 1, "sender": ""}],
        names, "老张", False, line_chars=100)
    ok &= check("按 line_chars 截断", len(cut[0]) < 200, len(cut[0]))
    ok &= check("空白内容不占一行",
                agent_tools.format_history_lines(
                    [{"time": "t", "content": "   ", "is_self": 1, "sender": ""}],
                    names, "老张", False) == [])

    print("\n── 原始 id 原样透传，且不消耗查库预算 ──")

    class _Boom:
        """一被查库就炸——用来证明透传这条路根本没碰客户端。"""
        def __getattr__(self, name):
            raise AssertionError(f"不该查库，却调了 {name}")

    box = agent_tools.ToolBox(_Boom(), {"agent": {"max_queries": 3}},
                              [{"wxid": "wxid_friendA", "name": "张三", "remark": "老张"}],
                              SELF_WXID, "wxid_control")
    ok &= check("wxid 原样透传", box._resolve("wxid_zzz")[0]["wxid"] == "wxid_zzz")
    ok &= check("roomid 原样透传（群终于能读了）",
                box._resolve("room123@chatroom")[0]["wxid"] == "room123@chatroom")
    ok &= check("filehelper 原样透传", box._resolve("filehelper")[0]["wxid"] == "filehelper")
    ok &= check("已知 wxid 仍用列表里的显示名",
                box._resolve("wxid_friendA")[0].get("remark") == "老张")
    ok &= check("透传不消耗查库预算", box.budget.left == 3, box.budget.left)

    print("\n── 重名不再静默取第一个 ──")
    dup = agent_tools.ToolBox(_Boom(), {"agent": {"max_queries": 3}}, [
        {"wxid": "wxid_a", "name": "张三", "remark": "老张"},
        {"wxid": "wxid_b", "name": "张三", "remark": "小张"},
    ], SELF_WXID, "wxid_control")
    cand, err = dup._one("张三")
    ok &= check("重名时要求用户指定", cand is None and "匹配到多个人" in err, (cand, err))
    cand2, err2 = dup._one("老张")
    ok &= check("名字唯一时正常返回", cand2 and cand2["wxid"] == "wxid_a", (cand2, err2))

    print("\n── 预取路径也必须显示名字（build_user_prompt 用的就是它）──")
    bnames = {"wxid_x": "张三", "12345678901@chatroom": "老同学群"}
    ok &= check("单聊显示备注名",
                bot._msg_speaker(
                    {"talker": "wxid_x", "is_self": 0, "sender": "wxid_x"}, bnames) == "张三")
    ok &= check("自己发的显示「我」",
                bot._msg_speaker({"talker": "wxid_x", "is_self": 1, "sender": ""}, bnames) == "我")
    ok &= check("群聊带上群名和发言人",
                bot._msg_speaker(
                    {"talker": "12345678901@chatroom", "is_self": 0, "sender": "wxid_x"},
                    bnames) == "老同学群/张三")
    ok &= check("群里认不出发言人时不编",
                bot._msg_speaker(
                    {"talker": "12345678901@chatroom", "is_self": 0, "sender": ""},
                    bnames) == "老同学群/群成员")
    # 这条以前期望返回裸 `wxid_zzz`——那**正是** CLAUDE.md 明令禁止的
    # 「把 talker 原样塞进给模型的文本」。改成：查不到显示名就退回「对方」。
    ok &= check("认不出的 wxid 不外泄原始 id（退回「对方」）",
                bot._msg_speaker({"talker": "wxid_zzz", "is_self": 0, "sender": ""},
                                 bnames) == "对方")

    print("\n── 群成员解码（用真抓到的 ext_buffer，纯函数）──")
    mem = live_history.decode_room_members(ROOM_BUF)
    ok &= check("解出成员数", len(mem) == 2, mem)
    ok &= check("解出 wxid + 群昵称 + 角色",
                mem and mem[0] == {"wxid": "wxid_aaaaaaaaaaaaa",
                                   "name": "老张", "role": 9}, mem)
    ok &= check("群昵称缺失时留空、不编",
                len(mem) > 1 and mem[1]["name"] == "" and mem[1]["role"] == 8193, mem[1:])
    ok &= check("空/垃圾输入返回空而不是崩",
                live_history.decode_room_members("") == []
                and live_history.decode_room_members("zzzz") == []
                and live_history.decode_room_members("0A0BFF") == [])

    print("\n── 发图的目录边界（path 是模型填的，必须限制）──")
    with tempfile.TemporaryDirectory() as td:
        okdir = os.path.join(td, "ok")
        os.makedirs(okdir)
        good = os.path.join(okdir, "a.png")
        with open(good, "wb") as f:
            f.write(b"x")
        bad_ext = os.path.join(okdir, "b.txt")
        with open(bad_ext, "wb") as f:
            f.write(b"x")
        outside = os.path.join(td, "c.png")
        with open(outside, "wb") as f:
            f.write(b"x")
        ibox = agent_tools.ToolBox(
            _Boom(), {"agent": {"max_queries": 3, "send_image_dirs": [okdir]}},
            [], SELF_WXID, "wxid_control")
        p, e = ibox._image_path_ok(good)
        ok &= check("允许目录内的图片放行", bool(p) and e is None, (p, e))
        p, e = ibox._image_path_ok(outside)
        ok &= check("目录外的文件被拒", p == "" and "不在允许" in e, e)
        p, e = ibox._image_path_ok(bad_ext)
        ok &= check("不是图片后缀的被拒", p == "" and "不是图片" in e, e)
        p, e = ibox._image_path_ok(os.path.join(okdir, "nope.png"))
        ok &= check("文件不存在被拒", p == "" and "找不到" in e, e)
        p, e = ibox._image_path_ok(os.path.join(okdir, "..", "c.png"))
        ok &= check("用 .. 绕出去也被拒", p == "" and "不在允许" in e, e)

    print("\n── 确认发送的分派：文本 / 图片 / 转发 ──")

    class _Rec:
        def __init__(self):
            self.calls = []

        def send_text(self, msg, wxid):
            self.calls.append(("text", wxid, msg))

        def send_image(self, path, wxid):
            self.calls.append(("image", wxid, path))

        def send_xml(self, xml, wxid):
            self.calls.append(("xml", wxid, xml))

    rec = _Rec()
    agent_tools.send_pending(rec, {"to_wxid": "w", "text": "hi", "count": 2}, 0.0)
    ok &= check("文本可以连发",
                rec.calls == [("text", "w", "hi"), ("text", "w", "hi")], rec.calls)
    rec.calls.clear()
    agent_tools.send_pending(rec, {"to_wxid": "w", "image": "C:/a.png", "count": 9}, 0.0)
    ok &= check("图片只发一次（count 对它没意义）",
                rec.calls == [("image", "w", "C:/a.png")], rec.calls)
    rec.calls.clear()
    agent_tools.send_pending(rec, {"to_wxid": "w", "xml": "<x/>"}, 0.0)
    ok &= check("转发只发一次", rec.calls == [("xml", "w", "<x/>")], rec.calls)

    print("\n── sender_name 优先（群昵称比拿 wxid 查表准）──")
    ok &= check("有 sender_name 就用它",
                agent_tools.speaker_of(
                    {"is_self": 0, "sender": "wxid_x", "sender_name": "老张"},
                    {"wxid_x": "张三"}, "群", True) == "老张")
    ok &= check("没有 sender_name 才回退查表",
                agent_tools.speaker_of(
                    {"is_self": 0, "sender": "wxid_x"}, {"wxid_x": "张三"},
                    "群", True) == "张三")

    print("\n── 短期对话记忆：TTL / 上限 / 会话隔离 ──")
    dcfg = {"agent": {"dialog_turns": 2, "dialog_ttl": 900}}
    ok &= check("一开始没有记忆", bot.dialog_history("c1", dcfg) == [])
    bot.dialog_append("c1", "user", "你好", dcfg)
    bot.dialog_append("c1", "assistant", "在的", dcfg)
    ok &= check("记下一问一答",
                [t["content"] for t in bot.dialog_history("c1", dcfg)] == ["你好", "在的"])
    ok &= check("按会话隔离", bot.dialog_history("c2", dcfg) == [])
    ok &= check("空内容不记",
                bot.dialog_append("c1", "user", "   ", dcfg) is None
                and len(bot.dialog_history("c1", dcfg)) == 2)
    for t in ("第三句", "第四句", "第五句"):
        bot.dialog_append("c1", "user", t, dcfg)
    got = [t["content"] for t in bot.dialog_history("c1", dcfg)]
    ok &= check("超过 dialog_turns 从头丢", got == ["在的", "第三句", "第四句", "第五句"], got)
    ok &= check("dialog_turns=0 等于关闭",
                bot.dialog_history("c1", {"agent": {"dialog_turns": 0}}) == [])
    bot._DIALOG["c1"]["ts"] = time.time() - 10_000
    ok &= check("超过 dialog_ttl 就失效", bot.dialog_history("c1", dcfg) == [])
    bot.dialog_forget("c1")
    ok &= check("dialog_forget 清干净", bot.dialog_history("c1", dcfg) == [])

    print("\n── 4.x：fts 句柄失效时退到 session.db 兜底 ──")
    srv3 = ThreadingHTTPServer(("127.0.0.1", 0), _V4StaleFtsStub)
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    v4c = aixed_api.AixedClient(base_url=f"http://127.0.0.1:{srv3.server_address[1]}")
    live_history.set_self_wxid(SELF_WXID)

    ok &= check("识别为微信 4.x", live_history.is_wechat4(v4c) is True)
    ok &= check("fts 分片表探测为空", live_history._v4_fts_tables(v4c) == [])
    ok &= check("探测为空时主动重扫了一次", _V4StaleFtsStub.dbs_called == 1,
                _V4StaleFtsStub.dbs_called)

    cur = live_history.latest_cursor(v4c)
    ok &= check("起点退回 session.db 时间游标", cur.get("__time__") == V4_SESSION_TS, cur)

    msgs, cur2 = live_history.new_messages(v4c, {"__time__": NOW})
    ok &= check("fts + Msg_ 双掉线仍能拿到消息",
                len(msgs) == 1 and msgs[0]["content"] == V4_SESSION_SUMMARY, msgs)
    ok &= check("认出这条是自己发的", bool(msgs) and msgs[0]["is_self"] == 1, msgs)
    ok &= check("游标推进到最新", cur2.get("__time__") == V4_SESSION_TS, cur2)

    # 重扫必须限流：紧接着再来一轮不该再调 GetAllDBName
    before = _V4StaleFtsStub.dbs_called
    live_history.new_messages(v4c, {"__time__": NOW})
    ok &= check("重扫受限流保护，不重复调 GetAllDBName",
                _V4StaleFtsStub.dbs_called == before, _V4StaleFtsStub.dbs_called)

    # recent_messages 走 session.db 的 SessionTable（一次查询），不该碰 fts，
    # 也不该调 GetAllDBName——所以上面的计数必须原地不动。
    box4 = agent_tools.ToolBox(v4c, {"agent": {"max_queries": 3}}, [],
                               SELF_WXID, "wxid_control")
    before_r = _V4StaleFtsStub.dbs_called
    txt = box4.t_recent_messages({"limit": 5})
    ok &= check("recent_messages 拿到 session.db 的那条",
                V4_SESSION_SUMMARY in txt, txt)
    ok &= check("recent_messages 不触发 GetAllDBName",
                _V4StaleFtsStub.dbs_called == before_r, _V4StaleFtsStub.dbs_called)
    ok &= check("recent_messages 扣了一次查库预算", box4.budget.left == 2, box4.budget.left)
    # 控制会话自己不能出现在「最近消息」里（否则助手把自己的旧答复当新消息）
    box4b = agent_tools.ToolBox(v4c, {"agent": {"max_queries": 3}},
                                [{"wxid": "filehelper", "name": "文件传输助手"}],
                                SELF_WXID, "filehelper")
    ok &= check("排除控制会话自己",
                "没查到" in box4b.t_recent_messages({}), box4b.t_recent_messages({}))

    # 群成员：走 contact.db 的 chat_room.ext_buffer（不碰 fts）
    box5 = agent_tools.ToolBox(v4c, {"agent": {"max_queries": 5}}, [],
                               SELF_WXID, "wxid_control")
    gtxt = box5.t_group_members({"contact": "12345678901@chatroom"})
    ok &= check("group_members 标出群昵称和群主", "老张（群主）" in gtxt, gtxt)
    ok &= check("group_members 报出总人数", "共 2 人" in gtxt, gtxt)
    ok &= check("非群会话被拒绝，不硬查",
                "不是群" in box5.t_group_members({"contact": "wxid_friendA"}))
    before_g = _V4StaleFtsStub.dbs_called
    ok &= check("group_members 不触发 GetAllDBName",
                _V4StaleFtsStub.dbs_called == before_g, _V4StaleFtsStub.dbs_called)

    # 未读：直接用微信自己在 SessionTable 里维护的 unread_count
    ptxt = box5.t_pending_replies({"limit": 5})
    ok &= check("pending_replies 用微信自己的未读数", "未读 3 条" in ptxt, ptxt)
    ok &= check("pending_replies 带上最后一条内容",
                V4_SESSION_SUMMARY in ptxt, ptxt)

    # 关掉自动重扫后不再触发；session.db 兜底必须照常工作
    live_history.set_rescan_interval(0)
    v4b = aixed_api.AixedClient(base_url=f"http://127.0.0.1:{srv3.server_address[1]}")
    before2 = _V4StaleFtsStub.dbs_called
    ok &= check("关闭后 fts 仍探测为空", live_history._v4_fts_tables(v4b) == [])
    ok &= check("关闭后不再触发重扫", _V4StaleFtsStub.dbs_called == before2,
                _V4StaleFtsStub.dbs_called)
    msgs_b, _ = live_history.new_messages(v4b, {"__time__": NOW})
    ok &= check("关掉重扫后 session.db 兜底照常工作", len(msgs_b) == 1, msgs_b)
    live_history.set_rescan_interval(300)
    srv3.shutdown()

    print("\n── 未登录时必须判定为「未就绪」，不能硬闯 ──")
    srv2 = ThreadingHTTPServer(("127.0.0.1", 0), _NoDbStub)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    nli = aixed_api.AixedClient(base_url=f"http://127.0.0.1:{srv2.server_address[1]}")
    okp, msg = nli.ping()
    ok &= check("未登录时 ping 返回 False", okp is False, okp)
    ok &= check("未登录时提示里包含「登录」", "登录" in str(msg), msg)
    srv2.shutdown()

    print("\n── 连不上时的报错 ──")
    dead = aixed_api.AixedClient(base_url="http://127.0.0.1:1", timeout=2)
    try:
        dead.get_dbs()
        ok &= check("连不上应抛 AixedError", False)
    except aixed_api.AixedError as e:
        ok &= check("连不上应抛 AixedError", True)

    srv.shutdown()
    print("\n" + "=" * 50)
    print("全部通过 ✅" if ok else "有失败项 ❌")
    print("=" * 50)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
