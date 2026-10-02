"""压缩包递归读（`archive_read`）的回归自测。

**不联网、不需要微信**：现场用标准库造 zip 样本，验的是"**不许静默糊弄**"这几条：
  * 成员逐个读出来、**标明是哪一个成员**；
  * 嵌套压缩包**共用同一个解压额度**（套娃不能绕过 zip 炸弹防护）；
  * 超过 `archive.max_depth` / `archive.max_members` 时**说清还有多少没读**；
  * 炸弹（声明值撒谎 / 解压后过大）**整包拒绝**，不截断当正常内容；
  * 成员名带 `../`（zip-slip）**削平**，绝不解到用户目录里；
  * `.7z/.rar` 缺可选依赖时给**能照做**的安装指引（不是一句"读不了"）；
  * 临时产物用完即删。

用法：`.venv/Scripts/python.exe selftest_archive.py`
"""
import os
import shutil
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import archive_read  # noqa: E402
import file_read  # noqa: E402

TMP = tempfile.mkdtemp(prefix="selftest_archive_")
_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


def _zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def _docx_bytes(text):
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    return {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "word/document.xml": (f'<?xml version="1.0"?><w:document {W}>'
                              f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:document>').encode("utf-8"),
    }


def _docx(path, text):
    return _zip(path, _docx_bytes(text))


def t1_members_and_labels():
    print("\n── 1 · 逐个成员读出来，且标明是哪一个 ──")
    inner = os.path.join(TMP, "内层.docx")
    _docx(inner, "这是 Word 里的正文，长度足够以通过乱码判据，用来验证成员解析。")
    p = _zip(os.path.join(TMP, "包.zip"), {
        "说明.txt": "这是压缩包里的一个纯文本成员，内容够长以通过乱码判据。".encode("utf-8"),
        "子目录/表格.csv": "姓名,数量\n张三,1\n".encode("utf-8"),
    })
    with zipfile.ZipFile(p, "a") as z:                     # 把 docx 塞进去
        z.write(inner, "报告/内层.docx")
    text, err = archive_read.read_archive(p, {"archive": {"max_members": 10}})
    check("整包读成功", err is None and text, err)
    check("标明了成员名（纯文本）", "说明.txt" in text and "纯文本成员" in text, text[:200])
    check("成员名带子目录也照原样标明", "子目录/表格.csv" in text and "张三" in text, text[:400])
    check("里面的 docx **被解析成正文**（不是二进制垃圾）",
          "报告/内层.docx" in text and "Word 里的正文" in text, text[-400:])

    t, e = archive_read.read_archive(os.path.join(TMP, "不是包.txt"))
    check("不是压缩包 → 如实说", t is None and e and "不是压缩包" in e, e)


def t2_caps_and_depth():
    print("\n── 2 · 成员数上限 / 层数上限：都要说清还有多少没读 ──")
    p = _zip(os.path.join(TMP, "很多成员.zip"),
             {f"f{i}.txt": f"第 {i} 份的内容，够长以通过乱码判据。".encode("utf-8")
              for i in range(6)})
    text, err = archive_read.read_archive(p, {"archive": {"max_members": 2}})
    check("只读了 2 个成员", err is None and text.count("〔f") == 2, text)
    check("并且明说还有 4 个没读（带上限名）",
          "还有 4 个成员没读" in text and "max_members" in text, text[-160:])

    nested = _zip(os.path.join(TMP, "内层.zip"),
                  {"深处.txt": "最里面那份的内容，够长以通过乱码判据。".encode("utf-8")})
    outer = os.path.join(TMP, "外层.zip")
    with zipfile.ZipFile(outer, "w") as z:
        z.write(nested, "套一层.zip")
    text, err = archive_read.read_archive(outer, {"archive": {"max_depth": 0}})
    check("层数到顶时**说明没往下读**（不假装读完）",
          err is None and "max_depth" in text and "没再往下读" in text, text[-200:])
    text, err = archive_read.read_archive(outer, {"archive": {"max_depth": 1}})
    check("层数够时能读到最里面的成员",
          err is None and "套一层.zip" in text and "最里面那份" in text, text[-300:])


def t3_bomb_and_shared_budget():
    print("\n── 3 · 炸弹：声明值撒谎 / 解压后过大 → 整包拒绝 ──")
    # ① 声明值撒谎：小包声称能解出 300MB（超过默认 200MB 封顶）
    liar = os.path.join(TMP, "撒谎.zip")
    _zip(liar, {"big.txt": b"A" * (300 * 1024 * 1024)})
    real_open = zipfile.ZipFile

    class _Lying:
        def __init__(self, real):
            self._real = real

        def infolist(self):
            infos = self._real.infolist()
            for i in infos:
                i.file_size = 400 * 1024 * 1024
            return infos

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._real.close()
            return False

        def __getattr__(self, k):
            return getattr(self._real, k)

    zipfile.ZipFile = lambda *a, **kw: _Lying(real_open(*a, **kw))
    try:
        text, err = archive_read.read_archive(liar, {})
    finally:
        zipfile.ZipFile = real_open
    check("声明值超封顶 → 整包拒绝（文本为 None）", text is None, (text or "")[:60])
    check("拒的理由说清「解压后太大、出于安全没读」",
          err and "解压后太大" in err and "安全" in err, err)

    # ② 嵌套共用额度：给很小的额度，套娃里的成员必须撞上同一个封顶
    small = _zip(os.path.join(TMP, "小内层.zip"),
                 {"深处.txt": b"B" * (60 * 1024)})
    outer = os.path.join(TMP, "小外层.zip")
    with zipfile.ZipFile(outer, "w") as z:
        z.write(small, "内层.zip")
        z.writestr("垫.txt", b"C" * (60 * 1024))
    cfg = {"file": {"max_unpack": 100 * 1024}, "archive": {"max_members": 10, "max_depth": 2}}
    text, err = archive_read.read_archive(outer, cfg)
    # 额度是**整包共享**的：内层撞上封顶就被如实拒绝，外层把这件事标出来。
    # （**局部拒绝 + 明确标注**比"整包作废"更有用：没超限的成员内容还能给用户。）
    check("嵌套包共用同一个解压额度：内层撞封顶被如实拒绝",
          text is not None and "解压后太大" in text and "内层.zip" in text, (text or "")[:200])
    check("而且没把超限的内容当正常内容塞进来",
          "B" * 100 not in (text or ""), (text or "")[:120])


def t4_zip_slip_and_tmp_cleanup():
    print("\n── 4 · zip-slip 削平 + 临时产物不留 ──")
    p = os.path.join(TMP, "穿越.zip")
    _zip(p, {"../../evil.txt": "我不该被写到外面去，但作为成员内容应该能读。".encode("utf-8")})
    before = set(os.listdir(TMP))
    text, err = archive_read.read_archive(p, {})
    after = set(os.listdir(TMP))
    check("成员照样读得出来（标明的是原始成员名）",
          err is None and "evil.txt" in text and "不该被写到外面去" in text, (text, err))
    check("**没有**在样本目录里新建文件（更没写到上层目录）", after == before, after - before)
    check("上层目录里没有 evil.txt", not os.path.exists(os.path.join(os.path.dirname(TMP), "evil.txt")))
    check("临时目录里没留下成员残留",
          not [n for n in os.listdir(archive_read._TMP_DIR)] if os.path.isdir(archive_read._TMP_DIR) else True,
          os.listdir(archive_read._TMP_DIR) if os.path.isdir(archive_read._TMP_DIR) else [])


def t5_optional_backends_honest():
    print("\n── 5 · .7z/.rar 缺可选依赖 → 给能照做的指引 ──")
    for name, mod, hint in (("a.7z", "py7zr", "py7zr"), ("b.rar", "rarfile", "rarfile")):
        p = os.path.join(TMP, name)
        with open(p, "wb") as f:
            f.write(b"7z\xbc\xaf\x27\x1c" if name.endswith(".7z") else b"Rar!\x1a\x07")
        text, err = archive_read.read_archive(p, {})
        if mod in sys.modules:
            check(f"{name}：装了 {mod}，只要求不炸", err is None or isinstance(err, str), err)
            continue
        check(f"{name} 缺依赖时如实说需要 {hint}", text is None and err and hint in err, err)
        check(f"{name} 的提示里带 pip 安装命令", err and "pip install" in err, err)


def t6_real_machine():
    print("\n── 6 · 真机：盘上现成的压缩包读一个（没有就跳过） ──")
    zips = []
    for r in file_read.files_roots():
        for dp, _dn, fns in os.walk(r):
            zips += [os.path.join(dp, f) for f in fns if f.lower().endswith(".zip")]
    if not zips:
        print("  ⏭️  本机文件目录里没有 .zip 样本，跳过")
        return
    p = min(zips, key=os.path.getsize)
    text, err = file_read.extract(p, {"file": {"max_bytes": 0},
                                      "archive": {"max_members": 2}})
    check(f"真机读压缩包成功（{os.path.basename(p)[:30]}）",
          err is None and text and "压缩包里的内容" in text, (err, (text or "")[:80]))


def main():
    print("=" * 60)
    print("压缩包递归读回归自测（临时目录：%s）" % TMP)
    print("=" * 60)
    try:
        t1_members_and_labels()
        t2_caps_and_depth()
        t3_bomb_and_shared_budget()
        t4_zip_slip_and_tmp_cleanup()
        t5_optional_backends_honest()
        t6_real_machine()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
        archive_read.sweep_tmp(max_age=0)
    print("\n" + "=" * 60)
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
