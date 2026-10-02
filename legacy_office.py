"""老 Office 格式（`.doc` / `.xls` / `.ppt`）→ 文字：**多引擎自动降级**。

规格：`docs/file-input-spec.md` 第九节「老 Office」。**不绑死某一个软件**：
用户机器上可能装了 Office、也可能只有 WPS、或者装了 LibreOffice —— 谁在就用谁。

引擎顺序（`.xls` 例外，见下）：

```
① office-com   Microsoft Office 的 COM（Word/Excel/PowerPoint 都支持）
② wps-com      WPS 的 COM（KWps/KET/KWpp）—— 装了 WPS 就没 Office 也能转
③ libreoffice  soffice --headless（无界面，哪怕没装 Office/WPS 也能用）
④ antiword     只有 .doc 能用的小工具（第三方 exe，用户自己放了才走这一级）
⑤ olefile      **粗略**扫 .doc 的 WordDocument 流：能看个大概，**必须如实标注不可靠**
→ 都没有：如实说缺什么、怎么补，并建议让对方另存 .docx
```

`.xls` 会**先试 `xlrd`**（纯 Python，专读 .xls，比启动 Excel 快也稳），不行再走上面的链。

三条铁律：
  * **只读、无窗口、带超时**（见 `tools/office2text.ps1`）——绝不允许改用户文档；
  * 每次返回都带**用的是哪个引擎**——用户才知道这份结果可不可信；
  * 缺引擎时给**能照做**的话（装什么、或者让对方另存），绝不一句"读不了"了事。
"""
import os
import re
import shutil
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_PS1 = os.path.join(_HERE, "tools", "office2text.ps1")

KIND_BY_EXT = {".doc": "word", ".xls": "excel", ".ppt": "ppt"}
ENGINE_ORDER = ("office-com", "wps-com", "libreoffice", "antiword", "olefile")

# COM 组件名：Office 与 WPS 各一套
_PROGIDS = {
    "office-com": {"word": "Word.Application", "excel": "Excel.Application",
                   "ppt": "PowerPoint.Application"},
    "wps-com": {"word": "KWps.Application", "excel": "KET.Application",
                "ppt": "KWpp.Application"},
}
_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)

# 引擎名 → 给用户看的说明（缺了它时告诉他怎么补）
_HOWTO = {
    "office-com": "装 Microsoft Office，或改用 WPS / LibreOffice",
    "wps-com": "装 WPS Office（免费）",
    "libreoffice": "装 LibreOffice（免费，无界面也能转）",
    "antiword": "放一个 antiword.exe 到 PATH 里（只对 .doc 有用）",
    "olefile": "pip install olefile（只能粗略抽 .doc，质量不保证）",
}


def _cfg(cfg):
    return ((cfg or {}).get("legacy") or {})


def enabled(cfg=None):
    """`legacy.engines: off` 时整块关掉。"""
    return str(_cfg(cfg).get("engines", "auto") or "auto").strip().lower() != "off"


def engines_of(cfg=None):
    """要按什么顺序试。`auto`（默认）就是内置顺序；也可以写死一个（或用逗号列几个）。"""
    want = str(_cfg(cfg).get("engines", "auto") or "auto").strip().lower()
    if want in ("", "auto"):
        return list(ENGINE_ORDER)
    picked = [x.strip() for x in want.split(",") if x.strip()]
    return [e for e in picked if e in ENGINE_ORDER] or list(ENGINE_ORDER)


def _timeout(cfg=None):
    try:
        n = int(_cfg(cfg).get("office_timeout") or 120)
    except (TypeError, ValueError):
        n = 120
    return max(10, min(n, 600))


def _decode_out(path):
    """读转换产物的文字。Excel 的 xlUnicodeText 是 **UTF-16LE + BOM**（实测），
    所以这里按 BOM 优先解，别硬按 UTF-8 读（那样会得到一片乱码）。"""
    with open(path, "rb") as f:
        raw = f.read()
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"),
                     (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            return raw.decode(enc, "replace")
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _com(path, kind, progid, cfg):
    """走 COM 转一次。返回 (文本, 错误)。"""
    if not os.path.isfile(_PS1):
        return None, "找不到 tools/office2text.ps1"
    out = path + f".{kind}.conv.txt"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _PS1,
             "-Path", path, "-Out", out, "-Kind", kind, "-ProgId", progid],
            capture_output=True, timeout=_timeout(cfg))
    except subprocess.TimeoutExpired:
        return None, f"超过 legacy.office_timeout={_timeout(cfg)} 秒"
    except OSError as e:
        return None, f"起不了 PowerShell：{e}"
    text_out = (r.stdout or b"").decode("utf-8", "ignore").strip()
    try:
        if r.returncode != 0 or not text_out.startswith("OK:"):
            return None, (text_out or f"退出码 {r.returncode}")[:200]
        if not os.path.isfile(out):
            return None, "COM 说成功，但没生成文本文件"
        return _decode_out(out), None
    finally:
        try:
            if os.path.isfile(out):
                os.remove(out)          # 转换产物是临时的，别留在用户目录里
        except OSError:
            pass


def _xlrd(path):
    """`.xls` 专用：纯 Python（比启动 Excel 快、也不依赖任何人装 Office）。"""
    try:
        import xlrd
    except ImportError:
        return None, "没装 xlrd"
    try:
        book = xlrd.open_workbook(path)
    except Exception as e:
        return None, f"xlrd 打不开：{type(e).__name__}: {str(e)[:120]}"
    out = []
    for sh in book.sheets():
        rows = []
        for r in range(min(sh.nrows, 200)):
            cells = [str(sh.cell_value(r, c)) for c in range(min(sh.ncols, 40))]
            if any(x.strip() for x in cells):
                rows.append("\t".join(cells))
        if rows:
            out.append(f"— 工作表「{sh.name}」—\n" + "\n".join(rows))
    if not out:
        return None, "xlrd 抽出来是空的"
    return "\n\n".join(out), None


def _soffice():
    for p in _SOFFICE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return shutil.which("soffice") or shutil.which("libreoffice")


def _libreoffice(path, cfg):
    exe = _soffice()
    if not exe:
        return None, "没装 LibreOffice"
    outdir = os.path.join(os.path.dirname(os.path.abspath(path)), "_conv")
    try:
        os.makedirs(outdir, exist_ok=True)
        r = subprocess.run(
            [exe, "--headless", "--norestore", "--convert-to",
             "txt:Text (encoded):UTF8", "--outdir", outdir, path],
            capture_output=True, timeout=_timeout(cfg))
    except subprocess.TimeoutExpired:
        return None, f"超过 legacy.office_timeout={_timeout(cfg)} 秒"
    except OSError as e:
        return None, f"起不了 LibreOffice：{e}"
    stem = os.path.splitext(os.path.basename(path))[0]
    target = os.path.join(outdir, stem + ".txt")
    try:
        if r.returncode != 0 or not os.path.isfile(target):
            msg = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "ignore").strip()
            return None, (msg or f"退出码 {r.returncode}")[:200]
        return _decode_out(target), None
    finally:
        try:
            if os.path.isfile(target):
                os.remove(target)
            os.rmdir(outdir)
        except OSError:
            pass


def _antiword(path):
    exe = shutil.which("antiword")
    if not exe:
        return None, "PATH 里没有 antiword"
    for args in (["-m", "UTF-8.txt", path], [path]):
        try:
            r = subprocess.run([exe] + args, capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            return None, f"antiword 起不来/超时：{e}"
        if r.returncode == 0 and (r.stdout or b"").strip():
            return r.stdout.decode("utf-8", "replace"), None
    return None, "antiword 没抽出文字"


def _olefile(path):
    """**粗略**扫 `.doc` 的 WordDocument 流。

    这一级存在的理由只有一个：前四级都没有时，总比一句"读不了"强 ——
    但它**抽出来的东西不可信**（可能缺字、串行、夹着格式码），所以调用方必须
    把"粗略"这件事写进结果里。
    """
    try:
        import olefile
    except ImportError:
        return None, "没装 olefile"
    if not olefile.isOleFile(path):
        return None, "不是 OLE2 复合文档"
    try:
        ole = olefile.OleFileIO(path)
        try:
            if not ole.exists("WordDocument"):
                return None, "没有 WordDocument 流（多半不是 .doc）"
            data = ole.openstream("WordDocument").read()
        finally:
            ole.close()
    except Exception as e:
        return None, f"olefile 读不动：{type(e).__name__}: {str(e)[:120]}"

    # UTF-16LE 的可读片段：汉字/常用标点/ASCII 可打印字符连续 4 个以上才算一段
    runs, cur = [], []
    for i in range(0, len(data) - 1, 2):
        ch = chr(data[i] | (data[i + 1] << 8))
        ok = (ch.isprintable() and ch not in "\ufffd") or ch in "\n\r\t"
        if ok and (ord(ch) >= 32):
            cur.append(ch)
        else:
            if len(cur) >= 4:
                runs.append("".join(cur))
            cur = []
    if len(cur) >= 4:
        runs.append("".join(cur))
    text = "\n".join(r for r in runs if r.strip())
    if len(text.strip()) < 20:
        return None, "粗略抽取没得到成句的内容"
    return text, None


def capabilities(cfg=None):
    """每个引擎现在能不能用（给用户看的诊断）。返回 {引擎: 说明}。"""
    caps = {}
    for name in ENGINE_ORDER:
        if name == "office-com" or name == "wps-com":
            # COM 是否可用只能真调用才知道；这里如实标"装了才知道"
            caps[name] = "（要真调一次才知道，装了 Office/WPS 就能用）"
        elif name == "libreoffice":
            caps[name] = _soffice() or f"没装（{_HOWTO[name]}）"
        elif name == "antiword":
            caps[name] = shutil.which("antiword") or f"没有（{_HOWTO[name]}）"
        else:
            try:
                import olefile  # noqa: F401
                caps[name] = "已装"
            except ImportError:
                caps[name] = f"没装（{_HOWTO[name]}）"
    return caps


def convert(path, cfg=None):
    """把老 Office 文件转成文字。返回 `(文本, 用的引擎, 说明)`。

    * 成功：文本非空、`engine` 是引擎名、`note` 里写着前几级为什么被跳过（如果有）；
    * 失败：文本为 None、`engine` 为 None、`note` 是**能照做**的一段话。
    """
    ext = os.path.splitext(path)[1].lower()
    kind = KIND_BY_EXT.get(ext)
    if kind is None:
        return None, None, f"不是老 Office 格式（{ext or '无后缀'}）"
    if not enabled(cfg):
        return None, None, ("config.yaml 里 legacy.engines=off，老格式转换整块关着；"
                            "要让助手读 .doc/.xls/.ppt，就把它改回 auto")
    if not os.path.isfile(path):
        return None, None, "文件不在本机"

    notes = []
    # `.xls` 先试 xlrd（纯 Python，快又稳）
    if ext == ".xls":
        text, err = _xlrd(path)
        if text:
            return text, "xlrd", ""
        notes.append(f"xlrd：{err}")

    for engine in engines_of(cfg):
        if engine == "antiword" and ext != ".doc":
            notes.append(f"{engine}：只支持 .doc，跳过")
            continue
        if engine == "olefile" and ext != ".doc":
            notes.append(f"{engine}：这一级只实现 .doc，跳过")
            continue
        try:
            if engine in _PROGIDS:
                text, err = _com(path, kind, _PROGIDS[engine][kind], cfg)
            elif engine == "libreoffice":
                text, err = _libreoffice(path, cfg)
            elif engine == "antiword":
                text, err = _antiword(path)
            else:
                text, err = _olefile(path)
        except Exception as e:                      # 引擎自己炸了也不许把上层带走
            text, err = None, f"{type(e).__name__}: {str(e)[:120]}"
        if text and text.strip():
            note = ""
            if engine == "olefile":
                note = ("⚠️ 这一级是**粗略抽取**（没有可用的转换引擎），可能缺字、串行、"
                        "夹着格式码，**关键内容请人工核对**。")
            if notes:
                note = (note + "\n" if note else "") + "（前面的引擎： " + "；".join(notes) + "）"
            return text, engine, note
        notes.append(f"{engine}：{err}")

    return None, None, (
        "这份老格式文件读不了——本机没有一个能转换它的引擎：\n  · "
        + "\n  · ".join(notes)
        + "\n要读它，任选一种：① 装 Office 或 WPS 或 LibreOffice（自动就会用）；"
          "② 让对方另存成 .docx/.xlsx/.pptx 再发；"
          "③ 把 legacy.engines 调成某个你确实装了的引擎。\n"
          "**请如实告诉用户读不了，不要编内容。**")
