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

# hook 的默认端口是 **30001**（config.yaml 的 aixed_base_url、CLAUDE.md、
# docs/aixed-api.postman.json 三处都是它）。这里只是配置缺失时的兜底默认值，
# 以前写成 8080，兜底生效时就会去连一个没人监听的端口，报「连不上」而不是真因。
DEFAULT_BASE_URL = "http://127.0.0.1:30001"

# 超过这个秒数的查询会被打上警告。
# hook 是「内存扫描找数据库句柄」的实现，开始劣化时最直观的信号就是查询变慢，
# 所以这个阈值要低——宁可多报，也不要等到微信卡死才发现。
SLOW_QUERY_SEC = 1.0

# 发普通文件走哪个端点。**这是个反直觉的坑，别按名字选**：
#   "imgmsg"（默认）= POST /SendImgMsg —— 本版 hook 上**唯一真能发出普通文件**的路。
#     端点名字里的 Img 是历史遗留：上游 update.log 20260727
#     「发送图片等接口统一改为发送文件类接口」，实测 xlsx / zip 都发出成文件消息。
#   "filemsg"      = POST /SendFileMsg —— 名字最正经的那条，但本版**没有这个路由**
#     （实测 404）。只在换了带该接口的 hook 之后才有意义。
SEND_FILE_ENDPOINTS = {
    "imgmsg": "/SendImgMsg",
    "filemsg": "/SendFileMsg",
}


def send_file_via(cfg=None):
    """解析「发普通文件打哪个端点」，返回路径。**写歪的值一律回退到默认的 imgmsg**。

    为什么不 fail-safe 到「什么都不发」：这是**能力**不是**安全闸**——
    写错一个词就让用户以为「发文件坏了」，比回退到唯一能用的那条更糟。
    （真正把关的是 `agent_tools.send_file_on` 那个能力闸和待确认队列。）
    """
    sec = (cfg or {}).get("agent") or {}
    raw = str(sec.get("send_file_via") or "").strip().lower()
    return SEND_FILE_ENDPOINTS.get(raw) or SEND_FILE_ENDPOINTS["imgmsg"]


class AixedError(RuntimeError):
    """连不上服务或服务返回了错误。"""


class AixedUnreachable(AixedError):
    """**根本没连上** hook（进程没了 / 端口不通 / 卡到不接连接）。

    为什么要单独一个类型（2026-10-06）：这两种失败在处置上**完全不同** ——
    「连不上」归登录探针（微信没了就该叫用户重启），而「连上了、但它回『库查不动』」
    才是「hook 可达、库层已死」。拿一个通用异常 + 去 grep 文案区分是判据散落，
    所以在唯一知道这个区别的地方（`_http` 的 except 分支）把它类型化。
    ⚠️ 它**是** `AixedError` 的子类：现有 `except AixedError` 的地方行为一个字不变。
    """


def _as_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


class Msg:
    """冒充 wcferry 的 Message，让 bot.py 的消息处理逻辑可以原样复用。

    只保留 bot.py 用到的：type / sender / roomid / content / from_self()。
    `local_type` 是 4.x 的消息类型（1=文本 3=图片 49=appmsg…）：**图片不在 fts 里**，
    是靠 `live_history` 的非文本补漏捞回来的，上层要区分它才能决定该不该响应
    （例如自己刚发出去的图不该再当成新消息答一遍）。默认 1 = 文本，老调用不受影响。

    `local_id` 是**这条消息在 `Msg_<md5(会话)>` 表里的主键**（fts 那条路没有它，
    非文本补漏那条路有）。**语音转写必须要它**：`live_history.voice_info()` 就是按
    (talker, local_id) 点查的。默认空串 = 这条消息没带，调用方要自己判。
    """

    __slots__ = ("talker", "content", "is_self", "create_time", "local_type",
                 "local_id")

    def __init__(self, talker="", content="", is_self=0, create_time=0, local_type=1,
                 local_id=""):
        self.talker = str(talker or "")
        self.content = str(content or "")
        self.is_self = _as_int(is_self)
        self.create_time = _as_int(create_time)
        self.local_type = _as_int(local_type) or 1
        self.local_id = str(local_id or "")

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


def detect_self_wxid(client):
    """任意 aixed 后端客户端 → 自己的 wxid（认不出给空串）。

    模块级入口，给 `bot.py` 启动那一段用：**配置没有、接口也给不出来时才走它**。
    `getattr` 是因为自测里那些假客户端只认 `query_sql(db, sql)`；不支持就返回空串，
    这里**不吞真异常**（认不出自己本来就是「如实说不知道」那一类，不拦截别的问题）。
    """
    fn = getattr(client, "detect_self_wxid", None)
    if not callable(fn):
        return ""
    try:
        return str(fn() or "")
    except Exception:
        return ""


def account_dir_wxids():
    """本机微信**登录过的账号目录**里的 wxid（离线，不碰 hook；认不出给空列表）。

    微信 4.x 的账号目录名就是 `<wxid>_<数字后缀>`（`D:\\wechat\\xwechat_files\\wxid_xxx_2895`），
    目录名是微信自己写的、跟登录态绑定 —— 这是本项目里唯一一条**确定**的「本机有谁登录过」
    的判据，`find_self_wxid.py` 一直用它。这里把它接进「我是谁」那条链，
    用来**核实**下面那条经验判据（见 `resolve_self_wxid`）。

    ⚠️ 它列出的是「这台机器登录过的账号」，**不保证只有一个**（同一个人有两台号、或
    别人在这台机器上登录过）。多个时**绝不替用户挑**——调用方要么不用、要么如实报出来。
    """
    try:
        import find_self_wxid
        found = find_self_wxid.find_self_wxid()      # [(wxid, 账号目录), ...]
    except Exception:
        return []
    out = []
    for item in found or []:
        try:
            w = str(item[0] or "")
        except (TypeError, IndexError):
            continue
        if w and w not in out:
            out.append(w)
    return out


def resolve_self_wxid(cfg, client, backend):
    """「我是谁」的**唯一**解析入口 → `(wxid, 来源说明, 这次查了什么, 说明/告警)`。

    四级（顺序就是优先级）：
      ① `config.yaml` 的 `self_wxid` —— 用户手填，最高，**不查任何库**；
      ② hook 的 `/GetSelfProfile`（`client.get_self_wxid()`；**有的构建不给 wxid**）；
      ③ 本机微信**账号目录名**（`account_dir_wxids()`，离线确定；只有一个账号时才敢直接用）；
      ④ 从本地 `contact` 表认（`detect_self_wxid`）—— **必须与 ③ 对得上才敢用**。

    **为什么 ④ 必须被核实**（2026-10-06 第二次换台电脑真机）：④ 的判据是「contact 表里
    第一个 `wxid_` 开头的行」，那是从**一台机器**上观察到的**行序**，不是证明。那台机器上
    它认出了 `wxid_q73…`，但消息里的 `sender_id` 根本不是它 ⇒ `is_self` 恒为 0 ⇒ bot 把
    「自己刚发出去的回复」当成对方的新消息，一遍遍自己答自己（用户看到的就是「重复回复」），
    而且**全程不报错**。认错比认不出更糟：认错等于拿别人的身份说话（不跳过自己、
    历史里把别人当我、还可能拿别人的号发消息）。所以对不上就**否掉**、如实说「认不出」。

    4 元组的第 4 项是给用户看的说明（正常时是 `""`）：bot 启动和 `verify_real.py`
    都把它打出来，**不许静默**。`wxid` 为空而第 4 项非空 = 「本来猜了一个、被我否掉了」。
    """
    wxid = str((cfg or {}).get("self_wxid") or "")
    if wxid:
        return wxid, "config.yaml", [], ""

    used = []
    try:
        wxid = client.get_self_wxid() or ""
    except Exception:
        wxid = ""
    used.append("get_self_wxid")
    if wxid:
        return wxid, "hook 接口", used, ""

    if backend != "aixed":
        # wcferry/3.9.x 那条路没有这套「从库里认」的判据（见 bot.py 的 connect_wcferry）。
        return "", "", used, ""

    dirs = account_dir_wxids()               # ③ 离线、确定（不查库）
    used.append("account_dir_wxids")
    if len(dirs) == 1:
        return dirs[0], "账号目录（离线判据）", used, ""

    guess = detect_self_wxid(client)          # ④ 经验判据（要核实）
    used.append("detect_self_wxid")
    if not guess:
        return "", "", used, ""

    if not dirs:
        # 找不到微信账号目录 ⇒ 没法核实。保留原有兜底行为，但如实标注「没核实过」。
        return guess, "contact 表（自动认的·未核实）", used, \
            "本机找不到微信账号目录，这个值是猜的、没能核实（建议写进 config.yaml 的 self_wxid）"

    if guess in dirs:
        return guess, "contact 表（自动认的·已核实）", used, ""

    return "", "", used, (
        "contact 表猜出来的「%s」**不在本机登录过的账号里**（%s），已否掉："
        "认错人比认不出更糟（认错会拿别人的身份说话）"
        % (guess, "、".join(dirs)))


# 本机 hook 的 HTTP 请求**一律不走代理**（2026-10-06 真机：助手"看起来没在工作"的真因）：
# 助手的进程环境里可能带 `http_proxy` / `ALL_PROXY`（那台现场 `netstat` 能看到它往
# `127.0.0.1:10808` 发 SYN_SENT，而那个代理并没在跑 ⇒ **每个请求都 10061**，于是收不到消息、
# 状态盘 healthy=False，看起来像"助手死了"）。urllib 的 `proxy_bypass` 对字面量 `127.0.0.1`
# **并不保证**成立，而这个 hook **永远在本机回环上** —— 走代理在物理上就是错的。
# 所以显式给一个空 `ProxyHandler`：与系统/环境里的代理设置**彻底无关**。
# 回归：`selftest_aixed`（子进程里带上指向死端口的代理变量，请求必须照样成功）。
# ⚠️ 同一类问题还有 `web_read.py`（本机 SearXNG 也是回环），它那边同样显式禁代理。
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AixedClient:
    # 允许调用方**按次**覆盖超时（`query_sql(db, sql, timeout=...)`）。live_history 用它
    # 给轮询里的查询压短超时：hook 偶尔会卡住，而 15 秒 × 一轮六七个查询 = 一轮一分多钟，
    # 用户看到的就是「发了消息没反应」（2026-10-05 真机）。
    supports_call_timeout = True

    def __init__(self, base_url=DEFAULT_BASE_URL, timeout=15):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout


    def _request(self, method, path, payload=None, timeout=None):
        url = self.base_url + path
        data, headers = None, {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            # ⚠️ 用 `_LOOPBACK_OPENER` 而不是 `urllib.request.urlopen`：见它上面的注释
            # （本机回环**永不走代理**；urlopen 会按环境/系统代理设置走）。
            with _LOOPBACK_OPENER.open(req, timeout=timeout or self.timeout) as r:
                body = r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            raise AixedError(f"{path} 返回 HTTP {e.code}：{detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise AixedUnreachable(
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
    def _neg(v):
        """值是「负数」就返回它，否则 None（兼容 `-1` 和 `"-1"` 两种写法）。"""
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v if v < 0 else None
        if isinstance(v, str):
            try:
                n = int(v.strip())
            except (TypeError, ValueError):
                return None
            return n if n < 0 else None
        return None

    @classmethod
    def _check(cls, resp, what=""):
        """失败必须显式抛错——否则会被 `_rows` 当成「空结果」静默吞掉。

        这套 hook 的失败标记**有两种**：
          * `{"status": <负数>, "desc": ...}`（查库那些接口）
          * `{"ret": <负数>, "msg"/"retmsg": ...}`（发送那些接口；成功是
            `{"ret":0,"retmsg":"success"}`，JSON 坏掉是 `{"ret":-1,...}`）
        以前这里**只认 `status`**，于是发送类接口的 `ret:-1` 被当成功放过去。
        ⚠️ 但要知道限度：`/SendImgMsg` **成功也是无条件 `ret:0`**（hook 侧
        `WeixinSend::SendImage` 是 void、连 HeapAlloc 失败都只是 return），
        所以「没报错」**不等于真发出去了**——发图之后要不要读回校验，
        由上层按代价决定（见 `live_history` 的图片补漏与 `bot.py` 的发送分支）。
        """
        if isinstance(resp, dict):
            for key, desc in (("status", "desc"), ("ret", "retmsg")):
                if cls._neg(resp.get(key)) is not None:
                    detail = resp.get(desc) or resp.get("msg") or resp
                    raise AixedError(f"{what}失败：{detail}")
        return resp


    def query_sql(self, db, sql, timeout=None):
        """执行 SQL，返回行列表（dict 或 list 均可）。`timeout` 给轮询那条路压短用。"""
        t0 = time.time()
        resp = self._request("POST", "/QueryDB/execute", {"optDbName": db, "SQL": sql},
                             timeout=timeout)
        dt = time.time() - t0
        if dt >= SLOW_QUERY_SEC:
            print(f"[aixed] ⚠️ 慢查询 {dt:.2f}s  db={db}  sql={str(sql)[:70]}",
                  file=sys.stderr, flush=True)
        self._check(resp, f"查库 {db} ")
        return self._rows(resp)

    def get_dbs(self, timeout=None):
        """返回所有数据库名。

        实测返回形如 [{"dbHandle": 123, "dbName": "contact.db"}, ...]。

        ⚠️ 这个接口会触发 700MB 进程里的**全内存扫描**（`m_dbs.clear()` + 重新搜），
        所以只由 `live_history.force_rescan()` 调、且带着限流；这里的 `timeout`
        就是给那条路压短用的——我们只要「句柄表被重建」这个副作用，返回快慢不重要。
        """
        resp = self._request("POST", "/QueryDB/GetAllDBName", {}, timeout=timeout)
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

        ⚠️ **这条路当前是死的**（2026-10-02 核实）：`wx_send_xml.cpp` 里
        `ForwardXMLMsg` 对**所有**类型都 `return false`——真机实测转发会把微信进程
        带崩（连 dmp 都不留），所以作者改成了安全拒绝。所以它现在总是失败，
        调用方必须如实报「转发不了」，绝不许说成已发出。

        用于「把某条消息转给某人」——微信的转发本质就是把原 XML 再发一次。
        """
        return self._request("POST", "/ForwardXMLMsg", {"to_wxid": wxid, "content": xml})

    def send_file(self, path, wxid, cfg=None):
        """发一个**普通文件**（pdf / Word / Excel / zip …）。

        ⚠️ 这里有个**反直觉的事实**（2026-10-02 真机实测，别再照着接口名猜）：
        **4.1.10.27 这个 hook 发普通文件走的就是 `/SendImgMsg`**，端点名字里那个
        "Img" 是历史遗留（上游 update.log 20260727：「发送图片等接口**统一改为**
        发送文件类接口」，我们实测 xlsx / zip 都发出成**文件消息**）。

        实测证据：`POST /SendImgMsg {"path": "...\\xlsx"}` 之后，文件传输助手的
        `Msg_` 表新增一条 `local_type = 25769803825 = (6<<32)|49`（**文件消息**，
        不是图片的 3），XML 里 `title/totallen(与磁盘字节数一致)/fileext/attachid/
        cdnattachurl/aeskey/fileuploadtoken` 全是服务端签发的真值；调用前后
        `IsLogin:1`、crashinfo 无新转储。

        而真正名为 `/SendFileMsg` 的那个路由**不存在**（实测 HTTP 404）——它是
        「等一个带发文件接口的 hook」时留的接口形状，不是本版能用的。

        端点由 `send_file_via(cfg)` 决定：默认 `imgmsg`（本版唯一可用的那条），
        想只用那个「正经」端点就配 `agent.send_file_via: filemsg`。

        上层必须由能力闸把关（`agent_tools.send_file_on`，默认**开**），
        并且**发文件永远要用户确认**（`kind="file"` 的待确认项）。
        """
        return self._request("POST", send_file_via(cfg),
                             {"wxidorgid": wxid, "path": path})


    def call_voip(self, wxid, self_wxid=None, *, msg_type=None, body=None):
        """发起一通微信语音通话。

        契约（**仓库源码里实装的 `/CallVoip`**，2026-10-03）：

            POST /CallVoip   {"wxid": <被叫 wxid>}

        实现：hook 源码 `src/wx_send.cpp` 的 `SendVoipInvite()` +
        `src/SendTextMsg.cpp` 的路由。原理 —— 微信发起语音通话 = 发一条
        **类型 50（0x32）** 的消息，正文是那段 277 字节的邀请 XML
        （`<voipinvitemsg>`+`<voipextinfo>`+`<voiplocalinfo>`，**全是常量或 0**，
        没有任何服务端下发的一次性数据）。取证与互证见
        `_audit/通话功能-逆向进度与恢复.md` 第二十三轮。

        `self_wxid` **不再需要**（发送方由 WeChat 自己填，走的和发文本同一条
        路），保留这个参数只是为了不打断已有调用点。

        `msg_type` / `body` 是给**真机试验**用的：不给就用内置的邀请 XML。
        做成参数是因为每改一次常量都得重编 DLL + 重启微信（每次重启都要
        重新扫码），而做成参数就一个 HTTP 调用试一个取值。

        ⚠️ 即使 HTTP 成功、`ret == 0`，也只能说「**邀请已发出**」，
        **绝不许说「对方收到了」** —— 本地无法确认对方是否响铃/接听
        （和 `/SendImgMsg` 同一个道理）。
        """
        payload = {"wxid": str(wxid or "")}
        if msg_type is not None:
            payload["type"] = int(msg_type)
        if body is not None:
            payload["body"] = str(body)
        return self._request("POST", "/CallVoip", payload)

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
            st = self.db_status()
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
        # 库全打不开时**必须分清**「没登录」和「已登录但句柄表是空的」（2026-10-05 真机）：
        # 两种都查不出东西，修法却完全相反——掉登录只能人工扫码（重扫也白搭），
        # 而「IsLogin=1 + 库全打不开」是掉登录再登录之后 hook 的句柄表被重建了，
        # **重扫一次（force_rescan）就修好**。旧实现两种都报「微信没登录？请扫码登录」，
        # 把日志和排查方向一起指错：真机上用户明明已经扫码登录了，还在被叫去扫码。
        try:
            logged = int((st or {}).get("IsLogin", 0)) == 1
        except (TypeError, ValueError, AttributeError):
            logged = False
        if logged:
            return False, ("hook 在、微信也显示已登录（IsLogin=1），但数据库句柄打不开"
                           "——掉登录再登录后常见，重扫一次通常就修好")
        return False, "hook 已加载，但数据库打不开（微信没登录？请在微信里扫码登录）"

    # ---------- 自己的 wxid：接口给不出来时，从库里认 ----------

    def detect_self_wxid(self):
        """拿不到自己的 wxid 时，从 SQLite 里把「自己」认出来。

        **为什么必须补这一条**（2026-10-05 部署真机）：`get_self_wxid()` 走的是
        `/GetSelfProfile`，而这个构建里那个接口**不给 wxid**，返回里没有
        `wxid`/`userName` 任何一个键 → 拿到空串。于是别的电脑上装完就露出一串后果：
        `resolve_contacts(..., "我自己")` 认不出自己、群发时不跳过自己、
        历史里分不清「我」和「对方」；而打开包的 `config.yaml` **本来就没填 `self_wxid`**
        （只有开发机那份填了）——纯换台机器就命中，不报错、只是功能歪。

        判据（微信 4.x 真机取证）：自己的账号在 `contact` 表里是 **第一个
        `wxid_` 开头**的会话行，`filehelper` 与 `@chatroom` 排在它后面，
        `@openim`（企业微信）也带着 `wxid_`；**行序不稳定**，所以只钉这条判据、
        不依赖顺序。

        拿不到就如实返回空串（`""` 表示「这次没认出来」，调用方照旧走原有兜底），
        **绝不猜一个 id 出来**——猜错等于把别人的 wxid 当自己，比认不出更糟。

        ⚠️ **这条判据是经验（行序），不是证明**：调用方（`bot` / `verify_real`）
        必须走 `resolve_self_wxid()`，它会拿本机**账号目录**核实这个值，
        对不上就否掉。2026-10-06 换台电脑真机上它就是认错了人，而且不报错，
        后果是 bot 自己回答自己刚发出的回复（见 `resolve_self_wxid` 的注释）。
        """
        try:
            rows = self.query_sql(
                "contact.db",
                "SELECT username FROM contact "
                "WHERE username LIKE 'wxid\\_%' ESCAPE '\\' "
                "AND username NOT LIKE '%@%' LIMIT 1")
        except Exception:
            return ""
        for r in self._rows(rows):
            v = r.get("username") if isinstance(r, dict) else (
                r[0] if isinstance(r, (list, tuple)) and r else None)
            v = str(v or "").strip()
            if v and v.startswith("wxid_") and "@" not in v:
                return v
        return ""

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

    def poll_messages(self, since=0, seen=None, limit=None, seen_max=2000):
        """查 create_time >= since 的文本消息。

        返回 (Msg 列表, 新游标, seen)。调用方把这三样存下来，下次原样传回。

        用 >= 而不是 >，再配合 seen 去重：边界上同一秒到达的消息不会漏，
        重复查到的也不会二次处理。seen 是 dict（当有序集合用），超过 seen_max 淘汰最早的。

        `limit=None` = **别在这里定这个数**，用 `live_history` 的默认
        （`POLL_ROWS_PER_SHARD`：每分片每轮的行数，2026-10-05 从 200 降到 20 ——
        「每轮满页」在追赶积压时会把 hook 压垮，理由写在那条常量的注释里）。
        以前这里写死 200，等于**第二个所有者**，改一处不生效。
        """
        import live_history
        if seen is None:
            seen = {}
        if limit is None:
            raw, new_cursor = live_history.new_messages(self, since)
        else:
            raw, new_cursor = live_history.new_messages(self, since, limit)
        out = []
        for m in raw:
            key = self._key(m)
            if key in seen:
                continue
            seen[key] = None
            out.append(Msg(m["talker"], m["content"], m["is_self"], m["_ts"],
                           m.get("local_type", 1), m.get("local_id", "")))
        while len(seen) > seen_max:
            seen.pop(next(iter(seen)))
        return out, new_cursor, seen
