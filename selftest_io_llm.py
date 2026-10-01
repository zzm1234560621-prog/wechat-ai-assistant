"""file_read / llm / settings 的回归自测（T1~T8）。

**不联网、不碰 30001、不需要微信**：假响应对象 + 临时目录 + 现场构造的 zip 样本。
用法：`.venv/Scripts/python.exe selftest_io_llm.py`

风格照抄 selftest_aixed.py：每项一行 ✅/❌，结尾汇总，有失败就 sys.exit(1)。
"""
import io
import json
import os
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


def _docx_bytes(text, filler=b""):
    return {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "word/document.xml": (
            b'<?xml version="1.0" encoding="UTF-8"?><w:document ' + _W.encode() + b'>'
            + filler
            + f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>'.encode("utf-8")
            + b"</w:document>"),
    }


def _xlsx_bytes(cell):
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
