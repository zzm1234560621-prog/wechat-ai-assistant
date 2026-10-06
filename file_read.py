"""读文件：**别人发来的和你自己发出去的**（PDF / Office / 纯文本 / 当文件发来的图片 / 音频）。

微信 4.x 把文件**明文**落在磁盘上（两个方向都落，见下面「局限」）：

    <微信数据目录>/<账号>/msg/file/<YYYY-MM>/<原文件名>

实测（2026-10-01）pdf / docx / xlsx / pptx 的魔数都对（`%PDF-1.3`、`PK\\x03\\x04`），
**不用解密** —— 和图片不是一回事（图片是加密的 `.dat`，见 docs/wechat4-dat-image-notes.md）。

文件名从消息里拿：文件消息的 `local_type` 低 32 位是 49（appmsg）、subtype 是 6，
把 `message_content` 解压出来 XML 里的 `<title>` **就是磁盘上的文件名**（实测逐字吻合）。

**安全边界**：只允许读 `msg/file/` 下面的文件。文件名来自数据库、等价于来自发文件的人，
所以对方能控制这个字符串 —— 必须挡住 `../` 和绝对路径，绝不能让模型或对方指定任意路径。
和 `send_image` 的白名单是同一个道理，只是方向相反（那个是别把本地文件发出去）。

**局限（回答用户时要如实说）**
* `msg/file/<年-月>/` 里**别人发来的和你自己发出去的都在**（2026-10-01 实测：
  发出去的 `wechat-ai-assistant.zip` 与 dist 里那份同大小同秒）；没下载完的不在
* 同名文件会被微信存成 `xxx(1).pdf`，所以要按「同名或带 (N) 后缀」去找
* 扫描件 PDF（整页是图片）抽不出文字 —— 这种情况要如实说，**绝不编内容**
* 只支持下面 SUPPORTED 里列的后缀，其余的一律明说读不了
* **图片也走这里**（`IMAGE_SUPPORTED`：jpg/png/webp…）：当**文件**发来的图是
  明文原图，交给 `image_read` 走系统 OCR 认图里的字。这比 `read_image` 那条
  强得多 —— 那条只能读微信缓存的**缩略图**（自己发的图通常连缩略图都没有）。
  体积上限用 `file.max_bytes`，**不是** `image.max_bytes`。
* **`pick()` 可以只按文件名找**（`read_file` 只给 `name` 时走这条）：`find_files`
  是按消息记录列文件的，自己发出去的 / 记录里没留痕的在列表里就是没有，
  而文件明明在盘上。命中多份时返回候选、**绝不替用户挑一份**。
"""
import glob
import hashlib
import html
import io
import json
import os
import re
import time
import unicodedata
import zipfile

import executor
import image_cache
import tempdir          # 临时目录/导出目录的统一入口（可用 PROJ_TMP 改道）
import archive_read      # 压缩包递归（它只在函数里 import 本模块，所以这里没有循环导入）
import video_read
import mail_read
import db_read

# 项目根目录（和 usage.py / executor.py 同一个算法：相对路径按它解析，不按 CWD）
_HERE = os.path.dirname(os.path.abspath(__file__))

SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx",
             ".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".htm"}

# **当文件发来的图片**：交给 image_read 走 OCR。**不并进 SUPPORTED**——
# SUPPORTED 是「文档解析」白名单（`extract` 按后缀分派到各解析器），
# 图片走的是另一套实现（系统 OCR / 视觉模型），混在一起会让分派语义变糊。
# 值是**明文原图**（不是聊天里那个加密 .dat、也不是缩略图），所以这条比
# read_image 清楚得多。
IMAGE_SUPPORTED = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}

# 音频另走一条路（转写），**不进 SUPPORTED**：那是「文档解析」的白名单。
# 一并 import 是为了分派时用；audio_read 反过来不 import 本模块的顶层符号（只在
# 取字节上限时函数内 import），所以没有循环导入。
import audio_read                                    # noqa: E402

_MAX_BYTES = 30 * 1024 * 1024      # 没配 file.max_bytes 时的默认上限（配 0 = 不限）
_MAX_ZIP_UNPACK = 200 * 1024 * 1024  # 防 zip 炸弹：**解压后**的累计上限（默认值，见 unpack_cap）
_INLINE_BYTES = 2 * 1024 * 1024    # 超过它就被上层当成「重活」丢给 read_worker（见 inline_bytes）
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


def _warn(msg):
    """这个模块的告警出口（配置不合法时**绝不许静默**）。"""
    print(f"⚠️ file_read: {msg}", flush=True)


def _opt_int(value, default, allow_zero=False):
    """把配置里的一个整数读出来，**把 `0` 和「没配」分开**。

    为什么要单独写一个：以前是 `int(f.get("max_bytes") or _MAX_BYTES)`，而 `0` 是 falsy
    —— 用户明确写 `file.max_bytes: 0`（"不限大小"）会被**静默**当成"没配"退回 30MB。
    这正是这个项目最怕的那种「配了却不生效」。所以逐个判：

    * `None` / `""` / 读不出来的 → `default`
    * `0` → `allow_zero` 时就是 `0`（= 不限）；否则也退回 `default`（那些位置 0 没意义）
    * `< 0` → `default`
    """
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 0:
        return default
    if n == 0:
        return 0 if allow_zero else default
    return n


def _cfg(cfg=None):
    """返回 (单文件体积上限, 一次交给模型的字数上限)。体积上限 **0 = 不限**。"""
    f = ((cfg or {}).get("file") or {})
    return (_opt_int(f.get("max_bytes"), _MAX_BYTES, allow_zero=True),
            _opt_int(f.get("max_chars"), 20000))


def unpack_cap(cfg=None):
    """解压后累计上限（防 zip 炸弹）。默认 200MB，**只许调大、不许关**。

    配成 0/负数一律退回默认并告警 —— 它和 `file.max_bytes`（可以 0 = 不限）**不是一回事**：
    输入文件多大都行，但几十 KB 的 docx 能解出几十 GB，放开这个封顶等于拿用户的内存赌。
    """
    f = ((cfg or {}).get("file") or {})
    want = f.get("max_unpack")
    if want is None or want == "":
        return _MAX_ZIP_UNPACK
    try:
        n = int(want)
    except (TypeError, ValueError):
        n = -1
    if n <= 0:
        _warn(f"file.max_unpack={want!r} 不合法（防解压炸弹的封顶不能关），"
              f"已退回默认 {_MAX_ZIP_UNPACK // 1048576}MB。")
        return _MAX_ZIP_UNPACK
    return n


def inline_bytes(cfg=None):
    """超过它就被上层当成「重活」丢给后台 worker（默认 2MB）。

    放在这里而不是 agent_tools：它是**文件读取自己的**一组上限之一，
    和 max_bytes / max_unpack 同一个真源，别在两处各写一个数。
    """
    f = ((cfg or {}).get("file") or {})
    return _opt_int(f.get("inline_bytes"), _INLINE_BYTES)


def is_heavy(path, cfg=None):
    """这份文件该不该丢后台 worker？返回 `(是否重活, 为什么)`。

    判据（宁可多丢后台，也别让收消息的线程停在那儿）：
      * **体积**超过 `file.inline_bytes`；
      * **老格式** `.doc/.xls/.ppt`：要起 Office/LibreOffice 转换，几秒起步；
      * **音视频**：要抽轨/抽帧/转写；
      * **真压缩包**（zip/7z/rar）——注意 Office 包（docx/xlsx/pptx）也是 zip，
        但它们有专门的白名单分支，**不算重活**，否则一份小 Word 也要排队。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, ""
    limit = inline_bytes(cfg)
    if limit and size > limit:
        return True, f"{size / 1048576:.1f}MB，超过 file.inline_bytes"
    ext = os.path.splitext(path)[1].lower()
    if ext in (".doc", ".xls", ".ppt"):
        return True, "老格式要用转换引擎"
    if audio_read.is_audio(path):
        return True, "音频要转写"
    kind, _why = sniff(path)
    if kind in ("video", "audio", "riff"):
        return True, "音视频要抽轨/抽帧"
    if kind in ("zip", "7z", "rar", "gzip") and ext not in SUPPORTED:
        return True, "压缩包要递归读"
    # ⚠️ **inline 模式的图片故意留在同步路径上**：原图要附进"这一轮"发给模型的消息，
    #    而后台读完时那一轮早结束了 —— 丢给 worker 反而会把原图丢掉。
    #    代价可控：inline 那边有 `image.send_max_bytes`（默认 8MB）卡着。
    try:
        import image_read
        if kind == "image" and image_read.mode_of(cfg) == "inline":
            return False, ""
    except Exception:
        pass
    return False, ""


def _unpack_limit(cfg=None):
    """Office/压缩包解压后的总上限（防 zip 炸弹）。

    = `unpack_cap(cfg)`（绝对封顶）与「单文件上限的 3 倍」取小：正常 docx/xlsx 解压后
    往往只有压缩包的几倍，而 zip 炸弹动辄解出几百上千倍——这一刀正好砍在中间。

    ⚠️ `file.max_bytes: 0`（不限）时**不能再乘 3**：那会算出 0，把一切正常文件都拒了。
    """
    mb, _mc = _cfg(cfg)
    cap = unpack_cap(cfg)
    if not mb:                      # 0 = 不限 → 只用绝对封顶
        return cap
    return min(cap, max(1, mb * 3))


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


def search_files(name=None, since=None, until=None, limit=20):
    """在**本地文件目录**里按文件名/时间找文件（**纯磁盘，不查库**）。

    收到的文件是明文躺在 `<账号>/msg/file/<年-月>/` 下的，所以这条搜索不碰 hook，
    也就没有「并发放把微信搞崩」那一层风险。

    ⚠️ **路径里没有「谁发的 / 发在哪个群」**——那是消息记录里的信息。所以：
      * 要按**人/群**找文件 → 指定那个会话，由上层去查那个会话的消息记录（`v4_files`）；
      * 不指定会话 → 只能给「文件叫什么、什么时候到的、多大」，**不许替它编一个来源**。
    时间用**文件落盘的 mtime**（= 微信把它写到本机的时刻），比月份目录细一档。
    """
    key = str(name or "").strip().lower()
    lo = float(since) if since else None
    hi = float(until) if until else None
    out = []
    for root in files_roots():
        try:
            months = os.listdir(root)
        except OSError:
            continue
        for month in months:
            d = os.path.join(root, month)
            if not os.path.isdir(d):
                continue
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for fn in entries:
                p = os.path.join(d, fn)
                if key and key not in fn.lower():
                    continue
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                except OSError:
                    continue
                if (lo is not None and st.st_mtime < lo) or \
                   (hi is not None and st.st_mtime > hi):
                    continue
                out.append({"name": fn, "path": p, "size": st.st_size,
                            "mtime": st.st_mtime, "month": month})
    out.sort(key=lambda m: -m["mtime"])
    return out[:int(limit)]


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


def pick(name, limit=8):
    """按文件名挑一份**要读的**文件。返回 (路径, 错误文本, 候选列表)。

    为什么需要它（2026-10-01 定的）：`agent_tools.find_files` 是按**消息记录**
    列文件的（`local_type = (6<<32)|49`）。**用户自己发出去的文件**、以及记录里
    没留痕的（消息太老、类型没记对），在列表里就是没有 —— 可文件明明躺在
    `msg/file/<年-月>/` 下（实测：发出去的 zip 与本地那份同大小同秒）。
    入口不该因为「记录里没有」就装作没有，所以留这条**纯磁盘、不查库**的兜底。

    解析顺序：**先精确**（`locate`：同名，含 `(1)` 这类重名退让），精确不成再
    按子串翻目录。命中多份时**绝不替用户挑**：返回候选列表（此时路径为 None），
    由上层去问用户要哪一份——挑错一份等于把别的内容当答案说出来。
    路径边界和 `locate` 完全一样（只许 `msg/file/` 下的文件）。
    """
    safe = _safe_name(name)
    if not safe:
        return None, (f"文件名「{name}」不合法：只给文件名本身就行，"
                      f"不要带目录、盘符或开头的点。"), []
    hit = locate(safe)
    if hit:
        return hit, None, []
    hits = search_files(name=safe, limit=max(1, int(limit)))
    if not hits:
        return None, (f"本机收到的文件里没找到名字含「{safe}」的。\n"
                      f"（只翻了 `msg/file/<年-月>/` —— 别人发来的和**你发出去的**"
                      f"都在那儿；`msg/file` 之外的目录一律不看。要按人/群找，"
                      f"就用 find_files 带上 contact。）"), []
    if len(hits) == 1:
        return hits[0]["path"], None, []
    return None, None, hits


# ---------------- 内容嗅探（不看后缀，看内容）----------------
# **为什么要它**：用户要的是「任何文件都能读」。按后缀白名单永远会漏——`README`、
# `.srt`、`.ini`、`.py`、无后缀的日志全是文本；反过来 `.dat` 却不是。所以先按魔数
# 认容器/二进制，认不出再判「能不能当文本解」。**拿不准就说读不了**——宁可少读，
# 也不把二进制当文本喂给模型。
#
# 顺序有意义：先特殊后通用（`PK` 也是 docx/xlsx/pptx/jar/apk/whl 的魔数）。
_MAGIC = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),                              # 空压缩包
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),       # 老 Office（.doc/.xls/.ppt）/ .msg
    (b"{\\rtf", "rtf"),
    (b"SQLite format 3\x00", "sqlite"),      # SQLite 库（P3）
    (b"Rar!\x1a\x07", "rar"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\x1f\x8b", "gzip"),
    (b"\xff\xd8\xff", "image"),                          # JPEG
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"GIF8", "image"),
    (b"BM", "image"),                                    # BMP
    (b"II*\x00", "image"), (b"MM\x00*", "image"),        # TIFF
    (b"RIFF", "riff"),                                   # wav / avi / webm
    (b"ID3", "audio"), (b"\xff\xfb", "audio"), (b"\xff\xf3", "audio"),
)
# ISO-BMFF（mp4/mov/heic/avif…）：魔数在偏移 4，要看 brand 才能分清图还是视频
_FTYP_IMAGE_BRANDS = (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"avif", b"avis")
_SNIFF_BYTES = 8192

_KIND_NAMES = {
    "zip": "压缩包 / Office 包（zip 家族：zip/docx/xlsx/pptx/whl/apk…）",
    "ole2": "老版 Office（.doc/.xls/.ppt）或 .msg 邮件",
    "rar": "RAR 压缩包", "7z": "7z 压缩包", "gzip": "gzip 压缩文件",
    "image": "图片", "video": "视频", "audio": "音频", "riff": "RIFF 容器（wav/avi）",
    "pdf": "PDF", "rtf": "RTF 文档", "binary": "二进制文件",
    "sqlite": "SQLite 数据库文件",
}


def sniff(path):
    """看**内容**判断这是什么东西。返回 `(kind, 说明)`。

    kind ∈ text / binary / pdf / zip / ole2 / rtf / rar / 7z / gzip / image / audio /
           riff / video
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_SNIFF_BYTES)
    except OSError:
        return "binary", "读不到这个文件"
    if not head:
        return "binary", "空文件"

    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind, ""
    if len(head) > 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        return ("image", "") if brand in _FTYP_IMAGE_BRANDS else ("video", "")

    if b"\x00" in head:
        return "binary", "内容里有 NUL 字节（二进制）"
    ctrl = sum(1 for b in head if b < 32 and b not in (9, 10, 13))
    if ctrl / len(head) >= 0.02:
        return "binary", "控制字符太多（二进制）"
    for enc in ("utf-8", "gb18030"):
        try:
            head.decode(enc)
            return "text", ""
        except UnicodeDecodeError:
            continue
    # 两种常见编码都解不出来，但控制字符很少 → 大概率是别的老编码，按文本试读
    return "text", "编码不是 utf-8/gb18030，按 latin-1 读（内容可能不准）"


def _unreadable(kind, why=""):
    """嗅探出来的类型「当前还没接上」时的话术。

    每一类都由后面的任务接掉（压缩包→archive_read、老 Office→legacy_office、
    视频→video_read），**接掉就把这个分支删掉**，别留着说"读不了"。
    """
    name = _KIND_NAMES.get(kind, kind)
    tail = f"（{why}）" if why else ""
    return (f"我认出这是{name}{tail}，但这一类的内容我还没接上。\n"
            f"现在能读的是：{', '.join(sorted(SUPPORTED))}（文档/文本）、"
            f"{', '.join(sorted(IMAGE_SUPPORTED))}（图片，认图里的字）、"
            f".m4a/.mp3/.wav/.amr 这类（音频转文字）。"
            f"请如实告诉用户读不了，**不要编内容**。")


def _xml_text(xml):
    """把所有 <xxx:t> 里的文字抽出来（docx/pptx 都用这个）。"""
    out = []
    for m in re.finditer(r"<(?:w|a):t(?:\s+[^>]*)?>(.*?)</(?:w|a):t>", xml, re.S):
        out.append(html.unescape(m.group(1)))
    return "".join(out)


# ---------------- 文档内嵌图片（Office 包 / PDF 页图）----------------
#
# 规格：docs/file-input-spec.md 第九节「现代 Office：含里面的图片」+「PDF：含扫描页/内嵌图」。
# 两条规矩：
#   ① **共享同一套解压预算**（_zip_budget），不然内嵌图会成为绕过 zip 炸弹防护的后门；
#   ② 解读不了的要**说出来**（张数 + 原因），静默丢弃在这个项目里不允许。
_EMBED_IMG_MAX = 20          # 一份文档最多解读几张内嵌图
_IMAGE_EXTS_ANY = tuple(IMAGE_SUPPORTED)


def _image_from_bytes(data, name, cfg, collect=None):
    """把一段图片字节交给图片通道。返回 `(文本, 说明)`（读不出时文本为空、说明写清原因）。

    为什么要落一次临时文件：`image_read.describe()` 吃的是**路径**（系统 OCR 是
    PowerShell 脚本、视觉模型也要读文件）。临时文件写在导出目录下的 `_tmp_img/`
    （data/ 里，已被 .gitignore 忽略），用完立刻删。
    """
    import image_read
    if not data:
        return "", "空图片"
    if image_read.mode_of(cfg) == "off":
        return "", "图片解读已关闭（config.yaml 的 image.mode=off）"
    ext = os.path.splitext(str(name))[1].lower()
    if ext not in IMAGE_SUPPORTED:
        ext = ".png"                     # 兜底：内容判据交给解码器，别因为后缀放弃
    d = os.path.join(export_dir(cfg), "_tmp_img")
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, hashlib.sha1(data).hexdigest()[:16] + ext)
        with open(p, "wb") as f:
            f.write(data)
    except OSError as e:
        return "", f"临时落盘失败：{e}"
    try:
        text, err = _image(p, cfg, max_bytes=max(len(data), 1), collect=collect)
    finally:
        try:
            os.remove(p)
        except OSError:
            pass
    if err:
        return "", err
    return text, ""


def _embed_images(path, cfg, prefixes, label, collect=None):
    """抽 Office 包里的内嵌图片并解读。返回要拼进正文的行（含张数与跳过的说明）。"""
    limit = _unpack_limit(cfg)
    read, _used = _zip_budget(limit)
    found, skipped_vec, skipped_ext = [], 0, 0
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist()
                       if n.startswith(prefixes) and not n.endswith("/"))
        for n in names:
            if os.path.splitext(n)[1].lower() not in _IMAGE_EXTS_ANY:
                skipped_vec += 1          # emf/wmf/svg 这类矢量图，OCR 读不了
                continue
            if len(found) >= _EMBED_IMG_MAX:
                skipped_ext += 1
                continue
            found.append((n, read(z, n, limit)))
    if not names:
        return []
    lines = [f"—— {label}里的图片：共 {len(names)} 个文件 ——"]
    for i, (n, data) in enumerate(found, 1):
        text, why = _image_from_bytes(data, n, cfg, collect)
        if text.strip():
            lines.append(f"〔第 {i} 张 {n}〕{text.strip()}")
        else:
            lines.append(f"〔第 {i} 张 {n}〕没读出内容：{why or '图里没识别到文字'}")
    if skipped_vec:
        lines.append(f"（另有 {skipped_vec} 个矢量图/非位图没解读：系统 OCR 读不了 emf/wmf/svg）")
    if skipped_ext:
        lines.append(f"（还有 {skipped_ext} 张图超出单份文档上限 {_EMBED_IMG_MAX} 张，没解读）")
    return lines


def _docx(path, cfg=None, collect=None):
    limit = _unpack_limit(cfg)
    read, _used = _zip_budget(limit)
    with zipfile.ZipFile(path) as z:
        xml = _xml_bytes(read(z, "word/document.xml", limit), "word/document.xml")
    lines = []
    for para in re.findall(r"<w:p(?:\s[^>]*)?>.*?</w:p>", xml, re.S):
        t = _xml_text(para).strip()
        if t:
            lines.append(t)
    try:
        lines += _embed_images(path, cfg, ("word/media/",), "这份 Word", collect)
    except _ZipTooLarge as e:
        lines.append(f"（文档里的图片没解读：{e}）")
    except Exception as e:
        lines.append(f"（文档里的图片没解读：{type(e).__name__}: {str(e)[:120]}）")
    return "\n".join(lines)


def _pptx(path, cfg=None, collect=None):
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
    try:
        out += _embed_images(path, cfg, ("ppt/media/",), "这份 PPT", collect)
    except _ZipTooLarge as e:
        out.append(f"（PPT 里的图片没解读：{e}）")
    except Exception as e:
        out.append(f"（PPT 里的图片没解读：{type(e).__name__}: {str(e)[:120]}）")
    return "\n\n".join(out)


def _xlsx(path, cfg=None, collect=None):
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
    try:
        out += _embed_images(path, cfg, ("xl/media/",), "这个表格", collect)
    except _ZipTooLarge as e:
        out.append(f"（表格里的图片没解读：{e}）")
    except Exception as e:
        out.append(f"（表格里的图片没解读：{type(e).__name__}: {str(e)[:120]}）")
    return "\n\n".join(out)


def _archive(path, cfg=None, on_image=None):
    """压缩包（zip/7z/rar，可嵌套）→ 文字。返回 `(文本, 错误)`。"""
    return archive_read.read_archive(path, cfg, on_image=on_image)


def _append_text(path, text, cfg=None):
    """把一段文字**追加**到导出文件（原子：先写临时文件再 `os.replace`）。"""
    tmp = path + f".tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(("\n\n" if os.path.getsize(path) else "") + open(tmp, encoding="utf-8").read())
            f.flush()
            os.fsync(f.fileno())
        os.remove(tmp)
    except OSError as e:
        _warn(f"写导出失败（不影响这次读取）：{e}")
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _write_sidecar(path, data):
    tmp = path + f".tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        _warn(f"写续读标记失败（「继续」会不好使）：{e}")


def _media_id(path):
    """续读 id：按**路径**取（不是内容）—— 同一份文件跨窗口要是同一个 id。"""
    return hashlib.md5(os.path.abspath(path).lower().encode("utf-8", "replace")).hexdigest()[:16]


def _window_tail(prefix, path, cfg, text, next_start, what, unit):
    """视频 / 长音频共用的收尾：**逐段累积导出** + 给「继续」用的 cursor。

    cursor 形状和文档分页一样是 `<id>:<位置>`，但位置的含义不同：
    文档是**字节偏移**，视频/音频是**秒**。靠导出目录里的 `<prefix>_<id>.json`
    侧车区分（`extract_page` 先认侧车、再当文档游标处理）。

    ⚠️ **短内容不写导出、不加尾巴**：一段 3 秒的语音没有"分页/续读"这回事，
    给它写一份导出文件、再补一句"已读到末尾"只会变成噪音（且每天几百条语音＝几百个文件）。
    只有真需要续读（有下一段）或内容超过一页（`file.max_chars`）时才走导出那套。
    """
    max_chars = _cfg(cfg)[1]
    d = export_dir(cfg)
    eid = _media_id(path)
    ep = os.path.join(d, f"{prefix}_{eid}.txt")
    side = os.path.join(d, f"{prefix}_{eid}.json")
    paged_before = os.path.isfile(side)
    need_export = (next_start is not None or paged_before or len(text) > max_chars)

    if need_export:
        os.makedirs(d, exist_ok=True)
        _append_text(ep, text, cfg)
    if next_start is not None:
        _write_sidecar(side, {"path": os.path.abspath(path), "next": int(next_start),
                              "done": False, "at": time.time()})
        return (text + f"\n\n…（{what}还没读完。要从第 {next_start} {unit}接着读，"
                       f"就说「继续」、把 cursor 原样带上：cursor={eid}:{next_start}。"
                       f"已经读过的内容累积在本机：{ep}）"), None
    if paged_before:
        # 上一次分过段 → 留个 done 标记：用户再点「继续」时要**说准话**（"已经到末尾"），
        # 而不是去重读一段、也不是含糊地说"导出不在了"。
        _write_sidecar(side, {"path": os.path.abspath(path), "next": None,
                              "done": True, "at": time.time()})
    if need_export:
        return text + f"\n\n（{what}已读到末尾。）全文累积在本机：{ep}", None
    return text, None


def _video(path, cfg=None, start=0, on_image=None):
    """视频 → 文字（音轨转写）+ 画面（抽帧走图片通道）。返回 `(文本, 错误)`。

    长视频按 `video.max_seconds` 分段：这段读完给 `cursor=<id>:<下一段起点秒>`，
    「继续」由 `extract_page` 认这个 cursor 再调回来（**不重不漏**）。
    """
    import video_read
    text, next_start, note = video_read.read_window(path, cfg, start=start, on_image=on_image)
    if not text:
        return None, note or "这个视频读不出内容"
    return _window_tail("v", path, cfg, text, next_start, "这个视频", "秒")


def _audio(path, cfg=None, start=0):
    """音频 → 文字。长音频（超过 `audio.max_seconds`）**分段续读**，不是拒绝。

    短音频走 `audio_read.transcribe` 老路（原文件直接转，不重编码）；
    超长的由 `audio_read.window` 切一段再转，并给同一个形状的 cursor。
    """
    import audio_read
    text, next_start, note = audio_read.window(path, cfg, start=start)
    if text is None:
        return None, note or "这段音频读不出内容"
    return _window_tail("a", path, cfg, text, next_start, "这段音频", "秒")


def _legacy(path, cfg=None):
    """老 Office（.doc/.xls/.ppt）→ 文字，走多引擎降级（见 legacy_office.py）。

    返回 `(文本, 错误)`；结果里**必须**写明用的是哪个引擎——用户才知道这份可不可信。
    """
    import legacy_office
    text, engine, note = legacy_office.convert(path, cfg)
    if not text:
        return None, note
    head = (f"（这是老 Office 格式 {os.path.splitext(path)[1].lower()}，"
            f"用「{engine}」转出来的）")
    body = text.strip()
    tail = f"\n\n{note}" if note else ""
    return head + "\n" + body + tail, None


def _image(path, cfg, max_bytes=None, collect=None):
    """读一张图。返回 (文本, 错误)。

    **当文件发过来的图是明文原图**（和聊天里那条加密 `.dat` / 缩略图不是一回事），
    所以这条路比 `read_image` 清楚得多：不受「微信只缓存滚动看过的缩略图」限制。
    体积上限用调用方的 `file.max_bytes`（`extract` 已经卡过一道），**不用**
    `image.max_bytes` —— 那个 5MB 是为聊天缩略图设的，套到原图上会一动就拒。

    `collect`：`image.mode=inline` 时，`image_read.handoff` 会把**原图路径**交给
    这个回调（由上层塞进这一轮发给模型的消息），而这里只回一句"图已交给模型"。
    """
    import image_read
    r = image_read.handoff(path, cfg, max_bytes=max_bytes, collect=collect)
    if r["kind"] == "image":
        name = os.path.basename(str(r.get("path") or path))
        if r.get("attached") is False:
            # 收图的人拒了（比如超过 image.max_per_round）→ **不许**说"已经给模型看了"
            note = (f"[这张原图**没能**交给模型看：{name}（超过每轮上限或收图方拒收）。"
                    f"下面是图里能认出来的字]")
        else:
            note = f"[已把原图交给模型看：{name}]"
        if r.get("text"):
            note += f"（图里还认出了这些字：{r['text'][:200]}）"
        if r.get("why"):
            note += f"\n{r['why']}"
        return note, None
    if r["kind"] == "text":
        return r["text"], None
    return None, f"这张图读不出内容：{r.get('why') or '没识别到文字'}"


_PDF_SCAN_MAX = 10           # PDF 里最多 OCR 几个「整页是图」的页（多出来的如实说）


def _pdf(path, cfg=None, collect=None):
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

    # 页数上限：**0 = 不限**（默认）。以前写死 `pages[:40]` 且只字不提 —— 那是静默截断，
    # 用户的 PDF 有 300 页时会以为"助手读完了"。
    f = ((cfg or {}).get("file") or {})
    max_pages = _opt_int(f.get("pdf_max_pages"), 0, allow_zero=True)
    total = len(reader.pages)
    pages, scanned, scan_skipped, chars = [], 0, 0, 0
    for i, page in enumerate(reader.pages, 1):
        if max_pages and i > max_pages:
            break
        if chars > _MEM_TEXT_CAP:
            pages.append(f"…（正文已超过 {_MEM_TEXT_CAP} 字，剩下 {total - i + 1} 页没读）")
            break
        try:
            t = (page.extract_text() or "").strip()
        except Exception as e:
            t = ""
            _warn(f"第 {i} 页抽文字出错（跳过这一页）：{type(e).__name__}: {str(e)[:80]}")
        if t:
            pages.append(f"— 第 {i} 页 —\n{t}")
            chars += len(t)
            continue
        # 整页没有文字 → 多半是**扫描页**：把页图交给图片通道
        if scanned >= _PDF_SCAN_MAX:
            scan_skipped += 1
            continue
        got = _pdf_page_image(page, i, cfg, collect)
        if got:
            pages.append(got)
            scanned += 1
        else:
            scan_skipped += 1

    tail = []
    if max_pages and total > max_pages:
        tail.append(f"（这份 PDF 共 {total} 页，按 file.pdf_max_pages={max_pages} 只读了前 {max_pages} 页；"
                    f"要读更多就把这个值调大或写 0）")
    if scan_skipped:
        tail.append(f"（另有 {scan_skipped} 页没文字、也没给出可读的图，跳过了："
                    f"可能本身是空白页，或页图格式系统 OCR 认不了）")
    if not pages:
        return None, ("这个 PDF 抽不出文字。多半是**扫描件**（整页是图片），"
                      "或者用了少见的字体编码——我读不了，不编。")
    return "\n\n".join(pages + tail), None


def _pdf_raw_images(page, limit=3):
    """不靠 Pillow，直接从页的 `/XObject` 里掏**本身已是图片格式**的流（JPEG / JP2）。

    返回 `(可用图片 [(data, ext)], 这一页 /Image 对象总数)`。

    为什么需要这条：pypdf 的 `page.images` **要 Pillow**（实测报
    `ImportError: pillow is required to do image extraction`）。而扫描页绝大多数就是
    DCTDecode(JPEG)——那本来就是一个完整 JPEG 文件，掏出来直接喂系统 OCR 就行，
    不必为了这件事给所有人装 Pillow。总数用来区分「这页根本没有图」和「有图但我取不出来」。
    """
    out, total = [], 0
    try:
        res = page.get("/Resources") or {}
        xobjs = res.get("/XObject")
        if xobjs is None:
            return out, 0
        xobjs = xobjs.get_object() if hasattr(xobjs, "get_object") else xobjs
        for _key, ref in xobjs.items():
            obj = ref.get_object() if hasattr(ref, "get_object") else ref
            if str(obj.get("/Subtype") or "") != "/Image":
                continue
            total += 1
            if len(out) >= limit:
                continue
            filt = obj.get("/Filter")
            # ⚠️ `/Filter` 可能是**链**（实测扫描页是 ['/FlateDecode','/DCTDecode']）——
            # 只看第一个会漏掉 DCT，于是整条路白跑。`get_data()` 会把链解完，
            # 剩下的正是一个完整 JPEG。
            names = filt if isinstance(filt, list) else [filt]
            names = " ".join(str(x) for x in names)
            if "DCTDecode" in names:
                out.append((bytes(obj.get_data()), ".jpg"))
            elif "JPXDecode" in names:
                out.append((bytes(obj.get_data()), ".jp2"))
    except Exception as e:
        _warn(f"从页里直接掏图片流失败：{type(e).__name__}: {str(e)[:80]}")
    return out, total


def _pdf_page_image(page, index, cfg, collect=None):
    """把「整页是图」的一页交给图片通道。

    返回一段可读结果；**这一页压根没有图时返回 None**（调用方当空白页计数）。
    """
    import image_read
    raw_items, img_total = _pdf_raw_images(page)
    head = f"— 第 {index} 页（整页是图）—"
    if img_total == 0:
        try:
            if not list(page.images)[:1]:        # 有 Pillow 时再确认一次
                return None
        except ImportError:
            return None                          # 两条路都说明这页没有位图
        except Exception:
            return None
    if image_read.mode_of(cfg) == "off":
        return head + "\n（图片解读已关闭：config.yaml 的 image.mode=off）"

    items, why_none = [], ""
    try:
        for k, im in enumerate(list(page.images)[:3], 1):
            items.append((f"p{index}_{k}.png", bytes(im.data)))
    except ImportError as e:
        why_none = ("这一页的图要装了 Pillow 才能取出来"
                    "（`.venv\\Scripts\\python.exe -m pip install Pillow`）")
        _ = e
    except Exception as e:
        why_none = f"取页图出错：{type(e).__name__}: {str(e)[:80]}"
    if not items:
        items = [(f"p{index}_{k}{ext}", data) for k, (data, ext) in enumerate(raw_items, 1)]
        if items:
            why_none = ""
    if not items:
        return head + "\n（没读出内容：" + (
            why_none or "这一页有图，但都不是我能认的编码（我的系统 OCR 认 jpg/png/tif 这类）") + "）"

    got = []
    for name, data in items:
        text, why = _image_from_bytes(data, name, cfg, collect)
        got.append(text.strip() if text.strip()
                   else f"（这一页的图没读出内容：{why or '图里没识别到文字'}）")
    return head + "\n" + "\n".join(got)


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
        纯英文/纯 ASCII 文本本来就没有汉字，所以这条对它们不生效；
      * **短文本只在"一个字都没有"时才可疑**（2026-10-02 改）：以前 `len < 20`
        一律判乱码，于是「好的」「OK」或者压缩包里 19 个字的成员全被拒 ——
        音频转写和图片 OCR 早就吃过这个亏（各自绕开了），而**短短一句正常文本**
        本来就该能读。现在只有"既短、又没有任何字母/汉字"才算可疑（真乱码长这样）。
    """
    if not text:
        return True
    if len(text) < 20:
        wordish = any(c.isalnum() or 0x4E00 <= ord(c) <= 0x9FFF for c in text)
        return not wordish
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


def extract(path, cfg=None, full=False, on_image=None):
    """抽文本。返回 (文本, 提示语)；失败时文本为 None、提示语说明原因。

    `full=True`：**不按 `file.max_chars` 截断**，返回全文（给 `extract_page` 导出用）。
    默认 False 保持老行为——上层拿到的就是"能直接喂模型的一段"。
    """
    max_bytes, max_chars = _cfg(cfg)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, "读不到这个文件。"
    if max_bytes and size > max_bytes:      # 0 = 不限（见 _opt_int）
        return None, f"文件太大（{size/1048576:.1f}MB），我不读这么大的。"
    if not max_bytes:
        max_bytes = size

    ext = os.path.splitext(path)[1].lower()
    # 音频（语音输入）走 audio_read 转写：它有自己的上限、隐私（默认不出本机）
    # 和失败语义，见 docs/voice-input-spec.md。**字节上限复用 file.max_bytes**，
    # 所以这里把已经算好的 max_bytes 传下去，不另立一份真源。
    from_audio = audio_read.is_audio(path)
    from_image = ext in IMAGE_SUPPORTED
    # 文本是不是**图片通道**产出的（含"按内容嗅探出是图"那一路，如 .heic）。
    # 乱码判据按它跳过 —— 只看后缀会漏掉嗅探出来的图（这个 bug 被 T10 的 heic 用例抓到过）。
    via_image = from_image

    note = None
    try:
        if from_audio:
            text, err = _audio(path, cfg, 0)
            if err:
                return None, err
        elif from_image:
            text, err = _image(path, cfg, max_bytes=max_bytes, collect=on_image)
            if err:
                return None, err
        elif ext == ".pdf":
            text, err = _pdf(path, cfg, on_image)
            if err:
                return None, err
        elif ext == ".docx":
            text = _docx(path, cfg, on_image)
        elif ext == ".xlsx":
            text = _xlsx(path, cfg, on_image)
        elif ext == ".pptx":
            text = _pptx(path, cfg, on_image)
        elif ext in (".doc", ".xls", ".ppt"):
            # 老 Office：多引擎降级（Office COM → WPS → LibreOffice → antiword → 粗略抽取）
            text, err = _legacy(path, cfg)
            if err:
                return None, err
        elif ext in mail_read.MAIL_EXT:
            text, err = (mail_read.read_eml(path, cfg, on_image) if ext == ".eml"
                         else mail_read.read_msg(path, cfg, on_image))
            if err:
                return None, err
        elif ext in db_read.DB_EXT:
            # `.db` 不一定是 sqlite —— 不是就当普通文件走别的路（不许硬打开再报错）
            if db_read.is_sqlite(path):
                text, err = db_read.read_db(path, cfg)
                if err:
                    return None, err
            else:
                kind0, why0 = sniff(path)
                if kind0 == "text":
                    text, _enc, note = _plain(path, ext)
                else:
                    return None, (f"这个 .db 不是 SQLite 库（文件头不对），"
                                  f"按内容看它更像：{_KIND_NAMES.get(kind0, kind0)}。"
                                  f"要读它请告诉我它是什么格式。")
        elif ext in video_read.VIDEO_EXT:
            # 视频：音轨转写 + 抽帧走图片通道（长视频分段，见 _video）
            text, err = _video(path, cfg, 0, on_image)
            if err:
                return None, err
        elif ext in archive_read.ARCHIVE_EXTS:
            # 压缩包：递归读成员（同额度、有层数与成员数上限）
            text, err = _archive(path, cfg, on_image)
            if err:
                return None, err
        elif ext in SUPPORTED:
            # 只有纯文本这一支会带编码提示；office/pdf 的 XML 里是明确的 UTF-8
            text, _enc, note = _plain(path, ext)
        else:
            # 后缀不在白名单 → **按内容嗅探**（用户要的是"任何文件都能读"）
            kind, why = sniff(path)
            if kind == "text":
                text, _enc, note = _plain(path, ext)
                if why:
                    note = ((note + " ") if note else "") + "⚠️ " + why
            elif kind == "image":
                # 嗅探出是图（如 .heic 这类不在白名单的图片后缀）→ 走图片通道，
                # 让 OCR/视觉模型自己去判能不能认；认不出它会如实说，不会编。
                via_image = True
                text, err = _image(path, cfg, max_bytes=max_bytes, collect=on_image)
                if err:
                    return None, err
            elif kind == "sqlite":
                text, err = db_read.read_db(path, cfg)
                if err:
                    return None, err
            elif kind == "video":
                text, err = _video(path, cfg, 0, on_image)
                if err:
                    return None, err
            elif kind in archive_read.ARCHIVE_KINDS:
                text, err = _archive(path, cfg, on_image)
                if err:
                    return None, err
            elif kind == "ole2":
                # 老 Office（.doc/.xls/.ppt）**或** .msg 邮件 —— 后缀不认识也照样试
                text, err = _legacy(path, cfg)
                if err:
                    return None, err
            else:
                return None, _unreadable(kind, why)
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
        # 图片这一支的空文本有它自己的话（见 _image），走到这儿的是文档/音频
        return None, "这个文件里没有可提取的文字。"
    if not from_audio and not via_image and _looks_garbled(text):
        # **音频转写和图片 OCR 都不走这个判据**：`_looks_garbled` 里「短于 20 字就当
        # 可疑」是为「字节解码错了」设计的（短文本没法判）；而 STT / OCR 的输出根本
        # 不是解码产物——一句 3 秒的「好的」只有两个字，一张图上的「交 易 猫」只有
        # 四个字，按那条会被当成乱码拒掉，是错的。
        return None, ("抽出来的内容像是乱码（可能是特殊字体编码或扫描件），"
                      "我不拿它当内容用。")
    if len(text) > max_chars and not full:
        text = text[:max_chars] + f"\n…（太长，只取前 {max_chars} 字）"
    if note:
        # 提示语由本模块决定（llm.py 那层只暴露事实，不拼文案），
        # 拼在正文**后面**并保证不被截断：先给正文留出提示语的位置。
        room = max(0, max_chars - len(note))
        if len(text) > room and not full:
            text = text[:room] + f"\n…（太长，只取前 {room} 字）"
        text = text + "\n\n" + note
    return text, None


# ---------------- 全文导出与分页（"不限大小"的落地方式）----------------
#
# 规格见 docs/file-input-spec.md 第七节。要点：
#   * 抽出来的**全文**落到 `<export_dir>/<id>.txt`（输入多大都行，本地全文可读）；
#   * 一次**只给模型一页**（file.max_chars）——模型上下文和费用是真限制，
#     GB 级文本全塞进去在上下文和钱上都做不到，也不该做；
#   * 尾部给一个 `cursor`，说「继续」就取下一页，**不重不漏**；
#   * 导出是临时物：超期/超量清理**并打日志**（静默丢弃不允许）。
_MEM_TEXT_CAP = 50_000_000      # 一次抽进内存的**字数**上限（约 100MB 级）
_EXPORT_KEEP = 50               # 导出目录最多留几份
_EXPORT_MAX_AGE = 7 * 86400.0   # 或最多留几天
_CURSOR_RE = re.compile(r"^([0-9a-f]{16}):(\d+)$")


def export_dir(cfg=None):
    """导出目录（配置里的相对路径按**项目根**解析，不按 CWD）。

    没显式配 `file.export_dir` 时，落到 `tempdir` 的根目录下（默认仍是 `<项目>/data/exports`，
    但受限环境/别的部署形态可以用 `PROJ_TMP` 改道 —— 见 `tempdir.py`）。
    ⚠️ **用户显式配了路径就一个字都不动**：那是他的选择，不该被环境变量顶掉。
    """
    f = ((cfg or {}).get("file") or {})
    d = str(f.get("export_dir") or "").strip()
    if not d:
        return tempdir.get("exports")
    if not os.path.isabs(d):
        d = os.path.join(_HERE, d)
    return d


def sweep_exports(cfg=None, keep=_EXPORT_KEEP, max_age=_EXPORT_MAX_AGE):
    """清理导出目录：留最新的 `keep` 份，并把超过 `max_age` 的删掉。

    **删了什么要打日志**——静默丢弃在这个项目里是不允许的。
    """
    d = export_dir(cfg)
    try:
        names = [n for n in os.listdir(d) if n.endswith(".txt")]
    except OSError:
        return []
    rows = []
    now = time.time()
    for n in names:
        p = os.path.join(d, n)
        try:
            rows.append((os.path.getmtime(p), p))
        except OSError:
            continue
    rows.sort(reverse=True)
    dead = [p for i, (_m, p) in enumerate(rows) if i >= keep or (now - _m) > max_age]
    for p in dead:
        try:
            os.remove(p)
        except OSError as e:
            _warn(f"清理导出失败（{os.path.basename(p)}）：{e}")
    if dead:
        _warn(f"导出目录清理了 {len(dead)} 份旧文件（只留最近 {keep} 份 / {max_age/86400:.0f} 天内）。")
    return dead


def _export_write(text, name, cfg=None):
    """把全文写进导出目录（**原子写**），返回 (导出 id, 路径)。

    id 取**内容**的 sha1 前 16 位（内容寻址）：同一份文件读多次不会堆一堆副本，
    内容不同的两份也绝不会撞同一个 id。`name` 只是给人看的标签（写在返回文案里），
    **不参与 id** —— 改名不该产生第二份导出。
    """
    d = export_dir(cfg)
    os.makedirs(d, exist_ok=True)
    eid = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    p = os.path.join(d, eid + ".txt")
    tmp = p + f".tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    return eid, p


def _page_from(path, offset, max_chars):
    """从导出文件里取一页。offset 是**字节**偏移（会在 UTF-8 边界上对齐）。"""
    size = os.path.getsize(path)
    off = max(0, min(int(offset), size))
    with open(path, "rb") as f:
        f.seek(off)
        # 对齐：跳过被切开的 UTF-8 续字节（0x80~0xBF），否则页首会出现半个字
        while off < size:
            b = f.read(1)
            if not b:
                break
            if not (0x80 <= b[0] <= 0xBF):
                f.seek(off)
                break
            off += 1
        raw = f.read(max(4, max_chars * 4 + 4))
    page = raw.decode("utf-8", "ignore")[:max_chars]
    nxt = off + len(page.encode("utf-8"))
    return page, nxt, nxt >= size


def extract_page(path=None, cfg=None, cursor=None, on_image=None):
    """读一份文件并返回**一页**，全文导出到本地。返回 `(页文本, 提示语)`。

    * 不给 `cursor`：抽全文 → 导出 → 返回前 `max_chars` 字 + 尾部 `cursor`
    * 给了 `cursor`（`<导出id>:<字节偏移>`）：从导出文件里读下一页
      —— **不需要再给 contact/local_id**，"继续"就是继续，不用重新定位文件。
    """
    max_bytes, max_chars = _cfg(cfg)

    if cursor:
        m = _CURSOR_RE.match(str(cursor).strip())
        if not m:
            return None, (f"cursor 格式不对（{str(cursor)[:40]}）。"
                          f"原样带上上一次结果里 `cursor=` 后面那串就行。")
        eid, off = m.group(1), int(m.group(2))
        d = os.path.realpath(export_dir(cfg))
        side = None
        kind = "v"
        for pre in ("v", "a"):
            cand = os.path.realpath(os.path.join(d, pre + "_" + eid + ".json"))
            if _under_allowed(cand, [d]) and os.path.isfile(cand):
                side, kind = cand, pre
                break
        if side:
            # 这是**视频**的续读 cursor（位置是「第几秒」，不是字节偏移）
            try:
                with open(side, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception as e:
                return None, f"续读标记读不出来（{e}）。要接着读请把文件名再给我一次。"
            if meta.get("done"):
                return None, "这份已经读到末尾了，没有更多内容。"
            vp = str(meta.get("path") or "")
            if not vp or not os.path.isfile(vp):
                return None, ("这个视频已经不在了（移走或删了）。"
                              "要接着读请把文件名再给我一次。")
            start_at = int(meta.get("next") or off)
            if kind == "v":
                text, err = _video(vp, cfg, start_at, on_image=on_image)
            else:
                text, err = _audio(vp, cfg, start_at)
            return (text, None) if text else (None, err)
        p = os.path.realpath(os.path.join(d, eid + ".txt"))
        if not _under_allowed(p, [d]) or not os.path.isfile(p):
            return None, ("这份导出已经不在了（被清理过，或换过机器）。"
                          "要重新读一次那份文件，请把文件名再给我一次。")
        page, nxt, done = _page_from(p, off, max_chars)
        if not page.strip():
            return None, "这份导出已经读到末尾了，没有更多内容。"
        head = f"（续读：从第 {off} 字节开始）\n"
        tail = ("" if done else
                f"\n\n…（还有内容没读完。要接着读就把 cursor 原样带上：cursor={eid}:{nxt}）")
        return head + page + tail, None

    text, err = extract(path, cfg, full=True, on_image=on_image)
    if err:
        return None, err
    if len(text) > _MEM_TEXT_CAP:
        text = text[:_MEM_TEXT_CAP]
        _warn(f"这份文件抽出来的文字超过 {_MEM_TEXT_CAP} 字，只导出前 {_MEM_TEXT_CAP} 字"
              f"（内存保护）。")
    eid, ep = _export_write(text, os.path.basename(str(path or "未知")), cfg)
    sweep_exports(cfg)
    page, nxt, done = _page_from(ep, 0, max_chars)
    if done:
        tail = ""
    elif "cursor=" in text:
        # 视频/长音频**自带分段 cursor**（秒级）。再叠一个字符级 cursor 会让模型拿到
        # 两个 cursor、选错那个 —— 所以这里只报导出位置。
        tail = f"\n\n（全文已导出到本机：{ep}）"
    else:
        tail = (f"\n\n…（这份文件一共 {len(text)} 字，上面是前 {max_chars} 字。"
                f"要接着读就说「继续」（把 cursor 原样带上：cursor={eid}:{nxt}）。"
                f"全文也已导出到本机：{ep}）")
    return page + tail, None


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
    chk(not _looks_garbled("ab"), "短的正常文本**不再**被当乱码（「好的」只有两个字）")
    chk(not _looks_garbled("好的"), "两个汉字的短句也放行")
    chk(_looks_garbled("\x01\x02\x03\x04"), "又短、又没有任何字母/汉字的才算可疑")
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
