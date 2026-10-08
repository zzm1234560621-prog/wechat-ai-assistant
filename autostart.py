"""设置/取消「开机自启」：让 bot 在后台一直跑，你在微信文件传输助手里直接对话。

用法：
  python autostart.py on       # 开启（注册计划任务；需要管理员，会弹一次 UAC）
  python autostart.py off      # 取消
  python autostart.py status   # 查看状态（**只读，不提权**）

## 为什么是计划任务，不是写 HKCU Run（2026-10-07 改，取代 2026-10-06 的 Run 键方案）

老实现写 `HKCU\\...\\Run`，三条毛病叠在一起，真机后果是「自启开着、却静默失联一整晚」：

  1. **Run 键是"一次性发射"，不是守护**：只在登录那一刻响一次；进程之后死掉，没有任何东西再拉它；
  2. **它只等约 5 分钟就放弃**（`bot.py` 的启动闸门）：微信晚开一会儿，它就永久下线；
  3. **开机那一刻没人点 UAC**：Run 键拉起来的**一定是普通权限**，而提权要弹 UAC；
     没人点就 `exit(2)`，而 pythonw 无窗口 ⇒ 一点提示都没有。

完整时间线、根因与真机证据见 **`docs/autostart-task-notes.md`**。这里只留结论：
任务是「登录时 + 每 5 分钟重复」，重复触发在任务实例（= bot 进程）还活着时被
`IgnoreNew` 忽略、死了才会重新拉起 —— **自启和看门狗是同一件事**。

任务的形状（`WeChatAIAssistant`）：

| 项 | 值 | 为什么 |
|---|---|---|
| 触发器 | `AtLogOn`（本用户）+ `Once` + 每 5 分钟重复、**不写 Duration** | 省略 Duration = 无限重复；写 `[TimeSpan]::MaxValue` 会被 Task Scheduler 拒收（`0x80041318`） |
| 权限 | `RunLevel=Highest` | 静默管理员：语音条那条硬约束直接满足，开机不再需要点 UAC |
| 多实例 | `MultipleInstances=IgnoreNew` | 「重复触发」= 挂了才拉起，而不是起第二个 |
| 时长上限 | `ExecutionTimeLimit=PT0S` | 不限；默认 3 天会把常驻的助手杀掉 |
| 其他 | `-StartWhenAvailable`、允许电池 | 错过触发点也补一次 |

`bot.py` 的 39001 单实例锁是第二道保险：不管谁同时拉，只会有一个活着。
"""
import json
import os
import subprocess
import sys
import time

import admin
import envsetup as env

# 任务名与老的注册表自启值**故意同名**：迁移时好认，也方便 status 一眼看出「这台机器是旧装法」。
TASK_NAME = "WeChatAIAssistant"
LEGACY_RUN_NAME = "WeChatAIAssistant"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 「每 5 分钟自检」的间隔。和 `bot.py` 的轮询间隔无关，只决定「挂了多久被发现」。
REPEAT_MINUTES = 5
# 等助手占住单实例端口的时长。父进程（非提权那一份）用它确认结果，绝不上来就报成功。
WAIT_BOT_SEC = 90


# ── 纯函数：命令串只在这里拼，便于自测（不真碰计划任务 / 不弹 UAC）─────────────

def _psq(s):
    """PowerShell 单引号字面量：内部 `'` 双写。含空格/中文/单引号都安全。"""
    return "'" + str(s).replace("'", "''") + "'"


def build_register_command(pyw, bot_py, workdir, task_name=TASK_NAME,
                           repeat_minutes=REPEAT_MINUTES):
    """构造「注册计划任务」的 PowerShell 命令（纯函数）。

    ⚠️ 三处**别改**：
      * `-RepetitionInterval` 必须带、`-RepetitionDuration` **必须不写**（写了才是坑，见文件头）；
      * `-ExecutionTimeLimit ([TimeSpan]::Zero)`（不限时长）；
      * `-MultipleInstances IgnoreNew`（自愈靠它，不是靠"多起几个"）。
    用户取 `Win32_ComputerSystem.UserName`（**控制台会话真正登录的那个用户**），
    绝不直接用当前身份：提权可能是别人用管理员账户点的，那会把任务挂到错的账户上。
    """
    arg = '"' + str(bot_py) + '"'          # -Argument 里给脚本路径加引号（路径可能含空格）
    return (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$u=(Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName; "
        "if(-not $u){$u=[Security.Principal.WindowsIdentity]::GetCurrent().Name}; "
        f"$a=New-ScheduledTaskAction -Execute {_psq(pyw)} -Argument {_psq(arg)} "
        f"-WorkingDirectory {_psq(workdir)}; "
        "$t1=New-ScheduledTaskTrigger -AtLogOn -User $u; "
        f"$t2=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {int(repeat_minutes)}); "
        "$p=New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Highest; "
        "$s=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
        f"Register-ScheduledTask -TaskName {_psq(task_name)} -Action $a -Trigger @($t1,$t2) "
        "-Principal $p -Settings $s -Force | Out-Null; "
        "Write-Output 'OK'"
    )


def build_unregister_command(task_name=TASK_NAME):
    return (f"Unregister-ScheduledTask -TaskName {_psq(task_name)} -Confirm:$false "
            "-ErrorAction Stop; Write-Output 'OK'")


def build_start_command(task_name=TASK_NAME):
    return (f"Start-ScheduledTask -TaskName {_psq(task_name)} -ErrorAction Stop; "
            "Write-Output 'OK'")


def build_query_command(task_name=TASK_NAME):
    """查询任务状态，**输出一行 JSON**（不解析本地化文本：中文 Windows 的 `schtasks` 没法稳解析）。"""
    return (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        f"$t=Get-ScheduledTask -TaskName {_psq(task_name)} -ErrorAction SilentlyContinue; "
        "if(-not $t){Write-Output '{}'} else { "
        f"$i=Get-ScheduledTaskInfo -TaskName {_psq(task_name)} -ErrorAction SilentlyContinue; "
        "$r=@($t.Triggers | ForEach-Object { [string]$_.Repetition.Interval } | "
        "Where-Object { $_ }); "
        "[pscustomobject]@{task=[string]$t.TaskName; state=[string]$t.State; "
        "runLevel=[string]$t.Principal.RunLevel; "
        "multiple=[string]$t.Settings.MultipleInstances; "
        "timeLimit=[string]$t.Settings.ExecutionTimeLimit; "
        "repeats=($r -join ','); lastRun=[string]$i.LastRunTime; "
        "lastResult=$i.LastTaskResult; nextRun=[string]$i.NextRunTime} | "
        "ConvertTo-Json -Compress }"
    )


# ── 与系统打交道 ───────────────────────────────────────────────────────

# Task Scheduler 的结果码 → 人话。`0x800710E0` 是**常态**（任务已在跑，本次重复触发被
# `IgnoreNew` 拦下）——不翻译的话 `status` 每 5 分钟都显示一个 2147946720，像是坏了。
_TASK_RESULT_NOTES = {
    0: "成功",
    267009: "任务正在运行",
    0x800710E0: "已在运行，本次重复触发被忽略（正常）",
}


def task_result_note(code):
    """把结果码翻成人话；不认识就返回 None（**绝不编**）。"""
    try:
        return _TASK_RESULT_NOTES.get(int(code))
    except (TypeError, ValueError):
        return None


def _run_ps(script, timeout=120):
    """跑一段 PowerShell，返回 `(rc, stdout, stderr)`。**不抛异常**（拿不到就如实回）。"""
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", f"{type(e).__name__}: {e}"
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def task_info(task_name=TASK_NAME):
    """任务状态 dict；**不存在或读不到返回 None**（`{}` 也当不存在）。"""
    rc, out, _err = _run_ps(build_query_command(task_name), timeout=45)
    if rc != 0:
        return None
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        return d or None
    return None


def run_value(name=LEGACY_RUN_NAME):
    """老实现留下的 HKCU Run 值（没有/读不到 → None）。**只读，不写。**"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, name)
        return str(v)
    except (FileNotFoundError, OSError):
        return None


def delete_run_value(name=LEGACY_RUN_NAME):
    """删掉老实现留下的 HKCU Run 值。返回 True = 真的删掉了。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _lock_owner():
    """谁占着 39001（助手在跑的判据）。**判据只走 botctl**，别在这里再写一份 netstat 解析。"""
    try:
        import botctl
        return botctl.owner()
    except Exception:
        return None


def _wait_bot(seconds=WAIT_BOT_SEC):
    deadline = time.time() + max(5, int(seconds))
    while time.time() < deadline:
        pid = _lock_owner()
        if pid:
            return pid
        time.sleep(2)
    return None


def _elevate(what):
    """需要管理员的动作先过这道闸。返回 `(能继续, 已交给提权进程)`。

    `capture=True`：拿得到 UAC 被拒的原因（**被拒绝绝不静默降级**）。
    """
    ok, msg, launched = admin.ensure_elevated(argv=sys.argv, exe=env.VENV_PY,
                                              cwd=env.BASE, capture=True)
    if not ok:
        print(f"[!] {msg}")
        return False, False
    if launched:
        print(f"[√] {msg}")
        if what == "on":
            # ⚠️ 这里**只报自己真知道的事**（2026-10-07 真机踩到）：父进程是非提权那一份，
            # 「注册任务 / 停掉旧助手 / 由任务接管」全在另一个提权窗口里做。
            # 早先这里报「助手在跑（pid N）」——那个 N 是**接管之前**的旧实例，
            # 说成"已交给任务"就等于把不知道的事说成知道。现在只等任务出现，如实转述状态。
            info = None
            for _ in range(6):
                info = task_info()
                if info:
                    break
                time.sleep(2)
            if info:
                print(f"[√] 计划任务已注册（{info.get('state')}）；"
                      f"「停掉旧助手 → 由任务接管」在那个提权窗口里做，"
                      f"最终结果看：python autostart.py status")
            else:
                print(f"[?] 提权窗口已经起来了，但还没读到计划任务（可能还在注册）。"
                      f"看那个窗口的输出和 bot.log；任务每 {REPEAT_MINUTES} 分钟还会再试。")
        return True, True
    return True, False


# ── 三个动作 ───────────────────────────────────────────────────────────

def enable():
    """开启自启：注册计划任务 + 清掉老 Run 值 + 让任务把助手拉起来。返回是否成功。"""
    if not env.venv_ready():
        print("[!] 虚拟环境未就绪或依赖缺失，请先双击 install.bat 完成安装。")
        print("    （如果刚移动过文件夹，重跑 install.bat 会自动修复。）")
        return False

    pyw = env.VENV_PYW if os.path.exists(env.VENV_PYW) else env.VENV_PY
    bot = os.path.join(env.BASE, "bot.py")

    ok, handed = _elevate("on")
    if not ok:
        return False
    if handed:
        return True

    print(f"[1/3] 注册计划任务「{TASK_NAME}」：登录时启动 + 每 {REPEAT_MINUTES} 分钟自检，"
          f"静默管理员权限（以后开机不再需要点 UAC）…")
    rc, out, err = _run_ps(build_register_command(pyw, bot, env.BASE))
    info = task_info()
    if rc != 0 or not info:
        print(f"[!] 注册失败（rc={rc}）：{(err or out)[:200] or '没有输出'}")
        return False
    print(f"[√] 任务已注册（{info.get('state')} / 权限 {info.get('runLevel')} / "
          f"多实例 {info.get('multiple')}）")

    if delete_run_value():
        print(f"[√] 顺手清掉了老的注册表自启值（HKCU Run\\{LEGACY_RUN_NAME}）"
              f"——两条路并存会在登录时打架")
    else:
        print("[i] 没有老的注册表自启值要清（干净）")

    if info.get("state") == "Running" and _lock_owner():
        print("[i] 任务已经在跑、助手也在跑：本次只刷新了任务定义，没动正在运行的助手。")
        return True

    if _lock_owner():
        print("[2/3] 先停掉当前这个（不由任务托管的）助手，改由任务接管…")
        try:
            import botctl
            _ok, msg = botctl.stop()
            print(f"      {msg}")
        except Exception as e:
            print(f"      ⚠️ 停不掉（{type(e).__name__}: {e}），继续往下走")

    print("[3/3] 启动任务…")
    rc2, out2, err2 = _run_ps(build_start_command())
    if rc2 != 0:
        print(f"      ⚠️ 启动指令返回 rc={rc2}：{(err2 or out2)[:160]}")
    owner = _wait_bot(WAIT_BOT_SEC)
    if owner:
        print(f"[√] 助手在跑（pid {owner}），由任务托管：挂了 {REPEAT_MINUTES} 分钟内自己回来。")
        print("    日志见 bot.log；关闭自启：python autostart.py off")
        return True
    print(f"[!] 任务已注册，但 {WAIT_BOT_SEC} 秒内没看到助手占住 39001。"
          f"看 bot.log；任务下一次触发（≤{REPEAT_MINUTES} 分钟）还会再试一次。")
    return False


def disable():
    """取消自启：删任务 + 删老 Run 值。**不停止正在跑的助手**（那和"自启"是两件事）。"""
    ok, handed = _elevate("off")
    if not ok:
        return False
    if handed:
        return True

    info = task_info()
    if info:
        rc, out, err = _run_ps(build_unregister_command())
        if rc != 0:
            print(f"[!] 删任务失败（rc={rc}）：{(err or out)[:200] or '没有输出'}")
            return False
        print(f"[√] 已取消开机自启（任务「{TASK_NAME}」已删除）。")
    else:
        print("当前没有开启自启（任务不存在）。")
    if delete_run_value():
        print("[√] 也清掉了老的注册表自启值（HKCU Run）。")
    print("[i] 助手本身没有被停：要停它用「助手.bat」菜单，或 python botctl.py stop。")
    return True


def status():
    """查看状态。**只读，不提权**（看一眼状态不该弹 UAC）。返回退出码。"""
    info = task_info()
    rv = run_value()
    if info:
        print(f"自启状态：已开启（计划任务「{info.get('task')}」）")
        print(f"  状态={info.get('state')}  权限={info.get('runLevel')}  "
              f"多实例={info.get('multiple')}  时长上限={info.get('timeLimit')}")
        if info.get("repeats"):
            print(f"  自检重复间隔={info.get('repeats')}（任务活着时被忽略，挂了才重新拉起）")
        _note = task_result_note(info.get("lastResult"))
        print(f"  上次触发={info.get('lastRun')}（结果 {info.get('lastResult')}"
              + (f"：{_note}" if _note else "") + f"）  下次触发={info.get('nextRun')}")
    else:
        print("自启状态：未开启（没有计划任务）。开启：python autostart.py on")
    if rv:
        print(f"  ⚠️ 还留着**老的注册表自启值**（HKCU Run\\{LEGACY_RUN_NAME}）：")
        print("     这台机器是旧装法，两条路并存会在登录时同时拉进程（还会弹 UAC）。")
        print("     迁移：python autostart.py on（注册任务并顺手删掉它）")
    owner = _lock_owner()
    print("  助手现在：" + (f"在跑（pid {owner}）" if owner else "**没有在跑**"))
    return 0


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if arg == "on":
        return 0 if enable() else 2
    if arg == "off":
        return 0 if disable() else 2
    if arg != "status":
        print(f"[!] 不认识的动作「{arg}」。用法：python autostart.py on|off|status")
        return 2
    return status()


if __name__ == "__main__":
    sys.exit(main())
