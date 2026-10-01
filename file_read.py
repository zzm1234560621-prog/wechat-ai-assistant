"""读**别人发来的文件**（PDF / Office / 纯文本）。

微信 4.x 把收到的文件**明文**落在磁盘上：

    <微信数据目录>/<账号>/msg/file/<YYYY-MM>/<原文件名>

实测（2026-10-01）pdf / docx / xlsx / pptx 的魔数都对（`%PDF-1.3`、`PK\\x03\\x04`），
**不用解密** —— 和图片不是一回事（图片是加密的 `.dat`，见 docs/wechat4-dat-image-notes.md）。

文件名从消息里拿：文件消息的 `local_type` 低 32 位是 49（appmsg）、subtype 是 6，
把 `message_content` 解压出来 XML 里的 `<title>` **就是磁盘上的文件名**（实测逐字吻合）。

**安全边界**：只允许读 `msg/file/` 下面的文件。文件名来自数据库、等价于来自发文件的人，
所以对方能控制这个字符串 —— 必须挡住 `../` 和绝对路径，绝不能让模型或对方指定任意路径。
和 `send_image` 的白名单是同一个道理，只是方向相反（那个是别把本地文件发出去）。

**局限（回答用户时要如实说）**
* 只有**你收过**的文件才在本地；发出去的文件、没下载完的不在
* 同名文件会被微信存成 `xxx(1).pdf`，所以要按「同名或带 (N) 后缀」去找
* 扫描件 PDF（整页是图片）抽不出文字 —— 这种情况要如实说，**绝不编内容**
* 只支持下面 SUPPORTED 里列的后缀，其余的一律明说读不了
"""
import glob
import html
import os
import re
import zipfile

import image_cache

# 允许抽文本的后缀
SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx",
             ".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".htm"}

_MAX_BYTES = 30 * 1024 * 1024      # 超过就不读（PDF 抽出来也多半没意义，还慢）
_MAX_ZIP_UNPACK = 200 * 1024 * 1024  # 防 zip 炸弹


def _cfg(cfg=None):
    f = ((cfg or {}).get("file") or {})
    try:
        mb = int(f.get("max_bytes") or _MAX_BYTES)
    except (TypeError, ValueError):
        mb = _MAX_BYTES
    try:
        mc = int(f.get("max_chars") or 20000)
    except (TypeError, ValueError):
        mc = 20000
    return mb, mc


def files_roots():
    """所有账号的 msg/file 目录（通常是 1 个）。"""
    out = []
    for acc in image_cache.account_dirs():
        d = os.path.join(acc, "msg", "file")
        if os.path.isdir(d):
            out.append(d)
    return out


def _safe_name(name):
    """只收「纯文件名」：不许有目录分隔符、不许 .. 、不许盘符。"""
    name = str(name or "").strip().replace("\\", "/")
    if not name or "/" in name or name.startswith("."):
        return None
    if os.path.splitdrive(name)[0]:
        return None
    return name


def locate(name):
    """按文件名找磁盘上的文件。返回路径；找不到返回 None。

    微信遇到重名会存成 `xxx(1).pdf`，所以先精确找，再按「主干 + 任意后缀 + 同扩展名」退一步。
    """
    safe = _safe_name(name)
    if not safe:
        return None
    norm = os.path.normcase
    for root in files_roots():
        exact = glob.glob(os.path.join(root, "*", safe))
        for p in exact:
            if norm(os.path.basename(p)) == norm(safe):
                return p
    # 退一步：主干相同、扩展名相同（覆盖 (1) 这类重名后缀）
    stem, ext = os.path.splitext(safe)
    if not ext:
        return None
    for root in files_roots():
        # 用 glob 的转义比较麻烦，直接列目录比一比
        for month in os.listdir(root):
            d = os.path.join(root, month)
            if not os.path.isdir(d):
                continue
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for fn in entries:
                if os.path.splitext(fn)[1].lower() != ext.lower():
                    continue
                base = os.path.splitext(fn)[0]
                if base == stem or re.fullmatch(re.escape(stem) + r"\(\d+\)", base):
                    return os.path.join(d, fn)
    return None


# ---------------- 各格式抽文本 ----------------

def _xml_text(xml):
    """把所有 <xxx:t> 里的文字抽出来（docx/pptx 都用这个）。"""
    out = []
    for m in re.finditer(r"<(?:w|a):t(?:\s+[^>]*)?>(.*?)</(?:w|a):t>", xml, re.S):
        out.append(html.unescape(m.group(1)))
    return "".join(out)


def _docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    lines = []
    for para in re.findall(r"<w:p(?:\s[^>]*)?>.*?</w:p>", xml, re.S):
        t = _xml_text(para).strip()
        if t:
            lines.append(t)
    return "\n".join(lines)


def _pptx(path):
    out = []
    with zipfile.ZipFile(path) as z:
        slides = sorted(n for n in z.namelist()
                        if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        for i, n in enumerate(slides, 1):
            xml = z.read(n).decode("utf-8", "replace")
            texts = []
            for para in re.findall(r"<a:p(?:\s[^>]*)?>.*?</a:p>", xml, re.S):
                t = _xml_text(para).strip()
                if t:
                    texts.append(t)
            if texts:
                out.append(f"— 第 {i} 页 —\n" + "\n".join(texts))
    return "\n\n".join(out)


def _xlsx(path):
    """把每个 sheet 渲染成 TSV。只取前若干行，表格动辄几万行，全抽没意义。"""
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
        # sheet 名 -> 文件
        book = {}
        if "xl/workbook.xml" in names:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rels = {}
            if "xl/_rels/workbook.xml.rels" in names:
                r = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
                for rel in r:
                    rels[rel.get("Id")] = rel.get("Target")
            for sh in wb.iter(f"{NS}sheet"):
                rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                tgt = rels.get(rid, "")
                if tgt and not tgt.startswith("xl/"):
                    tgt = "xl/" + tgt.lstrip("/")
                book[sh.get("name") or "Sheet"] = tgt
        if not book:
            for n in names:
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n):
                    book[n.rsplit("/", 1)[-1]] = n
        out = []
        for name, target in book.items():
            if target not in names:
                continue
            root = ET.fromstring(z.read(target))
            rows = []
            for row in root.iter(f"{NS}row"):
                cells = []
                for c in row.findall(f"{NS}c"):
                    v = c.find(f"{NS}v")
                    is_ = c.find(f"{NS}is")
                    if c.get("t") == "s" and v is not None:
                        try:
                            cells.append(shared[int(v.text)])
                        except (ValueError, IndexError):
                            cells.append("")
                    elif is_ is not None:
                        cells.append("".join(t.text or "" for t in is_.iter(f"{NS}t")))
                    else:
                        cells.append(v.text if v is not None and v.text else "")
                if any(x.strip() for x in cells):
                    rows.append("\t".join(cells))
                if len(rows) >= 200:
                    rows.append("…（表格太长，只取前 200 行）")
                    break
            if rows:
                out.append(f"— 工作表「{name}」—\n" + "\n".join(rows))
        return "\n\n".join(out)


def _pdf(path):
    import pypdf
    reader = pypdf.PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")          # 很多 PDF 只是「空密码加密」
        except Exception:
            return None, "这个 PDF 有密码，读不了。"
    pages = []
    for i, page in enumerate(reader.pages[:40], 1):
        try:
            t = (page.extract_text() or "").strip()
        except Exception:
            t = ""
        if t:
            pages.append(f"— 第 {i} 页 —\n{t}")
    if not pages:
        return None, ("这个 PDF 抽不出文字。多半是**扫描件**（整页是图片），"
                      "或者用了少见的字体编码——我读不了，不编。")
    return "\n\n".join(pages), None


def _plain(path, ext):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _looks_garbled(text):
    """抽出来的东西是不是乱码。宁可说读不了，也不把乱码喂给模型。"""
    if not text or len(text) < 20:
        return True
    good = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
    return good / len(text) < 0.75


def extract(path, cfg=None):
    """抽文本。返回 (文本, 提示语)；失败时文本为 None、提示语说明原因。"""
    max_bytes, max_chars = _cfg(cfg)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, "读不到这个文件。"
    if size > max_bytes:
        return None, f"文件太大（{size/1048576:.1f}MB），我不读这么大的。"

    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED:
        return None, f"我暂时只认 {', '.join(sorted(SUPPORTED))} 这几种，{ext or '这种'} 读不了。"

    try:
        if ext == ".pdf":
            text, err = _pdf(path)
            if err:
                return None, err
        elif ext == ".docx":
            text = _docx(path)
        elif ext == ".xlsx":
            text = _xlsx(path)
        elif ext == ".pptx":
            text = _pptx(path)
        else:
            text = _plain(path, ext)
    except zipfile.BadZipFile:
        return None, "这个文件损坏了（压缩包打不开）。"
    except Exception as e:
        return None, f"解析失败：{type(e).__name__}: {str(e)[:120]}"

    text = (text or "").strip()
    if not text:
        return None, "这个文件里没有可提取的文字。"
    if _looks_garbled(text):
        return None, ("抽出来的内容像是乱码（可能是特殊字体编码或扫描件），"
                      "我不拿它当内容用。")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（太长，只取前 {max_chars} 字）"
    return text, None


if __name__ == "__main__":
    # 纯逻辑自测，不碰微信： .venv/Scripts/python.exe file_read.py
    def chk(cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            raise SystemExit(1)

    print("路径安全（文件名来自数据库 = 来自发文件的人，必须挡住）:")
    for bad in ["../../windows/system.ini", "..\\..\\x", "C:\\Windows\\win.ini",
                "/etc/passwd", "", "   ", ".hidden"]:
        chk(_safe_name(bad) is None, f"挡住 {bad!r}")
    for good in ["报告.pdf", "a b.docx", "中文 名字(1).xlsx"]:
        chk(_safe_name(good) == good, f"放行 {good!r}")

    print("\n乱码检测:")
    chk(_looks_garbled(""), "空文本算不可用")
    chk(_looks_garbled("ab"), "太短算不可用")
    chk(not _looks_garbled("这是一段正常的中文文本，用来测试乱码检测是否工作正常。"),
        "正常中文判为可用")
    chk(_looks_garbled("\x00\x01\x02\x03\x04\x05\x06\x07" * 10), "控制字符判为乱码")

    print("\n后缀白名单:")
    chk(".pdf" in SUPPORTED and ".xyz" not in SUPPORTED, "只认白名单里的后缀")

    print("\n真实文件（磁盘上有就抽，没有就跳过）:")
    tests = [("2023 ASOP marking scheme.docx", ".docx"),
             ("2024 Physics ASOP marking scheme.pdf", ".pdf")]
    for name, ext in tests:
        p = locate(name)
        if not p:
            print(f"  skip  磁盘上没有 {name}")
            continue
        text, err = extract(p)
        if err:
            print(f"  FAIL  {name}: {err}")
            raise SystemExit(1)
        chk("<w:" not in text, f"{name} 抽出的文本没有残留 XML 标记")
        print(f"        抽到 {len(text)} 字，前 60 字：{text[:60]!r}")

    print("\n全部通过。")
