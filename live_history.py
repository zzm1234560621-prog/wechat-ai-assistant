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
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
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
        return _render_nontext(0, summary)

    title = _xml_field(xml, "title")
    ref_block = _xml_field(xml, "refermsg")
    ref_content = _xml_field(ref_block, "content") if ref_block else ""
    ref_name = _xml_field(ref_block, "displayname") if ref_block else ""

    label = "引用" if ref_block else _type_label(4 | (49 << 0))
    parts = []
    if ref_content:
        who = f"{ref_name}：" if ref_name else ""
        parts.append(f"[引用 {who}{ref_content[:160]}]")
    elif title:
        parts.append(f"[{label}]")

    said = title or _xml_field(xml, "content") or _xml_field(xml, "des") or summary
    if said:
        parts.append(str(said)[:200])
    return " ".join(parts) if parts else _render_nontext(0, summary)


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


def _v3_query_history(client, talker, limit=50, keyword=None):
    rows = []
    t = _q(talker)
    for db in _v3_msg_dbs(client):
        sql = (
            "SELECT StrTalker, StrContent, IsSender, CreateTime FROM MSG "
            f"WHERE StrTalker = '{t}' AND Type = 1"
        )
        if keyword:
            sql += f" AND StrContent LIKE '%{_q(keyword)}%'"
        sql += f" ORDER BY CreateTime DESC LIMIT {int(limit)}"
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


def _v3_search(client, keyword, limit=30):
    rows = []
    k = _q(keyword)
    for db in _v3_msg_dbs(client):
        sql = (
            "SELECT StrTalker, StrContent, IsSender, CreateTime FROM MSG "
            f"WHERE Type = 1 AND StrContent LIKE '%{k}%' "
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
    """会话名 -> 这个 fts 库里的 session_id。（目前只给调试用）"""
    try:
        rows = _query(client, "message_fts.db",
                      f"SELECT rowid FROM Name2Id WHERE username = '{_q(talker)}' LIMIT 1")
    except Exception:
        return None
    for r in rows:
        return _as_int(_pick(r, "rowid", 0))
    return None


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


def _v4_history_from_fts(client, talker, limit=50, keyword=None):
    """从全文索引取某会话的历史。

    按 session_id 过滤是**便宜**的：过滤先把行集缩小到这个会话自己的消息，
    排序只发生在小集合上（实测 0.14 秒）。真正贵的是不带选择性过滤、
    直接 `WHERE local_type=1 ORDER BY create_time` —— 那要排全表。

    这条路径不依赖 message_0.db（实测它常常解析不出句柄）。
    """
    if not _uses_fts(client):
        return []
    sid = _v4_fts_session_id(client, talker)
    if sid is None:
        return []
    where = f"session_id = {sid}"
    if keyword:
        # 关键词检索只在文本里找——MATCH 打在非文本的摘要上没意义
        where += f" AND local_type = 1 AND acontent MATCH '{_q(keyword)}'"
    self_id = _v4_fts_self_id(client)
    return _v4_fts_rows(client, where, limit, {sid: talker}, self_id, first_hit=True)


def _v4_query_history(client, talker, limit=50, keyword=None):
    hits = _v4_history_from_fts(client, talker, limit, keyword)
    if hits:
        return hits
    return _v4_history_from_tables(client, talker, limit, keyword)


def _v4_history_from_tables(client, talker, limit=50, keyword=None):
    """查该会话的 Msg_ 表。

    按 **local_id 倒序**（它是这张表的主键）——走 PK 索引，很便宜。
    别按 create_time 排：那列没索引，一条查询能到 1 秒以上（实测）。
    local_id 是自增的，倒序就是最近的在前。
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
        if keyword:
            sql += f" WHERE local_type = 1 AND message_content LIKE '%{_q(keyword)}%'"
        sql += f" ORDER BY local_id DESC LIMIT {int(limit)}"
        try:
            found = _query(client, db, sql)
        except Exception:
            continue  # 这个分片里没有该会话的表
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
                    try:
                        import image_cache
                        p = image_cache.find(table[4:], lid, ct)
                        if p:
                            content += f"（本地已解码缩略图：{p}）"
                    except Exception:
                        pass
            rows.append({
                "talker": talker,
                "local_id": str(lid) if lid is not None else "",
                "local_type": lt,
                "content": content,
                "is_self": 1 if (self_id is not None and sid_i == self_id) else 0,
                "time": _fmt_time(ct),
                "_ts": _as_int(ct),
            })
    rows.sort(key=lambda m: m["_ts"])
    return rows[-limit:]


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

    例：「我和李同学聊了什么」-> ['李同学']
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
    k = _q(keyword)
    scanned = 0
    for db in _v4_msg_dbs(client):
        self_id = _v4_self_rowid(client, db)
        for table in _v4_tables(client, db):
            if scanned >= max_tables:
                break
            scanned += 1
            cond = f"local_type = 1 AND message_content LIKE '%{k}%'" if k else "local_type = 1"
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
    """按昵称/备注/微信号模糊匹配联系人。"""
    if is_wechat4(client):
        n = _q(name)
        where = (f"username LIKE '%{n}%' OR nick_name LIKE '%{n}%' "
                 f"OR remark LIKE '%{n}%' OR alias LIKE '%{n}%'")
        return _v4_contact_rows(client, where=where, limit=limit)
    n = _q(name)
    where = (f"UserName LIKE '%{n}%' OR NickName LIKE '%{n}%' "
             f"OR Remark LIKE '%{n}%' OR Alias LIKE '%{n}%'")
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


def query_contact_history(client, talker, limit=50, keyword=None):
    """查某个会话的文本历史，时间升序，最多 limit 条。"""
    if is_wechat4(client):
        return _v4_query_history(client, talker, limit, keyword)
    return _v3_query_history(client, talker, limit, keyword)


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
            n = _POLL_ERRORS.get(tab, ("", 0))[1] + 1
            _POLL_ERRORS[tab] = (str(e), n)
            # 只报第 1/10/50 次，避免刷屏；不静默是因为静默会让人以为「只是没消息」
            if n in (1, 10, 50):
                print(f"[live] ⚠️ 轮询分片 {tab} 连续失败 {n} 次：{e}",
                      file=sys.stderr, flush=True)
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
            })
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
    except Exception:
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
        return _v4_new_messages_session(client, cursors, limit)
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
