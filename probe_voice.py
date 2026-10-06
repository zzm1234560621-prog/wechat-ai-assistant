"""语音条观测器：把「语音条到底存了什么」一次抓全。

**为什么先写这个、而不是先写解密器**：逆向的第一步是拿到真实数据。
现在这台机器**一条语音条的字节都没有**（82 个 `Rec\` 全空、全盘无 silk/amr、
`Msg_` 里没有音频列），连样本都没有——此时写出来的解密代码只能是编的。
这个脚本就是「样本一出现，立刻把该看的全看清」的那件工具。

它查四件事（全部只读）：
  1. **DB**：fts 里 `local_type=34` 的行 + 对应 `Msg_` 行的**每一个字段**（BLOB 以十六进制原样打）
     —— 看语音的 `message_content` XML 里有没有文件名/密钥/时长；
  2. **磁盘**：`Rec\` 与全盘的音频类文件快照，跑两次（`--tag before` / `--tag after`）就能看出
     「发一条语音到底有没有落文件、落在哪」；
  3. **消息 XML**：把 `message_content` 还原成 XML（有 zstd 就解），直接看结构；
  4. **结论**：一句话告诉你「字节在不在磁盘上」。

用法（**必须先停 bot**，因为它要手工查库——hook 不支持并发）：
    .venv/Scripts/python.exe probe_voice.py                 # 看快照 + 当前语音条
    .venv/Scripts/python.exe probe_voice.py --tag before    # 发语音**之前**存一次盘面快照
    （去微信里发一条语音条）
    .venv/Scripts/python.exe probe_voice.py --tag after     # 发完之后：自动 diff 出新增文件
"""
import binascii
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

STATE = os.path.join(BASE, "data", "_voice_probe.json")
# 语音条候选后缀（含各种可能的容器）
AUDIO_EXT = (".silk", ".slk", ".amr", ".aud", ".m4a", ".mp3", ".wav", ".ogg",
             ".aac", ".opus", ".dat")
_LOCK_PORT = 39001          # bot 的单实例锁；占着就说明它在跑


def _bot_running():
    import socket
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", _LOCK_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _audio_accounts():
    """微信 4.x 账号目录（data_root 下的 wxid_*_xxxx 那种）。"""
    try:
        import image_cache
        return image_cache.account_dirs()
    except Exception:
        return []


def disk_snapshot():
    """盘面快照：`msg\` 与 `cache\` 下的**所有**文件（路径 → 大小/mtime）。

    ⚠️ **不能只记「有音频后缀」的文件**：微信的媒体多数**没有后缀**
    （`msg\attach\<md5>\<月>\Rec\<哈希>\Dat\0\<md5>` 这种），只按后缀过滤的话
    「语音落地了」这件事根本看不见——第一版就是这么错的（只认扩展名）。
    宁可多记几万个缩略图，也不能漏掉那个无后缀的新文件。
    """
    out = {}
    for acct in _audio_accounts():
        for sub in ("msg", "cache"):
            base = os.path.join(acct, sub)
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for fn in files:
                    p = os.path.join(root, fn)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    out[p] = [st.st_size, int(st.st_mtime)]
    return out


def voice_temp_dirs():
    """`cache\\<月>\\Message\\<md5(会话)>\\VoiceTemp\\` —— **语音就落在这里**（2026-10-01 找到）。

    命名 `<local_id>_<create_time>`，与图片的 `Thumb\\<local_id>_<create_time>_thumb.jpg`
    完全对称。实测现状：有语音的会话这目录**存在但基本是空的**
    （19 个里几乎全空，只有一个剩两个 **0 字节**文件）。`filehelper` 连这个目录都没有
    ——因为它里面一条语音都没有过。

    所以「发一条语音 → 这个目录出现文件」就是判定「字节到底会不会落地」的**直接证据**，
    比全盘 diff 更聚焦（Temp 目录常常是明文/半明文，比加密 `.dat` 好解）。
    """
    out = []
    for acct in _audio_accounts():
        cache = os.path.join(acct, "cache")
        if not os.path.isdir(cache):
            continue
        for month in os.listdir(cache):
            mdir = os.path.join(cache, month, "Message")
            if not os.path.isdir(mdir):
                continue
            for chat in os.listdir(mdir):
                vt = os.path.join(mdir, chat, "VoiceTemp")
                if not os.path.isdir(vt):
                    continue
                files = []
                for r, _dd, ff in os.walk(vt):
                    for fn in ff:
                        p = os.path.join(r, fn)
                        try:
                            files.append((fn, os.path.getsize(p)))
                        except OSError:
                            pass
                out.append((os.path.join(month, "Message", chat), vt, files))
    return out


def rec_dirs():
    """所有 `Rec` 目录 + **递归**文件数。

    ⚠️ 必须递归数：第一版只数了 `os.listdir` 的条目数，得出过
    「82 个 Rec 全空」的**错误结论**——实际上 `Rec/<哈希>/Dat/0`、
    `Rec/<哈希>/Img/...` 这种子目录里躺着两万多个文件。
    只看顶层会把「有东西」看成「空的」，进而把整条结论带偏。
    """
    out = []
    for acct in _audio_accounts():
        for root, dirs, _files in os.walk(acct):
            for d in dirs:
                if d != "Rec":
                    continue
                p = os.path.join(root, d)
                n = 0
                for _r, _dd, ff in os.walk(p):
                    n += len(ff)
                out.append((p, n))
    return out


def _hexdump(raw, limit=96):
    b = bytes(raw)[:limit]
    return binascii.hexlify(b).decode() + ("" if len(raw) <= limit else f"…(+{len(raw)-limit}B)")


def _maybe_zstd(raw):
    """`message_content` 常见是 zstd 压缩（magic 28 B5 2F FD）。能解就解。"""
    if len(raw) < 4 or raw[:4] != b"\x28\xb5\x2f\xfd":
        return None
    try:
        import zstandard
    except ImportError:
        return "__NEED_ZSTANDARD__"
    try:
        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=1 << 20).decode(
            "utf-8", "replace")
    except Exception:
        return None


def decode_field(name, val):
    """把一个 DB 字段尽量还原成人能看的东西。"""
    if val is None:
        return None, "(NULL)"
    if isinstance(val, bytes):
        raw = val
        shown = _hexdump(raw)
    else:
        s = str(val)
        if len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s) and len(s) >= 8:
            try:
                raw = binascii.unhexlify(s)
                shown = _hexdump(raw)
            except Exception:
                return None, s[:200]
        else:
            return None, s[:400]
    z = _maybe_zstd(raw)
    if z == "__NEED_ZSTANDARD__":
        shown += "  ← 是 zstd，但没装 zstandard（pip install zstandard 就能看到内容）"
    elif z:
        shown += "  ← zstd 解出来：\n" + z[:800]
    return raw, shown


def voice_in_chat(client, talker, limit=5):
    """某个会话里的语音（`local_type=34`）。用户场景是「我发在文件传输助手里的」。"""
    import live_history
    out = []
    tbl = live_history._v4_table_for(talker)
    for db in live_history._v4_msg_dbs(client):
        sql = (f"SELECT local_id, local_type, create_time, real_sender_id FROM {tbl} "
               f"WHERE local_type = 34 ORDER BY create_time DESC LIMIT {int(limit)}")
        try:
            out += client.query_sql(db, sql)
        except Exception:
            continue
    return out


def find_voice_in_msg_tables(client, max_chats=30):
    """**在 `Msg_` 表里找语音**（`local_type=34`）。

    为什么不能只搜 fts：**语音和图片一样不进 fts**（实测四个分片
    `local_type` 只有 1 / 42 / 48 / appmsg 那几种，没有 34）。fts 那条路是结构性盲的。
    图片当初就是靠 `Msg_` 表找到的（见 docs/wechat4-dat-image-notes.md）。

    只查**最近活跃的会话**（`SessionTable` 取前 N 个），一圈查询数有界；
    每个会话一条带过滤的查询，不做全表排序。
    """
    import live_history
    out = []
    try:
        rows = client.query_sql("session.db",
                                "SELECT username, last_timestamp FROM SessionTable "
                                "ORDER BY last_timestamp DESC LIMIT " + str(int(max_chats)))
    except Exception as e:
        print(f"  取活跃会话失败：{str(e)[:70]}")
        return out
    dbs = live_history._v4_msg_dbs(client)
    for r in rows:
        talker = str(r.get("username") or "")
        if not talker:
            continue
        tbl = live_history._v4_table_for(talker)
        for db in dbs:
            sql = (f"SELECT local_id, local_type, create_time FROM {tbl} "
                   f"WHERE local_type = 34 ORDER BY create_time DESC LIMIT 3")
            try:
                hits = client.query_sql(db, sql)
            except Exception:
                continue
            for h in hits:
                out.append((talker, db, tbl, h))
    return out


def find_voice_rows(client, scan=8000):
    """在 fts 各分片最近 `scan` 行里找语音（local_type=34）。返回 [(分片, 行)]。

    用 `rowid > MAX-scan` 先缩小范围（纯索引范围扫描），**不做无过滤的全表排序**
    ——铁律见 CLAUDE.md。
    """
    out = []
    for t in _fts_tables(client):
        try:
            mx = client.query_sql("message_fts.db", f"SELECT MAX(rowid) AS m FROM {t}")
            top = int((mx[0].get("m") if mx else 0) or 0)
        except Exception as e:
            print(f"  {t}: 取 MAX(rowid) 失败 {str(e)[:60]}")
            continue
        sql = (f"SELECT rowid, session_id, local_type, sender_id, create_time, "
               f"message_local_id FROM {t} WHERE rowid > {top - int(scan)} "
               f"AND local_type = 34 LIMIT 10")
        try:
            rows = client.query_sql("message_fts.db", sql)
        except Exception as e:
            print(f"  {t}: 查询失败 {str(e)[:60]}")
            continue
        print(f"  {t}: 最近 {scan} 行里语音 {len(rows)} 条")
        for r in rows:
            out.append((t, r))
    return out


def _fts_tables(client):
    import live_history
    return live_history._v4_fts_tables(client)


def dump_msg_row(client, talker, local_id):
    """把 `Msg_` 里那一行**全部字段**打出来（语音的线索就在这里面）。"""
    import live_history
    import voice_msg
    tbl = live_history._v4_table_for(talker)
    dbs = live_history._v4_msg_dbs(client)
    print(f"\n  Msg_ 表：{tbl}（会话 {talker}，local_id={local_id}）")
    for db in dbs:
        sql = f"SELECT * FROM {tbl} WHERE local_id = {int(local_id)}"
        try:
            rows = client.query_sql(db, sql)
        except Exception as e:
            print(f"    {db}: 查询失败 {str(e)[:70]}")
            continue
        if not rows:
            continue
        print(f"    [{db}]")
        for row in rows:
            for k, v in row.items():
                raw, shown = decode_field(k, v)
                print(f"      {k} = {shown}")
                # 语音的 XML 一解出来就顺手解析：aeskey/时长/格式是后面解密要用的
                if k == "message_content" and raw:
                    z = _maybe_zstd(raw)
                    if z:
                        info = voice_msg.parse_voicemsg(z)
                        if info:
                            print("      ★ 这是语音条，解析结果：")
                            for kk in ("format_name", "voiceformat", "length_bytes",
                                       "duration_ms", "aeskey", "fromusername"):
                                print(f"          {kk} = {info.get(kk)}")
                            print(f"          key 可用 = {bool(info.get('key_bytes'))}"
                                  f"（16 字节；解密只用它，**不需要破全局密钥**）")
    return


def main(argv):
    print("=" * 66)
    print("语音条观测器（只读；查库前请先停 bot）")
    print("=" * 66)

    if _bot_running():
        print("❌ bot 正在跑（回环端口 39001 被占）。")
        print("   请先停掉它——bot 轮询 + 手工查库同时压 hook 会把微信搞崩。")
        return 2

    tag = None
    if "--tag" in argv:
        i = argv.index("--tag")
        tag = argv[i + 1] if len(argv) > i + 1 else None
        if tag not in ("before", "after"):
            print("--tag 只认 before / after")
            return 2

    print("\n【1】盘面快照（msg\\ + cache\\ 下**所有**文件，不限后缀）")
    now = disk_snapshot()
    recs = rec_dirs()
    print(f"  音频类文件：{len(now)} 个")
    empty_rec = sum(1 for _p, n in recs if n == 0)
    print(f"  Rec 目录：{len(recs)} 个，其中空的 {empty_rec} 个"
          f"{'  ← 全空 = 语音条没落盘' if recs and empty_rec == len(recs) else ''}")

    prev = None
    if os.path.isfile(STATE):
        try:
            prev = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            prev = None

    if tag == "before":
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump({"files": now, "recs": [[p, n] for p, n in recs]},
                  open(STATE, "w", encoding="utf-8"))
        print(f"  已存基线 → {STATE}（现在去发一条语音，然后跑 --tag after）")
        return 0

    if tag == "after" and prev:
        old = prev.get("files") or {}
        added = {p: v for p, v in now.items() if p not in old}
        changed = {p: v for p, v in now.items() if p in old and old[p] != v}
        print(f"\n【1b】与基线对比（{len(old)} → {len(now)}）")
        print(f"  新增 {len(added)} 个，变化 {len(changed)} 个")
        for p, v in list(added.items())[:20]:
            print(f"    + {p}  {v[0]}B")
        for p, v in list(changed.items())[:20]:
            print(f"    ~ {p}  {v[0]}B")
        if not added and not changed:
            print("    **一个文件都没多** → 这条语音条没有在磁盘上留下任何新文件。")
            print("    （对照：图片会往 cache\\...\\Thumb\\ 里落已解码缩略图，语音没有对应缓存。）")

    # ---- 2) 定时那侧无关，直接查 DB ----
    print("\n【2】DB：fts 里的语音条（local_type=34）")
    import yaml
    import live_history
    from aixed_api import AixedClient
    try:
        cfg = yaml.safe_load(open(os.path.join(BASE, "config.yaml"), encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    live_history.set_self_wxid(str(cfg.get("self_wxid") or ""))
    c = AixedClient(cfg.get("aixed_base_url") or "http://127.0.0.1:30001")

    print("  ping:", c.ping())
    found = find_voice_rows(c)

    print("\n【2b】DB：`Msg_` 表里的语音（**fts 里没有语音，必须查这里**）")
    msg_hits = find_voice_in_msg_tables(c)
    print(f"  最近活跃会话里找到 {len(msg_hits)} 条语音")

    # 用户的场景是「我发在文件传输助手里面的」——单独把控制会话挑出来看
    control = "filehelper"
    ch_hits = voice_in_chat(c, control)
    print(f"  控制会话（{control}）里的语音：{len(ch_hits)} 条")
    for h in ch_hits[:5]:
        print(f"    local_id={h.get('local_id')} create_time={h.get('create_time')}")
    msg_hits = msg_hits + [(control, "message_0.db", None, h) for h in ch_hits]

    if not found and not msg_hits:
        print("\n【3】结论")
        print("  fts 与 Msg_ 两条路都没找到语音条。**先发一条语音再跑本脚本**——")
        print("  没有真实样本时，任何解密代码都是在猜（见 docs/voice-msg-feasibility.md）。")
        return 1

    smap = live_history._v4_fts_session_map(c)
    for tab, r in found[:5]:
        talker = smap.get(int(r.get("session_id", -1)), f"session_{r.get('session_id')}")
        print(f"\n【3】fts 命中：{tab} 会话={talker} local_id={r.get('message_local_id')}")
        dump_msg_row(c, talker, r.get("message_local_id"))

    for talker, db, tbl, h in msg_hits[:5]:
        print(f"\n【3b】Msg_ 命中：会话={talker} 表={tbl} 分片={db} "
              f"local_id={h.get('local_id')} create_time={h.get('create_time')}")
        dump_msg_row(c, talker, h.get("local_id"))

    print("\n【4】结论")
    print("  上面 `message_content` 的 XML 就是语音条的全部线索（有没有文件名/密钥/时长）。")
    print("  把这段输出发回来，就能判断走哪条路（见 docs/voice-msg-feasibility.md）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
