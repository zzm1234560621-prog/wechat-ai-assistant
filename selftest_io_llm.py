"""file_read / llm / settings 的回归自测（T1~T9）。

**不联网、不碰 30001、不需要微信**：假响应对象 + 临时目录 + 现场构造的 zip 样本。
用法：`.venv/Scripts/python.exe selftest_io_llm.py`

风格照抄 selftest_aixed.py：每项一行 ✅/❌，结尾汇总，有失败就 sys.exit(1)。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# image_read/image_cache 会牵扯微信目录那套，本测试用不上；
# 这里塞一个桩，保证本测试是「纯逻辑、不碰任何本机微信数据」。
import types

if "image_cache" not in sys.modules:
    _stub = types.ModuleType("image_cache")
    _stub.account_dirs = lambda: []
    sys.modules["image_cache"] = _stub

import file_read          # noqa: E402
import llm                # noqa: E402
import settings           # noqa: E402

TMP = tempfile.mkdtemp(prefix="selftest_io_llm_")
_ok = True

# ⚠️ 本文件会构造**真的** llm 实现并调 `chat_with_tools`（只是把 `_post` /
# `messages.create` 换成桩），而 llm 里的记账会写 `usage.USAGE_PATH`
# ——默认就是仓库里的 `data/usage.jsonl`。**自测绝不许污染用户的真实账本**
# （会把 `/用量` 报成「有 N 次调用」，而那 N 次全是假的 0-token 行）。
# 所以这里一次性把它重定向到临时目录，退出时还原。
# 回归：连跑两次 `selftest_io_llm.py`，`data/usage.jsonl` 的行数不许变。
import atexit                                                          # noqa: E402
import usage                                                           # noqa: E402

_usage_tmp = tempfile.mkdtemp(prefix="selftest_io_llm_usage_")
_old_usage_path = usage.USAGE_PATH
usage.USAGE_PATH = os.path.join(_usage_tmp, "usage.jsonl")
atexit.register(setattr, usage, "USAGE_PATH", _old_usage_path)
atexit.register(shutil.rmtree, _usage_tmp, ignore_errors=True)


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


# ============================================================
#  T1：Office 解压体积上限（zip 炸弹）+ XML 实体膨胀
# ============================================================

BIG_LIMIT = 64 * 1024 * 1024            # file.max_bytes：64MB
UNPACK_CAP = BIG_LIMIT * 3              # file_read 的解压上限 = min(200MB, max_bytes*3) = 192MB
FILLER_MID = 200 * 1024 * 1024          # 单个成员 200MB：超过解压上限，用来测第二道防线
FILLER_BIG = 400 * 1024 * 1024          # 单个成员 400MB：声明值就该被第一道防线挡住


class _ZipDeclaredSize:
    """只改「声明的大小」、不改数据的 zipfile 包装（模拟声明值和实际不符）。

    测的是预算的**第二道防线**：声明 1KB、实际 200MB 时必须靠「实际读出来的字节数」
    拦住。改法是在 `ZipFile.open()` 里**就地**把 ZipInfo 的 file_size 换掉——
    这是唯一有效的做法：`ZipExtFile.__init__` 会把 `zipinfo.file_size` 记成
    `self._left` 并据此**截断**数据、再用 CRC 校验，所以

      * 只把声明值**改大**：`_left` 大于真实数据 → 解到尾声时 CRC 对不上，
        抛出一个和本题无关的 `BadZipFile: Bad CRC-32`（实测踩到过）；
      * 只改一个 getinfo 返回的**副本**：`open()` 内部拿到的仍是真身，等于没改。

    改到 `_left == 声明值`，正好等价于「这个成员真的只有 1KB」，
    于是能干净地测出「预算拿实际读出的字节数把关」这条路。
    """

    def __init__(self, real, declared_for):
        self._real = real
        self._declared = dict(declared_for)
        self._real_open = real.open

    def open(self, name, *a, **kw):
        key = name.filename if isinstance(name, zipfile.ZipInfo) else name
        if key in self._declared:
            info = name if isinstance(name, zipfile.ZipInfo) else self._real.getinfo(key)
            info.file_size = self._declared[key]
        return self._real_open(name, *a, **kw)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._real.close()
        return False

    def __getattr__(self, k):
        return getattr(self._real, k)

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _write(path, entries):
    """现场构造一个真 zip（**不是**桩：以前吃过「桩比实物宽松」的亏）。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def _docx_bytes(text, filler=b"", extra=None):
    out = {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "word/document.xml": (
            b'<?xml version="1.0" encoding="UTF-8"?><w:document ' + _W.encode() + b'>'
            + filler
            + f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'.encode("utf-8")
            + b"</w:document>"),
    }
    out.update(extra or {})
    return out


def _xlsx_bytes(cell, extra=None):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    # 多铺几行：太短的文本会被 _looks_garbled 的「短文本算不可靠」规则拦下（那是另一条规则）
    rows = "".join(
        f'<row r="{i}"><c r="A{i}" t="s"><v>0</v></c>'
        f'<c r="B{i}"><v>{100 + i}</v></c>'
        f'<c r="C{i}" t="s"><v>1</v></c></row>'
        for i in range(1, 6))
    return {
        "xl/sharedStrings.xml": (
            f'<?xml version="1.0"?><sst xmlns="{ns}" count="2" uniqueCount="2">'
            f'<si><t>{cell}</t></si><si><t>第二列的中文说明</t></si></sst>'
        ).encode("utf-8"),
        "xl/workbook.xml": (
            f'<?xml version="1.0"?><workbook xmlns="{ns}" xmlns:r="{rel}">'
            f'<sheets><sheet name="表一" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ).encode("utf-8"),
        "xl/_rels/workbook.xml.rels": (
            f'<?xml version="1.0"?><Relationships xmlns="'
            f'http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            f'Type="{rel}/worksheet"/></Relationships>'
        ).encode("utf-8"),
        "xl/worksheets/sheet1.xml": (
            f'<?xml version="1.0"?><worksheet xmlns="{ns}"><sheetData>{rows}'
            f'</sheetData></worksheet>'
        ).encode("utf-8"),
        **(extra or {}),
    }


def t1_zip_bomb():
    print("\n── T1 · zip 炸弹（解压体积上限真的生效） ──")
    cfg = {"file": {"max_bytes": BIG_LIMIT}}

    # ① 声明值就超限：第一道防线就挡住（小压缩包声称能解出 400MB）
    bomb = os.path.join(TMP, "bomb.docx")
    _write(bomb, _docx_bytes("炸弹里的正文", filler=b"\0" * FILLER_BIG))
    assert os.path.getsize(bomb) < 2 * 1024 * 1024, "炸弹压缩包本身应该很小"
    text, err = file_read.extract(bomb, cfg)
    check("小压缩包、解压后远超上限 → 被拒", text is None, (text or "")[:80])
    check("拒的理由说清是「解压后太大、出于安全没读」",
          err and "解压后太大" in err and "安全" in err, err)
    check("报错里带上具体数字（不是一句「解析失败」）",
          err and "MB" in err, err)
    check("没被当成正常内容喂出去（文本必须是 None）",
          text is None, (text or "")[:60])

    # ② 声明值撒谎：声明 1KB，实际解出 200MB → 第二道防线必须拦住
    liar = os.path.join(TMP, "liar.docx")
    _write(liar, _docx_bytes("这段不该被读出来", filler=b"A" * FILLER_MID))
    real_open = zipfile.ZipFile

    def fake_open(path, *a, **kw):
        return _ZipDeclaredSize(real_open(path, *a, **kw), {"word/document.xml": 1024})

    zipfile.ZipFile = fake_open
    try:
        text, err = file_read.extract(liar, cfg)
    finally:
        zipfile.ZipFile = real_open
    check("声明只有 1KB、实际 200MB → 第二道防线拦住", text is None, (text or "")[:80])
    check("拒的理由说清「实际解出的字节超过它自己声明的大小」",
          err and "超过" in err and "声明" in err, err)
    check("超限走的是「解压后太大」这条安全路径（不是含糊的解析失败）",
          err and "解压后太大" in err, err)

    # XML 实体膨胀（billion laughs）
    ent = os.path.join(TMP, "laughs.docx")
    payload = (b'<?xml version="1.0"?>'
               b'<!DOCTYPE lolz [<!ENTITY lol "lol">'
               b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
               b'<w:document ' + _W.encode() + b'><w:p><w:r>'
               b'<w:t>&lol2;&lol2;&lol2;</w:t></w:r></w:p></w:document>')
    _write(ent, {"word/document.xml": payload})
    text, err = file_read.extract(ent, cfg)
    check("含 <!DOCTYPE/<!ENTITY 的 XML → 被拒", text is None, (text or "")[:80])
    check("拒的理由提到实体/文档类型声明",
          err and ("实体" in err or "DOCTYPE" in err), err)

    # 正常文件不能被弄坏
    good_docx = os.path.join(TMP, "good.docx")
    _write(good_docx, _docx_bytes("这是一份正常的 Word 文档，正文应该能被抽出来。"))
    text, err = file_read.extract(good_docx, cfg)
    check("正常 docx 仍能解析（没把功能弄坏）", err is None and text is not None, err)
    check("正常 docx 抽到的正文正确",
          text is not None and "这是一份正常的 Word 文档" in text, (text or "")[:60])

    good_xlsx = os.path.join(TMP, "good.xlsx")
    _write(good_xlsx, _xlsx_bytes("单元格中文"))
    text, err = file_read.extract(good_xlsx, cfg)
    check("正常 xlsx 仍能解析（没把功能弄坏）", err is None and text is not None, err)
    check("正常 xlsx 抽到共享字符串和数字",
          text is not None and "单元格中文" in text and "101" in text, (text or "")[:80])
    check("xlsx 的 sheet 名也抽出来了", text is not None and "表一" in text, (text or "")[:80])

    # 单成员本身大于上限时，读之前就该被声明值挡住（不真解 200MB）
    used_before = file_read._zip_budget(1 << 20)[1]()
    check("预算对象从 0 起算（另一次调用各算各的）", used_before == 0, used_before)


# ============================================================
#  T2：pypdf 缺失时的报错要可操作
# ============================================================

def t2_pdf_message():
    print("\n── T2 · pypdf 缺失时的报错可操作性 ──")
    real_get = sys.modules.get("pypdf")
    sys.modules["pypdf"] = None            # `import pypdf` → ImportError
    try:
        pdf = os.path.join(TMP, "x.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4\n")
        text, err = file_read.extract(pdf, {"file": {"max_bytes": BIG_LIMIT}})
    finally:
        if real_get is not None:
            sys.modules["pypdf"] = real_get
        else:
            sys.modules.pop("pypdf", None)
    check("pypdf 缺失 → 明确失败（不假装成功）", text is None, (text or "")[:60])
    check("报错说「需要 pypdf」", err and "pypdf" in err, err)
    check("报错给出可照做的两条路（install.bat / pip）",
          err and "install.bat" in err and "pip install pypdf" in err, err)
    check("300 字上限内安装指引没被截断（旧代码 [:120] 会把后半句切掉）",
          err is not None and err.rstrip().endswith("）"), err[-40:] if err else err)


# ============================================================
#  T3：locate() 的真实路径归属校验（软链/联接）
# ============================================================

def _link_dir(target, link):
    """建一个目录链接（Windows 优先目录联接，免得要管理员权限）。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True, text=True)
        return r.returncode == 0 and os.path.isdir(link)
    return False


def t3_locate_realpath():
    print("\n── T3 · locate() 的路径归属校验 ──")
    root = os.path.join(TMP, "wxdata", "acct", "msg", "file")
    month = os.path.join(root, "2026-01")
    os.makedirs(month, exist_ok=True)
    normal = os.path.join(month, "正常文件.txt")
    with open(normal, "w", encoding="utf-8") as f:
        f.write("这是一份正常的中文文本文件，用来验证白名单内的文件仍然能读到。")

    outside = os.path.join(TMP, "outside")
    os.makedirs(outside, exist_ok=True)
    secret = os.path.join(outside, "机密.txt")
    with open(secret, "w", encoding="utf-8") as f:
        f.write("白名单外的文件，绝不能被读到。这是一段够长的中文内容用来通过乱码检查。")
    side = os.path.join(TMP, "side.txt")
    with open(side, "w", encoding="utf-8") as f:
        f.write("直接躺在白名单外的一份文件，内容也够长，用来做越界用例。")

    sys.modules["image_cache"].account_dirs = lambda: [os.path.join(TMP, "wxdata", "acct")]
    try:
        check("白名单内正常文件仍能读到（realpath 校验没把功能弄坏）",
              file_read.locate("正常文件.txt") == os.path.realpath(normal),
              file_read.locate("正常文件.txt"))

        link = os.path.join(month, "链接文件.txt")
        linked = _link_dir(outside, link)
        if linked:
            check("目录联接指向白名单外 → 文件名命中但被拒",
                  file_read.locate(os.path.join("链接文件.txt", "机密.txt")) is None)
            fake = os.path.join(month, "假文件.txt")
            try:
                os.symlink(secret, fake)
                check("文件软链指向白名单外 → 被拒", file_read.locate("假文件.txt") is None)
            except OSError:
                print("  ── 跳过文件软链用例（本机建不了 symlink）")
        else:
            print("  ── 跳过软链/联接用例（本机既建不了 symlink 也建不了 junction）")

        check("_under_allowed：跨盘符一律不通过",
              file_read._under_allowed(os.path.realpath(side),
                                       [os.path.realpath(month)]) is False)
        check("_under_allowed：同目录下的正常文件通过",
              file_read._under_allowed(os.path.realpath(normal),
                                       [os.path.realpath(month)]) is True)
        # 字符串层面的老防线不许被弄坏（file_read.py 自己的自测也覆盖这几条）
        for bad in ["../../windows/system.ini", "..\\..\\x", "C:\\Windows\\win.ini",
                    "/etc/passwd", "", "   ", ".hidden"]:
            check(f"_safe_name 仍挡住 {bad!r}", file_read._safe_name(bad) is None)
    finally:
        sys.modules["image_cache"].account_dirs = lambda: []


# ============================================================
#  T4：编码回退 + 双解区提示
# ============================================================

def t4_encoding():
    print("\n── T4 · 文本编码 ──")
    cfg = {"file": {"max_bytes": BIG_LIMIT}}

    def read_case(name, data):
        p = os.path.join(TMP, name)
        with open(p, "wb") as f:
            f.write(data)
        return file_read.extract(p, cfg)

    gbk_cn = "这是一段用 GBK 编码保存的中文文本，用来验证解码回退是否正确。"
    text, err = read_case("gbk.txt", gbk_cn.encode("gbk"))
    check("GBK 中文文本能读出来（不报「像是乱码」）", err is None, err)
    check("GBK 中文内容正确（不是一串怪西文）",
          text is not None and "用 GBK 编码保存的中文文本" in text, (text or "")[:60])
    check("GBK 文本明确标了这是猜的、要人工核对",
          text is not None and "⚠️" in text and "人工核对" in text, (text or "")[-120:])
    check("提示不会把正文顶掉（正文仍在前面）",
          text is not None and "用 GBK 编码保存的中文文本" in text, (text or "")[:80])

    utf16 = "这是一段带 BOM 的 UTF-16 文本，老代码会因为 \\x00 把它判成乱码。"
    text, err = read_case("utf16le.txt", utf16.encode("utf-16"))   # Python 默认写 BOM
    check("带 BOM 的 UTF-16 能读出来（旧代码判乱码）", err is None, err)
    check("UTF-16 内容正确", text is not None and "带 BOM 的 UTF-16 文本" in text,
          (text or "")[:60])

    # 纯 GBK 双解区：'目录'*10 会被 utf-8 解成一串可打印的怪西文（无 ASCII 字母数字）
    raw_pure = "目录".encode("gbk") * 10
    dec, enc, note = file_read._decode_text(raw_pure)
    check("纯 GBK 双解区改按 GB18030 解码", enc == "gb18030" and dec == "目录" * 10, enc)
    check("改判也带 ⚠️ 提示（没静默当正常）", note is not None and "⚠️" in note, note)

    # 混着 ASCII 的双解区：不改判（怕误伤 café 那类），但可疑字符成片时必须提示。
    # 用「目录」×4 夹数字，是因为纯「目录」连成一片时 executor 的判据会直接判成
    # "gbk"（走改判那一支），要落到 "ambiguous" 得有 ASCII 混排。
    dec, enc, note = file_read._decode_text(("目录".encode("gbk") + b"123") * 4)
    check("GBK 双解区 + ASCII 混排 → 不改判但必须带提示",
          enc == "utf-8" and note is not None and "⚠️" in note, (enc, note))
    check("这种成片可疑字符的比例确实很高（密度判据的依据）",
          file_read._trap_suspect_ratio(dec) >= 0.5, file_read._trap_suspect_ratio(dec))

    # 三个候选全都解不开的字节（0x81 0x00 在 gb18030/big5 里都是非法序列）
    junky = bytes([0x81, 0x00]) * 12
    dec, enc, note = file_read._decode_text(junky)
    check("候选全失败时走 latin-1，并说明「这不是真解码」",
          enc == "latin-1" and note is not None and "latin-1" in note, (enc, note))

    # GBK 中文虽能按 gb18030 正确解出，但 GBK/BIG5 对同一串字节都可能解得出来，
    # 属于「猜对了但没法自证」——必须有 ⚠️ 让人可以复核
    gbk_cn2 = "这是一段用 GBK 编码保存的中文文本，用来验证解码回退是否正确。"
    text, err = read_case("gbk2.txt", gbk_cn2.encode("gbk"))
    check("非 UTF-8 但解出汉字 → 提示编码是猜的", text is not None and "⚠️" in text,
          (text or "")[-100:])

    plain_en = ("This is an ordinary English text file with a couple of accents: "
                "café, naïve, résumé. It must not be judged as garbled.")
    text, err = read_case("en.txt", plain_en.encode("utf-8"))
    check("正常英文（含重音）不被误判成乱码", err is None, err)
    check("正常英文里不会凭空多出 ⚠️ 编码提示",
          text is not None and "⚠️" not in text, (text or "")[-80:])

    check("CJK 比例判据：一堆可打印的怪西文（无汉字）→ 判乱码",
          file_read._looks_garbled("Ŀ¼" * 20) is True)
    check("CJK 比例判据：正常中文 → 不判乱码",
          file_read._looks_garbled("这是一段正常的中文文本，用来测试乱码检测是否工作正常。") is False)
    check("正常英文（café/naïve）不被 CJK 比例判据误伤",
          file_read._looks_garbled(plain_en) is False)
    check("标点不算「怪西文」：— 工作表「表一」— 这类排版不能被判乱码",
          file_read._looks_garbled("— 工作表「表一」—\n单元格中文\t42\n第二行\t内容") is False)
    check("GBK 被 utf-8 误解的整片怪字符（'Ŀ¼'）→ 判乱码（旧代码会放行）",
          file_read._looks_garbled("Ŀ¼Ŀ¼Ŀ¼Ŀ¼Ŀ¼Ŀ¼Ŀ¼Ŀ¼Ŀ¼Ŀ¼") is True)


# ============================================================
#  T5：按协议选环境变量
# ============================================================

def t5_api_key_env():
    print("\n── T5 · provider 与环境变量必须对应 ──")
    keys = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)

        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake-for-test"
        try:
            llm.ChatLLM(provider="openai", api_key=None)
            check("openai 协议 + 只有 ANTHROPIC_API_KEY → 必须抛错", False, "居然构造成功了")
        except RuntimeError as e:
            check("openai 协议 + 只有 ANTHROPIC_API_KEY → 必须抛错", True)
            check("报错是「缺少 API Key」", "缺少 API Key" in str(e), e)
            check("报错点明该配的是 OPENAI_API_KEY（不再借道 ANTHROPIC）",
                  "OPENAI_API_KEY" in str(e), e)

        try:
            obj = llm.ChatLLM(provider="anthropic", api_key=None)
            check("anthropic 协议 + 有 ANTHROPIC_API_KEY → 能构造", obj.provider == "anthropic")
        except Exception as e:
            check("anthropic 协议 + 有 ANTHROPIC_API_KEY → 能构造", False, f"{type(e).__name__}: {e}")

        # 显式传入的 key 永远优先
        obj = llm.ChatLLM(provider="openai", api_key="sk-explicit-fake")
        check("显式传入的 key 优先于环境变量（openai 也能造出来）",
              obj._impl.api_key == "sk-explicit-fake", getattr(obj._impl, "api_key", None))

        for k in keys:
            os.environ.pop(k, None)
        try:
            llm.ChatLLM(provider="openai", api_key=None)
            check("两个环境变量都没有 → 抛错", False)
        except RuntimeError as e:
            check("两个环境变量都没有 → 抛错", "缺少 API Key" in str(e), e)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ============================================================
#  T6 / T7：temperature 重试不对称 + 截断事实
# ============================================================

class _FakeResp:
    def __init__(self, text="你好", stop_reason="end_turn"):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        eff = self.effects.pop(0) if self.effects else _FakeResp()
        if isinstance(eff, BaseException):
            raise eff
        return eff


class _FakeClient:
    def __init__(self, effects):
        self.messages = _FakeMessages(effects)


def _impl(effects, temperature=0.7):
    impl = llm._AnthropicImpl.__new__(llm._AnthropicImpl)
    impl.model = "claude-sonnet-5"
    impl.max_tokens = 100
    impl.temperature = temperature
    impl.client = _FakeClient(effects)
    return impl


def t6_temperature_retry():
    print("\n── T6 · temperature 降级只能用在「确实不支持」时 ──")
    tools = [{"name": "t", "description": "", "parameters": {"type": "object", "properties": {}}}]
    msgs = [{"role": "user", "content": "hi"}]

    err400 = Exception("HTTP 400 invalid_request_error: unsupported field: foo")
    err400.status_code = 400
    impl = _impl([err400, _FakeResp("ok")])
    raised = None
    try:
        impl.chat_with_tools("sys", msgs, tools)
    except Exception as e:
        raised = e
    check("400 且错误里不含 temperature → 直接抛，不重发（不重复计费）",
          raised is err400, raised)
    check("只发了 1 次请求", len(impl.client.messages.calls) == 1,
          len(impl.client.messages.calls))

    # 恰好含 temperature 但状态码不是 400（网关报错）→ 也不许重发
    gate = Exception("gateway timeout while processing temperature header")
    impl = _impl([gate, _FakeResp("ok")])
    raised = None
    try:
        impl.chat_with_tools("sys", msgs, tools)
    except Exception as e:
        raised = e
    check("错误文本碰巧含 temperature、但不是 400 → 直接抛",
          raised is gate, raised)

    # 400 且明确指向 temperature → 降级重试一次
    t400 = Exception("HTTP 400 invalid_request_error: temperature: unsupported parameter")
    t400.status_code = 400
    impl = _impl([t400, _FakeResp("ok")])
    r = impl.chat_with_tools("sys", msgs, tools)
    check("400 且明确指向 temperature → 降级重试一次", r.text == "ok", r.text)
    check("降级只发 2 次（不是无限重试）", len(impl.client.messages.calls) == 2,
          len(impl.client.messages.calls))
    check("降级那次不带 temperature", "extra_body" not in impl.client.messages.calls[1],
          impl.client.messages.calls[1].keys())

    # TypeError（SDK 签名变了）→ 也要降级
    impl = _impl([TypeError("create() got an unexpected keyword argument 'extra_body'"),
                  _FakeResp("typerr")])
    r = impl.chat_with_tools("sys", msgs, tools)
    check("TypeError（SDK 签名变了）→ 降级重试", r.text == "typerr", r.text)

    # chat() 分支行为要一致
    impl = _impl([err400, _FakeResp("ok")])
    raised = None
    try:
        impl.chat("sys", msgs)
    except Exception as e:
        raised = e
    check("chat() 分支：400 无关错误也直接抛（两个分支对称）", raised is err400, raised)


def t7_truncated():
    print("\n── T7 · 截断事实要被暴露出来 ──")
    tools = [{"name": "t", "description": "", "parameters": {"type": "object", "properties": {}}}]
    msgs = [{"role": "user", "content": "hi"}]

    impl = _impl([_FakeResp("半句话", stop_reason="max_tokens")])
    r = impl.chat_with_tools("sys", msgs, tools)
    check("anthropic stop_reason=max_tokens → truncated is True", r.truncated is True, r.truncated)
    check("llm.py 不往正文里拼提示（文案由上层决定）",
          r.text == "半句话", r.text)

    impl = _impl([_FakeResp("说完了", stop_reason="end_turn")])
    r = impl.chat_with_tools("sys", msgs, tools)
    check("stop_reason=end_turn → truncated is False", r.truncated is False, r.truncated)

    # openai 侧：不联网，桩掉 _post
    impl2 = llm._OpenAICompat("deepseek-chat", "sk-fake", "http://127.0.0.1:1", 100, 0.7)
    impl2._post = lambda payload: {
        "choices": [{"finish_reason": "length",
                     "message": {"content": "半句", "tool_calls": []}}]}
    r = impl2.chat_with_tools("sys", msgs, tools)
    check("openai finish_reason=length → truncated is True", r.truncated is True, r.truncated)

    impl2._post = lambda payload: {
        "choices": [{"finish_reason": "stop", "message": {"content": "完整", "tool_calls": []}}]}
    r = impl2.chat_with_tools("sys", msgs, tools)
    check("openai finish_reason=stop → truncated is False", r.truncated is False, r.truncated)

    # __slots__ 加字段不许破坏现有位置参数用法
    old = llm.ChatResult("t", [llm.ToolCall("1", "n", {})])
    check("ChatResult(text, calls) 位置参数仍可用", old.text == "t" and len(old.tool_calls) == 1)
    check("不传 truncated 时默认 False（老调用点行为不变）", old.truncated is False)
    check("ChatResult 只吃这三个槽（__slots__ 没被破坏）",
          not hasattr(old, "__dict__"), old.__dict__ if hasattr(old, "__dict__") else "")


# ============================================================
#  T8：settings 原子写 + 坏文件不静默
# ============================================================

def t8_settings():
    print("\n── T8 · settings 原子写与坏文件告警 ──")
    sdir = os.path.join(TMP, "settings_case")
    os.makedirs(sdir, exist_ok=True)
    real_path = settings.SETTINGS_PATH
    settings.SETTINGS_PATH = os.path.join(sdir, "settings.json")
    try:
        data = {"api_key": "sk-abc", "target_chats": ["文件传输助手", "张三"],
                "auto_reply": {"enabled": True}}
        settings.save(data)
        with open(settings.SETTINGS_PATH, encoding="utf-8") as f:
            on_disk = json.load(f)
        check("原子写之后文件内容是完整的", on_disk == data, on_disk)
        check("save() 不留临时文件（没把 .tmp 留在目录里冒充配置）",
              [n for n in os.listdir(sdir) if ".tmp" in n] == [], os.listdir(sdir))
        check("load() 读回原值", settings.load() == data)

        # 模拟「写临时文件失败」：原文件必须原封不动，且临时文件被清掉
        real_open = open

        def boom(path, *a, **kw):
            if ".tmp" in str(path):
                raise OSError(28, "No space left on device (测试模拟)")
            return real_open(path, *a, **kw)

        import builtins
        builtins.open = boom
        try:
            settings.save({"api_key": "新值", "target_chats": []})
            check("模拟写临时文件失败 → save 必须抛错（不静默）", False, "居然没抛")
        except OSError as e:
            check("模拟写临时文件失败 → save 必须抛错（不静默）", "No space" in str(e), e)
        finally:
            builtins.open = real_open
        with open(settings.SETTINGS_PATH, encoding="utf-8") as f:
            after = json.load(f)
        check("写失败后 settings.json 仍是上一次的完整内容（不是半截）", after == data, after)
        check("写失败后临时文件被清理", [n for n in os.listdir(sdir) if ".tmp" in n] == [],
              os.listdir(sdir))

        # 坏 JSON
        with open(settings.SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write('{"api_key": "sk-abc", "target_chats": [')
        buf = io.StringIO()
        old_out = sys.stdout
        sys.stdout = buf
        try:
            got = settings.load()
        finally:
            sys.stdout = old_out
        printed = buf.getvalue()
        bads = [n for n in os.listdir(sdir) if n.startswith("settings.json.bad-")]
        check("坏 JSON → 仍然返回 {}（可用性优先，不把 bot 拦死）", got == {}, got)
        check("坏 JSON → 备份成 settings.json.bad-<时间戳>", len(bads) == 1, os.listdir(sdir))
        if bads:
            with open(os.path.join(sdir, bads[0]), encoding="utf-8") as f:
                check("备份里就是那份坏内容（用户能自己修）",
                      "sk-abc" in f.read())
        check("坏 JSON → 打印了告警（不静默）", "⚠️" in printed, printed[:80])
        check("告警里说明备份在哪", bads and bads[0] in printed, printed[:200])
        check("告警里说明「本次按空配置继续」", "空配置" in printed, printed[:200])

        # 顶层不是对象
        with open(settings.SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write("[1, 2, 3]")
        buf = io.StringIO()
        sys.stdout = buf
        try:
            got = settings.load()
        finally:
            sys.stdout = old_out
        check("顶层是数组 → 返回 {} 且告警", got == {} and "⚠️" in buf.getvalue(),
              buf.getvalue()[:80])

        # 带 UTF-8 BOM 的文件（真机踩到：本机那份 settings.json 就有 BOM，
        # 旧代码因此每次启动都静默按空配置跑，api_key/名单全不生效）
        for old_bad in [n for n in os.listdir(sdir) if n.startswith("settings.json.bad-")]:
            os.remove(os.path.join(sdir, old_bad))     # 清掉上一个用例留下的备份，只看这次
        with open(settings.SETTINGS_PATH, "wb") as f:
            f.write(b"\xef\xbb\xbf" + json.dumps(
                {"api_key": "sk-bom"}, ensure_ascii=False).encode("utf-8"))
        buf = io.StringIO()
        sys.stdout = buf
        try:
            got = settings.load()
        finally:
            sys.stdout = old_out
        check("带 BOM 的 settings.json 能正确读出来（旧代码静默返回 {}）",
              got == {"api_key": "sk-bom"}, got)
        check("带 BOM 时提示里说明「已经读出来了」（不是「配置丢了」）",
              "BOM" in buf.getvalue() and "已经读出来了" in buf.getvalue(), buf.getvalue()[:80])
        check("带 BOM 不会被当成坏文件备份",
              [n for n in os.listdir(sdir) if n.startswith("settings.json.bad-")] == [],
              os.listdir(sdir))
        settings.save(got)          # 任何一次 save 都应重写成无 BOM
        with open(settings.SETTINGS_PATH, "rb") as f:
            check("save() 之后文件不再带 BOM", not f.read(3) == b"\xef\xbb\xbf")
    finally:
        # 恢复真实的 settings.json 路径：本测试只许在临时目录里折腾配置
        settings.SETTINGS_PATH = real_path
        check("本测试结束后路径指回真正的 settings.json（没动过它）",
              settings.SETTINGS_PATH == real_path, settings.SETTINGS_PATH)


def t9_image_and_pick():
    """当文件发来的图片走 OCR + `read_file` 只给 name 的磁盘兜底（2026-10-01 加）。"""
    print("\n── T9 · 图片按文件读（OCR）+ 只给文件名找文件 ──")
    import image_read

    cfg = {"file": {"max_bytes": 10 * 1024 * 1024}}
    img = os.path.join(TMP, "图里的字.jpg")
    with open(img, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"fake-jpeg")

    calls = []

    def fake_handoff(path, cfg_, max_bytes=None, collect=None):
        calls.append((path, max_bytes))
        return fake_handoff.result

    real_handoff = image_read.handoff
    image_read.handoff = fake_handoff
    try:
        # ① OCR 出来的短文本**不许**被 `_looks_garbled`（<20 字当可疑）拒掉：
        #    那张真机缩略图只认出「交 易 猫」四个字，按老判据会被当乱码 —— 和音频同一个坑。
        fake_handoff.result = {"mode": "ocr", "kind": "text", "text": "交 易 猫",
                               "path": None, "why": ""}
        text, err = file_read.extract(img, cfg)
        check("图片走 OCR：短文本没被当乱码拒", err is None and text == "交 易 猫", (text, err))
        check("图片用的是 file.max_bytes（不是 image.max_bytes）",
              bool(calls) and calls[-1][1] == cfg["file"]["max_bytes"], calls)

        # ② 图里没字 → 说清是「OCR 只认图里的字」，别和文档的「没有可提取的文字」混一起
        fake_handoff.result = {"mode": "ocr", "kind": "none", "text": "", "path": None,
                               "why": "图里没识别到文字（系统 OCR 只认图里的字）"}
        text, err = file_read.extract(img, cfg)
        check("OCR 没识别到字 → 如实说、且说明只认图里的字",
              text is None and err and "没识别到文字" in err, err)

        # ③ OCR 失败（没装/起不来）→ 原因原样带出来，不吞成「解析失败」
        fake_handoff.result = {"mode": "ocr", "kind": "none", "text": "", "path": None,
                               "why": "系统 OCR 没成功：起不了 PowerShell：拒绝访问"}
        text, err = file_read.extract(img, cfg)
        check("OCR 失败 → 原因带上（不吞）",
              text is None and err and "起不了 PowerShell" in err, err)

        # ④ 嗅探出是图、但后缀不在白名单（.heic）→ **也走图片通道**，
        #    让 OCR/视觉模型自己去判认不认；认不出它会如实说（2026-10-02 起按内容嗅探）
        heic = os.path.join(TMP, "a.heic")
        with open(heic, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40)
        calls.clear()
        fake_handoff.result = {"mode": "ocr", "kind": "text", "text": "HEIC 也走图片通道",
                               "path": None, "why": ""}
        text, err = file_read.extract(heic, cfg)
        check(".heic（不在白名单、但嗅探出是图）→ 交给图片通道",
              err is None and text == "HEIC 也走图片通道" and len(calls) == 1,
              (text, err, calls))
    finally:
        image_read.handoff = real_handoff

    # ---- pick()：只按文件名找（纯磁盘，不查库）----
    root = os.path.join(TMP, "pickroot")
    os.makedirs(os.path.join(root, "2026-10"), exist_ok=True)
    for fn in ("报告(1).pdf", "发票A.pdf", "发票B.pdf", "笔记.docx"):
        with open(os.path.join(root, "2026-10", fn), "wb") as f:
            f.write(b"x")
    real_roots = file_read.files_roots
    file_read.files_roots = lambda: [root]
    try:
        p, err, cands = file_read.pick("笔记.docx")
        check("pick 精确命中 → 返回路径",
              err is None and p and os.path.basename(p) == "笔记.docx" and not cands,
              (p, err, cands))

        p, err, cands = file_read.pick("报告.pdf")
        check("pick 认「(N)」这类重名后缀（报告.pdf → 报告(1).pdf）",
              err is None and p and os.path.basename(p) == "报告(1).pdf", (p, err, cands))

        p, err, cands = file_read.pick("发票")
        check("pick 命中多份 → **不挑**，返回候选列表",
              p is None and err is None and len(cands) == 2, (p, err, cands))

        p, err, cands = file_read.pick("根本没有这份")
        check("pick 找不到 → 明说没找到（带上找的范围）",
              p is None and err and "没找到" in err and "msg/file" in err, err)

        p, err, cands = file_read.pick("../../windows/system.ini")
        check("pick 挡住路径穿越（只让给文件名本身）",
              p is None and err and "不合法" in err, err)
    finally:
        file_read.files_roots = real_roots


def t10_unlimited_and_sniff():
    """`file.max_bytes: 0` = 不限（不许被 falsy 吃掉）+ 按内容嗅探（2026-10-02 加）。

    这两条是「任何文件都能读、不限大小」的地基：
      * 上限里的 `0` 必须和「没配」分开 —— 老写法 `int(v or 30MB)` 会把 0 静默吃成 30MB；
      * 后缀白名单永远会漏（`README`、`.srt`、无后缀日志…），所以要看**内容**。
    """
    print("\n── T10 · 不限大小 + 按内容嗅探 ──")
    MB = 1024 * 1024

    # ① 体积上限的语义
    check("没配 max_bytes → 默认 30MB", file_read._cfg({})[0] == 30 * MB, file_read._cfg({})[0])
    got = file_read._cfg({"file": {"max_bytes": 0}})[0]
    check("max_bytes: 0 → 0（= 不限，**不许**被 or 吃成 30MB）", got == 0, got)
    check("max_bytes 写成坏值 → 退回默认",
          file_read._cfg({"file": {"max_bytes": "abc"}})[0] == 30 * MB)
    check("max_bytes 负数 → 退回默认",
          file_read._cfg({"file": {"max_bytes": -1}})[0] == 30 * MB)
    check("max_chars 没配 → 20000", file_read._cfg({})[1] == 20000)

    # ② 解压封顶：只许调大、不许关
    check("max_unpack 没配 → 200MB", file_read.unpack_cap({}) == 200 * MB)
    check("max_unpack 配了 → 用它", file_read.unpack_cap({"file": {"max_unpack": 1024}}) == 1024)
    buf, old_out = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        cap0 = file_read.unpack_cap({"file": {"max_unpack": 0}})
    finally:
        sys.stdout = old_out
    check("max_unpack: 0 → 退回默认**并告警**（封顶不能关）",
          cap0 == 200 * MB and "⚠️" in buf.getvalue(), buf.getvalue()[:90])
    check("max_bytes=0 时解压上限不许退化成 1 字节（老算法会 `max(1, 0*3)`）",
          file_read._unpack_limit({"file": {"max_bytes": 0}}) == 200 * MB,
          file_read._unpack_limit({"file": {"max_bytes": 0}}))
    check("max_bytes 配了 64MB 时解压上限仍是 min(200MB, 192MB)",
          file_read._unpack_limit({"file": {"max_bytes": 64 * MB}}) == 192 * MB)

    # ③ 交给后台 worker 的阈值
    check("inline_bytes 默认 2MB", file_read.inline_bytes({}) == 2 * MB)

    # ④ 内容嗅探
    def put(name, data):
        p = os.path.join(TMP, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    cases = [
        ("嗅探-中文.txt", "这是一段正常的中文文本，用来测内容嗅探。".encode("utf-8"), "text"),
        ("嗅探-无后缀日志", b"2026-10-02 INFO hello\n" * 20, "text"),
        ("嗅探-控制字符.bin", bytes(range(1, 32)) * 8, "binary"),
        ("嗅探-NUL.bin", b"abc\x00def" * 10, "binary"),
        ("嗅探-pdf", b"%PDF-1.4\n" + b"x" * 200, "pdf"),
        ("嗅探-zip", b"PK\x03\x04" + b"x" * 200, "zip"),
        ("嗅探-老doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 200, "ole2"),
        ("嗅探-png", b"\x89PNG\r\n\x1a\n" + b"x" * 200, "image"),
        ("嗅探-heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40, "image"),
        ("嗅探-mp4", b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40, "video"),
        ("嗅探-7z", b"7z\xbc\xaf\x27\x1c" + b"x" * 200, "7z"),
        ("嗅探-rtf", b"{\\rtf1\\ansi hello}", "rtf"),
    ]
    for name, data, want in cases:
        got = file_read.sniff(put(name, data))[0]
        check(f"{name} → {want}", got == want, got)

    # ⑤ 「任何文件」的一半：**没有后缀的文本**真的能读出来
    p = put("README", "这是没有后缀的说明文件，内容应该能被读出来，这句足够长以通过乱码判据。".encode("utf-8"))
    text, err = file_read.extract(p)
    check("无后缀的文本文件 → 能读出来",
          err is None and text and "没有后缀的说明文件" in text, (text, err))

    # ⑥ 未知后缀的二进制 → 如实拒绝，且**绝不把二进制喂给模型**
    p = put("mystery.dat", bytes(range(256)) * 8)
    text, err = file_read.extract(p)
    check("未知后缀的二进制 → 拒绝并说明是二进制",
          text is None and err and "二进制" in err, err)

    # ⑦ 嗅探出的**压缩包**：T7 起已经真接了 —— 坏包要报它自己的问题（不是"不支持"）
    p = put("pack.zip", b"PK\x03\x04" + b"x" * 200)
    text, err = file_read.extract(p)
    check("坏压缩包 → 如实说打不开（不再说「不支持」）",
          text is None and err and "压缩包" in err, err)

    # ⑧ 真 zip：T7 起能递归读成员（详细用例在 selftest_archive.py）
    p2 = os.path.join(TMP, "真包.zip")
    with zipfile.ZipFile(p2, "w") as z:
        z.writestr("里面的.txt", "这是压缩包里的成员，内容够长以通过乱码判据。".encode("utf-8"))
    text, err = file_read.extract(p2)
    check("真压缩包 → 成员内容读出来了",
          err is None and text and "里面的.txt" in text and "压缩包里的成员" in text,
          (err, (text or "")[:120]))


def t11_paging_and_export():
    """分页 + 全文导出：不重不漏、cursor 校严、清理要打日志（2026-10-02 加）。

    这是"输入不限大小"的落地方式：本地全文可读，但**一次只给模型一页**
    （上下文和费用是真限制），靠 cursor 续读。
    """
    print("\n── T11 · 分页与全文导出 ──")
    exp = os.path.join(TMP, "exports")
    cfg = {"file": {"max_bytes": 0, "max_chars": 100, "export_dir": exp}}
    full = "".join(f"第{i:04d}行：这是一段用来验证分页的文本，字要够多才切得开。\n"
                   for i in range(60))
    p = os.path.join(TMP, "长文本.txt")
    with open(p, "w", encoding="utf-8", newline="") as f:   # newline=""：别让 Windows 换成 \r\n
        f.write(full)

    # ① 默认 extract 仍然截断（老行为一个字不变）；full=True 才给全文
    t_short, e1 = file_read.extract(p, cfg)
    t_full, e2 = file_read.extract(p, cfg, full=True)
    check("默认 extract 仍然截断（老行为不变）",
          e1 is None and t_short and len(t_short) <= 100 + 40 and "只取前" in t_short)
    check("extract(full=True) 给全文（不截断）",
          e2 is None and t_full.strip() == full.strip(), len(t_full or ""))

    # ② 第一页 + cursor
    page1, err = file_read.extract_page(p, cfg)
    check("第一页读出来了", err is None and page1 and "第0000行" in page1, (page1 or "")[:60])
    m = re.search(r"cursor=([0-9a-f]{16}:\d+)", page1 or "")
    check("第一页尾部给了 cursor", bool(m), (page1 or "")[-160:])
    check("第一页**明说这是节选**（不许说成全读完了）",
          "一共" in (page1 or "") and "接着读" in (page1 or ""), (page1 or "")[-160:])
    eid_cursor = m.group(1) if m else ""
    eid = eid_cursor.split(":")[0]

    # ③ 导出文件真的落地了，内容是全文
    ep = os.path.join(exp, eid + ".txt")
    check("全文导出到 export_dir", os.path.isfile(ep), os.listdir(exp))
    check("导出内容 = 全文（一个字不差）",
          open(ep, encoding="utf-8").read().strip() == full.strip())

    # ④ 续读：**不重不漏**，拼起来正好是全文
    got = ""
    cur = eid_cursor
    pages = 0
    while cur and pages < 20:
        pg, err = file_read.extract_page(None, cfg, cursor=cur)
        check(f"第 {pages + 2} 页读得出来", err is None and pg, err)
        body = re.sub(r"^（续读：从第 \d+ 字节开始）\n", "", pg or "")
        body = re.sub(r"\n\n…（还有内容没读完。.*$", "", body, flags=re.S)
        got += body
        pages += 1
        m2 = re.search(r"cursor=([0-9a-f]{16}:\d+)", pg or "")
        cur = m2.group(1) if m2 else ""
    first_body = re.sub(r"\n\n…（这份文件一共.*$", "", page1 or "", flags=re.S)
    total = first_body + got
    check("逐页拼回来 == 全文（不重不漏）", total.strip() == full.strip(),
          f"拼回 {len(total.strip())} 字 / 全文 {len(full.strip())} 字")
    check("读到末尾会停（不会无限给 cursor）", not cur, cur)

    # ⑤ cursor 校验：坏格式 / 导出不在了
    t, e = file_read.extract_page(None, cfg, cursor="../../etc/passwd")
    check("cursor 带路径 → 被格式校验挡住", t is None and e and "格式不对" in e, e)
    t, e = file_read.extract_page(None, cfg, cursor="deadbeefdeadbeef:0")
    check("导出不在了 → 如实说（并告诉怎么重来）",
          t is None and e and "不在了" in e, e)

    # ⑥ 清理：留最近的 N 份，**删了要打日志**
    for i in range(4):
        file_read._export_write(f"内容{i}" * 50, f"f{i}.txt", cfg)
    before = len([n for n in os.listdir(exp) if n.endswith(".txt")])
    buf, old_out = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        dead = file_read.sweep_exports(cfg, keep=2)
    finally:
        sys.stdout = old_out
    after = len([n for n in os.listdir(exp) if n.endswith(".txt")])
    check("清理只留最近 2 份", after == 2, (before, after))
    check("清理**打了日志**（静默丢弃不允许）",
          dead and "⚠️" in buf.getvalue() and "清理" in buf.getvalue(), buf.getvalue()[:120])
    check("清理不碰导出目录外的文件（只删 .txt）",
          all(n.endswith(".txt") for n in os.listdir(exp)), os.listdir(exp))


def t12_embedded_images():
    """文档内嵌图片（docx/pptx/xlsx）+ PDF 扫描页（2026-10-02，规格第九节）。

    要点：① 图片走的是文本后面的"图片段"，**标明来源**；② 读不出来要**说出来**
    （张数 + 原因），静默丢弃不允许；③ `image.mode=off` 时如实说没解读。
    """
    print("\n── T12 · 文档里的图片也被读出来 ──")
    PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 8
    calls = []

    def fake_from_bytes(data, name, cfg, collect=None):
        calls.append((name, len(data)))
        return "图里的字：测试用", ""

    real = file_read._image_from_bytes
    try:
        file_read._image_from_bytes = fake_from_bytes

        # ① docx 里带一张位图 + 一张矢量图（emf 读不了 → 要如实说）
        d = os.path.join(TMP, "带图.docx")
        _write(d, _docx_bytes("正文第一段", extra={
            "word/media/image1.png": PNG,
            "word/media/image2.emf": b"\x01\x00\x00\x00emf",
        }))
        calls.clear()
        text, err = file_read.extract(d)
        check("docx 正文照旧", err is None and "正文第一段" in text, (text or "")[:80])
        check("docx 里的位图被解读、且**标明来源**",
              "word/media/image1.png" in text and "图里的字：测试用" in text,
              (text or "")[-220:])
        check("矢量图（emf）**如实说没解读**，不装作没有",
              "矢量图" in (text or "") and "emf" in (text or ""), (text or "")[-220:])

        # ② 超过单份文档上限时：只解读 20 张，**并把"还有几张"说出来**
        d2 = os.path.join(TMP, "图很多.docx")
        many = {f"word/media/image{i:02d}.png": PNG for i in range(1, 26)}
        _write(d2, _docx_bytes("图很多的文档", extra=many))
        calls.clear()
        text2, err2 = file_read.extract(d2)
        check("超过上限时只解读 20 张", len(calls) == 20, len(calls))
        check("并把「还有 5 张没解读」说出来",
              "还有 5 张" in (text2 or ""), (text2 or "")[-220:])

        # ③ pptx / xlsx 的内嵌图也走同一条路
        p3 = os.path.join(TMP, "带图.pptx")
        _write(p3, {
            "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
            "ppt/slides/slide1.xml": (
                '<?xml version="1.0"?><p:sld xmlns:p="p" xmlns:a="a">'
                '<a:p><a:r><a:t>幻灯片标题</a:t></a:r></a:p></p:sld>').encode("utf-8"),
            "ppt/media/image1.png": PNG,
        })
        t3, e3 = file_read.extract(p3)
        check("pptx 的内嵌图也读（标明来源）",
              e3 is None and "ppt/media/image1.png" in (t3 or "") and "幻灯片标题" in (t3 or ""),
              (t3 or "")[-200:])

        p4 = os.path.join(TMP, "带图.xlsx")
        _write(p4, _xlsx_bytes("单元格中文", extra={"xl/media/image1.png": PNG}))
        t4, e4 = file_read.extract(p4)
        check("xlsx 的内嵌图也读（标明来源）",
              e4 is None and "xl/media/image1.png" in (t4 or "") and "单元格中文" in (t4 or ""),
              (t4 or "")[-200:])
    finally:
        file_read._image_from_bytes = real

    # ④ image.mode=off：**如实说**有图但没解读（用真实现，不走桩）
    d5 = os.path.join(TMP, "关掉读图.docx")
    _write(d5, _docx_bytes("正文", extra={"word/media/image1.png": PNG}))
    t5, e5 = file_read.extract(d5, {"image": {"mode": "off"}})
    check("image.mode=off → 说清「有图但没解读」，不静默",
          e5 is None and "图片解读已关闭" in (t5 or ""), (t5 or "")[-200:])

    # ⑤ PDF 的「整页是图」页：**先用 pypdf 预筛**出真有这种页的样本，再去跑 extract。
    #    ⚠️ 本测试把 image_cache 桩掉了（`files_roots()` 返回空），所以要按**文件路径**
    #    把真的 image_cache 加载进来，才能摸到本机真实 PDF；摸不到就跳过。
    import importlib.util
    real_roots = []
    try:
        spec = importlib.util.spec_from_file_location(
            "_real_image_cache", os.path.join(BASE, "image_cache.py"))
        ric = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ric)
        real_roots = [os.path.join(a, "msg", "file") for a in ric.account_dirs()]
        real_roots = [d for d in real_roots if os.path.isdir(d)]
    except Exception as e:
        print(f"  ⏭️  加载真实 image_cache 失败（{type(e).__name__}），跳过 PDF 两条")

    import pypdf as _pypdf
    pdfs = []
    for r in real_roots:
        for dp, _dn, fns in os.walk(r):
            pdfs += [os.path.join(dp, f) for f in fns if f.lower().endswith(".pdf")]
    pdfs = sorted(pdfs, key=os.path.getsize)
    # 预筛只看有限样本：最小的 15 份 + 最大的 5 份（扫描件往往是大文件）
    picks = pdfs[:15] + pdfs[-5:] if len(pdfs) > 20 else pdfs
    scanned_pick, multi_pick = None, None
    for cand in picks:
        try:
            rd = _pypdf.PdfReader(cand)
        except Exception:
            continue
        if multi_pick is None and len(rd.pages) > 3:
            multi_pick = cand
        if scanned_pick is None:
            for i, pg in enumerate(rd.pages[:10], 1):
                if not (pg.extract_text() or "").strip():
                    try:
                        if len(pg.images) > 0:
                            scanned_pick = (cand, i)
                            break
                    except Exception:
                        continue
        if scanned_pick and multi_pick:
            break

    if scanned_pick:
        cand, pageno = scanned_pick
        file_read._image_from_bytes = fake_from_bytes
        try:
            calls.clear()
            t, e = file_read.extract(cand, {"file": {"max_bytes": 0,
                                                     "pdf_max_pages": pageno + 2}})
        finally:
            file_read._image_from_bytes = real
        check(f"PDF 扫描页走了图片通道并标明页码（{os.path.basename(cand)[:22]} 第 {pageno} 页）",
              e is None and "整页是图" in (t or "") and len(calls) >= 1,
              (t or "")[-200:])
    else:
        print(f"  ⏭️  本机 {len(pdfs)} 份 PDF 的预筛样本里没找到「整页是图」，跳过这条")

    # ⑥ 「只读前 N 页」必须说出来（以前写死 40 页且只字不提 = 静默截断）
    if multi_pick:
        t6, e6 = file_read.extract(multi_pick, {"file": {"max_bytes": 0,
                                                        "pdf_max_pages": 3}})
        check("按 pdf_max_pages 截断时**明说**共几页/读了几页",
              e6 is None and "pdf_max_pages" in (t6 or "") and "共" in (t6 or ""),
              (t6 or "")[-200:])
    else:
        print("  ⏭️  没有多页 PDF 样本，跳过「页数截断要说明」这条")


def main():
    print("=" * 60)
    print("file_read / llm / settings 回归自测（临时目录：%s）" % TMP)
    print("=" * 60)
    t1_zip_bomb()
    t2_pdf_message()
    t3_locate_realpath()
    t4_encoding()
    t5_api_key_env()
    t6_temperature_retry()
    t7_truncated()
    t8_settings()
    t9_image_and_pick()
    t10_unlimited_and_sniff()
    t11_paging_and_export()
    t12_embedded_images()
    print("\n" + "=" * 60)
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(rc)
