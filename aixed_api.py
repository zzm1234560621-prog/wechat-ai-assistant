"""aixed/WeChat-Hook 的 HTTP 客户端，作为 wcferry 的替代后端。

设计要点：对外暴露 query_sql / get_dbs / send_text / get_self_wxid，
签名与 wcferry 一致，所以 live_history.py 那层一行都不用改
（那边本来就是 getattr 取方法，不认具体类型）。

和 wcferry 的两处关键差别：
  1. 这套接口**没有收消息**的接口，所以 bot.py 靠轮询数据库拿新消息，
     见 poll_messages / latest_cursor。
  2. 底层仍是同一个 SQLite，SQL 语法完全一致，只用把 db 名和 SQL 发过去。

接口来源：仓库 postman/WeChat-Hook.postman_collection.json（已存 docs/aixed-api.postman.json）
    POST /QueryDB/execute        {"optDbName": "MSG0.db", "SQL": "..."}
    POST /QueryDB/GetAllDBName   {}
    GET  /QueryDB/status
    POST /SendTextMsg            {"wxidorgid": "...", "msg": "..."}
    POST /GetSelfProfile         {}

注意：本文件对**返回结构**做了容错（官方只公开了请求格式），
如果实际返回跟预期不符，改 _rows 的取值键即可。
"""
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:8080"

# 超过这个秒数的查询会被打上警告。
# hook 是「内存扫描找数据库句柄」的实现，开始劣化时最直观的信号就是查询变慢，
# 所以这个阈值要低——宁可多报，也不要等到微信卡死才发现。
SLOW_QUERY_SEC = 1.0


class AixedError(RuntimeError):
    """连不上服务或服务返回了错误。"""


def _as_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class Msg:
    """冒充 wcferry 的 Message，让 bot.py 的消息处理逻辑可以原样复用。

    只保留 bot.py 用到的：type / sender / roomid / content / from_self()。
    """

    __slots__ = ("talker", "content", "is_self", "create_time")

    def __init__(self, talker="", content="", is_self=0, create_time=0):
        self.talker = str(talker or "")
        self.content = str(content or "")
        self.is_self = _as_int(is_self)
        self.create_time = _as_int(create_time)

    @property
    def type(self):
        return 1  # 轮询只取文本

    @property
    def sender(self):
        return self.talker

    @property
    def roomid(self):
        return self.talker if self.talker.endswith("@chatroom") else ""

    def from_self(self):
        return bool(self.is_self)


class AixedClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, timeout=15):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    # ---------- 底层 HTTP ----------

    def _request(self, method, path, payload=None):
        url = self.base_url + path
        data, headers = None, {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            raise AixedError(f"{path} 返回 HTTP {e.code}：{detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise AixedError(
                f"连不上 {url}（{e}）。确认微信已启动、version.dll 已加载、端口配对。"
            ) from e
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    @staticmethod
    def _rows(resp):
        """从各种可能的返回结构里取出行列表。"""
        if resp is None:
            return []
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for k in ("data", "Data", "result", "Result", "rows", "list"):
                v = resp.get(k)
                if isinstance(v, list):
                    return v
            # 有的实现把单行结果直接摊平在顶层
            if any(k in resp for k in ("StrTalker", "StrContent", "UserName")):
                return [resp]
        return []

    @staticmethod
    def _check(resp, what=""):
        """这套接口用 {"status": <负数>, "desc": "..."} 表示失败。

        必须显式检查：否则查库失败会被 _rows 当成「空结果」，
        表现成「没有新消息」，把错误静默吞掉。
        """
        if isinstance(resp, dict):
            st = resp.get("status")
            if isinstance(st, int) and st < 0:
                raise AixedError(f"{what}失败：{resp.get('desc') or resp}")
        return resp

    # ---------- 与 wcferry 对齐的接口 ----------

    def query_sql(self, db, sql):
        """执行 SQL，返回行列表（dict 或 list 均可）。"""
        t0 = time.time()
        resp = self._request("POST", "/QueryDB/execute", {"optDbName": db, "SQL": sql})
        dt = time.time() - t0
        if dt >= SLOW_QUERY_SEC:
            print(f"[aixed] ⚠️ 慢查询 {dt:.2f}s  db={db}  sql={str(sql)[:70]}",
                  file=sys.stderr, flush=True)
        self._check(resp, f"查库 {db} ")
        return self._rows(resp)

    def get_dbs(self):
        """返回所有数据库名。

        实测返回形如 [{"dbHandle": 123, "dbName": "contact.db"}, ...]。
        """
        resp = self._request("POST", "/QueryDB/GetAllDBName", {})
        out = []
        for r in self._rows(resp):
            if isinstance(r, str):
                out.append(r)
            elif isinstance(r, dict):
                for k in ("dbName", "name", "Name", "dbname", "fileName", "FileName"):
                    if r.get(k):
                        out.append(str(r[k]))
                        break
        return out

    def send_text(self, msg, wxid):
        """参数顺序与 wcferry.send_text(msg, receiver) 一致。"""
        return self._request("POST", "/SendTextMsg", {"wxidorgid": wxid, "msg": msg})

    def send_image(self, path, wxid):
        """发本地图片。hook 只要求 path 是个能读到的文件路径。"""
        return self._request("POST", "/SendImgMsg", {"wxidorgid": wxid, "path": path})

    def send_xml(self, xml, wxid):
        """转发一条已有消息：xml 是那条消息的**原始 XML**（不是摘要）。

        用于「把某条消息转给某人」——微信的转发本质就是把原 XML 再发一次。
        """
        return self._request("POST", "/ForwardXMLMsg", {"to_wxid": wxid, "content": xml})

    def self_profile(self):
        return self._request("POST", "/GetSelfProfile", {})

    def get_self_wxid(self):
        p = self.self_profile()
        for src in (p, p.get("data") if isinstance(p, dict) else None):
            if isinstance(src, dict):
                for k in ("wxid", "userName", "UserName", "wxid_orgid", "wxidOrgid"):
                    if src.get(k):
                        return str(src[k])
        return ""

    def db_status(self):
        """返回 {IsLogin: 0/1, hWeixin: 句柄}。"""
        return self._request("GET", "/QueryDB/status")

    def is_login(self):
        """微信是否已登录。没登录时数据库是空的，查什么都是空结果。"""
        s = self.db_status()
        try:
            return int((s or {}).get("IsLogin", 0)) == 1
        except (TypeError, ValueError, AttributeError):
            return False

    def ping(self):
        """服务可用且已登录。返回 (是否可用, 说明)。

        不用 GetAllDBName 判断——那个接口是内存扫描实现，实测会 500。
        改成直接探两个真实存在的库：查得通就说明已登录、hook 正常。
        """
        try:
            self.db_status()
        except AixedError as e:
            return False, str(e)
        # 4.x 探 session/contact.db，3.9.x 探 MicroMsg.db——两种后端都要覆盖，
        # 别只探一版的库，否则另一版会被误判成「没登录」。
        for db, sql in (("session.db", "SELECT 1 FROM SessionTable LIMIT 1"),
                        ("contact.db", "SELECT 1 FROM contact LIMIT 1"),
                        ("MicroMsg.db", "SELECT 1 FROM Contact LIMIT 1")):
            try:
                self.query_sql(db, sql)
                who = self.get_self_wxid()
                return True, who or "(已登录，但取不到 wxid)"
            except AixedError:
                continue
        return False, "hook 已加载，但数据库打不开（微信没登录？请在微信里扫码登录）"

    # ---------- 轮询收消息（这套接口没有收消息回调） ----------
    # 「怎么按微信版本取消息」的 schema 知识放在 live_history 里，这里只做委托与适配。

    def latest_cursor(self):
        """当前最新消息时间，用作轮询起点——只处理启动之后的新消息。"""
        import live_history
        return live_history.latest_cursor(self)

    def prime(self, seen_max=2000):
        """启动时调用一次：把当前最新那一批消息标记为「已见」。

        必须在开始轮询前调用。否则第一次轮询会把边界上那些老消息当成新消息，
        导致每次重启都对旧消息重复回复一遍。

        返回 (游标, seen)，之后交给 poll_messages 循环原样带着走。
        """
        import live_history
        cursor = live_history.latest_cursor(self)
        raw, cursor = live_history.new_messages(self, cursor)
        seen = {}
        for m in raw:
            seen[self._key(m)] = None
        return cursor, seen

    @staticmethod
    def _key(m):
        # 消息 dict 的内部排序键统一叫 _ts（live_history 里都是这个）
        return (m["talker"], m["_ts"], str(m["content"])[:120])

    def poll_messages(self, since=0, seen=None, limit=200, seen_max=2000):
        """查 create_time >= since 的文本消息。

        返回 (Msg 列表, 新游标, seen)。调用方把这三样存下来，下次原样传回。

        用 >= 而不是 >，再配合 seen 去重：边界上同一秒到达的消息不会漏，
        重复查到的也不会二次处理。seen 是 dict（当有序集合用），超过 seen_max 淘汰最早的。
        """
        import live_history
        if seen is None:
            seen = {}
        raw, new_cursor = live_history.new_messages(self, since, limit)
        out = []
        for m in raw:
            key = self._key(m)
            if key in seen:
                continue
            seen[key] = None
            out.append(Msg(m["talker"], m["content"], m["is_self"], m["_ts"]))
        while len(seen) > seen_max:
            seen.pop(next(iter(seen)))
        return out, new_cursor, seen
