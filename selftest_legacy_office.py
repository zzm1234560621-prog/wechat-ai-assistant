"""老 Office（`.doc/.xls/.ppt`）多引擎降级的回归自测。

**不联网、不装 Office、不需要微信**：把每个引擎函数换成桩，验的是**顺序**和**话术**：
  * 有 Office 就用 Office；没 Office 有 WPS 就用 WPS；都没有才往下走；
  * `.xls` **先试 xlrd**（纯 Python，比启动 Excel 快又稳）；
  * antiword / olefile 这两级**只对 .doc 有意义**，别的格式要**说明跳过原因**；
  * `olefile` 那一级抽出来的东西**必须**标注"粗略、不可信"；
  * 全都没成 → 给**能照做**的话（装什么 / 让对方另存 / 调 legacy.engines），
    并且明说"不要编内容"；
  * `legacy.engines: off` 整块关掉时要如实说关着；
  * 转换产物是临时的，**不许留在用户文件旁边**（`_com` 的清理契约）。

真机那一条（Office COM 真转一遍）由 `verify` 段单独跑，不进这个自测
（自测不许依赖机器上装没装 Office）。

用法：`.venv/Scripts/python.exe selftest_legacy_office.py`
"""
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import legacy_office  # noqa: E402

TMP = tempfile.mkdtemp(prefix="selftest_legacy_")
_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


def _mk(name, data=b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1fake"):
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


class _Stub:
    """把某个引擎变成"能用/不能用/抛异常"三种状态之一。"""

    def __init__(self, **kw):
        self.cfg = kw
        self.calls = []

    def __enter__(self):
        self.real = {}
        for name in ("_com", "_xlrd", "_libreoffice", "_antiword", "_olefile"):
            self.real[name] = getattr(legacy_office, name)
            setattr(legacy_office, name, self._make(name))
        return self

    def _make(self, name):
        def fn(*a, **kw):
            self.calls.append(name)
            # ⚠️ 只有**显式**在 cfg 里给 True 的引擎才算"能用"：
            # 以前写成 `self.cfg.get(name) or {}`，于是显式的 None 被 `or {}` 变成了"成功"，
            # 用例反而全绿不了（自测自己抓到的）。
            spec = self.cfg.get(name, False)
            if spec is False or spec is None:
                return None, "桩：这一级不可用"
            if spec == "boom":
                raise RuntimeError("桩：这一级炸了")
            if name == "_xlrd":
                return "xlrd 的表格内容", None
            return f"{name} 的内容", None
        return fn

    def __exit__(self, *a):
        for name, fn in self.real.items():
            setattr(legacy_office, name, fn)
        return False


def t1_order():
    print("\n── 1 · 引擎顺序：Office → WPS → LibreOffice → antiword → olefile ──")
    doc = _mk("样本.doc")

    with _Stub(_com=True) as s:                       # Office 在（_com 打头就是它）
        text, engine, note = legacy_office.convert(doc)
    check("有 Office 就用 office-com", engine == "office-com" and text, (engine, text))
    check("只试了第一级（没白跑后面）", s.calls[:1] == ["_com"] and len(s.calls) == 1, s.calls)

    # Office 的 ProgId 先错（WPS 才会成功后），这里直接验"顺序列表"本身
    check("默认顺序就是文档里写的那一串",
          legacy_office.engines_of({}) == ["office-com", "wps-com", "libreoffice",
                                           "antiword", "olefile"],
          legacy_office.engines_of({}))

    # 显式指定某个引擎：不许再试别的
    cfg = {"legacy": {"engines": "olefile"}}
    with _Stub(_com=True, _olefile=True) as s:
        text, engine, note = legacy_office.convert(doc, cfg)
    check("legacy.engines=olefile → 只用 olefile", engine == "olefile", (engine, s.calls))
    check("……且说明里写上「粗略、不可信」", "粗略" in note and "核对" in note, note)


def t2_xls_prefers_xlrd():
    print("\n── 2 · .xls 先试 xlrd（纯 Python，不启动 Excel） ──")
    xls = _mk("表格.xls")
    with _Stub(_xlrd=True, _com=True) as s:
        text, engine, note = legacy_office.convert(xls)
    check("xlrd 能用时就用 xlrd", engine == "xlrd" and "表格内容" in text, (engine, text))
    check("没去动 COM", "_com" not in s.calls, s.calls)

    with _Stub(_xlrd=None, _com=True) as s:          # xlrd 没装 → 落到 COM
        text, engine, note = legacy_office.convert(xls)
    check("xlrd 没装时自动落到下一级（COM）", engine == "office-com", engine)
    check("说明里交代了 xlrd 为什么被跳过", "xlrd" in note, note)


def t3_skip_and_fail_honestly():
    print("\n── 3 · 不适用的引擎要说清跳过；全失败要给能照做的话 ──")
    ppt = _mk("幻灯片.ppt")
    with _Stub(_com=None, _libreoffice=None, _antiword=True, _olefile=True) as s:
        text, engine, note = legacy_office.convert(ppt)
    check("PPT 落到最后都读不了（antiword/olefile 都不管 PPT）",
          text is None and engine is None, (text, engine))
    check("说明里逐条写了每个引擎的情况", "office-com" in note and "libreoffice" in note, note)
    check("说明里区分了「只支持 .doc 所以跳过」", "只支持 .doc" in note or "跳过" in note, note)
    check("给了能照做的办法（装什么 / 另存 docx）",
          ("装" in note and "docx" in note), note)
    check("明说不要编内容", "不要编内容" in note, note)

    doc = _mk("坏.doc")
    with _Stub(_com="boom", _libreoffice=None, _antiword=None, _olefile=None) as s:
        text, engine, note = legacy_office.convert(doc)
    check("某个引擎自己炸了也不许把上层带走", text is None and engine is None, (text, engine))
    check("炸掉的原因进了说明", "桩：这一级炸了" in note, note)


def t4_off_and_capabilities():
    print("\n── 4 · engines=off 要如实说；capabilities 给诊断 ──")
    doc = _mk("样本2.doc")
    text, engine, note = legacy_office.convert(doc, {"legacy": {"engines": "off"}})
    check("off 时明确说「整块关着」并给出怎么开",
          text is None and "off" in note and "auto" in note, note)

    caps = legacy_office.capabilities()
    check("capabilities 覆盖全部引擎", set(caps) == set(legacy_office.ENGINE_ORDER), list(caps))
    check("缺的引擎带「怎么补」的提示",
          all(("装" in v or "pip" in v or "PATH" in v or "真调" in v) for v in caps.values()),
          caps)
    check("非老格式的文件要直接说清", legacy_office.convert(_mk("a.txt", b"hi"))[2].find("不是老 Office") >= 0)


def t5_no_temp_left_behind():
    print("\n── 5 · 转换产物是临时的，不许留在用户文件旁边 ──")
    doc = _mk("清理.doc")
    leftovers_before = set(os.listdir(TMP))
    with _Stub(_com=True):
        legacy_office.convert(doc)
    check("没在样本目录里留下东西（桩路径）",
          set(os.listdir(TMP)) >= leftovers_before and
          not [n for n in os.listdir(TMP) if ".conv.txt" in n],
          os.listdir(TMP))
    # 真 _com 的清理契约：产物路径一定是 <原文件>.<kind>.conv.txt，且函数结束后不残留
    src_calls = []
    real_run = legacy_office.subprocess.run

    class _R:
        returncode = 0
        stdout = b"OK:word-com"
        stderr = b""

    def fake_run(cmd, **kw):
        src_calls.append(cmd)
        # 假装 COM 写出了产物（第二个 -Out 后面的路径）
        out = cmd[cmd.index("-Out") + 1]
        with open(out, "w", encoding="utf-8") as f:
            f.write("转出来的文字")
        return _R()

    legacy_office.subprocess.run = fake_run
    try:
        text, err = legacy_office._com(doc, "word", "Word.Application", {})
    finally:
        legacy_office.subprocess.run = real_run
    check("_com 能读回产物", text == "转出来的文字" and err is None, (text, err))
    check("_com 结束后把产物删了（不留在用户目录）",
          not [n for n in os.listdir(TMP) if ".conv.txt" in n], os.listdir(TMP))
    check("调 COM 时带上了只读/无窗口脚本", any("office2text.ps1" in str(c) for c in src_calls),
          src_calls[:1])


def t6_real_machine_report():
    """真机一条：装了 Office 就真转一份（装没装都只是打印，不算失败）。"""
    print("\n── 6 · 真机（有 Office 就顺手验一份；没有则跳过） ──")
    import file_read
    old = []
    for r in file_read.files_roots():
        for dp, _dn, fns in os.walk(r):
            for f in fns:
                if f.lower().endswith((".doc", ".xls", ".ppt")):
                    old.append(os.path.join(dp, f))
    if not old:
        print("  ⏭️  本机文件目录里没有老格式样本，跳过")
        return
    p = min(old, key=os.path.getsize)
    text, engine, note = legacy_office.convert(p)
    if text:
        check(f"真机转换成功（{os.path.basename(p)[:26]}，引擎 {engine}）",
              len(text.strip()) > 20, text[:60])
    else:
        print(f"  ⏭️  本机没有可用引擎（{note[:80]}…），跳过真机这条")


def main():
    print("=" * 60)
    print("老 Office 多引擎降级回归自测（临时目录：%s）" % TMP)
    print("=" * 60)
    try:
        t1_order()
        t2_xls_prefers_xlrd()
        t3_skip_and_fail_honestly()
        t4_off_and_capabilities()
        t5_no_temp_left_behind()
        t6_real_machine_report()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 60)
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
