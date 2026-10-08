"""`autostart.py`（开机自启 = **计划任务**）的回归自测。

**不碰计划任务、不弹 UAC、不改注册表**：命令串全是纯函数（直接断言形状），
系统交互一律用桩替换（假 `winreg` 模块 / 假 `_run_ps` / 假 `_lock_owner` / 假 `_elevate`）。

为什么必须有这份自测（2026-10-07 真机事故）：老的 Run 键那条路让助手
「自启开着、却静默失联一整晚」（时间线与根因见 `docs/autostart-task-notes.md`）。
换成计划任务时有两个**只有真机才会暴露**的坑，这里必须钉死：

  ① `-RepetitionDuration` 一旦写上（哪怕值写成 `[TimeSpan]::MaxValue`），Task Scheduler
     会拒收整份定义（`The task XML contains a value which is incorrectly formatted or out
     of range`，HRESULT `0x80041318`）—— 表面"配好了"，其实任务根本没注册。
     **省略 Duration 才是"无限重复"**；
  ② `-MultipleInstances IgnoreNew` 是**自愈的唯一根据**（任务实例就是 bot 进程：活着时
     重复触发被忽略、死了才重新拉起）。写成 Allow/Queue 就变成"每 5 分钟起一个"。

用法：`.venv/Scripts/python.exe selftest_autostart.py`
"""
import io
import os
import sys
import time
import types
from contextlib import redirect_stdout

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import autostart as auto  # noqa: E402

_PASS = 0
_OK = True


def chk(cond, label, extra=""):
    global _PASS, _OK
    cond = bool(cond)
    _PASS += 1 if cond else 0
    _OK = _OK and cond
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {extra}" if extra and not cond else ""))
    return cond


PYW = r"C:\Program Files\wechat-ai-assistant\.venv\Scripts\pythonw.exe"
BOT = r"C:\Program Files\wechat-ai-assistant\bot.py"
WORK = r"C:\Program Files\wechat-ai-assistant"


# ── 桩 ────────────────────────────────────────────────────────────────

class _FakeKey:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeWinreg:
    """假 winreg：绝不碰真实注册表。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.deleted = []

    def OpenKey(self, root, path, reserved=0, access=0):
        return _FakeKey(self)

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(2, "系统找不到指定的文件。")
        return self.values[name], 1

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(2, "系统找不到指定的文件。")
        self.deleted.append(name)
        del self.values[name]


class _Patch:
    """临时替换一堆模块属性，退出时全还原（顺序无关）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(auto, k)
            setattr(auto, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(auto, k, v)
        return False


# ── 用例 ──────────────────────────────────────────────────────────────

def t1_register_shape():
    print("\n[1] 注册命令的形状（纯函数）")
    s = auto.build_register_command(PYW, BOT, WORK)
    chk(f"-Execute '{PYW}'" in s, "动作的 exe 是 venv 的 pythonw")
    chk(f'-Argument \'"{BOT}"\'' in s, "脚本路径带引号传进 -Argument（路径可能有空格）", s[:120])
    chk(f"-WorkingDirectory '{WORK}'" in s, "工作目录 = 项目目录（相对路径才解析得对）")
    chk("New-ScheduledTaskTrigger -AtLogOn" in s, "触发器一：登录时")
    chk("Register-ScheduledTask" in s and "-Force" in s, "可重复运行（-Force 覆盖旧定义）")
    chk(f"'{auto.TASK_NAME}'" in s, "任务名写进命令串")
    chk("Write-Output 'OK'" in s, "成功时有可断言的输出")


def t2_no_duration_trap():
    print("\n[2] 真机踩过的坑：不许写 RepetitionDuration；三个关键设置必须在")
    s = auto.build_register_command(PYW, BOT, WORK, repeat_minutes=7)
    chk("RepetitionDuration" not in s,
        "**绝不写** -RepetitionDuration（写了会被 Task Scheduler 拒收 0x80041318）")
    chk("-RepetitionInterval (New-TimeSpan -Minutes 7)" in s,
        "重复间隔按参数来（默认 5 分钟）")
    chk("-MultipleInstances IgnoreNew" in s,
        "多实例=IgnoreNew（自愈的唯一根据：活着不重启、死了才拉起）")
    chk("-ExecutionTimeLimit ([TimeSpan]::Zero)" in s, "不限运行时长（默认 3 天会把常驻助手杀掉）")
    chk("-RunLevel Highest" in s, "静默管理员（开机不再需要点 UAC，语音条那条硬约束靠它）")
    chk("-StartWhenAvailable" in s, "错过触发点也补一次")
    chk("-LogonType Interactive" in s, "在用户会话里跑（要读微信进程内存）")
    d = auto.build_register_command(PYW, BOT, WORK)
    chk(f"-Minutes {auto.REPEAT_MINUTES}" in d, f"默认间隔 = REPEAT_MINUTES（{auto.REPEAT_MINUTES}）")


def t3_quoting():
    print("\n[3] 路径转义（含空格 / 单引号都安全）")
    chk(auto._psq(r"C:\a'b\c") == r"'C:\a''b\c'", "单引号双写（PowerShell 字面量规矩）")
    weird = r"C:\it's here\wechat 'assistant'\bot.py"
    s = auto.build_register_command(PYW, weird, r"C:\it's here\wechat 'assistant'")
    chk("''" in s and "-WorkingDirectory 'C:\\it''s here\\wechat ''assistant'''" in s,
        "含单引号的路径不会把命令串拼坏", s[:200])


def t4_user_is_console_user():
    print("\n[4] 任务挂到「控制台会话真正登录的用户」，不是当前身份")
    s = auto.build_register_command(PYW, BOT, WORK)
    chk("Win32_ComputerSystem" in s,
        "用户取控制台会话那个（提权可能是别人用管理员账户点的，直接用当前身份会挂错账）")
    chk("[Security.Principal.WindowsIdentity]::GetCurrent().Name" in s, "取不到时如实回退到当前身份")
    chk("-AtLogOn -User $u" in s and "-UserId $u" in s, "触发器和 principal 用同一个用户")


def t5_other_commands():
    print("\n[5] 卸载 / 启动 / 查询命令")
    u = auto.build_unregister_command()
    chk("Unregister-ScheduledTask" in u and "-Confirm:$false" in u, "卸载不弹交互确认")
    st = auto.build_start_command()
    chk("Start-ScheduledTask" in st, "启动任务（不走 botctl，那样就不是任务实例了）")
    q = auto.build_query_command()
    chk("ConvertTo-Json -Compress" in q, "查询输出 JSON（中文 Windows 的 schtasks 文本没法稳解析）")
    chk("Write-Output '{}'" in q, "任务不存在时输出空 JSON，而不是报错")
    for key in ("state", "runLevel", "multiple", "timeLimit", "repeats", "nextRun"):
        chk(key in q, f"状态里必须含 {key}（status 靠它说清「怎么自愈」）")


def t6_task_info_parsing():
    print("\n[6] task_info 解析（假 _run_ps，不真连计划任务）")
    cases = [
        ((0, '{"task":"WeChatAIAssistant","state":"Running"}', ""),
         {"task": "WeChatAIAssistant", "state": "Running"}, "正常 JSON"),
        ((0, "{}", ""), None, "空 JSON = 没有任务"),
        ((0, "noise\n{}", ""), None, "空 JSON 前面有噪声也算没有"),
        ((0, "", ""), None, "没有输出"),
        ((0, "not json at all", ""), None, "不是 JSON"),
        ((1, "", "拒绝访问"), None, "rc!=0（读不到不算存在）"),
    ]
    for (rc, out, err), want, label in cases:
        with _Patch(_run_ps=lambda *a, **k: (rc, out, err)):
            got = auto.task_info()
        chk(got == want, f"{label} → {want}", f"实际 {got!r}")


def t7_legacy_run_value():
    print("\n[7] 老的 Run 值：能读、能删（假 winreg，绝不动真注册表）")
    fake = _FakeWinreg({auto.LEGACY_RUN_NAME: '"C:\\pyw.exe" "bot.py"'})
    real = sys.modules.get("winreg")
    sys.modules["winreg"] = fake
    try:
        chk(auto.run_value() == '"C:\\pyw.exe" "bot.py"', "读得到老值（status 要能报警）")
        chk(auto.delete_run_value() is True, "删得掉（迁移用）")
        chk(fake.deleted == [auto.LEGACY_RUN_NAME], "删的就是那个名字")
        chk(auto.run_value() is None, "删完读不到 → None")
        chk(auto.delete_run_value() is False, "再删一次幂等（不算失败）")
    finally:
        if real is None:
            sys.modules.pop("winreg", None)
        else:
            sys.modules["winreg"] = real


def t8_run_path_retired():
    print("\n[8] 静态检查：老的 Run 键**写入**路径已退役")
    src = open(os.path.join(BASE, "autostart.py"), encoding="utf-8").read()
    chk("SetValueEx" not in src,
        "源码里不许再出现 SetValueEx（Run 键不再作为自启 owner）")
    chk("DeleteValue" in src, "但保留「删掉旧值」的迁移能力")
    chk("ScheduledTask" in src, "计划任务才是 owner")


def t9_enable_behavior():
    print("\n[9] enable() 行为：注册 → 清老值 → 让任务拉起（不谎报）")
    calls = []
    scripts = []

    def fake_ps(script, timeout=120):
        scripts.append(script)
        return 0, "OK", ""

    with _Patch(_elevate=lambda what: (True, False),
                _run_ps=fake_ps,
                task_info=lambda *a, **k: {"state": "Ready", "runLevel": "Highest",
                                           "multiple": "IgnoreNew"},
                delete_run_value=lambda *a, **k: (calls.append("del"), True)[1],
                _lock_owner=lambda: None,
                _wait_bot=lambda *a, **k: 4242):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = auto.enable()
    out = buf.getvalue()
    chk(ok is True, "enable() 成功")
    chk(any("Register-ScheduledTask" in s for s in scripts), "注册命令真的跑了")
    chk(any("Start-ScheduledTask" in s for s in scripts), "启动命令真的跑了")
    chk(calls == ["del"], "**顺手清掉了老 Run 值**（两条路并存会在登录时打架）")
    chk("清掉了老的注册表自启值" in out, "这件事如实说出来")
    chk("4242" in out, "报的是真拿到的 pid（不编）")


def t10_elevate_paths():
    print("\n[10] _elevate()：UAC 被拒**不降级**；交接后如实报结果")
    real = auto.admin.ensure_elevated
    try:
        auto.admin.ensure_elevated = lambda **k: (False, "提权被取消（UAC 里点了「否」）。", False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok, handed = auto._elevate("on")
        chk(ok is False and handed is False, "被拒 → 不继续、不偷偷降级")
        chk("[!]" in buf.getvalue(), "如实打印失败原因")

        auto.admin.ensure_elevated = lambda **k: (True, "已在新窗口里以管理员身份启动", True)
        with _Patch(task_info=lambda *a, **k: {"task": auto.TASK_NAME, "state": "Running"}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                ok, handed = auto._elevate("on")
            chk(ok and handed, "交接给提权进程 → 本进程不再做一遍")
            o = buf.getvalue()
            chk("计划任务已注册" in o, "如实转述「任务已注册」，并指向 status 看最终结果")
            chk("pid" not in o,
                "**不许报 pid**：接管发生在那个窗口里，这里的 pid 会是接管前的旧实例（真机踩到）")

        with _Patch(task_info=lambda *a, **k: None, _wait_bot=lambda *a, **k: None,
                    time=types.SimpleNamespace(sleep=lambda s: None, time=time.time)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                auto._elevate("on")
            o = buf.getvalue()
            chk("还没读到计划任务" in o,
                "没确认到就如实说没确认（按 5 分钟自检还会再试），不谎报成功")
    finally:
        auto.admin.ensure_elevated = real


def t11_status_output():
    print("\n[11] status()：已开启 / 未开启 / 残留旧值要报警 / 结果码说人话")
    with _Patch(task_info=lambda *a, **k: {"task": auto.TASK_NAME, "state": "Ready",
                                           "runLevel": "Highest", "multiple": "IgnoreNew",
                                           "timeLimit": "PT0S", "repeats": "PT5M",
                                           "lastRun": "x", "lastResult": 0x800710E0,
                                           "nextRun": "y"},
                run_value=lambda *a, **k: None,
                _lock_owner=lambda: 777):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = auto.status()
    o = buf.getvalue()
    chk(rc == 0 and "已开启" in o and "PT5M" in o, "已开启时把权限/多实例/重复间隔说出来")
    chk("777" in o, "顺带报「助手现在在不在跑」")
    chk("已在运行" in o and "0x800710E0" not in o,
        "`0x800710E0`（重复触发被 IgnoreNew 拦下，**每 5 分钟都会出现**）翻成人话，不吓人")

    with _Patch(task_info=lambda *a, **k: None,
                run_value=lambda *a, **k: '"old" "bot.py"',
                _lock_owner=lambda: None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            auto.status()
    o = buf.getvalue()
    chk("未开启" in o, "没有任务 → 如实说未开启")
    chk("老的注册表自启值" in o and "迁移" in o, "残留老 Run 值必须报警 + 给出迁移命令")
    chk("**没有在跑**" in o, "助手没在跑时说出来（这正是那次事故的可见性缺口）")

    chk(auto.task_result_note(0) == "成功", "结果码 0 = 成功")
    chk("已在运行" in (auto.task_result_note(0x800710E0) or ""),
        "0x800710E0 = 重复触发被忽略（常态，不是故障）")
    chk(auto.task_result_note("garbage") is None, "不认识的结果码返回 None（绝不编）")


def main():
    print("=" * 66)
    print("autostart.py 回归自测（不碰计划任务 / 不弹 UAC / 不改注册表）")
    print("=" * 66)
    for t in (t1_register_shape, t2_no_duration_trap, t3_quoting, t4_user_is_console_user,
              t5_other_commands, t6_task_info_parsing, t7_legacy_run_value,
              t8_run_path_retired, t9_enable_behavior, t10_elevate_paths, t11_status_output):
        t()
    print()
    if not _OK:
        print(f"❌ 有失败项（{_PASS} 项通过）")
        return 1
    print(f"✅ 全部通过（{_PASS} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
