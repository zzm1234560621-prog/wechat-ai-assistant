"""回归：2026-10-06「换了新包却还是旧 hook」那三处**静默失效**的修复。

真机怎么卡住的（一连串"什么也没说"）：

  1. 一键配置第 0 步版本闸没过 → `first_run()` **静默 return**，装 hook 从没跑过，
     用户看到的只是"打了一堆警告然后结束"，以为装完了 → 助手每 10 秒刷
     「hook 已加载，但数据库打不开（微信没登录？）」，方向被文案带到"扫码登录"上；
  2. `do_hook_install.ps1` 撞版本闸时**只写日志 + exit 2**，而它跑在提权新窗口里、
     窗口一关输出就没了 → 用户永远不知道装没装上；
  3. 提权脚本的输出**不落盘** → 我给用户的手工替换脚本明明成功了，他看到的是"一片空白"。

这个文件钉的就是「**上述每一处都必须把话说出来、并且能落到文件**」。
不碰真微信、不改任何系统文件（全部用桩 + 静态检查）。

用法：`.venv/Scripts/python.exe selftest_hook_recovery.py`
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import console  # noqa: E402

_PASS = 0
_OK = True
INST = os.path.join(BASE, "installers", "wechat-4.1.10.27")


def chk(cond, label, extra=""):
    global _PASS, _OK
    cond = bool(cond)
    _PASS += 1 if cond else 0
    _OK = _OK and cond
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {extra}" if extra and not cond else ""))
    return cond


def _capture(fn, *a, **kw):
    """跑一下并把它打印的字抓回来看（这些函数的全部价值就是"说人话"）。"""
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        fn(*a, **kw)
        return sys.stdout.getvalue()
    finally:
        sys.stdout = old


def t1_stop_message():
    print("\n[1] 版本闸没过时：必须明说「后面一步都没做」+ 现在那份 hook 是什么")
    txt = _capture(console._stop_after_version_gate)
    chk("没做完" in txt, "标题写明「没做完」", txt)
    chk("第 0 步" in txt, "说明停在第几步", txt)
    chk("装 hook" in txt, "点名「装 hook」没做", txt)
    chk("一步都没执行" in txt or "没做的" in txt, "明说后面的步骤没执行", txt)
    chk("version.dll" in txt, "给出「现在微信目录里那份」的信息", txt)
    chk("[9]" in txt or "[8]" in txt, "给出下一步该按哪个菜单", txt)
    # 关键：把「包里新 / 装的那份旧」这层关系点破（真机上就是这一条没说）
    old_state = console._hook_dll_state
    try:
        console._hook_dll_state = lambda: (519168, "519168 字节 —— **旧的**")
        txt2 = _capture(console._stop_after_version_gate)
        chk("旧的" in txt2, "旧构建时刻意标出「旧的」", txt2)
        chk("解压新包不会替换" in txt2,
            "把「解压新包不会替换它」说出来（真机上就缺这一句）", txt2)
    finally:
        console._hook_dll_state = old_state


def t2_report_install():
    print("\n[2] 装 hook 之后：读回结果；日志不存在 = 脚本压根没跑（真机那次就是）")
    txt = _capture(console._report_hook_install)
    chk("微信目录里的 hook" in txt, "先报微信目录里那份是什么", txt)

    real_isfile = os.path.isfile

    def fake_isfile(p):
        if os.path.basename(str(p)) == "hook-install-log.txt":
            return False                      # 模拟真机：日志不存在
        return real_isfile(p)

    try:
        console.os.path.isfile = fake_isfile
        txt2 = _capture(console._report_hook_install)
    finally:
        console.os.path.isfile = real_isfile
    chk("没有真的跑起来" in txt2 or "没运行" in txt2,
        "日志不存在时必须说「脚本没跑起来」，不许沉默", txt2)
    chk("UAC" in txt2, "并给出最常见的原因（UAC 被点否）", txt2)


def t3_ps1_scripts():
    print("\n[3] 两个 .ps1：中止要大声说 + 结果要落盘")
    ins = os.path.join(INST, "do_hook_install.ps1")
    txt = open(ins, "r", encoding="utf-8").read() if os.path.isfile(ins) else ""
    chk(bool(txt), "do_hook_install.ps1 存在")
    chk("Read-Host" in txt, "跑完会 pause（提权窗口不会一闪就没）", txt[:0])
    chk(txt.count("Read-Host") >= 2, "**中止**那条路也会 pause（真机就是一闪而过）")
    chk("Write-Host" in txt, "会往控制台打（不只是写日志）")
    chk("[X] 已中止" in txt, "中止时有一句醒目的「已中止」")
    chk("没有被替换" in txt, "并明说 version.dll 没被替换")
    chk("527360" in txt and "519168" in txt,
        "打印 DLL 大小时带上新旧对照值（一眼分辨）")
    chk("Show-CurrentDll" in txt, "有「显示当前那份 DLL」的函数")
    chk(txt.count("Show-CurrentDll") >= 3, "装完 / 中止 / 开头都调它")

    fix = os.path.join(INST, "do_fix_hook.ps1")
    fb = open(fix, "rb").read() if os.path.isfile(fix) else b""
    ftxt = fb.decode("utf-8-sig") if fb else ""
    chk(bool(ftxt), "do_fix_hook.ps1 随包（以后不用我再临时写一段给用户）")
    chk("Out-File $Log" in ftxt, "结果**落盘**（提权窗口一关就看不到输出，必须落盘）")
    chk("Out-File $Log -Append -Encoding ascii" in ftxt, "日志是 ascii（GBK 机器不乱码）")
    chk("RESULT: OK" in ftxt, "写一个机器可判的结论行")
    chk("bak_" in ftxt, "替换前留备份（可还原）")
    chk("_common.ps1" in ftxt, "点源引入 _common.ps1（和别的 do_*.ps1 同一条规矩）")
    chk("Find-Weixin" in ftxt, "用 _common.ps1 的 Find-Weixin 找微信，不写死路径")
    # `.ps1` 的规矩是「**去掉 BOM 后**全 ASCII」——BOM 本身（EF BB BF）必然非 ASCII，
    # 所以判据必须先把 BOM 剥掉（`selftest_portable` 也钉着「每个 .ps1 带 BOM」）。
    body = fb[3:] if fb[:3] == b"\xef\xbb\xbf" else fb
    chk(all(b < 128 for b in body),
        "正文是纯 ASCII（GBK 机器不乱码）", f"非 ASCII 字节 {sum(1 for b in body if b > 127)} 个")
    chk(fb[:3] == b"\xef\xbb\xbf", "带 UTF-8 BOM（PowerShell 5.1 的规矩）")


def t4_menu_wired():
    print("\n[4] 菜单里真的有这一项（不能让修复只存在于文档里）")
    src = open(os.path.join(BASE, "console.py"), "r", encoding="utf-8").read()
    chk("act_fix_hook" in src, "act_fix_hook 实现了")
    chk('"4", ("★ 只替换 version.dll' in src, "Hook 子菜单里挂了 [4]")
    chk("do_fix_hook.ps1" in src, "它会去调 do_fix_hook.ps1")
    chk("_report_hook_install()" in src, "一键配置装完 hook 会读回结果")
    chk("_stop_after_version_gate()" in src, "一键配置停在版本闸时会明说")


def t5_state_helper():
    print("\n[5] `_hook_dll_state()`：认得出已知构建（本机现在是 527360）")
    size, note = console._hook_dll_state()
    if size:
        chk(size in (519168, 527360) or "未知构建" in note,
            f"读到了微信目录里那份（{size}）", note)
        if size == 527360:
            chk("新的" in note, "527360 标成「新的」", note)
        if size == 519168:
            chk("旧的" in note, "519168 标成「旧的」", note)
    else:
        print(f"     （本机读不到微信目录：{note}）")


def main():
    print("=" * 64)
    print("hook 恢复链路自测（那三处静默失效的回归）")
    print("=" * 64)
    t1_stop_message()
    t2_report_install()
    t3_ps1_scripts()
    t4_menu_wired()
    t5_state_helper()
    print("\n" + "=" * 64)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 64)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
