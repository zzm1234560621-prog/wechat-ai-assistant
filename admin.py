"""以**管理员权限**运行助手（本项目的一条硬约束）。

## 为什么必须有这个模块

语音条要读微信进程内存，而**跨完整性级别读不了**：微信若是提权打开的（High），
普通权限的助手（Medium）对它的 `OpenProcess(QUERY_INFORMATION|VM_READ)` 会被系统拒绝
（`GetLastError=5`）。2026-10-06 真机取证见 `docs/voice-reliability-2026-10-03.md` 第六节。
用户 2026-10-06 拍板：**永远让助手跑在管理员上，部署到别的电脑也一样**。

所以所有「起 bot」的入口都要过这里：不是管理员就**重新以管理员身份拉起自己**
（`-Verb RunAs` → 弹一次 UAC），然后本进程退出。**唯一判据是「现在是不是管理员」**。

## 怎么证明"提权已经发生过"（防无限循环）

不能靠环境变量：`Start-Process -Verb RunAs` **不继承**父进程环境变量
（而 PowerShell 5.1 的 `Start-Process` 又没有 `-Environment`，7.4+ 才有）。
也不能靠命令行开关：每个入口的参数解析都不一样（`bot.py` 有 `--once/--probe`、
`autostart.py` 有 `on/off/status`、`console.py` 没有），塞一个进去就得逐处改解析，
**任何一处漏改就是无限提权循环**。

所以用**一次性令牌文件**：父进程提权前写 `%TEMP%\\wxa_elevate_<pid>.tok` 并把它记在
`WXA_ELEVATE_TOKEN` 环境变量里（**这个变量是由提权命令行自己显式带上的**，见下），
子进程起来时 `claim_launch_token()` 读它 → 删掉 → 返回 True，"我知道自己是被提权拉起来的"。
令牌**删掉即失效**，所以下一次普通启动不会误判。

## 唯一实现的边界

`-Verb RunAs` 的命令串只在本模块里写一次，别在调用方各写一份
（`console.build_admin_command` 是给 installers 下 .ps1 用的另一套，别混）。

## 局限（要如实知道，别当它万能）

* 用户点了 UAC 的「否」→ 起不来（`ERROR_CANCELLED=1223`）；这里会如实说，
  **绝不偷偷降级成普通权限接着跑** —— 那正是本项目最忌讳的失效形态。
* 开机自启那一刻**没人点 UAC**：`-Verb RunAs` 在那种场景不是"会弹一下"，
  而是可能静默失败。所以自启那条路走 `assume=True`（不弹 UAC、只告警）。
  想**完全静默**地在开机时提权，标准做法是计划任务（`RunLevel=Highest`），不是 RunAs。
"""
import os
import subprocess
import sys
import tempfile

# 提权命令行自己带上的环境变量：值是那个一次性令牌文件的路径。
ENV_TOKEN = "WXA_ELEVATE_TOKEN"

# CreateProcess 的 WinError：用户点了 UAC 的「否」，或 UAC 策略直接拒绝。
ERROR_CANCELLED = 1223


def is_admin():
    """当前进程是不是**以管理员（elevated token）**在跑。

    ⚠️ 只用 `IsUserAnAdmin()`，**绝不拿"在不在 Administrators 组里"当判据** ——
    那两件事不一样：管理员账户在 UAC 下跑的程序默认也是**非提权**的。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ps_quote(s):
    """PowerShell 单引号字面量：内部 ' 双写。含空格/中文/单引号都安全。"""
    return "'" + str(s).replace("'", "''") + "'"


def _token_path(pid=None):
    return os.path.join(tempfile.gettempdir(), f"wxa_elevate_{pid or os.getpid()}.tok")


def claim_launch_token():
    """本进程是不是**刚被提权拉起来的**？是则删掉令牌并返回 True（一次性）。

    只在环境变量 `WXA_ELEVATE_TOKEN` 指向的**存在**的文件上返回 True：
    令牌是我们自己提权前写的，所以"文件在"这件事本身就是证据。
    """
    p = os.environ.get(ENV_TOKEN) or ""
    if not p or not os.path.isfile(p):
        return False
    try:
        os.remove(p)          # 一次性：删掉之后再来一次就不算"刚被提权拉起"
    except OSError:
        pass
    return True


def build_relaunch_command(argv=None, exe=None, cwd=None, token=None, wait=False):
    """构造"以管理员身份重新拉起自己"的 PowerShell 命令（纯函数，便于自测）。

    `argv` 默认取 `sys.argv`（`argv[0]` 是脚本路径），`exe` 默认取 `sys.executable`。

    写法和仓库既有那套（`console.build_admin_command`）同源：
    `Start-Process -FilePath <exe> -ArgumentList '<参数…>' -WorkingDirectory <cwd> -Verb RunAs`。
    参数里含空格/制表符/双引号就整体包一层双引号（CreateProcess 走 CRT 解析）。
    没有参数时**整条 `-ArgumentList` 都不写**（空串会被 PowerShell 参数校验拒掉——
    这个坑让菜单 [1] 降级必然失败过，见 `console.build_admin_command` 的 docstring）。

    令牌通过 PowerShell 的 `$env:` 赋值**在提权命令行内部**带过去（不依赖继承）：
    `$env:WXA_ELEVATE_TOKEN='…'; Start-Process … -Verb RunAs`。

    `wait=True` 加 `-Wait -PassThru | … exit $_.ExitCode`，由 `relaunch_elevated` 追加
    （等子进程结束、并把它自己的退出码原样带回给命令式入口 `botctl.py`）。
    """
    argv = list(sys.argv if argv is None else argv)
    exe = exe or sys.executable
    workdir = cwd or (os.path.dirname(os.path.abspath(argv[0])) if argv else os.getcwd())
    quoted = []
    for a in argv:
        a = str(a)
        if a == "" or any(c in a for c in ' \t"'):
            quoted.append('"' + a.replace('"', '\\"') + '"')
        else:
            quoted.append(a)
    parts = []
    if token:
        parts += [f"$env:{ENV_TOKEN}=" + _ps_quote(str(token)) + ";"]
    parts += ["Start-Process", "-FilePath", _ps_quote(exe)]
    if quoted:
        parts += ["-ArgumentList", _ps_quote(" ".join(quoted))]
    parts += ["-WorkingDirectory", _ps_quote(workdir), "-Verb", "RunAs"]
    if wait:
        parts += ["-Wait", "-PassThru", "|", "ForEach-Object", "{ exit $_.ExitCode }"]
    return " ".join(parts)


def relaunch_elevated(argv=None, exe=None, cwd=None, capture=False, wait=False):
    """真的弹 UAC 重新拉起自己。返回 `(是否已拉起, 一句人话)`。

    **不在这里判断"要不要提权"** —— 那是 `ensure_elevated()` 的事，
    这样自测可以只验命令串、不真的弹 UAC。

    * `capture=False`（交互式脚本用）：子进程**继承本窗口**，用户能在同一个窗口里看到它做什么。
      代价是拿不到它的 stderr，于是 UAC 被拒只能靠 PowerShell 自己弹的那句错误——
      所以这时返回的 True 只能理解成"**已经拉起来了**"，不能理解成"它跑成功了"。
    * `capture=True`（无窗口 / 看日志的脚本用）：把 stderr 抓回来，能明确区分
      "你点了「否」" 和"别的失败"。子进程自己开窗口，看不到输出是正常的。
    * `wait=True`：等子进程结束，把它的**退出码**原样带回来（命令式入口要这个）。
    """
    token = _token_path()
    try:
        with open(token, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        return False, f"提权失败（写不了令牌文件 {token}）：{e}"
    line = build_relaunch_command(argv=argv, exe=exe, cwd=cwd, token=token, wait=wait)
    try:
        if capture:
            p = subprocess.run(["powershell", "-NoProfile", "-Command", line],
                               capture_output=True, text=True, timeout=120)
        else:
            p = subprocess.run(["powershell", "-NoProfile", "-Command", line],
                               timeout=None)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"提权失败（拉不起 PowerShell）：{type(e).__name__}: {e}"
    if p.returncode == 0:
        return True, ("已在新窗口里以管理员身份启动"
                      + ("（这个窗口可以关了）" if not wait else ""))
    err = ""
    if capture:
        err = (p.stderr or "").strip() or (p.stdout or "").strip()
    low = err.lower()
    if (p.returncode == ERROR_CANCELLED or "取消" in err
            or "canceled" in low or "cancelled" in low):
        return False, ("提权被取消（UAC 里点了「否」）。**没有以管理员身份跑起来** —— "
                       "语音条那条链要管理员权限，确认后再启动一次。")
    if not capture:
        return False, (f"提权没成功（PowerShell rc={p.returncode}）。"
                       f"UAC 里点了「否」？还是被策略拦了？确认后再试一次。")
    return False, f"提权失败（rc={p.returncode}）：{err[:200] or '没有输出'}"


def ensure_elevated(argv=None, exe=None, cwd=None, assume=False,
                    capture=False, wait=False):
    """`(是否可以往下跑, 一句人话)`。

    返回 True 有两种含义，调用方**必须分清**（这是本模块最容易用错的地方）：

    | 情形 | True 的含义 | 调用方该做什么 |
    |---|---|---|
    | 本来就是管理员 | 就是这一份进程 | 继续跑 |
    | `claim_launch_token()` 命中 | **本进程就是提权拉起来的那一份** | 继续跑 |
    | 刚重新拉起了一个提权进程 | "已经拉起来了"，**而这一份不是它** | **立刻 return，别再往下跑** |

    第三种怎么分辨？看第二项：`(True, "已经提权", False)` = 就是这一份、继续跑；
    `(True, "已经提权", True)` = 别往下跑。`botctl.start()` 就是这么用的
    （那里错一次就是**两个助手同时轮询 hook**，实测会把微信搞崩）。

    `assume=True`（开机自启那条路用）：**不弹 UAC**，只在不是管理员时告警并继续。
    ⚠️ 这一档**排在令牌判定之前**：自启这条路从来不会自己提权，所以哪怕环境里
    侥幸留着一个令牌（用户先双击提权过一次、自启进程继承了那个环境），也不许据此
    认为"我已经是提权的了"——那会把"不是管理员"这件事**静默吞掉**。
    """
    if is_admin():
        return True, "已经是管理员", False
    if assume:
        msg = ("开机自启：现在**不是**管理员，而开机时没人点 UAC，所以没有自动提权。"
               "语音条会读不到微信内存。想让它静默提权，改用计划任务"
               "（RunLevel=Highest）；想手动提权就双击「启动助手.bat」点一次 UAC。")
        print(f"[admin] ⚠️ {msg}", file=sys.stderr, flush=True)
        return True, ("自启时**不是**管理员（没人点 UAC，所以没提权）——语音条会读不到；"
                      "想静默提权改用计划任务（RunLevel=Highest）"), False
    if claim_launch_token():
        return True, "刚被提权拉起（本进程已经是提权那一份）", False
    ok, msg = relaunch_elevated(argv=argv, exe=exe, cwd=cwd,
                                capture=capture, wait=wait)
    return ok, msg, ok
