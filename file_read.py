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
import io
import os
import re
import unicodedata
import zipfile

import executor
import image_cache

# 允许抽文本的后缀
SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx",
             ".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".htm"}

# 音频另走一条路（转写），**不进 SUPPORTED**：那是「文档解析」的白名单。
# 一并 import 是为了分派时用；audio_read 反过来不 import 本模块的顶层符号（只在
# 取字节上限时函数内 import），所以没有循环导入。
import audio_read                                    # noqa: E402

_MAX_BYTES = 30 * 1024 * 1024      # 超过就不读（PDF 抽出来也多半没意义，还慢）
_MAX_ZIP_UNPACK = 200 * 1024 * 1024  # 防 zip 炸弹：**解压后**的累计上限（file.max_bytes 只管压缩包大小）
# XML 里出现这些就是「有实体声明」，见 _xml_bytes 里为什么直接拒绝
_XML_ENTITY_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")
# 「可疑区间里到底几个字母才敢判乱码」：一两个可能是 café/résumé 这类正常西文
# （实测那句含 café/naïve/résumé 的英文里就有 3 个），中文被误解时是整片整片的
# （'Ŀ¼Ŀ¼Ŀ¼' 一屏），所以门槛取 5
_TRAP_LETTERS = 5
# 光看个数还不够：模型/用户发来的英文里偶尔会出现十来个重音字母（人名、术语），
# 而「整片汉字被 utf-8 误解」时可疑字符是**成片**的。再看占字母总数的比例：
# 实测那句 café/naïve/résumé 的英文是 3/145 ≈ 2%，纯汉字被误解时 >50%。
_TRAP_RATIO = 0.15


class _ZipTooLarge(Exception):
    """解压后超过上限（zip 炸弹防护）。单独一类，好让上层给一句人话。"""


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


def _unpack_limit(cfg=None):
    """Office 文件解压后的总上限（防 zip 炸弹）。

    `_MAX_ZIP_UNPACK` 是绝对封顶；再叠一层「本次输入上限的 3 倍」是给用户自配的
    `file.max_bytes` 用的：正常 docx/xlsx 解压后往往只有压缩包的几倍，
    而 zip 炸弹动辄解出几百上千倍——这一刀正好砍在中间。
    **这是防炸弹的兜底，不是给正常文件设的门槛**。
    """
    mb, _mc = _cfg(cfg)
    return min(_MAX_ZIP_UNPACK, max(1, mb * 3))


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


def _zip_budget(cap):
    """返回 (读一个成员的函数, 已用字节数的查询函数)。

    **为什么不能只信 entry 自己声明的大小**：`file.max_bytes` 卡的是**压缩包**体积，
    而 docx/pptx/xlsx 是 zip——别人发来一个几十 KB 的包就能解出几十 GB（zip 炸弹）。
    所以这里两道关，**顺序不能反**：

    1. 按「剩余额度 + 1」去读，**实际读出来**的字节累加封顶——多读出来的那 1 字节
       就是「声明值在撒谎」的硬证据（声明 1KB、实际 1GB 这种）。声明值不可信，
       实际读出来的数才作数；
    2. 读之前先看声明值会不会超（多数炸弹在这一步就露了，省得白解一次）。

    超出时抛 `_ZipTooLarge`，由上层如实说明；**绝不截断后当成正常内容继续用**。
    """
    used = [0]

    def used_bytes():
        return used[0]

    def read(z, name, limit=None):
        """读 zip 里的一个成员。name 可以是名字、正则（取全部匹配）、或谓词。"""
        if isinstance(name, re.Pattern):
            names = sorted(n for n in z.namelist() if name.fullmatch(n))
        elif callable(name):
            names = sorted(n for n in z.namelist() if name(n))
        else:
            names = [name] if name in z.namelist() else []
        if not names:
            return b""
        room = cap if limit is None else min(cap, int(limit))
        out = []
        for n in names:
            try:
                declared = z.getinfo(n).file_size or 0
            except KeyError:
                continue
            if used[0] + declared > room:
                raise _ZipTooLarge(
                    f"解压后太大：已解出约 {used[0] / 1048576:.1f}MB，"
                    f"再加这个成员声明的 {declared / 1048576:.1f}MB 会超过上限 "
                    f"{room / 1048576:.0f}MB")
            remain = room - used[0] + 1        # +1：多读出来的那一字节就是超限的证据
            try:
                with z.open(n) as f:
                    data = f.read(remain)
            except zipfile.BadZipFile as e:
                # 成员解到一半被校验拦下（截断/伪造大小都会走到这里）——如实报，不静默
                raise _ZipTooLarge(f"压缩包里的「{n}」解不开或解到一半就坏了：{e}")
            if len(data) > remain - 1:
                raise _ZipTooLarge(
                    f"压缩包里的「{n}」实际解出的字节超过它自己声明的大小"
                    f"（超过剩余额度 {remain - 1} 字节），判定为解压炸弹")
            used[0] += len(data)
            out.append(data)
        return b"".join(out)

    return read, used_bytes


def _xml_bytes(raw, what="这段 XML"):
    """office 里的 XML 文本：先挡 XML 实体声明，再解码。

    **为什么不用 defusedxml**：那是个新依赖，而且本项目对第三方依赖是能不加就不加。
    `xml.etree` 从 Python 3.7 起就不展开外部实体了，但仍然会展开 `<!DOCTYPE` 里**内部**
    定义的实体（billion laughs / 十亿笑声），几 KB 的 XML 能撑出几 GB——这和 zip 炸弹
    是同一种攻击。既然 docx/xlsx/pptx 里正常的内容根本不会有文档类型声明，那就
    **一刀切：见到 `<!DOCTYPE` 或 `<!ENTITY` 直接拒绝并如实报错**，比去数嵌套层数可靠得多。
    （解析报错也如实往上抛，不吞。）
    """
    for pat in _XML_ENTITY_MARKERS:
        # 整份都扫：XML 声明前面允许有注释/空白，DOCTYPE 不一定在开头。
        # 两个定长子串查找，代价可忽略。
        if pat in raw:
            raise ValueError(f"{what}里有 XML 文档类型/实体声明（<!DOCTYPE / <!ENTITY），"
                             f"可能有实体膨胀攻击，出于安全不解析这个文件。")
    return raw.decode("utf-8", "replace")


def _under_allowed(real, roots):
    """real（已 realpath 解析）是否真的落在某个允许的目录里。

    **为什么要这一道**：`_safe_name` 只能挡字符串层面的 `..` / 绝对路径 / UNC，
    挡不住目录联接（junction）或符号链接——微信数据目录里若有人塞一个链接指向别处，
    字符串看着还在白名单里，打开却读到了白名单外的文件。
    判据用 `commonpath`：跨盘符时它会抛 ValueError，那也一律视为不通过。
    """
    for root in roots:
        try:
            if os.path.commonpath([real, root]) == root:
                return True
        except ValueError:      # 不同盘符，commonpath 直接报错 = 不通过
            continue
    return False


def locate(name):
    """按文件名找磁盘上的文件。返回路径；找不到返回 None。

    微信遇到重名会存成 `xxx(1).pdf`，所以先精确找，再按「主干 + 任意后缀 + 同扩展名」退一步。
    """
    safe = _safe_name(name)
    if not safe:
        return None
    roots = files_roots()
    # 允许的根自己也要 realpath：微信数据目录本身可能就是软链/重定向（OneDrive 之类），
    # 拿没解析过的路径去比会把正常文件全判成越界。
    real_roots = [os.path.realpath(r) for r in roots]
    norm = os.path.normcase
    for root in roots:
        exact = glob.glob(os.path.join(root, "*", safe))
        for p in exact:
            if norm(os.path.basename(p)) != norm(safe):
                continue
            real = os.path.realpath(p)
            if _under_allowed(real, real_roots):
                return real
    # 退一步：主干相同、扩展名相同（覆盖 (1) 这类重名后缀）
    stem, ext = os.path.splitext(safe)
    if not ext:
        return None
    for root in roots:
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
                    real = os.path.realpath(os.path.join(d, fn))
                    if _under_allowed(real, real_roots):
                        return real
    return None


# ---------------- 各格式抽文本 ----------------

def _xml_text(xml):
    """把所有 <xxx:t> 里的文字抽出来（docx/pptx 都用这个）。"""
    out = []
    for m in re.finditer(r"<(?:w|a):t(?:\s+[^>]*)?>(.*?)</(?:w|a):t>", xml, re.S):
        out.append(html.unescape(m.group(1)))
    return "".join(out)


def _docx(path, cfg=None):
    limit = _unpack_limit(cfg)
    read, _used = _zip_budget(limit)
    with zipfile.ZipFile(path) as z:
        xml = _xml_bytes(read(z, "word/document.xml", limit), "word/document.xml")
    lines = []
    for para in re.findall(r"<w:p(?:\s[^>]*)?>.*?</w:p>", xml, re.S):
        t = _xml_text(para).strip()
        if t:
            lines.append(t)
    return "\n".join(lines)


def _pptx(path, cfg=None):
    out = []
    limit = _unpack_limit(cfg)
    read, _used = _zip_budget(limit)
    with zipfile.ZipFile(path) as z:
        slides = sorted(n for n in z.namelist()
                        if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        for i, n in enumerate(slides, 1):
            xml = _xml_bytes(read(z, n, limit), n)
            texts = []
            for para in re.findall(r"<a:p(?:\s[^>]*)?>.*?</a:p>", xml, re.S):
                t = _xml_text(para).strip()
                if t:
                    texts.append(t)
            if texts:
                out.append(f"— 第 {i} 页 —\n" + "\n".join(texts))
    return "\n\n".join(out)


def _xlsx(path, cfg=None):
    """把每个 sheet 渲染成 TSV。只取前若干行，表格动辄几万行，全抽没意义。"""
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    import xml.etree.ElementTree as ET
    limit = _unpack_limit(cfg)
    read, _used = _zip_budget(limit)
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            # 先过 _xml_bytes（拒实体声明），再交给 ET 解析；
            # 解码后用 BytesIO 送回去，免得两处各写一套检查
            root = ET.parse(io.BytesIO(
                _xml_bytes(read(z, "xl/sharedStrings.xml", limit),
                           "xl/sharedStrings.xml").encode("utf-8"))).getroot()
            for si in root.findall(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))
        # sheet 名 -> 文件
        book = {}
        if "xl/workbook.xml" in names:
            wb = ET.parse(io.BytesIO(
                _xml_bytes(read(z, "xl/workbook.xml", limit),
                           "xl/workbook.xml").encode("utf-8"))).getroot()
            rels = {}
            if "xl/_rels/workbook.xml.rels" in names:
                # 关系表坏掉不该让整张表都读不出来：这一处按空处理，工作表列表退回按文件名列
                try:
                    r = ET.parse(io.BytesIO(
                        _xml_bytes(read(z, "xl/_rels/workbook.xml.rels", limit),
                                   "xl/_rels/workbook.xml.rels").encode("utf-8"))).getroot()
                    for rel in r:
                        rels[rel.get("Id")] = rel.get("Target")
                except (ValueError, ET.ParseError):
                    rels = {}
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
            root = ET.parse(io.BytesIO(
                _xml_bytes(read(z, target, limit), target).encode("utf-8"))).getroot()
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
    # **不许把 ImportError 兜成「解析失败：ModuleNotFoundError…」**：那句对用户毫无用处。
    # 这里直接给能照着做的文案（install.bat 或 pip 都写出来）。
    try:
        import pypdf
    except ImportError as e:
        raise RuntimeError(
            "读 PDF 需要 pypdf，但本机没装上。装一个就行："
            "双击项目里的 install.bat，或在项目目录执行 "
            "`.venv\\Scripts\\python.exe -m pip install pypdf`，装完重试。"
            f"（原始报错：{e}）") from e
    try:
        reader = pypdf.PdfReader(path)
    except Exception as e:
        raise RuntimeError(f"这个 PDF 打不开（pypdf 报：{type(e).__name__}: {str(e)[:120]}）") from e
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


_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),        # 先试最长的，免得 UTF-32 被 UTF-16 抢先
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _decode_text(raw):
    """把文本文件解成 (文本, 编码名, ⚠️提示或 None)。

    改这一处是因为原来那套「uf8 → gb18030 → big5 → latin-1 撞到哪个算哪个」有两头漏：
    * **带 BOM 的 UTF-16 会被自己拒掉**：解出来满屏 `\\x00`，`_looks_garbled` 判乱码 →
      用户发来的 UTF-16 txt 一律「读不了」，而它其实是最没歧义的一种（有 BOM）。
      → 现在 BOM 优先，按 BOM 指明的编码解。
    * **GBK 中文被 utf-8「解成功」这件事本身不会报错**，结果是一串可打印的怪西文
      （「目录」→ 'Ŀ¼'），`_looks_garbled` 只看可打印比例，照样放行 → 乱码喂给模型。
      → 复用 `executor._decode_printable_trap` 的三态判据（项目里已有的那一套，
        别再写第二份）：判成 "gbk" 就改按 gb18030 重解并注明这是猜的；
        判成 "ambiguous" 就不改判，但也**必须带 ⚠️**——不许一声不响。
    * **latin-1 兜底永远成功**，会把「前面所有候选都失败了」这件事盖住：
      它只是把字节一对一映成字符，不是真的解码。
      → 走到 latin-1 这一支时明确标注「这些字节不是上面任何一种编码」。
    """
    if raw.startswith(b"\x00\x00\xfe\xff") or raw.startswith(b"\xff\xfe\x00\x00"):
        raise ValueError("这个文件的 BOM 是 UTF-32，Windows 上几乎不会遇到，"
                         "我不猜它的编码——请另存成 UTF-8 或 UTF-16 再发。")
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc, None
            except UnicodeDecodeError as e:
                return (raw.decode(enc, "replace"), enc,
                        f"⚠️ 这个文件带 {enc} 的 BOM，但中间有解不出来的字节（{e}），"
                        f"下面这段可能没解码对，请人工核对。")

    text = None
    encoding = None
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            text = raw.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue

    note = None
    if text is None:
        # 所有候选都失败：latin-1 一对一映射（永远成功）只为「能看个大概」，
        # 必须把「这不是真解码」说出来，否则就是拿乱码骗人。
        text = raw.decode("latin-1")
        encoding = "latin-1"
        note = ("⚠️ 这个文件的字节既不是 UTF-8 也不是 GB18030/BIG5，"
                "下面是用 latin-1 一对一映出来的，**只能看个大概、内容不可信**，"
                "请人工核对或让对方另存编码后再发。")

    # utf-8 解出来落在「怀疑区间」时按 GB18030 复解：这是 CLAUDE.md 点名的老坑
    # （utf-8 与 GBK 有 1920 个 2 字节序列两边都能解、解出来还不一样）。
    if encoding == "utf-8":
        trap = executor._decode_printable_trap(raw)
        if trap == "gbk":
            try:
                text = raw.decode("gb18030")
                encoding = "gb18030"
                note = ("⚠️ 这段按 UTF-8 解是一串怪西文字符、按 GB18030 解才是通顺中文，"
                        "已按 GB18030 显示——**这一处可能是猜的**，若原文本来就是西文则不对，"
                        "请人工核对。")
            except UnicodeDecodeError:
                pass
        elif trap == "ambiguous":
            # 不改判（怕误伤 café/naïve 这类正常西文），但也**不能一声不响**。
            # 判「是不是真可疑」另有密度判据 —— 见 _trap_suspect_ratio 的注释：
            # 正常西文只有零星几个重音字母，整片汉字被误解时可疑字符是成片的。
            if _trap_suspect_ratio(text) >= _TRAP_RATIO:
                note = ("⚠️ 这段里有不少字符落在「UTF-8/GBK 都能解、结果还不一样」的区间，"
                        "**这一段可能没解码对**（原文可能是中文也可能是西文），"
                        "拿不准请人工核对一下。")

    if note is None and executor._looks_mojibake(text):
        note = ("⚠️ 解出来的内容里出现了替换字符（\\ufffd）或空字节，"
                "说明有部分字节没解码对，请人工核对。")
    return text, encoding, note


def _plain(path, _ext=None):
    """纯文本：解出来的编码要如实标出来。

    这里不只看「有没有解错」，而是**编码猜对没有本来就不确定**：GBK 与 BIG5
    对同一串汉字字节都可能解得出来、解出来还是不一样的汉字，而按 _looks_garbled
    的判据两边都「像正常中文」。所以只要走了非 UTF-8 的路并且真解出了汉字，
    就带上 ⚠️ 让人可以复核——宁可多一句提示，也不要静默给模型一段错内容。
    """
    raw = open(path, "rb").read()
    text, enc, note = _decode_text(raw)
    if note is None and enc not in (None, "utf-8", "utf-8-sig"):
        if len(text) >= 30 and _cjk_ratio(text) >= 0.10:
            note = (f"⚠️ 这份文本不是 UTF-8，是按 {enc} 解出来的；"
                    f"GBK/BIG5 对同一串字节可能解出不同的汉字，"
                    f"**请人工核对关键内容**（拿不准就请对方另存成 UTF-8 再发）。")
    return text, enc, note


def _looks_garbled(text):
    """抽出来的东西是不是乱码。宁可说读不了，也不把乱码喂给模型。

    在原来「可打印字符占比」之上补一条 **CJK 比例**判据：
    别人发来的文件里，正常中文文本的汉字占比不会低；而「GBK 汉字被 utf-8 误解」
    那类结果（'Ŀ¼'、'Ä¿Â¼'）虽然**每个字符都可打印**，里头却一个汉字都没有、
    净是 Latin-1 补充/拉丁扩展区的怪字符。只看可打印比例的话这类会 100% 通过，
    正是 CLAUDE.md 说的老坑。

    判据只在**确实可疑**时生效，不能误伤正常内容：
      * 只数「有字母/音节身份的字符」（`unicodedata.category` 以 L 开头）——
        em dash（—）、全角引号（「」）这些标点属于 P 类，是正常中文排版的一部分，
        按码点区间数会把 xlsx 的「— 工作表「表一」—」误判成乱码（实测踩到过）；
      * 至少 `_TRAP_LETTERS` 个，且文本里**一个汉字都没有**——
        纯英文/纯 ASCII 文本本来就没有汉字，所以这条对它们不生效。
    """
    if not text or len(text) < 20:
        return True
    good = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
    if good / len(text) < 0.75:
        return True
    letters = sum(1 for c in text
                  if (0x00A0 <= ord(c) <= 0x02FF or 0x0370 <= ord(c) <= 0x03FF)
                  and unicodedata.category(c).startswith("L"))
    cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
    return letters >= _TRAP_LETTERS and cjk == 0


def _trap_suspect_ratio(text):
    """可疑区字母 / 全部字母的比例（用来区分「正常西文重音」和「整片汉字被误解」）。

    只看个数会把一份含十几个重音字母的正常英文误标成「可能没解码对」；
    而汉字被 utf-8 误解时，可疑字符是**成片**的。分子分母都只数字母类字符，
    标点和数字不参与（否则排版符号会稀释比例）。
    """
    letters = [c for c in text if unicodedata.category(c).startswith("L")]
    if not letters:
        return 0.0
    suspect = sum(1 for c in letters
                  if 0x00A0 <= ord(c) <= 0x02FF or 0x0370 <= ord(c) <= 0x03FF)
    return suspect / len(letters)


def _cjk_ratio(text):
    if not text:
        return 0.0
    cjk = sum(1 for c in text if 0x4E00 <= ord(c) <= 0x9FFF)
    return cjk / len(text)


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
    # 音频（语音输入）走 audio_read 转写：它有自己的上限、隐私（默认不出本机）
    # 和失败语义，见 docs/voice-input-spec.md。**字节上限复用 file.max_bytes**，
    # 所以这里把已经算好的 max_bytes 传下去，不另立一份真源。
    from_audio = audio_read.is_audio(path)
    if not from_audio and ext not in SUPPORTED:
        return None, (f"我暂时只认 {', '.join(sorted(SUPPORTED))} 这几种"
                      f"（音频 .m4a/.mp3/.wav/.amr 这类也能转文字），"
                      f"{ext or '这种'} 读不了。")

    note = None
    try:
        if from_audio:
            text, err = audio_read.transcribe(path, cfg, max_bytes=max_bytes)
            if err:
                return None, err
        elif ext == ".pdf":
            text, err = _pdf(path)
            if err:
                return None, err
        elif ext == ".docx":
            text = _docx(path, cfg)
        elif ext == ".xlsx":
            text = _xlsx(path, cfg)
        elif ext == ".pptx":
            text = _pptx(path, cfg)
        else:
            # 只有纯文本这一支会带编码提示；office/pdf 的 XML 里是明确的 UTF-8
            text, _enc, note = _plain(path, ext)
    except _ZipTooLarge as e:
        # zip 炸弹：说清是「解压后太大」，而不是含糊的「解析失败」
        return None, (f"这个文件**解压后太大，出于安全我没读**（{e}）。"
                      f"如果是正常文档，请让对方另存一份更小的版本再发。")
    except zipfile.BadZipFile:
        return None, "这个文件损坏了（压缩包打不开）。"
    except Exception as e:
        # 120 字会把「怎么装 pypdf」这类可操作文案截掉，放宽到 300
        return None, f"解析失败：{type(e).__name__}: {str(e)[:300]}"

    text = (text or "").strip()
    if not text:
        return None, "这个文件里没有可提取的文字。"
    if not from_audio and _looks_garbled(text):
        # **音频转写不走这个判据**：`_looks_garbled` 里「短于 20 字就当可疑」是为
        # 「字节解码错了」设计的（短文本没法判）；而 STT 输出根本不是解码产物——
        # 一句 3 秒的「好的」只有两个字，按那条会被当成乱码拒掉，是错的。
        return None, ("抽出来的内容像是乱码（可能是特殊字体编码或扫描件），"
                      "我不拿它当内容用。")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…（太长，只取前 {max_chars} 字）"
    if note:
        # 提示语由本模块决定（llm.py 那层只暴露事实，不拼文案），
        # 拼在正文**后面**并保证不被截断：先给正文留出提示语的位置。
        room = max(0, max_chars - len(note))
        if len(text) > room:
            text = text[:room] + f"\n…（太长，只取前 {room} 字）"
        text = text + "\n\n" + note
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
