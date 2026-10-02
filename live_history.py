"""直接查微信本地数据库读取历史消息（无需导出）。

支持两套库结构，自动识别：

  微信 3.9.x（wcferry 注入）：
    - 库：MicroMsg.db / MSG0.db、MSG1.db...
    - 联系人：Contact(UserName, NickName, Remark, Alias)
    - 消息：MSG 表，列 StrTalker / StrContent / IsSender / CreateTime

  微信 4.x（aixed/WeChat-Hook 注入）：
    - 库：contact.db / message_0.db、message_1.db...（还有 .kvdb 等旁库）
    - 联系人：contact(username, nick_name, remark, alias)
    - 消息：**每个会话一张表** Msg_<md5(username)>，列
      local_type / create_time / real_sender_id / message_content
      会话名与 rowid 的映射在 Name2Id(user_name, is_session) 里，
      real_sender_id 指向 Name2Id.rowid。

客户端只需要提供 get_dbs() / query_sql(db, sql)（鸭子类型），
所以 wcferry 和 aixed_api.AixedClient 都能直接传进来。
"""
import binascii
import hashlib
import html
import re
import sys
import time

try:
    import zstandard
except ImportError:            # 没装也能跑，只是 appmsg 的 XML 解不开
    zstandard = None

# 微信 4.x 判断「这条是不是我发的」需要自己的 wxid，启动时由 bot 设置。
_SELF_WXID = ""

# 轮询分片最近一次失败：{分片名: (错误信息, 连续失败次数)}
# 存在的意义是让上层能发现「hook 查不动了」——以前这里静默 continue，
# 结果 bot 看起来只是「没新消息」，实际已经卡死好几分钟，日志里毫无痕迹。
_POLL_ERRORS = {}


def poll_errors():
    """返回当前轮询失败的分片 {分片名: (错误, 连续次数)}。空 dict 表示正常。"""
    return dict(_POLL_ERRORS)


def _note_poll_error(key, exc):
    """记一次轮询失败（同一键累计连续次数），返回新的连续次数。

    抽成函数是为了让**每一条**轮询路径都必须留痕：fts 分片和 session.db 兜底
    都是「查不动了就永远收不到消息」的地方，静默只有一种后果——
    日志看起来一切正常，实际一条消息都进不来。
    只打第 1/10/50 次，避免每 5 秒刷一行。
    """
    n = _POLL_ERRORS.get(key, ("", 0))[1] + 1
    _POLL_ERRORS[key] = (str(exc), n)
    if n in (1, 10, 50):
        print(f"[live] ⚠️ 轮询 {key} 连续失败 {n} 次：{exc}",
              file=sys.stderr, flush=True)
    return n


def set_self_wxid(wxid):
    global _SELF_WXID
    _SELF_WXID = str(wxid or "")


# fts 分片探测为空时自动重扫 hook 的最小间隔（秒），启动时由 bot 从 config 设置。
# 0 = 关闭自动重扫（session.db 兜底仍在，只是不主动去修 fts）。
# 默认保守到 5 分钟：GetAllDBName 是 700MB 进程里的全内存扫描，调勤了会把微信拖死。
_AUTO_RESCAN_INTERVAL = 300.0


def set_rescan_interval(seconds):
    global _AUTO_RESCAN_INTERVAL
    try:
        v = float(seconds)
    except (TypeError, ValueError):
        return
    _AUTO_RESCAN_INTERVAL = 0.0 if v <= 0 else v


def _q(s):
    """SQL 字符串转义（SQLite 里单引号转成两个单引号）。"""
    return str(s).replace("'", "''")


def _like(col, value):
    """生成一个**转义正确**的 LIKE 条件：`<col> LIKE '%值%' ESCAPE '\\'`。

    为什么不能只过一遍 _q：`%` / `_` 在 LIKE 里是通配符，用户搜「50%」、
    或者名字里带下划线的联系人（A_B）会命中一堆无关的人/消息
    （`_` 匹配任意单字符，`A_B` 连 `AXB` 都能命中）。所以值里的 `\\`、`%`、`_`
    都要转义，并配一个 ESCAPE 子句——**SQLite 没有默认转义符**，转义符必须自己声明，
    否则转义写进去也只是字面反斜杠。

    把整条条件（含 ESCAPE）放在这里生成，调用点就不会漏写 ESCAPE；
    `=` 精确匹配的地方照旧用 _q，那边的语义一个字都没动。
    """
    s = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{col} LIKE '%{_q(s)}%' ESCAPE '\\'"


def _cached_positive(client, attr, builder, ttl=90):
    """**只缓存成功的结果**，且带 TTL 会定期重探。

    两个都必须：
    - 只缓存成功：探测会因 hook 抖动失败，把 False 永久记住会让整个进程走错分支。
    - 带 TTL：hook 能发现的库是**会轮换的**（实测 message_fts.db 和 message_0.db
      交替可用），所以得定期重探才能自动切到当前可用的那条路。
    """
    hit = getattr(client, attr, None)
    if hit is not None:
        ts, val = hit
        if val and time.time() - ts < ttl:
            return val
    val = builder()
    try:
        setattr(client, attr, (time.time(), val))
    except Exception:
        pass
    return val


def _cached(client, attr, builder, ttl=None):
    """客户端级缓存（正负都缓存），用于「查到了就是稳定的」那类数据。

    ttl=None 表示进程内永久。注意：**探测类的东西别用这个**，用 _cached_positive。
    """
    hit = getattr(client, attr, None)
    if hit is not None and (ttl is None or time.time() - hit[0] < ttl):
        return hit[1]
    val = builder()
    try:
        setattr(client, attr, (time.time(), val))
    except Exception:
        pass
    return val


def _cached_filled(client, attr, builder, ttl=None):
    """缓存「确实查到了」的结果——**空结果和失败一律不缓存**。

    必须和 _cached 分开：_cached 连空结果也记，对这类数据是致命的。
    hook 偶尔会查不动，一旦失败把空值缓存下来，表现是：
      * 分片表列表为空 -> 轮询循环一次都不执行 -> 游标不动、无报错、无日志，
        bot 静默收不到消息（要卡满 TTL 才自愈）
      * 会话名映射为空 -> 所有 talker 变成 session_N -> 消息匹配不上目标聊天
      * 自己的 rowid 为 None -> 认不出自己发的消息
    """
    hit = getattr(client, attr, None)
    if hit is not None and hit[1] and (ttl is None or time.time() - hit[0] < ttl):
        return hit[1]
    val = builder()
    if val:
        try:
            setattr(client, attr, (time.time(), val))
        except Exception:
            pass
    return val


def _probe(client, db, sql="SELECT 1 FROM sqlite_master LIMIT 1"):
    """能不能真的查这个库。"""
    try:
        _query(client, db, sql)
        return True
    except Exception:
        return False


# 强制重扫的最小间隔（秒）。调一次代价可控（实测微信 CPU 只涨几秒），
# 但调太勤就是把微信拖死的元凶，所以必须限流。
RESCAN_MIN_INTERVAL = 45.0


def force_rescan(client, min_interval=None):
    """强制 hook 重新发现数据库句柄。返回这次是否真的触发了重扫。

    原理：`GetAllDBName` 的实现会先 `m_dbs.clear()` 再重新扫描，是唯一能
    触发重扫的入口。它自己**常常返回 500**（扫描中途崩），但**副作用是句柄表被重建**
    ——实测调用后 message_fts.db / message_0.db 都从 500 变回 200。

    所以这里「调用并忽略结果」，只为拿副作用。

    min_interval 是本次调用要求的最小间隔，缺省用 RESCAN_MIN_INTERVAL。
    时间戳 `client._lh_last_rescan` 是**所有调用方共享**的，所以 GetAllDBName 的
    总频率由在用的最紧那个间隔兜住——自动重扫用了较大的间隔，也不会把
    _probe_heal 那条路放得更松。
    """
    if min_interval is None:
        min_interval = RESCAN_MIN_INTERVAL
    now = time.time()
    if now - getattr(client, "_lh_last_rescan", 0.0) < min_interval:
        return False
    try:
        client._lh_last_rescan = now
    except Exception:
        pass
    try:
        client.get_dbs()
    except Exception:
        pass  # 返回 500 是常态，忽略
    return True


def _probe_heal(client, db, sql="SELECT 1 FROM sqlite_master LIMIT 1"):
    """探测；失败时强制重扫一次再试。

    hook 能发现的库会**漂移**（message_fts.db 和 message_0.db 交替掉线），
    重扫能把它们找回来——这是让整套东西能持续跑下去的关键。
    """
    if _probe(client, db, sql):
        return True
    if force_rescan(client):
        return _probe(client, db, sql)
    return False


def _query(client, db, sql):
    """执行 SQL，兼容不同后端的方法名。"""
    fn = getattr(client, "query_sql", None) or getattr(client, "exec_db_query", None)
    if fn is None:
        raise RuntimeError("当前后端没有 query_sql 接口")
    return fn(db, sql) or []


def _pick(row, key, idx):
    """兼容返回 dict 或 list/tuple 两种行格式。"""
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[idx]
    except (IndexError, TypeError):
        return None


def _fmt_time(ts):
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return str(ts)
    # 库里**真有** create_time = 0 的脏行（2026-10-01 实测：张三那条会话的
    # Msg_ 表里就有）。照原样格式化会输出 `1970-01-01 08:00:00`，模型会把它
    # 当成「最早的一条消息」——问「最近 N 天说了什么」就可能以一条 1970 年的
    # 记录开头。**时间不知道就说不知道，不许编一个时间出来。**
    if v <= 0:
        return "时间未知"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
    except Exception:
        return str(ts)


# local_type 的**低 32 位**是消息大类，高位是子类型（appmsg 用）。
# 实测：图片可能是 3，也可能是 appmsg `(子类型<<32)|49`（子类型 5 就是图片）。
_MSG_KIND = {
    1: "文本", 3: "图片", 34: "语音", 37: "好友申请", 42: "名片",
    43: "视频", 47: "表情", 48: "位置", 49: "链接/文件", 50: "通话",
    10000: "系统消息",
}


def _type_label(local_type):
    lt = _as_int(local_type)
    low = lt & 0xFFFFFFFF
    if low == 49:
        # appmsg 的子类型。5 实测是图片（缓存里那批），57 实测带 <title>
        # 是「引用」——别把没见过的一律叫"链接"，会误导模型。
        return _APPMSG_KIND.get(lt >> 32, "消息")
    return _MSG_KIND.get(low, f"类型{low}")


_APPMSG_KIND = {
    5: "图片", 6: "文件", 8: "表情", 19: "聊天记录", 33: "小程序", 36: "小程序",
    44: "视频", 51: "视频号", 57: "引用", 62: "视频号", 87: "群公告",
}


def _render_nontext(local_type, summary=""):
    """把非文本消息渲染成一行文本，**让下游（LLM）能看见**。

    以前这类消息在轮询里被直接 `continue` 丢掉，表现是
    「用户发了东西、bot 完全没反应」——实测用户发一条 appmsg（引用/链接）
    过去，bot 从头到尾看不见，还以为是自己没回。

    summary 用 fts 库的 acontent：微信自己给 appmsg 生成的摘要
    （文件名、链接标题之类）能直接用；表路径拿不到摘要就只给类型标签。
    **绝不能**把 message_content 原样塞进来——非文本那列是 zstd 压缩的
    十六进制，塞进去就是一坨乱码污染上下文。
    """
    label = _type_label(local_type)
    s = str(summary or "").strip()
    return f"[{label}] {s[:200]}" if s else f"[{label}]"


# 「能再发一次的媒体」：素材暂存区（assets.py）只收这三类。
# 依据是 hook 的能力边界——`ForwardXMLMsg` 只认图片/视频/动图（CLAUDE.md 记着）。
# 其余非文本（语音/文件/位置/名片/链接）**转发不了**，收进暂存区就是骗用户：
# 到时候工具只能回一句「已发」而对方什么都收不到。
_MEDIA_KIND = {3: "图片", 43: "视频", 47: "表情"}
_MEDIA_APPMSG = {5: "图片", 8: "表情", 44: "视频"}


def media_kind(local_type):
    """这条消息是不是「能再发出去的媒体」，是就返回类型名，否则返回 ""。

    4.x 里图片/表情有两种存法：直接的 local_type，以及 appmsg
    （`local_type = (子类型<<32)|49`，实测 5=图片、8=表情、44=视频）。判据和
    `_type_label` 一致，**只多一个「能不能转发」的取舍**，不要在这里塞别的语义。
    """
    lt = _as_int(local_type)
    if (lt & 0xFFFFFFFF) == 49:
        return _MEDIA_APPMSG.get(lt >> 32, "")
    return _MEDIA_KIND.get(lt, "")


def _xml_field(xml, tag):
    """从 XML 里抠一个标签的文本。微信的消息 XML 结构简单，正则够用。"""
    m = re.search(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", xml or "", re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def decode_msg_content(mc):
    """Msg_ 表的 message_content：hex 字符串 -> 明文（zstd 压缩的要先解）。"""
    if not mc:
        return ""
    try:
        raw = binascii.unhexlify(str(mc))
    except (binascii.Error, ValueError):
        return str(mc)
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        if zstandard is None:
            return ""
        try:
            raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=4 << 20)
        except Exception:
            return ""
    return raw.decode("utf-8", "ignore")


def render_appmsg(xml, summary=""):
    """把 appmsg 的 XML 渲染成一行**人能读懂**的文本。

    重点是**引用消息**（type 57）：用户的字在 `<title>`，而被引用的原文在
    `<refermsg><content>`。fts 库给的摘要只有 `<title>`——实测模型因此只看到
    用户打的四个字，完全不知道他在回哪条消息，回复就答非所问。
    所以这里把被引用的原文也带上。
    """
    if not xml:
        return _render_nontext(49, summary)

    title = _xml_field(xml, "title")
    ref_block = _xml_field(xml, "refermsg")
    ref_content = _xml_field(ref_block, "content") if ref_block else ""
    ref_name = _xml_field(ref_block, "displayname") if ref_block else ""

    # 49 是 appmsg 的**大类**（低位），这里拿不到子类型（子类型在 local_type 高 32 位，
    # 而本函数手里只有 XML），所以按 _type_label 的约定传 49：高位 0 不在 _APPMSG_KIND
    # 里，落到默认的「消息」。以前这里写 `4 | (49 << 0)` = 53，低位变成 53，
    # 于是每条非引用的 appmsg 都显示成「类型53」——那是内部数字，只会误导模型。
    label = "引用" if ref_block else _type_label(49)
    parts = []
    if ref_content:
        who = f"{ref_name}：" if ref_name else ""
        parts.append(f"[引用 {who}{ref_content[:160]}]")
    elif title:
        parts.append(f"[{label}]")

    said = title or _xml_field(xml, "content") or _xml_field(xml, "des") or summary
    if said:
        # 只有 <des>（或只有 <content>）的 appmsg：上面两个分支都进不去，这里要是
        # 光把内容塞进去，就丢了「[标签] 内容」的统一样式，模型看到的是一行没头没尾的
        # 文本。des **必须能单独成词**，而且得带上标签。
        if not parts:
            parts.append(f"[{label}]")
        parts.append(str(said)[:200])
    return " ".join(parts) if parts else _render_nontext(49, summary)


def _fetch_message_xml(client, talker, local_id):
    """按 local_id 回 Msg_ 表取一条消息的明文 XML。取不到返回 ""。"""
    try:
        lid = int(local_id)
    except (TypeError, ValueError):
        return ""
    table = _v4_table_for(talker)
    for db in _v4_msg_dbs(client):
        try:
            rows = _query(client, db,
                          f"SELECT message_content FROM {table} "
                          f"WHERE local_id = {lid} LIMIT 1")
        except Exception:
            continue
        for r in rows:
            return decode_msg_content(_pick(r, "message_content", 0))
        break
    return ""


def is_wechat4(client):
    """判断是 4.x 还是 3.9.x。只探 contact.db，且**只缓存成功**，避免抖动被永久记住。"""
    return _cached_positive(
        client, "_lh_is4",
        lambda: _probe_heal(client, "contact.db", "SELECT 1 FROM contact LIMIT 1"))


# ============================================================
#  微信 3.9.x
# ============================================================

def _v3_msg_dbs(client):
    """3.9.x 的消息分片库（MSG0.db ...）。探测式，不用 GetAllDBName。"""
    return _cached_positive(
        client, "_lh_v3_msgdbs",
        lambda: [f"MSG{i}.db" for i in range(8) if _probe_heal(client, f"MSG{i}.db")])


def _v3_contact_rows(client, where="", limit=20000):
    sql = "SELECT UserName, NickName, Remark, Alias FROM Contact "
    if where:
        sql += f"WHERE {where} "
    sql += f"LIMIT {int(limit)}"
    out = []
    for r in _query(client, "MicroMsg.db", sql):
        out.append({
            "wxid": _pick(r, "UserName", 0),
            "name": _pick(r, "NickName", 1),
            "remark": _pick(r, "Remark", 2),
            "alias": _pick(r, "Alias", 3),
        })
    return out


def _v3_query_history(client, talker, limit=50, keyword=None, since=None, until=None):
    rows = []
    t = _q(talker)
    for db in _v3_msg_dbs(client):
        sql = (
            "SELECT StrTalker, StrContent, IsSender, CreateTime FROM MSG "
            f"WHERE StrTalker = '{t}' AND Type = 1"
        )
        if keyword:
            sql += f" AND {_like('StrContent', keyword)}"
        # since / until = 只看这两个时刻之间（含端点）的消息，None = 那一侧不限。
        # 过滤写在排序前面，行集先被缩小，所以便宜。
        # ⚠️ **只给 since 是翻不到更早的**：它永远锚在「现在」，返回的永远是
        # 最近 limit 条；要往更早看必须同时给 until（`read_history` 的往更早一批
        # 就是这么做的）。2026-10-01 实测踩过：只给 days，days=10 和 days=30
        # 返回的完全是同一批。
        if since:
            sql += f" AND CreateTime >= {int(since)}"
        if until:
            sql += f" AND CreateTime <= {int(until)}"
        sql += f" ORDER BY CreateTime DESC LIMIT {int(limit)}"
        try:
            for r in _query(client, db, sql):
                rows.append({
                    "talker": _pick(r, "StrTalker", 0),
                    "content": _pick(r, "StrContent", 1),
                    "is_self": int(_pick(r, "IsSender", 2) or 0),
                    "time": _fmt_time(_pick(r, "CreateTime", 3)),
                    "_ts": int(_pick(r, "CreateTime", 3) or 0),
                    # 这条 SQL 的 WHERE 里已经写死 `Type = 1`，所以返回的**全是文本**。
                    # 显式给 1，好让 v3/v4 两条路的返回形状一致（下游按 local_type
                    # 挑文本时不必再分后端）。
                    "local_type": 1,
                })
        except Exception:
            continue
    rows.sort(key=lambda m: m["_ts"])
    return rows[-limit:]


def _v3_search(client, keyword, limit=30):
    rows = []
    for db in _v3_msg_dbs(client):
        sql = (
            "SELECT StrTalker, StrContent, IsSender, CreateTime FROM MSG "
            f"WHERE Type = 1 AND {_like('StrContent', keyword)} "
            f"ORDER BY CreateTime DESC LIMIT {int(limit)}"
        )
        try:
            for r in _query(client, db, sql):
                rows.append({
                    "talker": _pick(r, "StrTalker", 0),
                    "content": _pick(r, "StrContent", 1),
                    "is_self": int(_pick(r, "IsSender", 2) or 0),
                    "time": _fmt_time(_pick(r, "CreateTime", 3)),
                    "_ts": int(_pick(r, "CreateTime", 3) or 0),
                })
        except Exception:
            continue
    rows.sort(key=lambda m: m["_ts"])
    return rows[-limit:]


# ============================================================
#  微信 4.x
# ============================================================

def _v4_msg_dbs(client, max_probe=8):
    """4.x 的消息分片库（message_0.db ...）。

    探测式而不是取 GetAllDBName——那个接口每次调用都会触发全内存扫描。
    探到的结果缓存起来，只在第一次探测。
    """
    def build():
        return [f"message_{i}.db" for i in range(max_probe)
                if _probe_heal(client, f"message_{i}.db")]

    return _cached_positive(client, "_lh_v4_msgdbs", build)


def _v4_table_for(talker):
    """会话名 -> 消息表名：Msg_<md5(用户名)>"""
    return "Msg_" + hashlib.md5(str(talker).encode("utf-8")).hexdigest()


def _v4_self_rowid(client, db):
    """该分片里自己的 Name2Id.rowid（Name2Id 每个分片各有一份，rowid 不通用）。"""
    if not _SELF_WXID:
        return None
    try:
        rows = _query(client, db, "SELECT rowid FROM Name2Id "
                      f"WHERE user_name = '{_q(_SELF_WXID)}' LIMIT 1")
    except Exception:
        return None
    for r in rows:
        v = _pick(r, "rowid", 0)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


# message_N.db 分片 Name2Id 的缓存寿命（秒）。rowid -> wxid 一旦建立就稳定，
# 但 hook 的库句柄会轮换、库也可能被重建，所以给个上限，不许永久生效。
_N2ID_TTL = 600.0

# 「这一批 real_sender_id 里有解不出来的」上次打印时间。{(库名, 缺的 id): 时间戳}
# 为什么不做成 _POLL_ERRORS：那是「hook 查不动了」的心跳信号，health._healthy()
# 见到任何一条就把 bot 判成不健康、还会发告警。而「某个 id 在 Name2Id 里查不到」
# 是数据层面的缺失——hook 好得很，只是这几行拿不到发言人（上层退编号即可）。
# 把它塞进心跳等于让 bot 永远显示不健康、还发没用的告警，真故障反而被淹掉。
# 所以这类缺失只打 stderr（bot.log 是后台运行时唯一的信息来源），按时间限流。
_N2ID_MISS_NOTE = {}
_N2ID_MISS_INTERVAL = 300.0


def _note_n2id_miss(db, missing):
    """name2id 查得动、但里面没有这几个 id：留一行 stderr（限流），不猜名字。"""
    key = (db, tuple(missing[:5]))
    now = time.time()
    if now - _N2ID_MISS_NOTE.get(key, 0.0) < _N2ID_MISS_INTERVAL:
        return
    if len(_N2ID_MISS_NOTE) > 200:      # 长跑进程里不许无界长（限流表本身就是个小记忆）
        _N2ID_MISS_NOTE.clear()
    _N2ID_MISS_NOTE[key] = now
    print(f"[live] ⚠️ {db} 的 Name2Id 里查不到这些 real_sender_id：{missing[:5]}"
          f"（这些行拿不到发言人，上层会退回编号；不编名字）",
          file=sys.stderr, flush=True)


def _v4_shard_senders(client, db, ids):
    """该分片 Name2Id 里这几个 rowid 对应的 wxid：{rowid: user_name}。

    **每个 message_N.db 各有一份 Name2Id，rowid 不通用**（所以入参带 db，
    写法同 _v4_self_rowid）；**绝不能用 fts 库的 Name2Id 解这里的 id**——
    那是另一套 id 空间，混用会得到张冠李戴的名字，比拿不到更坏：
    群聊会照着错的那个人接话。

    只查真正要用的那几个 rowid（`WHERE rowid IN (...)` 走主键，选择性过滤），
    不做任何排序。解不出来的如实缺席，**不猜**。
    """
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    box = getattr(client, "_lh_n2id", None)
    if not (isinstance(box, tuple) and len(box) == 2 and isinstance(box[1], dict)
            and time.time() - box[0] < _N2ID_TTL):
        box = (time.time(), {})
    per = box[1].setdefault(db, {})
    need = [i for i in ids if i not in per]
    key = f"{db} Name2Id"
    if need:
        sql = ("SELECT rowid, user_name FROM Name2Id "
               f"WHERE rowid IN ({','.join(str(i) for i in need)})")
        try:
            found = _query(client, db, sql)
        except Exception as e:
            # 查不动 = hook 层面的故障，必须进心跳（同 fts 分片那条路）：
            # 静默的后果是「群里所有发言人又塌回未知」，而日志里一个字都没有，
            # 看起来只是「没人说话」。下一批查得动就会自己清掉。
            _note_poll_error(key, e)
            return {i: per[i] for i in ids if i in per}
        finally:
            try:
                client._lh_n2id = box
            except Exception:
                pass
        # 查得动就把这条故障清掉（key 的含义是「这个分片的 Name2Id 现在查不动」）。
        _POLL_ERRORS.pop(key, None)
        got = {}
        for r in found:
            rid = _as_int(_pick(r, "rowid", 0))
            nm = str(_pick(r, "user_name", 1) or "").strip()
            if rid and nm:
                got[rid] = nm
        per.update(got)              # 只记真的解出来的，空结果不记
        miss = [i for i in need if i not in got]
        if miss:
            # 查得动、却没有这几个人 = 解析不出。可能是这个 id 不属于本分片，
            # 也可能 Name2Id 里就没有它。**不编名字**，只如实留痕（见上面的注释）。
            _note_n2id_miss(db, miss)
    return {i: per[i] for i in ids if i in per}


def _v4_fill_senders(client, db, sheet):
    """把一批行的 real_sender_id 解成说话人，写进 sender / sender_name。

    字段约定（CLAUDE.md「渲染谁说的」），**上层必须照这个用**：
      * `sender` 只放 **wxid（原始 id）**，给上层拿去查联系人表换显示名。
        它**永远不是显示名**，任何调用方都不许把它直接渲染进给模型看的文本
        ——模型会照抄一串 wxid 回来（2026-10-01 那个坑）。
      * `sender_name` 只放**真显示名**。这条路上拿不到微信自己算好的显示名
        （那是 fts / SessionTable 才有的东西），所以**一律留空**，让上层退回
        「sender 查联系人表 → 还查不到就按顺序编号」。
        **绝不把 wxid 填进 sender_name**：那是把 id 当名字用，等于骗模型。
    """
    if not sheet:
        return
    # real_sender_id <= 0 视为「表里就没写谁发的」：没有可解析的东西，也不算
    # 解析失败——**不能**为它报警，否则每轮都刷一条，把真正的失败淹掉。
    # 这些行 sender 留空，上层照旧退编号。
    ids = {sid for _d, sid in sheet if sid > 0}
    if not ids:
        return
    names = _v4_shard_senders(client, db, ids)
    for d, sid in sheet:
        d["sender"] = str(names.get(sid) or "")


def _v4_tables(client, db):
    try:
        rows = _query(client, db, "SELECT name FROM sqlite_master "
                      "WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'")
    except Exception:
        return []
    return [str(_pick(r, "name", 0)) for r in rows if _pick(r, "name", 0)]


def _v4_contact_rows(client, where="", limit=20000):
    sql = "SELECT username, nick_name, remark, alias FROM contact "
    if where:
        sql += f"WHERE {where} "
    sql += f"LIMIT {int(limit)}"
    out = []
    for r in _query(client, "contact.db", sql):
        out.append({
            "wxid": _pick(r, "username", 0),
            "name": _pick(r, "nick_name", 1),
            "remark": _pick(r, "remark", 2),
            "alias": _pick(r, "alias", 3),
        })
    return out


def _v4_fts_session_id(client, talker):
    """会话名 -> 这个 fts 库里的 session_id。"""
    try:
        rows = _query(client, "message_fts.db",
                      f"SELECT rowid FROM Name2Id WHERE username = '{_q(talker)}' LIMIT 1")
    except Exception:
        return None
    for r in rows:
        return _as_int(_pick(r, "rowid", 0))
    return None


def _v4_history_from_fts(client, talker, limit=50, keyword=None, since=None,
                         until=None):
    """从全文索引取某会话的历史。

    按 session_id 过滤是**便宜**的：过滤先把行集缩小到这个会话自己的消息，
    排序只发生在小集合上（实测 0.14 秒）。真正贵的是不带选择性过滤、
    直接 `WHERE local_type=1 ORDER BY create_time` —— 那要排全表。

    这条路径不依赖 message_0.db（实测它常常解析不出句柄）。

    ⚠️ **`first_hit=True`（只查命中的那一个分片）是核对过的**，别再怀疑它：
    2026-10-01 真机实测，**一个会话的消息只会落在某一个分片里**——
    张三（session_id=2）的 6486 行全在 `message_fts_v4_1`，
    李四（session_id=326）的 5997 行全在 `message_fts_v4_0`，其余分片都是 0 行。
    而且这条路的返回确实是「该会话最新的 limit 条」（同一次实测：limit=30 拿到
    30 条、limit=50 拿到 50 条），**它没有偷偷截断**。以前怀疑过这里，
    真凶其实是「窗口按条数、不按时间」+ 上层把窗口跨度说成了用户问的时间范围。
    """
    if not _uses_fts(client):
        return []
    sid = _v4_fts_session_id(client, talker)
    if sid is None:
        return []
    where = f"session_id = {sid}"
    # since / until = 只看这两个时刻之间（含端点）的消息，None = 那一侧不限。
    # 会话过滤在前，所以加这两条不改变「快查询」这个性质（实测 0.2~0.3 秒）。
    if since:
        where += f" AND create_time >= {int(since)}"
    if until:
        where += f" AND create_time <= {int(until)}"
    if keyword:
        # 关键词检索只在文本里找——MATCH 打在非文本的摘要上没意义
        where += f" AND local_type = 1 AND acontent MATCH '{_q(keyword)}'"
    self_id = _v4_fts_self_id(client)
    return _v4_fts_rows(client, where, limit, {sid: talker}, self_id, first_hit=True)


def _v4_query_history(client, talker, limit=50, keyword=None, since=None,
                      until=None):
    hits = _v4_history_from_fts(client, talker, limit, keyword, since, until)
    if hits:
        return hits
    return _v4_history_from_tables(client, talker, limit, keyword, since, until)


def _v4_history_from_tables(client, talker, limit=50, keyword=None, since=None,
                            until=None):
    """查该会话的 Msg_ 表。

    按 **local_id 倒序**（它是这张表的主键）——走 PK 索引，很便宜。
    别按 create_time 排：那列没索引，一条查询能到 1 秒以上（实测）。
    local_id 是自增的，倒序就是最近的在前。

    发言人：这张表里**只有一个数字 real_sender_id**，要用**本分片自己的**
    Name2Id 解成 wxid 才有意义（fts 库那份 id 完全不是一套，不能混）。
    以前这里一个字都不填，于是这条路上所有非自己发的消息在上层全塌成
    「发言人未知」——群聊里「谁在跟谁说话」的判据就没了，所以补上
    `sender`（wxid，给上层查联系人表）/ `sender_name`（真显示名，拿不到就留空）。
    字段语义见 _v4_fill_senders 的注释。
    """
    table = _v4_table_for(talker)
    rows = []
    for db in _v4_msg_dbs(client):
        self_id = _v4_self_rowid(client, db)
        sql = (
            f"SELECT local_id, local_type, real_sender_id, create_time, message_content "
            f"FROM {table}"
        )
        # 关键词检索只在文本里找（非文本那列是压缩十六进制，LIKE 没意义）
        conds = []
        if keyword:
            conds.append(f"local_type = 1 AND {_like('message_content', keyword)}")
        # since 过滤的不是索引列（create_time 在这张表里没索引），但过滤写进
        # WHERE 之后 SQLite 可以沿 local_id 倒序边走边筛、凑够 limit 条就停；
        # 最坏也就是把这张**会话自己的**表扫一遍（实测该表 COUNT/MIN/MAX
        # 一次全扫 0.04 秒）。不违反 CLAUDE.md 铁律第 2 条——那条禁的是
        # 「不带选择性过滤还排序」，这里过滤和排序都在会话表内。
        if since:
            conds.append(f"create_time >= {int(since)}")
        # until = 只看这个时刻（含）以前的。**往更早翻页就靠它**：只给 since 的话
        # 锚点永远是「现在」，返回的永远是最近 limit 条（实测 days=10 与 days=30
        # 拿到的是同一批）。
        if until:
            conds.append(f"create_time <= {int(until)}")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" ORDER BY local_id DESC LIMIT {int(limit)}"
        try:
            found = _query(client, db, sql)
        except Exception:
            continue  # 这个分片里没有该会话的表
        sheet = []          # [(行, real_sender_id)]：攒齐本分片这一批再解一次 Name2Id
        for r in found:
            sid_i = _as_int(_pick(r, "real_sender_id", 2))
            lt = _as_int(_pick(r, "local_type", 1))
            lid = _pick(r, "local_id", 0)
            ct = _pick(r, "create_time", 3)
            if lt == 1:
                content = str(_pick(r, "message_content", 4) or "")
            elif (lt & 0xFFFFFFFF) == 49:
                # appmsg：这里有原始 message_content，直接解出来解析
                # （引用消息的被引用原文在 <refermsg> 里）
                content = render_appmsg(decode_msg_content(_pick(r, "message_content", 4)))
            else:
                # 非文本不再丢弃，渲染成标签；图片顺带把本地已解码缩略图路径带上
                content = _render_nontext(lt)
                if lt == 3:
                    # **带上 local_id**：模型据此能直接调 read_image(contact, local_id)
                    # 去看图；不带的话它只知道「有张图」，得先 find_images 再 read_image，
                    # 白多一次查库（每次查库都是压在 hook 上的真实开销）。
                    content += f"（local_id={lid}"
                    try:
                        import image_cache
                        p = image_cache.find(table[4:], lid, ct)
                        if p:
                            content += f"；本地已解码缩略图：{p}）"
                        else:
                            # **自己发出去的图常常没有明文缩略图**（微信多数只留加密原图
                            # `Bubble/<md5>_b.dat`）。注意：**不是绝对没有**——实测
                            # `md5("filehelper")` 那个缓存目录下就有一张自己发的图的
                            # Thumb（见 CLAUDE.md 的 image_cache 段）。所以这里只是
                            # 「这次没找到」，别写成「自己发的图一定没有」。
                            content += "；微信没留可解码缩略图，看不了内容）"
                    except Exception:
                        content += "）"
            d = {
                "talker": talker,
                "local_id": str(lid) if lid is not None else "",
                "local_type": lt,
                "content": content,
                "is_self": 1 if (self_id is not None and sid_i == self_id) else 0,
                "time": _fmt_time(ct),
                "_ts": _as_int(ct),
                # 新加的字段（老调用方读 content/is_self/time/_ts 的行为一个字没变）。
                # sender 只放 wxid，**不是显示名**；sender_name 拿不到就留空。
                "sender": "",
                "sender_name": "",
            }
            rows.append(d)
            sheet.append((d, sid_i))
        _v4_fill_senders(client, db, sheet)
    rows.sort(key=lambda m: m["_ts"])
    return rows[-limit:]


def voice_info(client, talker, local_id):
    """读某条**语音**消息，返回它自带的解密信息（`voice_msg.parse_voicemsg` 的结果）。

    为什么必须放这里：`live_history` 是唯一允许读微信库的地方（CLAUDE.md 铁律）。
    为什么不能靠 fts：**语音和图片一样不进 fts**（实测四个分片的 local_type 里
    没有 34）——那条路是结构性盲的，只能按会话表点查。

    返回 `{}`：不是语音 / 查不到 / XML 解不出来 —— **不抛异常**，
    调用方据此如实回话（「收到了语音但拿不到音频」），不许假装听懂。

    仅 v4（微信 4.x）。v3（wcferry 3.9.x）的语音是另一个布局，本函数不覆盖。
    """
    import voice_msg
    lid = _as_int(local_id)
    if not talker or lid <= 0:
        return {}
    tbl = _v4_table_for(talker)
    for db in _v4_msg_dbs(client):
        try:
            rows = _query(client, db,
                          f"SELECT local_type, message_content FROM {tbl} "
                          f"WHERE local_id = {lid}")
        except Exception:
            continue
        for r in rows:
            if _as_int(_pick(r, "local_type", 0)) != 34:
                return {}
            xml = decode_msg_content(_pick(r, "message_content", 1))
            info = voice_msg.parse_voicemsg(xml)
            if info:
                info["talker"] = talker
                info["local_id"] = str(lid)
                info["xml"] = xml[:4000]
            return info
    return {}


def v4_images(client, talker, limit=30):
    """该会话的图片消息，每条尽量带上本地已解码缩略图的路径。

    **为什么不能只按 `local_type = 3` 过滤**：4.x 里图片有两种存法——
    直接的 `local_type = 3`，以及 appmsg（`local_type = (subtype<<32)|49`，
    实测子类型 5 就是图片）。只按 3 过滤会漏掉一大半。

    所以这里以**本地缓存**为主要依据：微信会把渲染过的图片缩略图以明文缓存在

        <数据目录>\\cache\\<月>\\Message\\<md5(会话名)>\\Thumb\\
            <local_id>_<create_time>_thumb.jpg

    缓存里有 = 一定能拿到图。再补上 `local_type = 3` 的最近若干条
    （这些可能没缓存，但至少能告诉用户「有这张图，但没缓存看不到」）。

    为什么要查 `Msg_<hash>` 表而不是 fts 库：**fts 库里没有 local_id**
    （只有它自己那套 id），对不上缓存文件名。

    排序用 `ORDER BY local_id DESC`——local_id 是主键走 PK 索引；
    别用 create_time（没索引），也别用 rowid（实测会让微信崩）。
    """
    import image_cache

    table = _v4_table_for(talker)
    chat_hash = table[4:]
    idx = image_cache.cache_index(chat_hash)

    out = {}
    # 1) 缓存里时间最近的 limit 张——这些一定能看到图
    for (lid, ct), path in sorted(idx.items(), key=lambda kv: -_as_int(kv[0][1]))[:max(1, limit)]:
        out[lid] = {"talker": talker, "local_id": lid, "time": _fmt_time(ct),
                    "_ts": _as_int(ct), "is_self": None, "image": path}

    # 2) 库里 local_type=3 的最近若干条，补上没缓存的
    for db in _v4_msg_dbs(client):
        self_id = _v4_self_rowid(client, db)
        try:
            found = _query(client, db,
                           f"SELECT local_id, real_sender_id, create_time FROM {table} "
                           f"WHERE local_type = 3 ORDER BY local_id DESC LIMIT {int(limit)}")
        except Exception:
            continue  # 这个分片里没有该会话的表
        for r in found:
            lid = str(_pick(r, "local_id", 0))
            ct = _pick(r, "create_time", 2)
            sid_i = _as_int(_pick(r, "real_sender_id", 1))
            e = out.setdefault(lid, {"talker": talker, "local_id": lid,
                                     "time": _fmt_time(ct), "_ts": _as_int(ct),
                                     "is_self": None, "image": None})
            e["is_self"] = 1 if (self_id is not None and sid_i == self_id) else 0
        break

    # 3) 只从缓存来、还没标出「是不是我发的」那些，补一次按主键的批量查询
    missing = [k for k, v in out.items() if v.get("is_self") is None]
    if missing:
        ids = ",".join(missing[:200])
        for db in _v4_msg_dbs(client):
            self_id = _v4_self_rowid(client, db)
            try:
                found = _query(client, db,
                               f"SELECT local_id, real_sender_id FROM {table} "
                               f"WHERE local_id IN ({ids})")
            except Exception:
                continue
            for r in found:
                lid = str(_pick(r, "local_id", 0))
                if lid in out:
                    sid_i = _as_int(_pick(r, "real_sender_id", 1))
                    out[lid]["is_self"] = 1 if (self_id is not None and sid_i == self_id) else 0
            break

    # 返回时**优先给「能看到图的」**（有本地缓存那些），不够再用没缓存的补齐。
    # 否则会被最近那些没缓存的消息占满，一条能看的都没有（实测就是这个坑）。
    viewable = sorted((v for v in out.values() if v["image"]), key=lambda m: -m["_ts"])
    rest = sorted((v for v in out.values() if not v["image"]), key=lambda m: -m["_ts"])
    picked = viewable[:limit]
    if len(picked) < limit:
        picked += rest[:limit - len(picked)]
    picked.sort(key=lambda m: m["_ts"])
    return picked


def latest_media(client, talker, limit=3):
    """某会话最近的**可转发媒体**（图片/表情/视频），最近的在最前。

    给素材暂存区用：用户在控制会话里发了张图，bot 要立刻知道是哪条消息
    （`local_id`）、是不是他自己发的，好把那条的原始 XML 取出来留着转发。

    只查 `Msg_<hash>` 表、`ORDER BY local_id DESC LIMIT n`（主键索引，便宜），
    **不碰 fts**：图片/表情本来就不在 fts 里（见 `_v4_pickup_nontext`），走那条路
    永远查不到。取最近的一小批行再在 Python 里挑媒体，不写 `WHERE local_type IN (...)`
    ——那个条件没索引，稀疏类型会一路扫到底（CLAUDE.md 的 hook 铁律第 2 条）。

    `is_self`：和 `_v4_history_from_tables` 同源（比对本分片 Name2Id 的 rowid）。
    拿不到自己的 rowid 时为 None，调用方**必须当成"不确定"**，别当 0 用。

    取不到（3.9.x / 没有该会话的表 / 库句柄失效）返回 []，由调用方如实报错。
    仅 v4。
    """
    if not is_wechat4(client):
        return []
    table = _v4_table_for(talker)
    want = max(1, int(limit))
    # 多取几行：最近几条可能全是文本，媒体在更下面一点点。
    fetch = max(12, want * 4)
    out = []
    for db in _v4_msg_dbs(client):
        self_id = _v4_self_rowid(client, db)
        try:
            found = _query(client, db,
                           f"SELECT local_id, local_type, real_sender_id, create_time "
                           f"FROM {table} ORDER BY local_id DESC LIMIT {int(fetch)}")
        except Exception:
            continue  # 这个分片里没有该会话的表
        for r in found:
            lid = _pick(r, "local_id", 0)
            lt = _as_int(_pick(r, "local_type", 1))
            kind = media_kind(lt)
            if not kind or lid in (None, ""):
                continue
            ct = _pick(r, "create_time", 3)
            sid_i = _as_int(_pick(r, "real_sender_id", 2))
            path = None
            try:
                import image_cache
                path = image_cache.find(table[4:], lid, ct)
            except Exception:
                path = None
            out.append({
                "talker": talker,
                "local_id": str(lid),
                "local_type": lt,
                "kind": kind,
                "is_self": (None if self_id is None
                            else (1 if sid_i == self_id else 0)),
                # 明文缩略图（有就给，给不了 None）——转发才是主路径，
                # 这个只是「顺带拿到」的兜底信息，别让上层以为一定有图可发。
                "image": path,
                "time": _fmt_time(ct),
                "_ts": _as_int(ct),
            })
        break
    out.sort(key=lambda m: -_as_int(m.get("_ts")))     # 最近的在最前
    return out[:want]


# 文件消息：appmsg 子类型 6 → local_type = (6<<32)|49（实测值，和图片的 5 同理）
_V4_FILE_TYPE = (6 << 32) | 49
# 只按主键倒序取最近这么多条，再在 Python 里筛出文件。
# **不能**写 `WHERE local_type = <上面这个> ORDER BY local_id DESC LIMIT n`：
# local_type 没索引、文件又稀疏，碰上一个文件都没有的会话会一路扫到底
# —— 正是 CLAUDE.md 点名禁止的那种查询。
_V4_FILE_SCAN = 400


def v4_files(client, talker, limit=20, scan=_V4_FILE_SCAN):
    """该会话**最近收到的文件**：文件名 / 后缀 / 大小 / 谁发的。

    文件名就在 appmsg XML 的 `<title>` 里，和磁盘上 `msg/file/<月>/` 里的文件名
    **一致**（实测逐字吻合），所以拿到名字就能去本地找原文件（见 file_read.locate）。

    分两步查：先只取小列筛出文件行，再按 local_id 批量取 message_content ——
    一次把 400 条的 message_content 全拉回来太占带宽（每条约几 KB）。
    """
    table = _v4_table_for(talker)
    out = []
    for db in _v4_msg_dbs(client):
        self_id = _v4_self_rowid(client, db)
        try:
            rows = _query(client, db,
                          f"SELECT local_id, local_type, real_sender_id, create_time "
                          f"FROM {table} ORDER BY local_id DESC LIMIT {int(scan)}")
        except Exception:
            continue  # 这个分片里没有该会话的表

        meta = {}
        for r in rows:
            if _as_int(_pick(r, "local_type", 1)) != _V4_FILE_TYPE:
                continue
            lid = str(_pick(r, "local_id", 0))
            ct = _pick(r, "create_time", 3)
            meta[lid] = {
                "talker": talker, "local_id": lid,
                "time": _fmt_time(ct), "_ts": _as_int(ct),
                "is_self": 1 if (self_id is not None
                                 and _as_int(_pick(r, "real_sender_id", 2)) == self_id) else 0,
            }
        if not meta:
            break
        ids = ",".join(sorted(meta, key=lambda x: -int(x))[:max(1, limit)])
        try:
            found = _query(client, db,
                           f"SELECT local_id, message_content FROM {table} "
                           f"WHERE local_id IN ({ids})")
        except Exception:
            break
        for r in found:
            lid = str(_pick(r, "local_id", 0))
            if lid not in meta:
                continue
            xml = decode_msg_content(_pick(r, "message_content", 1))
            name = _xml_field(xml, "title")
            if not name:
                continue
            meta[lid]["name"] = name
            meta[lid]["ext"] = _xml_field(xml, "fileext").lower()
            meta[lid]["size"] = _as_int(_xml_field(xml, "totallen"))
            out.append(meta[lid])
        break
    out.sort(key=lambda m: m["_ts"])
    return out[-limit:]


# ---------- 微信 4.x 全文检索（message_fts.db） ----------
#
# 微信自己给消息建了 fts5 全文索引，用它的自研分词器 MMFtsTokenizer：
#   fts5(tokenize='MMFtsTokenizer disable_pinyin',
#        acontent, message_local_id, session_id, sender_id, create_time, local_type ...)
# 好处是正文/会话/发送者/时间都在一张表里，不用回查消息表，中文也能分词。
#
# 坑：message_fts.db 里的 Name2Id 和 message_N.db 里的**不是同一套 id**，
# session_id / sender_id 必须用 fts 库自己的 Name2Id 解。

def _uses_fts(client):
    return _cached_positive(client, "_lh_uses_fts", lambda: _probe_heal(client, "message_fts.db"))


def _v4_fts_tables(client):
    """fts 分片虚表（message_fts_v4_0 ... v4_3）。表结构基本不变，长时间缓存。"""
    def query_tables():
        rows = _query(client, "message_fts.db",
                      "SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'")
        return [str(_pick(r, "name", 0)) for r in rows
                if str(_pick(r, "name", 0)).startswith("message_fts_v4_")]

    def build():
        try:
            tabs = query_tables()
        except Exception as e:
            print(f"[live] ⚠️ 查 fts 分片表失败：{e}", file=sys.stderr, flush=True)
            return []
        if tabs:
            return tabs
        # 返回空**不等于**没有分片表：句柄失效时查询是「成功但 0 行」，连
        # sqlite_master 都列不出东西。_probe_heal 抓不到这种（它只看抛不抛异常），
        # 结果是 bot 静默收不到任何消息、游标不动、日志里毫无痕迹。所以这里主动补一刀。
        if _AUTO_RESCAN_INTERVAL <= 0:
            return []
        if force_rescan(client, min_interval=_AUTO_RESCAN_INTERVAL):
            print("[live] ⚠️ fts 分片表探测为空，已触发 hook 重扫", file=sys.stderr, flush=True)
            try:
                return query_tables()
            except Exception:
                return []
        return []
    return _cached_filled(client, "_lh_fts_tables", build, ttl=3600)


def _v4_fts_session_map(client):
    """fts 库的 Name2Id：rowid -> 会话名。（缓存 10 分钟，空结果不缓存）"""
    def build():
        out = {}
        try:
            rows = _query(client, "message_fts.db", "SELECT rowid, username FROM Name2Id")
        except Exception:
            return out
        for r in rows:
            out[_as_int(_pick(r, "rowid", 0))] = str(_pick(r, "username", 1) or "")
        return out
    return _cached_filled(client, "_lh_fts_smap", build)


def _v4_fts_self_id(client):
    """自己在这个 fts 库 id 空间里的 rowid。（缓存 10 分钟）"""
    def build():
        if not _SELF_WXID:
            return None
        try:
            rows = _query(client, "message_fts.db",
                          f"SELECT rowid FROM Name2Id WHERE username = '{_q(_SELF_WXID)}' LIMIT 1")
        except Exception:
            return None
        for r in rows:
            return _as_int(_pick(r, "rowid", 0))
        return None
    return _cached_filled(client, "_lh_fts_selfid", build)


def _v4_fts_rows(client, where, limit, smap, self_id, first_hit=False):
    """在各 fts 分片上查同一段 where。

    first_hit=True：**一个会话的消息只会落在某一个分片里**，所以逐个分片试、
    命中就停（1 次请求）。查单个会话的历史用这个。
    first_hit=False：跨分片检索（关键词搜索）用 UNION ALL 一次发出去。

    别对「全部消息」做排序——`WHERE local_type=1 ORDER BY create_time` 要排全表，
    那是把微信拖慢的根源。带 session_id 过滤就快，因为过滤先缩小了行集。
    """
    tables = _v4_fts_tables(client)
    if not tables:
        return []
    cols = "acontent, session_id, sender_id, create_time, local_type, message_local_id"

    if first_hit:
        found = []
        for t in tables:
            try:
                found = _query(client, "message_fts.db",
                               f"SELECT {cols} FROM {t} WHERE {where} "
                               f"ORDER BY create_time DESC LIMIT {int(limit)}")
            except Exception:
                continue
            if found:
                break
    else:
        sql = (" UNION ALL ".join(f"SELECT {cols} FROM {t} WHERE {where}" for t in tables)
               + f" ORDER BY create_time DESC LIMIT {int(limit)}")
        try:
            found = _query(client, "message_fts.db", sql)
        except Exception:
            return []

    # sender_id 和 session_id 在**同一个** Name2Id 里，都用 fts 库自己这套 id 解。
    # 单聊用不上（发言人就是会话对方），**群聊靠它标出「谁在说话」**。
    name_map = _v4_fts_session_map(client)
    out = []
    for r in found:
        lt = _as_int(_pick(r, "local_type", 4))
        sid = _as_int(_pick(r, "session_id", 1))
        sender = _as_int(_pick(r, "sender_id", 2))
        talker = smap.get(sid, f"session_{sid}")
        text = str(_pick(r, "acontent", 0) or "")
        # 和轮询那条路保持一致：非文本不再丢弃，渲染成可读文本。
        # （以前这里直接 continue，结果「引用」消息在历史里完全看不到）
        if lt != 1:
            if (lt & 0xFFFFFFFF) == 49:
                text = render_appmsg(
                    _fetch_message_xml(client, talker, _pick(r, "message_local_id", 5)),
                    text)
            else:
                text = _render_nontext(lt, text)
        out.append({
            "talker": talker,
            "content": text,
            "is_self": 1 if (self_id is not None and sender == self_id) else 0,
            "sender": name_map.get(sender, ""),
            "time": _fmt_time(_pick(r, "create_time", 3)),
            "_ts": _as_int(_pick(r, "create_time", 3)),
            # 新加的字段（老调用方读 content/is_self/time/_ts 的行为一个字没变）。
            # **必须带上**：fts 那条路上面已经把非文本渲染成 `[图片]` 这类标签了，
            # 而 `content` 看上去和真文本没区别——下游（比如「从历史学语气」要挑出
            # 用户自己发的**文本**）没有它就分不清「一句话」和「一张图的标签」。
            # 走表那条路（_v4_history_from_tables）早就带了这个字段。
            "local_type": lt,
        })
    out.sort(key=lambda m: m["_ts"])
    return out[-limit:]


# ---------- 中文问句的关键词抽取 ----------

# 疑问词/助词/常见动词，抽关键词时先去掉
_STOPWORDS = (
    "我们", "你们", "他们", "她们", "这个", "那个", "最近", "之前", "上次", "上个月",
    "昨天", "今天", "前天", "明天", "什么", "哪些", "哪个", "哪里", "怎么", "如何",
    "为什么", "有没有", "是不是", "关于", "有关", "记录", "消息", "内容", "事情",
    "聊天", "聊了", "聊过", "说过", "提到", "提过", "讲过", "说起", "请", "帮忙",
    "帮我", "一下", "看看", "查一下", "找一下", "谁", "和", "与", "跟", "的", "了",
    "吗", "呢", "吧", "啊", "我", "你", "他", "她", "它", "都", "还", "有", "是",
    "在", "聊", "说", "查", "找", "告诉", "问", "给", "被", "把", "就", "也", "很",
    "个", "些", "啥", "干",
)


def extract_keywords(query):
    """从中文问句里抽出可用于检索的关键词，按长度降序。

    例：「我和张三聊了什么」-> ['张三']
        「最近聊了什么」      -> []（没有可检索的词）
    """
    s = str(query)
    s = re.sub(r"[\s,，。！？!?、；;：:\"'“”‘’（）()《》【】\[\]…—\-~·]+", " ", s)
    for w in sorted(_STOPWORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    parts = [p for p in s.split() if len(p) >= 2]
    parts.sort(key=len, reverse=True)
    return parts


def _v4_fts_search(client, keyword, limit=30):
    """用微信自带的全文索引检索。"""
    terms = extract_keywords(keyword)
    if not terms:
        # 问句里没有可检索的词，退回原串，让分词器自己处理
        raw = str(keyword).strip()
        terms = [raw] if len(raw) >= 2 else []
    if not terms:
        return []

    terms = terms[:4]
    # FTS5 的多词是隐含 AND、太严，这里显式 OR；每个词加引号防语法错
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)

    smap = _v4_fts_session_map(client)
    self_id = _v4_fts_self_id(client)
    return _v4_fts_rows(client, f"acontent MATCH '{_q(match)}'", limit, smap, self_id)


def _v4_recent_talkers(client, n=10):
    """最近有消息的会话（按 SessionTable.last_timestamp 倒序）。

    session.db 的 SessionTable 只有几百行，查它很便宜——这是唯一该用来
    判断「哪些会话有新消息」的地方。
    """
    try:
        rows = _query(client, "session.db",
                      "SELECT username FROM SessionTable "
                      f"ORDER BY last_timestamp DESC LIMIT {int(n)}")
    except Exception:
        return []
    return [str(_pick(r, "username", 0)) for r in rows if _pick(r, "username", 0)]


def _v4_recent(client, limit=30):
    """最近的聊天概览。

    直接用 SessionTable——它每行带 summary（该会话最后一条消息的文本）
    和 last_msg_sender，436 行的小表，一次查询就够。
    逐个会话去 FTS 里捞要 10 次查询、4 秒多，不值。
    """
    try:
        rows = _query(client, "session.db",
                      "SELECT username, summary, last_timestamp, last_msg_sender, "
                      "last_sender_display_name "
                      f"FROM SessionTable ORDER BY last_timestamp DESC LIMIT {int(limit)}")
    except Exception:
        return []
    out = []
    for r in rows:
        content = str(_pick(r, "summary", 1) or "").strip()
        if not content:
            continue
        sender = str(_pick(r, "last_msg_sender", 3) or "")
        ts = _as_int(_pick(r, "last_timestamp", 2))
        out.append({
            "talker": str(_pick(r, "username", 0) or ""),
            "content": content,
            "is_self": 1 if (sender and _SELF_WXID and sender == _SELF_WXID) else 0,
            "sender": sender,
            # 微信自己算好的发言人显示名（群里尤其是群昵称），比自己拿 wxid 去查准
            "sender_name": str(_pick(r, "last_sender_display_name", 4) or "").strip(),
            "time": _fmt_time(ts),
            "_ts": ts,
        })
    out.sort(key=lambda m: m["_ts"])
    return out[-limit:]


def _v4_search(client, keyword, limit=30):
    if _uses_fts(client):
        return _v4_fts_search(client, keyword, limit)
    return _v4_search_by_scan(client, keyword, limit)


def recent_messages(client, limit=30):
    """最近的文本消息（跨会话）。

    4.x 走 SessionTable，**根本不需要 fts**——之前写成「fts 不可用就逐表 LIKE」
    是错的，那要扫几百张表、9 秒起步。
    """
    if is_wechat4(client):
        return _v4_recent(client, limit)
    return _v3_search(client, "", limit)


def _v4_search_by_scan(client, keyword, limit=30, max_tables=40):
    """没有 fts 时的兜底：跨会话逐表 LIKE。

    这个很贵（每个会话一张表，逐张查）。所以：
    - max_tables 压到 60（约 2 秒），**别调大**——慢查询本身就是把微信拖垮的东西；
    - 反正 hook 的自愈重扫（45 秒一次）会把 fts 找回来，这只是个临时退路。
    """
    rows = []
    k = _like("message_content", keyword) if keyword else ""
    scanned = 0
    for db in _v4_msg_dbs(client):
        self_id = _v4_self_rowid(client, db)
        for table in _v4_tables(client, db):
            if scanned >= max_tables:
                break
            scanned += 1
            cond = f"local_type = 1 AND {k}" if k else "local_type = 1"
            sql = (
                f"SELECT real_sender_id, create_time, message_content FROM {table} "
                f"WHERE {cond} ORDER BY create_time DESC LIMIT {int(limit)}"
            )
            try:
                found = _query(client, db, sql)
            except Exception:
                continue
            for r in found:
                sid_i = _as_int(_pick(r, "real_sender_id", 0))
                rows.append({
                    "talker": table,
                    "content": str(_pick(r, "message_content", 2) or ""),
                    "is_self": 1 if (self_id is not None and sid_i == self_id) else 0,
                    "time": _fmt_time(_pick(r, "create_time", 1)),
                    "_ts": _as_int(_pick(r, "create_time", 1)),
                })
    rows.sort(key=lambda m: m["_ts"])
    return rows[-limit:]


# ============================================================
#  对外接口（自动按微信版本分派）
# ============================================================

def all_contacts(client, limit=20000):
    """全部联系人 [{wxid, name, remark, alias}]。

    注意 limit 别设小：联系人总表动辄上万条，截断会导致按名字找人的功能失灵
    （踩过：limit=5000 时后面的人永远匹配不到）。
    """
    if is_wechat4(client):
        return _v4_contact_rows(client, limit=limit)
    return _v3_contact_rows(client, limit=limit)


def resolve_contact(client, name, limit=5):
    """按昵称/备注/微信号模糊匹配联系人。

    这里是 LIKE 模糊匹配，所以走 _like（会转义 `%` / `_` 并带上 ESCAPE）。
    用户搜「50%」或名字带下划线的（A_B）时，不转义会匹配到一堆无关的人。
    """
    if is_wechat4(client):
        where = (f"{_like('username', name)} OR {_like('nick_name', name)} "
                 f"OR {_like('remark', name)} OR {_like('alias', name)}")
        return _v4_contact_rows(client, where=where, limit=limit)
    where = (f"{_like('UserName', name)} OR {_like('NickName', name)} "
             f"OR {_like('Remark', name)} OR {_like('Alias', name)}")
    return _v3_contact_rows(client, where=where, limit=limit)


def _pb_varint(buf, i):
    """读一个 protobuf varint，返回 (值, 新下标)。"""
    r = s = 0
    while i < len(buf):
        x = buf[i]
        i += 1
        r |= (x & 0x7F) << s
        if not x & 0x80:
            break
        s += 7
        if s > 63:
            break
    return r, i


def _pb_fields(buf):
    """极简 protobuf 遍历：只认 varint(0) / 长度分隔(2) / 定长 32(5) / 定长 64(1)。

    遇到不认识的 wire type 就**停手**，不猜。
    """
    i = 0
    while i < len(buf):
        key, i = _pb_varint(buf, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _pb_varint(buf, i)
        elif wt == 2:
            n, i = _pb_varint(buf, i)
            v = buf[i:i + n]
            i += n
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        else:
            return
        yield f, v


def decode_room_members(blob):
    """chat_room.ext_buffer -> [{"wxid", "name", "role"}]。

    2026-10-01 对着真实数据反解确认的结构：外层是 repeated 子消息，每个成员
      1 = wxid（也可能是 ALIAS0313 这类别名 id）
      2 = 群昵称（**可能没有**）
      3 = 角色标记（群主实测是 9，普通成员 1 / 8193）
      4 = 邀请人 wxid
    只认这几项；解不出就返回空，**不猜格式**。

    为什么不用 chatroom_member 表：那张表只有 (room_id, member_id) 两个整数
    外键，还得再解一层 name2id；ext_buffer 直接带 wxid + 群昵称，少一跳。
    """
    if not blob:
        return []
    try:
        raw = binascii.unhexlify(str(blob))
    except (binascii.Error, ValueError):
        return []
    out = []
    try:
        for f, v in _pb_fields(raw):
            if not isinstance(v, bytes):
                continue
            d = {}
            for sf, sv in _pb_fields(v):
                if sf in (1, 2, 4) and isinstance(sv, bytes):
                    d[sf] = sv.decode("utf-8", "ignore")
                elif sf == 3 and not isinstance(sv, bytes):
                    d[sf] = sv
            wxid = str(d.get(1) or "").strip()
            if wxid:
                out.append({"wxid": wxid,
                            "name": str(d.get(2) or "").strip(),
                            "role": int(d.get(3) or 0)})
    except Exception:
        return []
    return out


def group_members(client, talker, owner=""):
    """群成员 [{wxid, name, role}]；name 是**群昵称**，可能为空。

    群主用 role==9 或与 chat_room.owner 相同来标（两个判据都留着，因为实测
    只见到一个群的 role，样本太少）。
    """
    if not is_wechat4(client):
        return []
    try:
        rows = _query(client, "contact.db",
                      "SELECT ext_buffer, owner FROM chat_room "
                      f"WHERE username = '{_q(talker)}' LIMIT 1")
    except Exception:
        return []
    for r in rows:
        mem = decode_room_members(_pick(r, "ext_buffer", 0))
        own = str(_pick(r, "owner", 1) or "") or str(owner or "")
        for m in mem:
            m["is_owner"] = bool(own and m["wxid"] == own) or m.get("role") == 9
        return mem
    return []


_SEARCH_KEY_SEP = "\x08"


def labels_of_search_key(key):
    """`contact_fts.db` 里 contact_fts_v5.search_key -> 这个人的**标签名**列表。

    ⚠️ 2026-10-01 对着真实数据反解确认的结构（和 remark/nick/alias 逐行对拍过）。
    整串固定 7 段、`\\x08` 分隔：

        0 = 备注   1 = ''（实测恒空）  2 = 昵称
        3 = **标签**（多个用英文逗号连，没有标签就是空串）
        4 = 微信号(alias)   5 = 地区   6 = ''（实测恒空）

    样本（右侧是 contact.db 里同一个人，用来对拍）：
        ['王小明','','小桐 王小明','亲人','lww00000000','某市 某区','']
            -> remark='王小明' nick='小桐 王小明' alias='lww00000000'
        ['赵六  小学同学','','六哥','','zhangsan_002','某国 某市 ','']
            -> 第 4 段是空的 = 这个人**没有**标签

    段数不足 4 就返回空（**不猜**：布局对不上时宁可说「读不到标签」）。
    """
    parts = str(key or "").split(_SEARCH_KEY_SEP)
    if len(parts) < 4:
        return []
    return [x.strip() for x in parts[3].split(",") if x.strip()]


def _is4_quiet(client):
    """`is_wechat4()` 的**不抛版本**：判不出来就当 False（=「读不到」）。

    标签那两条查询要在**读不到时说「读不到」**，而不是把异常丢给上层——上层拿到
    异常只能回一句「失败：…」，用户分不清是「你没有标签」还是「库没读上」。
    连不上 hook 时 `is_wechat4` 自己会抛（它要探库），所以这里必须兜住。
    """
    try:
        return bool(is_wechat4(client))
    except Exception:
        return False


def label_names(client):
    """微信自带的标签名 `[{"id","name"}]`（contact.db.contact_label，一次查询）。

    **读不到就返回 `None`**，和「一个标签都没有」的 `[]` 分开：前者只能如实说
    「读不到」（并提示可能是查库出问题），后者才能说「你还没建过标签」。
    把两者混成一个空列表，用户会以为自己的标签丢了。

    ⚠️ 这张表**只有标签本身**（id / 名字 / 排序），**没有成员**——所以
    「谁在标签里」必须走 `contacts_in_label()`（成员藏在 contact_fts 的
    search_key 第 4 段）。2026-10-01 把 22 个库的表名/列名全过了一遍，
    只有这张表带 label，所以别再去找第二张表了。
    """
    if not _is4_quiet(client):
        return None
    try:
        rows = _query(client, "contact.db",
                      "SELECT label_id_, label_name_ FROM contact_label "
                      "ORDER BY sort_order_, label_id_")
    except Exception:
        return None
    out = []
    for r in rows:
        name = str(_pick(r, "label_name_", 1) or "").strip()
        if name:
            out.append({"id": str(_pick(r, "label_id_", 0) or ""), "name": name})
    return out


def contacts_in_label(client, label, limit=1000):
    """微信某个标签下的人（wxid 列表）。**读不到返回 `None`**（和「没人」的 `[]` 分开）。

    **两步，缺一不可**：
      1. 用 `search_key LIKE %标签名%` 把候选缩小（一次 JOIN，见下）；
      2. 再用 `labels_of_search_key()` **精确核对第 4 段**。

    第 2 步不是保险，是必须的：实测标签「1」LIKE 命中 **412** 行，真成员只有
    **1** 个——`gzh001` 这种微信号里的数字会把 LIKE 骗过去。少了一步就会把
    412 个人当成「标签 1 的成员」，而群发是**不可逆**的。

    JOIN 用 contact_fts.db 自己的 name2id（它的 rowid 就是 fts 的 rowid），
    所以一条 SQL 就拿到 wxid，不用再解一层。
    """
    name = str(label or "").strip()
    if not name or not _is4_quiet(client):
        return None
    sql = ("SELECT n.username AS u, f.search_key AS k FROM contact_fts_v5 f "
           "JOIN name2id n ON n.rowid = f.rowid "
           f"WHERE {_like('f.search_key', name)}")
    try:
        rows = _query(client, "contact_fts.db", sql)
    except Exception:
        return None
    out = []
    for r in rows:
        if name not in labels_of_search_key(_pick(r, "k", 1)):
            continue
        u = str(_pick(r, "u", 0) or "").strip()
        if u and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


def pending_replies(client, limit=20):
    """「谁在等我回」：unread_count > 0 的会话。

    直接用微信自己在 SessionTable 里维护的 unread_count，不用我们猜
    「最后一条不是我发的」。几百行的小表，一次查询。
    注意：未读数要等你在微信里**点开那个会话**才会清零，所以这条列表会一直
    显示同一批人 —— 那是微信的语义，不是 bug。
    """
    if not is_wechat4(client):
        return []
    sql = ("SELECT username, unread_count, summary, last_timestamp, "
           "last_msg_sender, last_sender_display_name FROM SessionTable "
           f"WHERE unread_count > 0 ORDER BY last_timestamp DESC LIMIT {int(limit)}")
    try:
        rows = _query(client, "session.db", sql)
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({
            "talker": str(_pick(r, "username", 0) or ""),
            "unread": _as_int(_pick(r, "unread_count", 1)),
            "content": str(_pick(r, "summary", 2) or "").strip(),
            "sender": str(_pick(r, "last_msg_sender", 4) or ""),
            "sender_name": str(_pick(r, "last_sender_display_name", 5) or "").strip(),
            "time": _fmt_time(_pick(r, "last_timestamp", 3)),
            "_ts": _as_int(_pick(r, "last_timestamp", 3)),
        })
    return out


def message_xml(client, talker, local_id):
    """取一条消息的**原始 XML**（转发用）。拿不到返回 ""。

    只有 4.x 有这条路；3.9.x 没有对应实现，直接返回空让调用方如实报错。
    """
    if not is_wechat4(client):
        return ""
    return _fetch_message_xml(client, talker, local_id)


def query_contact_history(client, talker, limit=50, keyword=None, since=None,
                          until=None):
    """查某个会话的文本历史，时间升序，最多 limit 条。

    `since` / `until`（epoch 秒，可选）= 只看这两个时刻之间（含端点）的消息。
    **这是「最近 N 天」唯一能被真正回答的入口**：不传它们时返回的是「最新的
    limit 条」，那是个**按条数**的窗口，跨度可长可短（2026-10-01 真机实测：
    张三那条会话 30 条只覆盖 1.4 天、50 条只覆盖 3 天；李四 50 条只覆盖
    20 小时）。所以上层（`agent_tools.t_read_history`）必须把「这次实际覆盖到
    什么时候」如实告诉模型——**不说，模型就会把窗口跨度当成用户问的时间范围**
    （真机踩过：用户问「最近 10 天」，模型答「最近 10 天（9/30–10/1）」，而它
    手里其实只有 30 条、1.4 天）。

    ⚠️ **`since` 单独给是翻不到更早的**：它锚在「现在」，返回的永远是最近
    limit 条——实测 `days=10` 与 `days=30` 拿到的是同一批。要往更早看必须
    同时给 `until`（= 把上一批最早那条的时间当上界，一批一批往回走）。
    """
    if is_wechat4(client):
        return _v4_query_history(client, talker, limit, keyword, since, until)
    return _v3_query_history(client, talker, limit, keyword, since, until)


def collect_contact_history(client, talker, page=200, max_items=0, since=None,
                            until=None):
    """把一个会话的文本历史**尽量全**地按时间升序收回来（给「导出对话」用）。

    为什么要单独立一个：`query_contact_history` 一次只给 `limit` 条，
    而「导出和某人的全部对话」要的是**全量**。翻页必须靠 `until` 一页页往回走——
    只给 `since` 的话每一批都是「最近 page 条」，会拿到同一批（实测过，见上面
    `query_contact_history` 的说明）。这里显式这么走。

    返回 `(rows, meta)`：

        rows   时间升序的原始行（形状同 `query_contact_history`）
        meta   {"pages", "count", "truncated", "oldest", "newest"}

    ⚠️ `max_items > 0` 是**硬上限**：到了就停，并把 `truncated=True` 如实带出去——
    **绝不静默截断**（调用方必须把「只导了前 N 条」说出来，这是本项目最在意的
    那一类问题）。默认 0 = 不限（由调用方把关，因为这是用户主动发起的动作）。

    ⚠️ 每一页都是一次真实的 hook 查询，所以调用方**必须**给 `max_items` 兜底，
    别让它无上限地翻（hook 不支持并发，翻页期间轮询会一直等着）。
    """
    out = []
    seen = set()
    cur_until = until
    pages = 0
    truncated = False

    while True:
        batch = query_contact_history(client, talker, limit=page,
                                      since=since, until=cur_until)
        pages += 1
        if not batch:
            break

        # `until` 是「<=」（含端点），相邻两批会在边界那条上重叠 —— 必须去重，
        # 否则导出的文件里会出现重复的一行（而用户是拿它当存档的）。
        fresh = []
        for m in batch:
            key = (m.get("time"), str(m.get("content"))[:120], m.get("is_self"))
            if key in seen:
                continue
            seen.add(key)
            fresh.append(m)
        out = fresh + out          # 每批是「更早的那一段」，所以往前面接

        if max_items and len(out) >= max_items:
            out = out[-int(max_items):]     # 升序，保留最近的 max_items 条
            truncated = True
            break

        oldest = min((m.get("time") or 0) for m in batch)
        if not oldest or oldest == cur_until:
            # 时间戳取不到、或游标没往前走 —— 停，**绝不拿死循环去撞 hook**
            break
        cur_until = oldest

    meta = {"pages": pages, "count": len(out), "truncated": truncated}
    if out:
        times = [(m.get("time") or 0) for m in out]
        meta["oldest"], meta["newest"] = min(times), max(times)
    return out, meta


# ---------- 「这段时间里有多少条」 ----------
#
# 只为「如实告诉模型规模」存在：用户问「9 月我们都聊了什么」时，光给最新的 50 条
# 而不给总数，模型就不知道自己手里是 50/1400——那正是「静默失效」的同一族。
# 纯 COUNT/MIN/MAX，带会话过滤、不排序，所以不碰 hook 铁律第 2 条。

def _v4_history_count(client, talker, since=None, until=None, keyword=None):
    """4.x：某会话在某时间范围内有多少条。

    走 fts 分片（和 `_v4_history_from_fts` **同一数据源**，数字才和能翻到的行对得上），
    复用「命中就停」那套——实测一个会话的消息只落在一个分片里。
    分片一个都没命中就退回 Msg_ 表数一次（那条路也是收消息的兜底）。
    """
    if _uses_fts(client):
        sid = _v4_fts_session_id(client, talker)
        if sid is not None:
            where = f"session_id = {sid}"
            if since:
                where += f" AND create_time >= {int(since)}"
            if until:
                where += f" AND create_time <= {int(until)}"
            if keyword:
                where += f" AND local_type = 1 AND acontent MATCH '{_q(keyword)}'"
            for t in _v4_fts_tables(client):
                try:
                    rows = _query(
                        client, "message_fts.db",
                        f"SELECT COUNT(*) AS c, MIN(create_time) AS mn, "
                        f"MAX(create_time) AS mx FROM {t} WHERE {where}")
                except Exception:
                    continue
                r = rows[0] if rows else {}
                c = _as_int(_pick(r, "c", 0)) or 0
                if c:
                    return {"count": c,
                            "first": _as_int(_pick(r, "mn", 1)),
                            "last": _as_int(_pick(r, "mx", 2)),
                            "source": "fts"}
    table = _v4_table_for(talker)
    conds = []
    if keyword:
        conds.append(_like("message_content", keyword))
    if since:
        conds.append(f"create_time >= {int(since)}")
    if until:
        conds.append(f"create_time <= {int(until)}")
    tail = (" WHERE " + " AND ".join(conds)) if conds else ""
    for db in _v4_msg_dbs(client):
        try:
            rows = _query(
                client, db,
                f"SELECT COUNT(*) AS c, MIN(create_time) AS mn, "
                f"MAX(create_time) AS mx FROM {table}{tail}")
        except Exception:
            continue
        r = rows[0] if rows else {}
        c = _as_int(_pick(r, "c", 0)) or 0
        if c:
            return {"count": c,
                    "first": _as_int(_pick(r, "mn", 1)),
                    "last": _as_int(_pick(r, "mx", 2)),
                    "source": "table"}
    return {"count": 0, "first": 0, "last": 0, "source": ""}


def _v3_history_count(client, talker, since=None, until=None, keyword=None):
    """3.9.x：同上，按 MSG 分片求和。"""
    t = _q(talker)
    out = {"count": 0, "first": 0, "last": 0, "source": "table"}
    for db in _v3_msg_dbs(client):
        sql = ("SELECT COUNT(*) AS c, MIN(CreateTime) AS mn, MAX(CreateTime) AS mx "
               f"FROM MSG WHERE StrTalker = '{t}' AND Type = 1")
        if keyword:
            sql += f" AND {_like('StrContent', keyword)}"
        if since:
            sql += f" AND CreateTime >= {int(since)}"
        if until:
            sql += f" AND CreateTime <= {int(until)}"
        try:
            rows = _query(client, db, sql)
        except Exception:
            continue
        for r in rows:
            out["count"] += _as_int(_pick(r, "c", 0)) or 0
            mn = _as_int(_pick(r, "mn", 1))
            mx = _as_int(_pick(r, "mx", 2))
            if mn and (not out["first"] or mn < out["first"]):
                out["first"] = mn
            if mx and mx > out["last"]:
                out["last"] = mx
    return out


def count_history(client, talker, since=None, until=None, keyword=None):
    """某个会话在某个时间范围内有多少条（`{"count","first","last","source"}`）。

    **只用来把规模如实说给模型听**——「9 月一共 1400 条，这里只给你最新的 50 条」
    和「只给 50 条」是完全不同的两句话。查不到就是 `count=0`，不抛异常。
    """
    if is_wechat4(client):
        return _v4_history_count(client, talker, since, until, keyword)
    return _v3_history_count(client, talker, since, until, keyword)


# ---------- 「那天所有聊天」：跨会话按时间取 ----------
#
# 这两条是**没有 session_id 过滤**的按时间查询——CLAUDE.md 铁律第 2 条最警惕的形状。
# 2026-10-01 真机实测（停 bot、只读）之后才敢加：
#   `WHERE create_time >= ? AND create_time <= ?` 的 COUNT / GROUP BY，每个分片
#   0.05~0.11 秒；全天 4 个分片合计 0.29~0.40 秒，**一次慢查询都没有**。
#   铁律警告的是「不带过滤**还排序**」（`WHERE local_type=1 ORDER BY create_time DESC`
#   0.3s 起、劣化到 6s）；按时间过滤 + 聚合实测是快的。
# 仍然只在用户**明确问「那天发生了什么」**时走这条路，绝不进轮询、绝不进预取。

def day_overview(client, since, until, limit=200):
    """某时间段里**每个会话**各有多少条（跨所有会话，按条数降序）。

    `talker` 是 wxid / roomid —— **显示名由上层查联系人表**，这里不编名字。
    拿不到名字的会话 `talker` 为空串：**照实回出来**，不许悄悄丢
    （丢掉就等于「那天我跟某些人聊过」这件事本身被隐瞒了）。
    """
    since, until = int(since or 0), int(until or 0)
    if is_wechat4(client):
        if not _uses_fts(client):
            return []
        smap = _v4_fts_session_map(client)
        agg = {}
        for t in _v4_fts_tables(client):
            try:
                rows = _query(
                    client, "message_fts.db",
                    f"SELECT session_id, COUNT(*) AS c, MIN(create_time) AS mn, "
                    f"MAX(create_time) AS mx FROM {t} "
                    f"WHERE create_time >= {since} AND create_time <= {until} "
                    f"GROUP BY session_id")
            except Exception:
                continue
            for r in rows:
                sid = _as_int(_pick(r, "session_id", 0))
                c = _as_int(_pick(r, "c", 1)) or 0
                if not c:
                    continue
                cur = agg.setdefault(sid, {"count": 0, "first": 0, "last": 0})
                cur["count"] += c
                mn = _as_int(_pick(r, "mn", 2))
                mx = _as_int(_pick(r, "mx", 3))
                if mn and (not cur["first"] or mn < cur["first"]):
                    cur["first"] = mn
                if mx > cur["last"]:
                    cur["last"] = mx
        out = [{"talker": smap.get(sid, ""), "count": v["count"],
                "first": v["first"], "last": v["last"]}
               for sid, v in agg.items()]
        out.sort(key=lambda m: -m["count"])
        return out[:int(limit)]

    # 3.9.x：MSG 表没有会话索引，但这是「按时间过滤 + 分组」，与 v4 同形状。
    # ⚠️ **本机没有 3.9.x 可验，这条未在真机核实**（照 v4 对等实现）。
    agg = {}
    for db in _v3_msg_dbs(client):
        try:
            rows = _query(
                client, db,
                f"SELECT StrTalker, COUNT(*) AS c, MIN(CreateTime) AS mn, "
                f"MAX(CreateTime) AS mx FROM MSG WHERE Type = 1 "
                f"AND CreateTime >= {since} AND CreateTime <= {until} "
                f"GROUP BY StrTalker")
        except Exception:
            continue
        for r in rows:
            who = str(_pick(r, "StrTalker", 0) or "")
            c = _as_int(_pick(r, "c", 1)) or 0
            if not c:
                continue
            cur = agg.setdefault(who, {"count": 0, "first": 0, "last": 0})
            cur["count"] += c
            mn = _as_int(_pick(r, "mn", 2))
            mx = _as_int(_pick(r, "mx", 3))
            if mn and (not cur["first"] or mn < cur["first"]):
                cur["first"] = mn
            if mx > cur["last"]:
                cur["last"] = mx
    out = [{"talker": k, "count": v["count"], "first": v["first"],
            "last": v["last"]} for k, v in agg.items()]
    out.sort(key=lambda m: -m["count"])
    return out[:int(limit)]


def range_messages(client, since, until, max_total=20000, page=800, talker=None):
    """某时间段里**所有会话**的消息，时间升序（跨会话）。

    给了 `talker` 就只取**那一个会话**——会话过滤更窄，所以只会更便宜。
    「导出某个人的一整个月」走的就是这条路（那条会话自己的记录全给，不受
    工具返回的上下文闸限制）。

    **为什么翻页用 fts 的 rowid 而不是 create_time**：按时间翻页（`until=最早那条`）
    在「同一秒里有很多条」时会**丢消息或重复**——2026-10-01 实测过，548 条里多出 2 条
    重复。rowid 唯一且索引有序，翻页精确。

    正文只取 fts 里的 `acontent`，非文本渲染成 `[图片]` 这类标签；
    **appmsg 不回头去捞原始 XML**——那是一条消息一次查库，导出一整天的量会把
    hook 压死。代价是引用/链接只留下摘要，这一点会写进导出文件的开头。
    """
    since, until = int(since or 0), int(until or 0)
    max_total, page = int(max_total), max(int(page), 1)
    out = []
    if is_wechat4(client):
        if not _uses_fts(client):
            return []
        smap = _v4_fts_session_map(client)
        self_id = _v4_fts_self_id(client)
        only_sid = _v4_fts_session_id(client, talker) if talker else None
        cols = ("rowid AS rid, acontent, session_id, sender_id, create_time, "
                "local_type, message_local_id")
        for t in _v4_fts_tables(client):
            floor = None
            while len(out) < max_total:
                where = f"create_time >= {since} AND create_time <= {until}"
                if only_sid is not None:
                    where += f" AND session_id = {only_sid}"
                if floor is not None:
                    where += f" AND rowid < {int(floor)}"
                try:
                    rows = _query(client, "message_fts.db",
                                  f"SELECT {cols} FROM {t} WHERE {where} "
                                  f"ORDER BY rowid DESC LIMIT {page}")
                except Exception:
                    break
                if not rows:
                    break
                lids = []
                for r in rows:
                    rid = _as_int(_pick(r, "rid", 0))
                    if rid:
                        lids.append(rid)
                    sid = _as_int(_pick(r, "session_id", 2))
                    sender = _as_int(_pick(r, "sender_id", 3))
                    lt = _as_int(_pick(r, "local_type", 5))
                    text = str(_pick(r, "acontent", 1) or "")
                    if lt != 1:
                        text = _render_nontext(lt, text)
                    out.append({
                        "talker": talker if talker else smap.get(sid, ""),
                        "local_id": str(_pick(r, "message_local_id", 6) or ""),
                        "local_type": lt,
                        "content": text,
                        "sender": smap.get(sender, ""),
                        "is_self": 1 if (self_id is not None
                                         and sender == self_id) else 0,
                        "time": _fmt_time(_pick(r, "create_time", 4)),
                        "_ts": _as_int(_pick(r, "create_time", 4)),
                    })
                if not lids:
                    break
                floor = min(lids)
                if len(rows) < page:
                    break
        out.sort(key=lambda m: (m["_ts"], m["talker"], m["local_id"]))
        return out[:max_total]

    # 3.9.x：MSG 表用 rowid 翻页（SQLite 自带的隐含主键），形状与 v4 对等。
    # ⚠️ **本机没有 3.9.x 可验，这条未在真机核实**。
    for db in _v3_msg_dbs(client):
        floor = None
        while len(out) < max_total:
            cond = (f"Type = 1 AND CreateTime >= {since} AND CreateTime <= {until}")
            if talker:
                cond += f" AND StrTalker = '{_q(talker)}'"
            if floor is not None:
                cond += f" AND rowid < {int(floor)}"
            try:
                rows = _query(client, db,
                              f"SELECT rowid AS rid, StrTalker, StrContent, IsSender, "
                              f"CreateTime FROM MSG WHERE {cond} "
                              f"ORDER BY rowid DESC LIMIT {page}")
            except Exception:
                break
            if not rows:
                break
            lids = []
            for r in rows:
                rid = _as_int(_pick(r, "rid", 0))
                if rid:
                    lids.append(rid)
                out.append({
                    "talker": talker or str(_pick(r, "StrTalker", 1) or ""),
                    "local_id": str(rid or ""),
                    "local_type": 1,
                    "content": str(_pick(r, "StrContent", 2) or ""),
                    "sender": "",
                    "is_self": int(_pick(r, "IsSender", 3) or 0),
                    "time": _fmt_time(_pick(r, "CreateTime", 4)),
                    "_ts": _as_int(_pick(r, "CreateTime", 4)),
                })
            if not lids:
                break
            floor = min(lids)
            if len(rows) < page:
                break
    out.sort(key=lambda m: (m["_ts"], m["talker"], m["local_id"]))
    return out[:max_total]


def search_history(client, keyword, limit=30):
    """跨所有会话按关键词检索最近的文本消息。"""
    if is_wechat4(client):
        return _v4_search(client, keyword, limit)
    return _v3_search(client, keyword, limit)


# ============================================================
#  轮询收消息（aixed 后端没有收消息回调，只能轮询）
# ============================================================

def latest_cursor(client):
    """轮询起点。

    4.x 返回 {fts分片表名: 该分片最大 rowid}；3.9.x 返回 {"__time__": 最大 CreateTime}。

    4.x 用 fts 的 **rowid** 而不是时间：rowid 是 fts5 的索引主键，
    `WHERE rowid > N ORDER BY rowid` 是纯索引范围扫描（实测每分片 0.005 秒）；
    而按 create_time 过滤+排序要排全表（0.3 秒起，hook 劣化时能到 6 秒）。
    """
    if is_wechat4(client):
        if _uses_fts(client):
            cur = _v4_fts_cursors(client)
            if cur:
                return cur
        # fts 不可用（或分片都探不到）：退回时间游标，别返回空 dict，
        # 否则轮询会一直空转（踩过：启动时 fts 恰好在抖动，游标成了 {}）。
        try:
            rows = _query(client, "session.db",
                          "SELECT MAX(last_timestamp) AS m FROM SessionTable")
        except Exception:
            return {}
        for r in rows:
            return {"__time__": _as_int(_pick(r, "m", 0))}
        return {"__time__": 0}
    best = 0
    for db in _v3_msg_dbs(client):
        try:
            for r in _query(client, db, "SELECT MAX(CreateTime) AS m FROM MSG"):
                best = max(best, _as_int(_pick(r, "m", 0)))
        except Exception:
            continue
    return {"__time__": best}


def _v4_fts_cursors(client):
    """各 fts 分片当前的最大 rowid。"""
    out = {}
    for tab in _v4_fts_tables(client):
        try:
            rows = _query(client, "message_fts.db", f"SELECT MAX(rowid) AS m FROM {tab}")
        except Exception:
            continue
        for r in rows:
            out[tab] = _as_int(_pick(r, "m", 0))
    return out


def _as_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _v4_active_talkers(client, since):
    """last_timestamp >= since 的会话名（通常只有少数几个）。"""
    sql = ("SELECT username FROM SessionTable "
           f"WHERE last_timestamp >= {_as_int(since)}")
    try:
        rows = _query(client, "session.db", sql)
    except Exception:
        return []
    return [str(_pick(r, "username", 0)) for r in rows if _pick(r, "username", 0)]


def _v4_pickup_nontext(client, cursors, already, limit=10):
    """把 **fts 装不下的非文本（主要是图片）**从消息表里捞出来。

    为什么非有这一条不可（2026-10-01 实测）：
      * **fts 里根本不存在 `local_type = 3` 的行** —— 四个分片
        `SELECT COUNT(*) WHERE local_type = 3` 全是 **0**。所以轮询游标走 fts
        时，**任何会话里别人发来的图片它永远看不见**；
      * 另一条路 `_v4_new_messages_session` 靠 SessionTable.summary，
        而图片的 summary 是**空串** → 被 `if not content: continue` 跳过。
      两条路都瞎，用户看到的就是「我把图发过去了，它一点反应没有」。

    做法：只用 SessionTable 当「有新动静」的信号（几百行的小表、一次查询），
    条件收紧到 **最后一条不是文本**（summary 为空）才回查那个会话的消息表。
    稳态下这个查询返回 0 行 → **不增加任何额外查库**；真收到图才多 1~2 次查询。

    每个会话一个水位线 `cursors["__nonttext__"][talker]`，避免同一张图每轮重复报。
    **不做「第一次见到就只记水位线不报」那种 seed**——那会让「你在某个会话里发的
    第一张图」永远报不上来（那个会话还没进水位线，就被当成历史 seed 掉了）。
    启动边界的历史回放由两道现成机制挡着，不需要在这里再挡一次：
      * `last_timestamp >= since`（since 是 fts 游标 `__time__`，启动时就是最新）= 老会话根本进不来；
      * `prime()` 会把边界那批消息塞进 `seen`，第一轮再查到的会被去重掉；
      * 真有「停机期间的旧消息」漏进来，bot 主循环的 catchup 判定也只通知、不自动回复。
    """
    since = _as_int((cursors or {}).get("__time__", 0))
    sql = ("SELECT username, last_timestamp FROM SessionTable "
           f"WHERE last_timestamp >= {since} "
           "AND (summary IS NULL OR summary = '')")
    try:
        rows = _query(client, "session.db", sql)
    except Exception as e:
        _note_poll_error("session.db", e)
        return []

    wm = cursors.setdefault("__nonttext__", {})
    if not isinstance(wm, dict):          # 旧 state.json 里形状不对就重置，别让它挡住轮询
        wm = cursors["__nonttext__"] = {}

    seen = {(m.get("talker"), m.get("content"), m.get("_ts")) for m in (already or [])}
    out = []
    for r in rows:
        talker = str(_pick(r, "username", 0) or "")
        last_ts = _as_int(_pick(r, "last_timestamp", 1))
        if not talker or last_ts <= 0:
            continue
        prev = _as_int(wm.get(talker, 0))
        if last_ts <= prev:
            continue
        wm[talker] = last_ts
        # 这条会话最后一条是非文本 → 回查它的消息表，把新增的那几条渲染出来
        for m in _v4_history_from_tables(client, talker, limit=limit):
            if _as_int(m.get("_ts", 0)) <= prev:
                continue
            key = (m.get("talker"), m.get("content"), m.get("_ts"))
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
    return out


def _v4_new_messages(client, cursors, limit=200):
    """按 **rowid 游标**取新消息——纯索引范围扫描，最便宜的一条路。

    每条消息都带上 rowid，游标按分片推进。这样每次轮询只读「新增的那几行」，
    不去碰那 104MB 的索引，也不依赖 message_0.db（实测它常常解析不出句柄）。
    """
    cursors = dict(cursors or {})
    tables = _v4_fts_tables(client)
    if not tables:
        # message_fts.db 的句柄会轮换失效（查询成功但返回 0 行）。以前这里直接空转，
        # 表现是 bot 静默收不到任何消息——明明 message_0.db 是好的。退回按会话表查。
        if not cursors.get("__time__"):
            # 兜底只回看 10 分钟，避免 __time__ 缺失时把整库重放一遍
            cursors["__time__"] = int(time.time()) - 600
        print("[live] ⚠️ fts 分片不可用，退回按会话表轮询", file=sys.stderr, flush=True)
        return _v4_new_messages_tables(client, cursors, limit)

    # fts 这条路能走，会话兜底就不在本轮链路上：把它上一轮留下的旧错误清掉。
    # 不清的话 fts 修好之后 bot 心跳会**永远**挂着一条「session.db 失败」的假告警
    # （这条路不再查 session.db，也就没人清它了）。
    _POLL_ERRORS.pop("session.db", None)

    smap = _v4_fts_session_map(client)
    self_id = _v4_fts_self_id(client)
    out = []
    for tab in tables:
        cur = _as_int(cursors.get(tab, 0))
        sql = (
            "SELECT rowid, acontent, session_id, sender_id, create_time, local_type, "
            "message_local_id "
            f"FROM {tab} WHERE rowid > {cur} ORDER BY rowid ASC LIMIT {int(limit)}"
        )
        try:
            found = _query(client, "message_fts.db", sql)
            _POLL_ERRORS.pop(tab, None)
        except Exception as e:
            # 只报第 1/10/50 次，避免刷屏；不静默是因为静默会让人以为「只是没消息」
            _note_poll_error(tab, e)
            continue
        for r in found:
            rid = _as_int(_pick(r, "rowid", 0))
            if rid > cursors.get(tab, 0):
                cursors[tab] = rid
            # 同时维护时间游标：万一 fts 掉线要退回按会话表查，得有个合理的起点
            ts = _as_int(_pick(r, "create_time", 4))
            if ts > _as_int(cursors.get("__time__", 0)):
                cursors["__time__"] = ts
            lt = _as_int(_pick(r, "local_type", 5))
            text = str(_pick(r, "acontent", 1) or "")
            sid = _as_int(_pick(r, "session_id", 2))
            sender = _as_int(_pick(r, "sender_id", 3))
            talker = smap.get(sid, f"session_{sid}")
            # 非文本**不再丢弃**：渲染成一行文本让下游看得见。
            # 丢掉的后果是「用户发了图/链接，bot 完全没反应」。
            if lt != 1:
                if (lt & 0xFFFFFFFF) == 49:
                    # appmsg（链接/引用/文件…）：回查原始 XML。fts 的摘要只
                    # 相当于 <title>，**引用消息被引用的原文在 <refermsg> 里**，
                    # 不查原文的话模型只看到用户打的那几个字，答非所问。
                    text = render_appmsg(
                        _fetch_message_xml(client, talker,
                                           _pick(r, "message_local_id", 6)),
                        text)
                else:
                    text = _render_nontext(lt, text)
            out.append({
                "talker": talker,
                "content": text,
                "is_self": 1 if (self_id is not None and sender == self_id) else 0,
                "time": _fmt_time(_pick(r, "create_time", 4)),
                "_ts": ts,
                # local_type 带下去：上层要区分「文本 vs 图片」才能决定该不该
                # 响应（例如自己刚发出去的图不该再被当成新消息）。
                "local_type": lt,
            })

    # 补漏：图片之类**不在 fts 里**的消息，靠 SessionTable 的信号捞回来。
    # 详见 _v4_pickup_nontext 的 docstring（这是「对方发图、bot 没反应」的根治处）。
    try:
        out.extend(_v4_pickup_nontext(client, cursors, out, limit=10))
    except Exception as e:
        _note_poll_error("session.db", e)

    out.sort(key=lambda m: m["_ts"])
    return out, cursors


def _v4_new_messages_session(client, cursors, limit=200):
    """最后一道防线：只用 session.db，完全不碰 fts / Msg_ 表。

    fts 句柄失效、message_N.db 也解析不出句柄时（实测会同时发生），前面两条路
    一条都走不通，bot 就静默收不到任何消息。SessionTable 是这几张库里最稳的：
    每行带 summary（该会话最后一条消息的文本）和 last_timestamp，几百行的小表，
    一次查询就够。产出的消息形状与 _v4_recent 保持一致。

    固有代价：每个会话只能拿到**最后一条**，同一会话里连发多条会被折叠成一条。
    这是兜底路径——fts 一旦被重扫救回来，就恢复完整精度。
    """
    cursors = dict(cursors or {})
    since = _as_int(cursors.get("__time__", 0))
    sql = ("SELECT username, summary, last_timestamp, last_msg_sender FROM SessionTable "
           f"WHERE last_timestamp >= {since} "
           f"ORDER BY last_timestamp ASC LIMIT {int(limit)}")
    try:
        rows = _query(client, "session.db", sql)
        # 查得动就把上次的错误清掉，和 fts 分片那条路保持一致
        _POLL_ERRORS.pop("session.db", None)
    except Exception as e:
        # 这里是**最后一道防线**：fts 分片和 Msg_ 表都已经不可用了，session.db 再失败
        # 就等于本轮一条消息都收不到。以前这里静默 `return [], cursors`，连 _POLL_ERRORS
        # 都不写（那时候只统计 fts 分片），于是 bot 心跳报「正常」、日志里一个字都没有。
        # 现在必须留两处痕：stderr 一行明确告警 + _POLL_ERRORS["session.db"]，
        # 让 poll_errors() / bot 心跳能把这件事报出来。
        n = _note_poll_error("session.db", e)
        if n in (1, 10, 50):
            print("[live] ⚠️ session.db 兜底也失败：fts / Msg_ / session.db 三层都不可用，"
                  "本轮收不到任何消息", file=sys.stderr, flush=True)
        return [], cursors
    out = []
    for r in rows:
        content = str(_pick(r, "summary", 1) or "").strip()
        if not content:
            continue  # 最后一条不是文本（图片/语音…），summary 是空的
        sender = str(_pick(r, "last_msg_sender", 3) or "")
        ts = _as_int(_pick(r, "last_timestamp", 2))
        out.append({
            "talker": str(_pick(r, "username", 0) or ""),
            "content": content,
            "is_self": 1 if (sender and _SELF_WXID and sender == _SELF_WXID) else 0,
            # 群聊要靠它标出最后一句是谁说的（SessionTable 只有这一条的身份信息）
            "sender": sender,
            "time": _fmt_time(ts),
            "_ts": ts,
        })
    out.sort(key=lambda m: m["_ts"])
    cursors["__time__"] = max([m["_ts"] for m in out], default=since)
    return out, cursors


def _v4_new_messages_tables(client, cursors, limit=200):
    """没有 fts 时的退路：用 SessionTable 找活跃会话，再逐个查 Msg_ 小表。

    Msg_ 分片整个拿不到、或这条路上一条都没捞到时，退到 session.db 兜底——
    那条路只要 SessionTable 一张表，比逐会话去解析 Msg_ 句柄可靠得多。
    """
    cursors = dict(cursors or {})
    since = _as_int(cursors.get("__time__", 0))
    out = []
    if _v4_msg_dbs(client):
        for talker in _v4_active_talkers(client, since):
            for m in _v4_history_from_tables(client, talker, limit):
                if m["_ts"] >= since:
                    out.append(m)
    if not out:
        # 三层全废（fts 分片不可用 + Msg_ 分片拿不到 + session.db 也查不动）这件事的
        # 日志由 _v4_new_messages_session 自己发——只有它手里有那个异常原文，
        # 它会同时写 stderr 和 _POLL_ERRORS["session.db"]。这里不重复报，
        # 只在下面接力游标。注意「session.db 查得动、只是没有新消息」是正常空闲，
        # **不能**当成故障刷日志。
        return _v4_new_messages_session(client, cursors, limit)
    # 这条路自己捞到了消息，会话兜底就不在本轮链路上——清掉它的旧错误，
    # 别让一条早就恢复的告警永远挂在 bot 心跳上（同 _v4_new_messages 里那一处）。
    _POLL_ERRORS.pop("session.db", None)
    out.sort(key=lambda m: m["_ts"])
    # 只更新 __time__，别整个替换游标字典——那样 fts 恢复后分片游标会丢，整库重放
    cursors["__time__"] = max([m["_ts"] for m in out], default=since)
    return out, cursors


def _v3_new_messages(client, cursors, limit=200):
    out = []
    since = _as_int((cursors or {}).get("__time__", 0))
    for db in _v3_msg_dbs(client):
        sql = (
            "SELECT StrTalker, StrContent, IsSender, CreateTime FROM MSG "
            f"WHERE Type = 1 AND CreateTime >= {since} "
            f"ORDER BY CreateTime ASC LIMIT {int(limit)}"
        )
        try:
            found = _query(client, db, sql)
        except Exception:
            continue
        for r in found:
            out.append({
                "talker": str(_pick(r, "StrTalker", 0) or ""),
                "content": str(_pick(r, "StrContent", 1) or ""),
                "is_self": _as_int(_pick(r, "IsSender", 2)),
                "_ts": _as_int(_pick(r, "CreateTime", 3)),
            })
    out.sort(key=lambda m: m["_ts"])
    return out, {"__time__": max([m["_ts"] for m in out], default=since)}


def new_messages(client, cursors, limit=200):
    """按游标取新消息。返回 (消息列表, 新游标)。

    游标是 dict（4.x 用 rowid 或时间，3.9.x 用时间）。

    ⚠️ 如果游标格式跟**当前可用的路径**对不上（说明 hook 能发现的库换了，
    比如从 fts 切到 message_0.db），就先对齐游标、本轮不返回消息——
    否则会因为游标归零把整段历史当成新消息刷一遍。
    """
    cursors = dict(cursors or {})
    v4 = is_wechat4(client)
    # 用「分片表实际能不能查到」来决定走哪条路，而不是用 _uses_fts 的探测结果：
    # probe 只验「查询不报错」，而 message_fts.db 句柄失效时查询是**成功但返回 0 行**，
    # 探测照样通过。那样 expected 会是空集，下面的守卫每次成立，轮询永远空转，
    # bot 就静默地收不到任何消息（明明 message_0.db 好好的）。
    fts_tables = _v4_fts_tables(client) if v4 else []
    fts = bool(fts_tables)

    expected = set(fts_tables) if fts else {"__time__"}
    if not (set(cursors) & expected):
        return [], latest_cursor(client)

    if fts:
        return _v4_new_messages(client, cursors, limit)
    if v4:
        return _v4_new_messages_tables(client, cursors, limit)
    return _v3_new_messages(client, cursors, limit)
