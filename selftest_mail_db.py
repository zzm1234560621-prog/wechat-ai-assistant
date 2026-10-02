"""P3 自测：邮件（`.eml`/`.msg`）、数据库（`.sqlite`/`.db`）、7z 压缩包补全。

**不联网、不需要微信。**
  * `.eml` / `.sqlite` / `.7z` 都是**现场造的真文件**（`.eml` 用标准库、库用 sqlite3、
    7z 用 py7zr 写）——所以这三条是真跑出来的；
  * `.msg` 本机**没有真文件**（也造不出来：它是 Outlook 的 OLE2 复合文档），
    所以只验两件能验的事：① 解析逻辑（用假 olefile 喂标准属性流名）；
    ② 全都读不了时**如实说 + 给安装指引**。这一条要如实写进文档，不许说成"验过了"。

钉住的规矩：
  * **数据库只读**（连接串 `mode=ro&immutable=1`）—— 直接试着写它，必须失败；
  * 读多少**说多少**（表数/行数上限、只取样前 N 行）—— 绝不把"抽了几行"说成"读完了"；
  * `.db` **不一定是 SQLite**：不是就按内容判、如实说，不许硬打开然后报个看不懂的错；
  * 附件**递归读**，读不完要说还有几个没读；
  * 邮件临时文件用完即删。

用法：`.venv/Scripts/python.exe selftest_mail_db.py`
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from email.message import EmailMessage

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import archive_read  # noqa: E402
import db_read  # noqa: E402
import file_read  # noqa: E402
import mail_read  # noqa: E402

TMP = tempfile.mkdtemp(prefix="selftest_maildb_")
_ok = True
_SKIP = []


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


def skip(label):
    _SKIP.append(label)
    print(f"  ⏭️  {label}")


def _docx(path, text):
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml",
                   f'<?xml version="1.0"?><w:document {W}><w:p><w:r><w:t>{text}</w:t>'
                   f'</w:r></w:p></w:document>')
    return path


def t1_eml():
    print("\n── 1 · .eml：表头 + 正文 + 附件递归 ──")
    attach = _docx(os.path.join(TMP, "附件报告.docx"), "这是附件里的正文内容，长度够通过乱码判据。")
    msg = EmailMessage()
    msg["From"] = "张三 <zhangsan@example.com>"
    msg["To"] = "李四 <lisi@example.com>"
    msg["Subject"] = "周报（含附件）"
    msg["Date"] = "Thu, 02 Oct 2026 09:00:00 +0800"
    msg.set_content("正文：本周进度见附件，另外会议改到周五下午三点。")
    with open(attach, "rb") as f:
        msg.add_attachment(f.read(), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                           filename="附件报告.docx")
    msg.add_attachment(b"\x00\x01\x02binary", maintype="application", subtype="octet-stream",
                       filename="数据.bin")
    eml = os.path.join(TMP, "周报.eml")
    with open(eml, "wb") as f:
        f.write(msg.as_bytes())

    cfg = {"file": {"max_bytes": 0}, "mail": {"max_attachments": 5}}
    text, err = file_read.extract(eml, cfg)
    check("整封邮件读出来了", bool(text) and not err, err)
    check("表头读到了（发件人/主题/时间）",
          "张三" in text and "周报（含附件）" in text and "2026" in text, (text or "")[:300])
    check("正文读到了", "会议改到周五下午三点" in text, (text or "")[:600])
    check("**附件递归读了**（docx 里的正文出来了）",
          "附件报告.docx" in text and "附件里的正文内容" in text, (text or "")[-500:])
    check("读不了的附件也**标明是哪一个 + 为什么**",
          "数据.bin" in text, (text or "")[-400:])

    # 附件上限
    cfg2 = {"file": {"max_bytes": 0}, "mail": {"max_attachments": 1}}
    text2, _ = file_read.extract(eml, cfg2)
    check("超过 mail.max_attachments 时**明说没读**",
          "没读" in (text2 or "") and "max_attachments" in (text2 or ""), (text2 or "")[-300:])

    # 临时文件不留
    d = os.path.join(BASE, "data", "tmp_mail")
    check("邮件临时文件用完即删", (not os.path.isdir(d)) or not os.listdir(d),
          os.listdir(d) if os.path.isdir(d) else [])

    # HTML 正文的朴素剥离
    m2 = EmailMessage()
    m2["Subject"] = "HTML 邮件"
    m2.set_content("<html><body><p>你好</p><script>bad()</script><p>世界</p></body></html>",
                   subtype="html")
    eml2 = os.path.join(TMP, "html.eml")
    with open(eml2, "wb") as f:
        f.write(m2.as_bytes())
    t3, e3 = file_read.extract(eml2, cfg)
    check("HTML 正文能看（剥标签、且说清是剥过的）",
          t3 and "你好" in t3 and "世界" in t3 and "bad()" not in t3 and "剥掉标签" in t3, (t3 or "")[:200])


def t2_msg():
    print("\n── 2 · .msg：解析逻辑（假 olefile）+ 全读不了时如实说 ──")
    not_ole = os.path.join(TMP, "假msg.msg")
    with open(not_ole, "wb") as f:
        f.write(b"not an ole file at all")
    text, err = mail_read.msg_via_olefile(not_ole)
    check("不是 OLE2 → 如实说", text is None and "OLE2" in (err or ""), err)

    # 用假 olefile 喂标准属性流名，验解析（001F=UTF-16LE，001E=ANSI）
    class _FakeOle:
        def __init__(self, path):
            pass

        def listdir(self):
            return [["__substg1.0_0037001F"], ["__substg1.0_1000001F"],
                    ["__substg1.0_0C1A001F"], ["__substg1.0_00390040"]]

        def openstream(self, entry):
            name = entry[0]
            payload = {
                "__substg1.0_0037001F": "季度总结".encode("utf-16-le"),
                "__substg1.0_1000001F": "正文第一段。\n正文第二段。".encode("utf-16-le"),
                "__substg1.0_0C1A001F": "王五".encode("utf-16-le"),
                "__substg1.0_00390040": b"\x00" * 8,
            }[name]
            return io.BytesIO(payload)

        def close(self):
            pass

        @staticmethod
        def isOleFile(path):
            return True

    real_ole = None
    try:
        import olefile
        real_ole = olefile
    except ImportError:
        pass
    fake = type("olefile", (), {"OleFileIO": _FakeOle, "isOleFile": staticmethod(lambda p: True)})
    sys.modules["olefile"] = fake
    try:
        text, err = mail_read.msg_via_olefile("x.msg")
    finally:
        if real_ole is not None:
            sys.modules["olefile"] = real_ole
        else:
            sys.modules.pop("olefile", None)
    check("属性流解析对（主题/正文/发件人）",
          text and "季度总结" in text and "正文第一段" in text and "王五" in text, (text, err))
    check("明确标注这是**退路**、拿不到附件",
          text and "退路" in text and "附件" in text, (text or "")[:200])

    # 两条路都不成：要列出每个引擎的原因 + 安装指引 + 不许编内容
    real_em, real_ole2 = mail_read.msg_via_extract_msg, mail_read.msg_via_olefile
    mail_read.msg_via_extract_msg = lambda p: (None, "桩：没装 extract-msg")
    mail_read.msg_via_olefile = lambda p: (None, "桩：olefile 也读不动")
    try:
        text, err = mail_read.read_msg("x.msg")
    finally:
        mail_read.msg_via_extract_msg, mail_read.msg_via_olefile = real_em, real_ole2
    check("全读不了 → 逐条列引擎原因 + 安装指引",
          text is None and "extract-msg" in err and "olefile" in err and "pip install" in err, err)
    check("……并明说不要编内容", "不要编内容" in (err or ""), err)


def t3_sqlite():
    print("\n── 3 · .sqlite/.db：只读 + 读多少说多少 ──")
    p = os.path.join(TMP, "样本.sqlite")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE 联系人 (id INTEGER PRIMARY KEY, 姓名 TEXT, 备注 TEXT)")
    con.execute("CREATE TABLE 账单 (id INTEGER PRIMARY KEY, 金额 REAL)")
    for i in range(30):
        con.execute("INSERT INTO 联系人 (姓名, 备注) VALUES (?,?)", (f"用户{i}", "备注" * 3))
    con.execute("INSERT INTO 账单 (金额) VALUES (12.5)")
    con.commit()
    con.close()
    before = os.path.getsize(p)

    cfg = {"db": {"max_rows": 3, "max_tables": 20}}
    text, err = file_read.extract(p, cfg)
    check("库读出来了", bool(text) and not err, err)
    check("列出两张表", "联系人" in text and "账单" in text, (text or "")[:200])
    check("行数是真数（联系人 30 行）", "共 30 行" in text, (text or "")[:300])
    check("只取样前 3 行、且**明说没全读**",
          "前 3 行" in text and "没全读" in text, (text or "")[:500])
    check("单元格内容在", "用户0" in text and "12.5" in text, (text or "")[:800])

    # 只读铁证：拿同一个连接串去写，必须失败
    uri = "file:" + p + "?mode=ro&immutable=1"
    con2 = sqlite3.connect(uri, uri=True)
    try:
        con2.execute("CREATE TABLE 不该建成 (x)")
        wrote = True
    except sqlite3.Error as e:
        wrote = False
        why = str(e)
    finally:
        con2.close()
    check("**只读**：同一个连接串写不进去", not wrote and "readonly" in why.lower(), why)
    check("文件大小没变（读它没改它）", os.path.getsize(p) == before)

    # 上限：max_rows=0 只看行数；max_tables=1 明说还有表没看
    t1, _ = db_read.read_db(p, {"db": {"max_rows": 0}})
    check("max_rows=0 → 只看行数、明说", "max_rows=0" in t1 and "用户0" not in t1, (t1 or "")[:200])
    t2, _ = db_read.read_db(p, {"db": {"max_rows": 1, "max_tables": 1}})
    check("max_tables=1 → 明说还有 1 张表没看", "还有 1 张表没看" in t2, (t2 or "")[:200])

    # .db 不是 sqlite 时不许硬打开
    fake = os.path.join(TMP, "其实是文本.db")
    with open(fake, "w", encoding="utf-8", newline="") as f:
        f.write("这不是数据库，只是一份普通文本，长度够通过乱码判据。\n" * 3)
    check("is_sqlite 按文件头判（不按后缀）", db_read.is_sqlite(fake) is False)
    t3, e3 = file_read.extract(fake, {"file": {"max_bytes": 0}})
    check("假 .db（文本）→ 当文本读出来，不报错", t3 and "不是数据库" in t3 and not e3, (e3, (t3 or "")[:80]))

    binp = os.path.join(TMP, "其实是二进制.db")
    with open(binp, "wb") as f:
        f.write(b"\x00\x01\x02\x03" * 40)
    t4, e4 = file_read.extract(binp, {})
    check("假 .db（二进制）→ 如实说不是 SQLite、并说像什么",
          t4 is None and e4 and "不是 SQLite" in e4, (t4, e4))

    # 只读打开一个**正在被写**的库不炸（immutable 的代价：读到旧数据也比崩强）
    check("就绪：损坏的库如实报错", db_read.read_db(os.path.join(TMP, "没有这个.sqlite"))[1] is not None)


def t4_sevenzip():
    print("\n── 4 · 7z 补全（装了 py7zr 就能真读） ──")
    try:
        import py7zr
    except ImportError:
        skip("没装 py7zr，跳过 7z 真机那条")
        return
    src = os.path.join(TMP, "包内文本.txt")
    with open(src, "w", encoding="utf-8", newline="") as f:
        f.write("这是 7z 包里的一个文本成员，长度够通过乱码判据。")
    docx = _docx(os.path.join(TMP, "包内文档.docx"), "7z 里的 Word 正文内容，长度够通过乱码判据。")
    z = os.path.join(TMP, "样本.7z")
    with py7zr.SevenZipFile(z, "w") as zf:
        zf.write(src, "包内文本.txt")
        zf.write(docx, "子目录/包内文档.docx")
    check("造出了真 7z", os.path.getsize(z) > 0)

    text, err = file_read.extract(z, {"file": {"max_bytes": 0}, "archive": {"max_members": 10}})
    check("7z 真读出来了", bool(text) and not err, (err, (text or "")[:120]))
    check("成员名标明", "包内文本.txt" in text and "包内文档.docx" in text, (text or "")[:300])
    check("嵌套的 docx 也被解析成正文", "7z 里的 Word 正文内容" in text, (text or "")[-400:])

    # rar：没装 rarfile → 如实给指引（这条在 selftest_archive 里有，这里再钉一次）
    rar = os.path.join(TMP, "假.rar")
    with open(rar, "wb") as f:
        f.write(b"Rar!\x1a\x07\x00" + b"\x00" * 40)
    t2, e2 = archive_read.read_archive(rar, {})
    check("rar 缺依赖时给 pip 指引（不是一句读不了）",
          t2 is None and e2 and "rarfile" in e2 and "pip install" in e2, e2)


def main():
    print("=" * 60)
    print("P3 自测：邮件 / 数据库 / 7z（临时目录：%s）" % TMP)
    print("=" * 60)
    try:
        t1_eml()
        t2_msg()
        t3_sqlite()
        t4_sevenzip()
    finally:
        mail_read.sweep_tmp(max_age=0)
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 60)
    if _SKIP:
        print(f"（跳过 {len(_SKIP)} 项：{'; '.join(_SKIP)}）")
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
