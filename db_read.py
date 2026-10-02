"""数据库文件（`.sqlite` / `.db` / `.sqlite3`）→ 文字（P3）。

规矩只有两条，但都是硬的：

1. **只读、绝不允许写**：连接串写死 `mode=ro` + `immutable=1`
   （`file:...?mode=ro&immutable=1`）。别人发的库文件对我们是**不可信输入**，
   给它写权限等于让它带触发器/虚拟表反咬一口；`immutable=1` 还顺手避免在对方目录里
   建 `-wal`/`-shm`。**只读之外的一切（改表、建索引）都不做。**
2. **读多少说多少**：表名、行数、抽几行都**明说**（哪张表只看了前 N 行、哪张表太大没细看），
   **绝不把"抽了 20 行"说成"整个库读完了"**。

只认识真 SQLite：先按文件头 `SQLite format 3\\0` 判，不是就当普通文件走别的路
（`.db` 也可能是别的格式，**不许硬按 sqlite 打开然后报一个看不懂的错**）。
"""
import os
import sqlite3

DB_EXT = (".sqlite", ".sqlite3", ".db", ".db3")
_MAGIC = b"SQLite format 3\x00"

# 这些表/视图是 SQLite 自己的内部结构，给模型看只会占地方
_SKIP_TABLES = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4", "sqlite_master"}


def is_sqlite(path):
    """按**文件头**判（不是按后缀）——`.db` 也可能是别的格式。"""
    try:
        with open(path, "rb") as f:
            return f.read(16) == _MAGIC
    except OSError:
        return False


def _cfg(cfg):
    sec = ((cfg or {}).get("db") or {})
    return dict(sec) if isinstance(sec, dict) else {}


def _int_opt(value, default, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def max_tables(cfg=None):
    return _int_opt(_cfg(cfg).get("max_tables"), 20, 1, 200)


def max_rows(cfg=None):
    return _int_opt(_cfg(cfg).get("max_rows"), 20, 0, 500)


def max_chars(cfg=None):
    return _int_opt(_cfg(cfg).get("max_chars"), 20000, 500, 200000)


def _cell(v):
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return f"<{len(v)} 字节二进制>"
    s = str(v)
    return s if len(s) <= 120 else s[:120] + "…"


def read_db(path, cfg=None):
    """读一个 SQLite 库。返回 `(文本, 错误)`。**只读、绝不写。**"""
    if not os.path.isfile(path):
        return None, "这个库不在本机"
    if not is_sqlite(path):
        return None, ("这个文件不是 SQLite 库（文件头不对）。"
                      "如果它是别的数据库格式，我读不了。")

    uri = "file:" + path.replace("?", "%3f").replace("#", "%23") + "?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as e:
        return None, f"打不开这个库（只读方式）：{type(e).__name__}: {str(e)[:120]}"
    try:
        cur = con.cursor()
        cur.execute("SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('table','view') ORDER BY name")
        items = [(t, n) for t, n in cur.fetchall() if n not in _SKIP_TABLES]
        if not items:
            return "（这个库里没有任何表/视图）", None

        lim_t, lim_r, lim_c = max_tables(cfg), max_rows(cfg), max_chars(cfg)
        lines = [f"—— SQLite 库：{len(items)} 张表/视图"
                 + (f"（只细看前 {lim_t} 张）" if len(items) > lim_t else "") + " ——"]
        for kind, name in items[:lim_t]:
            safe = name.replace('"', '""')
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{safe}"')
                total = cur.fetchone()[0]
            except sqlite3.Error as e:
                lines.append(f"\n【{kind} {name}】读不了：{type(e).__name__}: {str(e)[:80]}")
                continue
            head = f"\n【{kind} {name}】共 {total} 行"
            if lim_r == 0 or not total:
                lines.append(head + "（db.max_rows=0，只看行数）")
                continue
            try:
                cur.execute(f'SELECT * FROM "{safe}" LIMIT {lim_r}')
                rows = cur.fetchall()
                cols = [d[0] for d in (cur.description or [])]
            except sqlite3.Error as e:
                lines.append(head + f"（取样失败：{type(e).__name__}）")
                continue
            got = [f"  {kind} {name} 的前 {len(rows)} 行"
                   + (f"（共 {total} 行，**没全读**）" if total > len(rows) else "") + "：",
                   "  " + " | ".join(cols)]
            for r in rows:
                got.append("  " + " | ".join(_cell(v) for v in r))
            block = "\n".join(got)
            lines.append(head + "\n" + block)
        text = "\n".join(lines)
        if len(text) > lim_c:
            text = text[:lim_c] + (f"\n\n…（这个库的文字超过 db.max_chars={lim_c}，"
                                   f"上面是前 {lim_c} 字；**没说全读完了**）")
        if len(items) > lim_t:
            text += (f"\n\n（还有 {len(items) - lim_t} 张表没看：超过 db.max_tables={lim_t}）")
        return text, None
    except sqlite3.Error as e:
        return None, f"读库失败：{type(e).__name__}: {str(e)[:150]}"
    finally:
        try:
            con.close()
        except Exception:
            pass
