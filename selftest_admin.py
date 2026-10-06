"""`admin.py`（提权跑助手）的回归自测。

**不弹 UAC、不真的提权、不动真实进程**：
  * 命令串用**纯函数**验（`build_relaunch_command`）；
  * 「要不要提权」的分支把 `is_admin` / `relaunch_elevated` 换成桩来验；
  * 令牌用临时目录里的真文件验（一次性、删掉即失效）。

为什么要专门钉这个模块（2026-10-06）：用户拍板「永远让助手跑在管理员上」，
而"提权"最容易出的错是**无限循环**（提权后仍判不是管理员 → 再提权 → …）和
**静默降级**（UAC 被拒后还按普通权限接着跑）。这两条都在这里钉住。

用法：`.venv/Scripts/python.exe selftest_admin.py`
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import admin  # noqa: E402

_PASS = 0
_OK = True


def chk(cond, label, extra=""):
    global _PASS, _OK
    cond = bool(cond)
    _PASS += 1 if cond else 0
    _OK = _OK and cond
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {extra}" if extra and not cond else ""))
    return cond


def t1_command_shape():
    print("\n[1] 提权命令串的形状（纯函数，不执行）")
    line = admin.build_relaunch_command(
        argv=[r"D:\a b\bot.py"],
        exe=r"D:\a b\.venv\Scripts\pythonw.exe",
        cwd=r"D:\a b", token=r"C:\Temp\wxa_elevate_1.tok")
    chk("-Verb RunAs" in line, "用 -Verb RunAs 提权（和 installers 那套一致）")
    chk("$env:WXA_ELEVATE_TOKEN=" in line, "令牌**在提权命令行内部**带过去（不靠继承）")
    chk("wxa_elevate_1.tok" in line, "……带的是那个令牌文件的路径")
    chk("cmd.exe" not in line,
        "**不再经 cmd.exe 中转**（PowerShell 5.1 下引号会被搞乱）", line)
    chk('"D:\\a b\\bot.py"' in line,
        "含空格的脚本路径包双引号（CreateProcess 才不拆开）", line)
    chk("-FilePath 'D:\\a b\\.venv\\Scripts\\pythonw.exe'" in line,
        "可执行文件整体作为单引号字面量（含空格也安全）", line)
    chk("-WorkingDirectory 'D:\\a b'" in line, "显式给工作目录（提权后 CWD 会变）")

    # 无参数时整条 -ArgumentList 都不许出现：空串会被 PowerShell 参数校验拒掉
    # （这个坑让菜单 [1] 降级必然失败过）
    line2 = admin.build_relaunch_command(
        argv=[], exe="p.exe", cwd="C:\\x", token="t.tok")
    chk("-ArgumentList" not in line2, "没有参数时**不写 -ArgumentList**", line2)

    # 单引号必须转义，否则路径里的 ' 会把 PowerShell 串截断（注入风险）
    line3 = admin.build_relaunch_command(
        argv=["a'b.py"], exe="p.exe", cwd="c'd", token=None)
    chk("'c''d'" in line3, "路径里的单引号双写转义（不注入）", line3)
    chk("$env:" not in line3, "不传 token 时不带环境变量赋值", line3)


def t1b_wait_and_capture():
    print("\n[1b] 命令式入口的两档：等子进程 / 带退出码")
    line = admin.build_relaunch_command(
        argv=["botctl.py", "start"], exe="py.exe", cwd="C:\\x",
        token=None, wait=True)
    chk("-Wait" in line and "-PassThru" in line and "exit $_.ExitCode" in line,
        "wait=True 会把子进程退出码带回来（命令式入口要这个）", line)
    line2 = admin.build_relaunch_command(
        argv=["botctl.py", "start"], exe="py.exe", cwd="C:\\x", token=None)
    chk("-Wait" not in line2, "wait=False 不等（启动助手/控制台那条路）", line2)


def t2_token():
    print("\n[2] 一次性令牌：证明「提权已经发生过」")
    saved_env = os.environ.pop(admin.ENV_TOKEN, None)
    tmp = tempfile.mkdtemp(prefix="selftest_admin_")
    try:
        tok = os.path.join(tmp, "wxa_elevate_999.tok")
        chk(admin.claim_launch_token() is False, "没有令牌 → False（普通启动）")
        with open(tok, "w", encoding="utf-8") as f:
            f.write("999")
        os.environ[admin.ENV_TOKEN] = tok
        chk(admin.claim_launch_token() is True, "令牌在 → True（我确实是提权拉起的那一份）")
        chk(not os.path.exists(tok), "……并且**当场删掉**（一次性）")
        chk(admin.claim_launch_token() is False, "再来一次 → False（令牌已失效）")
        os.environ[admin.ENV_TOKEN] = os.path.join(tmp, "根本没有.tok")
        chk(admin.claim_launch_token() is False, "环境变量指向不存在的文件 → False")
    finally:
        if saved_env is not None:
            os.environ[admin.ENV_TOKEN] = saved_env
        else:
            os.environ.pop(admin.ENV_TOKEN, None)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t3_ensure_branches():
    print("\n[3] ensure_elevated 的分支（桩掉真提权）")
    calls = []

    def _fake_relaunch(**kw):
        calls.append(kw)
        return True, "已在新窗口里以管理员身份启动"

    saved = (admin.is_admin, admin.relaunch_elevated, admin.claim_launch_token)
    try:
        # ① 本来就是管理员 → 不弹 UAC、不重启自己
        calls.clear()
        admin.is_admin = lambda: True
        admin.relaunch_elevated = _fake_relaunch
        admin.claim_launch_token = lambda: False
        ok, msg, launched = admin.ensure_elevated()
        chk(ok is True and launched is False and not calls,
            "已经是管理员 → 直接继续，不提权", (ok, msg, launched, calls))

        # ② 不是管理员 → 走提权；**launched=True 是调用方必须立刻返回的信号**
        calls.clear()
        admin.is_admin = lambda: False
        ok2, msg2, launched2 = admin.ensure_elevated(argv=["bot.py"])
        chk(ok2 is True and launched2 is True and len(calls) == 1,
            "不是管理员 → 提权一次，且 launched=True（调用方必须返回）",
            (ok2, msg2, launched2, calls))
        chk(calls[0].get("capture") is False and calls[0].get("wait") is False,
            "……默认 capture/wait 都是 False（交互式那条路）", calls)

        # ③ 令牌命中 → 本进程就是提权那一份 → **绝不再提权**（无限循环就在这里断掉）
        calls.clear()
        admin.claim_launch_token = lambda: True
        ok3, msg3, launched3 = admin.ensure_elevated()
        chk(ok3 is True and launched3 is False and not calls,
            "刚被提权拉起 → **不再提权**（防无限循环），且 launched=False（继续跑）",
            (ok3, msg3, launched3, calls))
        chk("提权拉起" in msg3, "……并说明是提权那一份", msg3)

        # ④ UAC 被拒 → **绝不静默降级**（返回 False，调用方必须退出）
        admin.claim_launch_token = lambda: False
        admin.relaunch_elevated = lambda **kw: (False, "提权被取消（UAC 里点了「否」）")
        ok4, msg4, launched4 = admin.ensure_elevated()
        chk(ok4 is False and "取消" in msg4 and launched4 is False,
            "UAC 被拒 → 不继续跑（绝不偷偷降级）", (ok4, msg4, launched4))

        # ⑤ assume=True（开机自启）→ 绝不弹 UAC，只告警后继续
        calls.clear()
        admin.relaunch_elevated = _fake_relaunch
        ok5, msg5, launched5 = admin.ensure_elevated(assume=True)
        chk(ok5 is True and launched5 is False and not calls,
            "assume=True → 不弹 UAC、也不让调用方返回", (ok5, msg5, launched5, calls))
        chk("自启" in msg5 and "计划任务" in msg5,
            "……并说明是自启场景 + 静默提权的正道", msg5)

        # ⑥ 自启那一档排在令牌之前：环境里侥幸留着令牌也不许把"不是管理员"吞掉
        calls.clear()
        admin.claim_launch_token = lambda: True
        ok6, msg6, _ = admin.ensure_elevated(assume=True)
        chk(ok6 is True and "自启" in msg6,
            "自启 + 环境里有残留令牌 → 仍然如实说「不是管理员」（令牌不许吞掉它）",
            (ok6, msg6))
    finally:
        (admin.is_admin, admin.relaunch_elevated,
         admin.claim_launch_token) = saved


def t4_real_probe():
    print("\n[4] 真实环境（只读判断，不提权）")
    a = admin.is_admin()
    chk(isinstance(a, bool), "is_admin() 返回布尔", a)
    print(f"     （本进程实际是管理员吗：{a} —— 自测两种结果都算通过）")


def main():
    print("=" * 60)
    print("admin.py 提权自测（不弹 UAC、不提权、不动真实进程）")
    print("=" * 60)
    t1_command_shape()
    t1b_wait_and_capture()
    t2_token()
    t3_ensure_branches()
    t4_real_probe()
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
