"""微信 AI 助手 - 统一控制台（单一入口，控制所有功能）。

双击 助手.bat 就会进入本菜单。分四组：
  · 安装 / 首次配置 —— 一键走完选 [4]；手动分步 1 降级 → 2 装依赖 → 3 配模型
  · 运行控制 —— 启动（后台/前台）/ 停止 / 重启 / 实时看日志
  · 诊断排查 —— 看状态 / 看日志 / 真机自检 / 跑全部自测
  · Hook 与微信 —— 装/摘/装回 hook、看微信版本与自启状态

⚠️ 这个文件是**系统 python** 跑的（见 助手.bat 里的 `where python`），**不是 venv**。
所以顶层只许导入标准库 + `envsetup`（它也只导标准库）——
一旦顶层去 import 第三方包（yaml 之类），「装依赖」这条路自己就先崩了，用户会卡死。
需要读配置时就**在函数里延迟导入**，并且自己兜住异常（见 `_status_page`）。
"""
import os
import re
import subprocess
import sys
import time

import admin
import botctl
import envsetup as env
import settings

BASE = env.BASE

# hook 相关脚本（都在 installers\wechat-4.1.10.27\ 下，**都要管理员**）
HOOK_DIR = os.path.join(BASE, "installers", "wechat-4.1.10.27")

# 本主线唯一支持的微信版本。hook（version.dll）是按 4.1.10.27 的**函数偏移**编译的，
# 换个版本就注不进去——而且**不报错**：DLL 会被微信正常加载、只是挂钩失败，
# 30001 永远没人监听，用户看到的只有 bot 一直「连不上 30001」。
# ⚠️ 这个字面量与 installers/wechat-4.1.10.27/_common.ps1 里那份 $WX_WANTED_VERSION
#    必须一致（selftest_portable.py 盯着这一对），改一处就得改另一处。
WANTED_WEIXIN = "4.1.10.27"


def _ps_quote(s):
    """把字符串变成 PowerShell 单引号字面量：内部的 ' 双写转义。

    PowerShell 里单引号串中只有 ' 需要转义（写成 ''），
    所以含空格/单引号/中文/反斜杠的路径都能安全放进去，不会被拆成多个参数或注入。
    """
    return "'" + str(s).replace("'", "''") + "'"


def build_admin_command(script, args=None, python=None, base=None):
    """构造控制台用来提权跑脚本的 PowerShell 命令行（纯函数，便于自测）。

    坑（曾让菜单 [1] 降级必然失败）：以前写成
        Start-Process -FilePath '...' -ArgumentList '' -WorkingDirectory '...' -Verb RunAs
    `-ArgumentList` 空串会被 PowerShell 的参数校验直接拒掉
    （Cannot validate argument on parameter 'ArgumentList'. The argument is null or empty）。
    所以：**没有参数时整条 -ArgumentList 都不写**；
    有参数时按 PowerShell 原生命令的参数规则逐个引用（含空格就整体加一层双引号）。
    仍然只用 -Verb RunAs 提权，不换别的方式。
    """
    exe = python if python is not None else sys.executable
    workdir = base if base is not None else BASE
    argv = [str(script)] + [str(a) for a in (args or [])]
    # PowerShell 把这条字符串交给原生程序（CreateProcess）时要走 CRT 的参数解析：
    # 参数里含空格/制表符/双引号就必须整体包一层双引号，内部的双引号用 \" 表示。
    quoted = []
    for a in argv:
        if a == "" or any(c in a for c in ' \t"'):
            quoted.append('"' + a.replace('"', '\\"') + '"')
        else:
            quoted.append(a)
    parts = [
        "Start-Process",
        "-FilePath", _ps_quote(exe),
    ]
    if quoted:
        parts += ["-ArgumentList", _ps_quote(" ".join(quoted))]
    parts += [
        "-WorkingDirectory", _ps_quote(workdir),
        "-Verb", "RunAs",
    ]
    return " ".join(parts)


def run(script, args=None, admin=False):
    cmd = [script] + (args or [])
    if admin:
        # 用系统 python 在新管理员窗口里跑（降级需要提权）。
        # powershell 收到的是**一条完整命令字符串**，不是 argv 列表，所以不能用 list 形式。
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          build_admin_command(script, args)])
    else:
        subprocess.run([sys.executable] + cmd, cwd=BASE)


def run_venv(script, args=None):
    """用 **venv 的** Python 跑一个脚本。

    ⚠️ `run()` 用的是**系统 Python**（console 自己就是这个），而「下语音模型」这类脚本
    必须在 venv 里跑（它要 import faster-whisper / yaml）。两件事别混。
    """
    if env.venv_python() is None:
        print("[!] 虚拟环境还没建好或已失效——先跑 install.bat（或菜单 [2] 安装依赖）。")
        return None
    return subprocess.run([env.VENV_PY, script] + (args or []), cwd=BASE)


def auto():
    """一键：检测版本 -> 不兼容则降级 -> 装依赖 -> 启动。"""
    print("\n[自动] 开始一键流程 ...")
    try:
        import wechat_version as wv
    except Exception as e:
        print(f"[自动] 无法加载版本检测：{e}")
        return

    info = wv.detect()
    if not info:
        print("[自动] 未检测到已安装的微信电脑版。请先安装并登录微信 3.9.x。")
        return

    wver = info["version"]
    wcfer, msg = wv.match_wcferry(wver)
    print(f"[自动] 检测到微信版本：{wver}")
    print(f"[自动] {msg}")

    if not wcfer:
        if str(wver).startswith("4."):
            # 4.x 是**主线**（微信 4.1.10.27 + aixed hook），跟 wcferry 没有关系。
            # 以前这里会直接把 4.x 用户推向「降级到 3.9.x」——那是把主线用户带沟里，
            # 而且降级会掉登录态、还要重新扫码。这里改成指路，不再劝降级。
            print("\n[自动] 检测到的是微信 4.x。4.x 走的是 **aixed hook** 这条主线"
                  "（version.dll 注入 + 本地 HTTP :30001），**不需要 wcferry，"
                  "也不要降级到 3.9.x**。")
            print("[自动] 请照 README 的「微信 4.x 主线」一节：先用"
                  " installers/wechat-4.1.10.27/ 里的脚本装好 hook，再双击 启动助手.bat。")
            return
        print("\n[自动] 当前版本配不上 wcferry。要么降级到 3.9.x，要么改用 4.x 主线"
              "（4.x 要装 aixed hook，见 README）。")
        ans = input("[自动] 现在启动降级程序吗？(y/n)：").strip().lower()
        if ans in ("y", "yes", "是"):
            run("downgrade.py", admin=True)
            print("[自动] 已在新窗口启动降级。完成后回到菜单，再选一次 [7] 自动。")
        else:
            print("[自动] 已取消。降级完成后请重新选 [7]，或按 1→2→3 手动走。")
        return

    if env.venv_ready():
        print("[自动] 虚拟环境已就绪，跳过安装。")
    else:
        print(f"[自动] 开始安装依赖（wcferry=={wcfer}）...")
        run("installer.py")
        if not env.venv_ready():
            print("[自动] 依赖仍未就绪（多半是网络或 Python 版本问题），已中止。")
            print("[自动] 请手动跑 install.bat 看详细报错。")
            return

    print("[自动] 启动助手（Ctrl+C 停止）...")
    _ok, _msg = admin.ensure_elevated(argv=["bot.py"], exe=env.VENV_PY, cwd=BASE)
    if not _ok:
        print(f"[自动] {_msg}")
        return
    subprocess.run([env.VENV_PY, "bot.py"], cwd=BASE)


def build_ps1_admin_command(script_path):
    """构造「提权跑一个 .ps1」的 PowerShell 命令行（纯函数，便于自测）。

    ⚠️ 不能走 `build_admin_command`：那个是给 **Python 脚本**用的
    （`-FilePath <python> -ArgumentList '<script>'`）。拿它去跑 `.ps1`
    等于让 python 去解释 PowerShell，必然失败。
    `.ps1` 要走 `powershell.exe -File <路径>`。

    同样注意坑：`-ArgumentList` 里每一项都用单引号字面量（`_ps_quote`），
    路径含空格/中文/单引号都不会被拆开或注入。
    """
    args = ",".join([_ps_quote("-NoProfile"), _ps_quote("-ExecutionPolicy"),
                     _ps_quote("Bypass"), _ps_quote("-File"),
                     _ps_quote(script_path)])
    return (f"Start-Process -FilePath 'powershell.exe' "
            f"-ArgumentList {args} -Verb RunAs")


def run_ps1(script, admin=True):
    """跑 installers 下的 .ps1。返回 `(是否真的拉起来了, 一句人话)`。

    `admin=True` 时用 `-Verb RunAs` 在**新窗口**里提权跑（这些脚本要改微信目录）；
    失败（比如用户点了「否」UAC）只能靠窗口里的输出，所以这里如实说「已在新窗口启动」。
    """
    p = os.path.join(HOOK_DIR, script)
    if not os.path.isfile(p):
        return False, f"找不到脚本：{p}"
    if not admin:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", p], cwd=BASE)
        return True, "已跑完。"
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          build_ps1_admin_command(p)])
    except OSError as e:
        return False, f"拉不起来：{type(e).__name__}: {e}"
    return True, ("已在**新窗口**里提权启动（可能弹 UAC，要点「是」）。\n"
                  "    结果看那个窗口（脚本也会写 *-log.txt 到 installers 目录）。")


def _status_page():
    """读 config.yaml 的 status 段。返回 `(enabled, host, port, 错误)`。

    ⚠️ **yaml 必须延迟导入**：这个文件是系统 python 跑的（见模块头注释），
    顶层导入 yaml 会让「装依赖」这条路自己也起不来。
    """
    try:
        import yaml
        with open(os.path.join(BASE, "config.yaml"), encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        st = d.get("status") or {}
        try:
            port = int(st.get("port") or 39002)
        except (TypeError, ValueError):
            port = 39002
        return bool(st.get("enabled")), str(st.get("host") or "127.0.0.1"), port, ""
    except Exception as e:
        return False, "", 0, f"{type(e).__name__}: {e}"


def _clean(inp):
    """把用户输入清干净：**去掉 BOM 再去空白**。

    ⚠️ BOM 不是幻想：PowerShell 5.1 往原生程序的管道里写字符串时会带一个 UTF-8 BOM，
    于是第一行读进来是 `'\\ufeff10'`，菜单会把它当非法输入。
    手敲键盘不会有 BOM，但**脚本化输入 / 自测会**——这个功能的自测一开始就被它绊了一下。
    `.strip()` 去不掉 BOM（它不是空白字符），所以必须显式 lstrip。
    """
    return str(inp or "").lstrip("\ufeff").strip()


def _confirm(prompt, default_no=True):
    """问一句 y/N。**空输入时**：`default_no=True`（默认）当「否」，否则当「是」。

    ⚠️ 两个方向的默认值都必须存在，别图省事统一成一个：
      * 停止/重启/装 hook —— 这些要**明确点头**，空回车一律当「否」（`default_no=True`）；
      * 「一键配置」那种想让人**一路回车走完**的流程 —— 空回车当「是」（`default_no=False`）。
        这是用户要的「一键」：按一下 9，然后连按回车就行。
    """
    v = _clean(input(prompt)).lower()
    if not v:
        return not default_no
    return v in ("y", "yes", "是")


def health_screen():
    """一屏：进程 + 健康快照。"""
    print()
    print(botctl.status_text())
    print()
    print(botctl.fmt_health(botctl.read_health()))


def _verify_real_flow():
    """真机自检。**它自己要求 bot 停着**（两路查询同时压在 hook 上会把微信搞崩），
    所以在跑的话先问要不要停，跑完再问要不要起回来。"""
    was_running = botctl.is_running()
    if was_running:
        print("[!] 真机自检是只读的，但它**要求 bot 先停**——")
        print("    两路查询同时压在 hook 上实测会把微信搞崩（项目里崩过 6 次）。")
        if not _confirm("    先停止助手再跑自检？(y/N) "):
            print("已取消。要自己先停：菜单 [6]。")
            return
        ok, msg = botctl.stop()
        print(("[√] " if ok else "[!] ") + msg)
        if not ok:
            return
    run("verify_real.py")
    if was_running and _confirm("\n跑完了。要把助手重新启动吗？(y/N) "):
        ok, msg = botctl.start()
        print(("[√] " if ok else "[!] ") + msg)


def _submenu(title, entries):
    """单键子菜单。`entries` = [(键, 标签, 回调)]；回调返回字符串就打印出来。

    ⚠️ **为什么一律单键**：这个菜单一度平铺到 20 项，于是 [10]~[20] 需要**敲两个数字**——
    用户当场就说「我键盘怎么有 10？」。手放在数字键上的人期待的是**一个键一个动作**，
    所以顶层只留最常用的 8 个，其余按主题收进子菜单；**[0] 恒为返回**（在顶层就是退出）。
    """
    while True:
        print()
        print("=" * 46)
        print(f"  {title}")
        print("=" * 46)
        for k, label, _fn in entries:
            print(f"   [{k}] {label}")
        print("   [0] 返回主菜单")
        print("=" * 46)
        c = _clean(input("请输入数字选择："))
        if c == "0":
            return None
        hit = None
        for k, _label, fn in entries:
            if c == k:
                hit = fn
                break
        if hit is None:
            top = max((k for k, _l, _f in entries), key=lambda s: int(s))
            print(f"无效选择，请输入 0~{top}。")
        else:
            msg = hit()
            if msg:
                print(msg)
        input("\n按回车返回 ... ")


# ── 各个动作（菜单和子菜单共用同一批函数，别写两份）──────────────────────

def act_downgrade():
    run("downgrade.py", admin=True)
    print("已在独立窗口启动降级程序（需管理员权限），请在那边操作。")
    return None


def act_start():
    ok, msg = botctl.start()
    return ("[√] " if ok else "[!] ") + msg


def act_stop():
    print(botctl.stop(dry_run=True)[1])
    if not _confirm("确认停止？(y/N) "):
        return "已取消。"
    ok, msg = botctl.stop()
    return ("[√] " if ok else "[!] ") + msg


def act_restart():
    if not _confirm("确认重启助手？(y/N) "):
        return "已取消。"
    ok, msg = botctl.restart()
    return ("[√] " if ok else "[!] ") + msg


def act_foreground():
    if not env.venv_ready():
        return "虚拟环境未就绪（不存在、已失效或依赖缺失），请先「安装依赖」。"
    # 和后台那条路同一个门（`botctl.start` 里也有一道）：助手必须以管理员跑。
    # 控制台通常已经是管理员了（`main()` 开头就提权），这里是给"别的调用方"兜底。
    _ok, _msg = admin.ensure_elevated(argv=["bot.py"], exe=env.VENV_PY, cwd=BASE)
    if not _ok:
        return _msg
    print("[自动] 前台启动（Ctrl+C 停止）...")
    subprocess.run([env.VENV_PY, "bot.py"], cwd=BASE)
    return None


def act_log_tail():
    print()
    print(botctl.tail(40))
    return None


def act_fix_hook():
    """只有一件事：**把包里那份 version.dll 覆盖到微信目录**，并当场核对哈希。

    为什么单独有这一项（2026-10-06 真机，用户卡了一下午）：
    「包里那份是新的」和「微信里装的那份是新的」是**两个文件**——解压新包、装依赖、
    配模型都不会碰微信目录里那个 `version.dll`。而 `do_hook_install.ps1` 前面还有一道
    版本闸（版本不对就什么都不做），于是出现过：用户重装了好几遍包，微信目录里
    躺的**始终是旧 DLL**，助手每 10 秒刷「hook 已加载，但数据库打不开（微信没登录？）」
    ——文案把人往"扫码登录"上带。
    这一项**不过版本闸**（换文件本身与版本无关），也不改任何设置，只做替换 + 哈希自验，
    结果**落盘**（提权窗口一关输出就没了）。要恢复：同目录下备份着 `version.dll.bak_*`。
    """
    script = "do_fix_hook.ps1"
    print("    用途：微信目录里那份 hook 是旧的（比如 519168），把包里这份（527360）换上去。")
    print("    ⚠️ 换之前请**完全退出微信**（右下角托盘图标右键退出）——文件被占用会替换失败。")
    size, note = _hook_dll_state()
    print(f"    现在微信目录里那份：{note}")
    if not _confirm("确认替换？(y/N) "):
        return "已取消。"
    ok, msg = run_ps1(script)
    logp = os.path.join(HOOK_DIR, "hook-fix-log.txt")
    lines = []
    try:
        with open(logp, "r", encoding="ascii", errors="replace") as fh:
            lines = [ln.rstrip() for ln in fh.read(4000).splitlines() if ln.strip()]
    except OSError:
        lines = []
    tail = "\n".join("    " + ln for ln in lines[-8:]) if lines else "    [!] 没读到日志（脚本没跑起来？）"
    return (("[√] " if ok else "[!] ") + msg + "\n" + tail +
            "\n    → 换完**重启微信并扫码登录**，再起助手。"
            f"\n    （要还原：微信目录下有 version.dll.bak_* 备份；日志：{logp}）")


def act_hook(script, what):
    # 装 hook / 装回 hook 之前先过版本闸：版本不对时装了也不生效（见 ensure_weixin_version）。
    # 「摘 hook」**不过闸** —— 想摘的时候，版本对不对都得能摘掉。
    if script in ("do_hook_install.ps1", "do_restore_hook.ps1"):
        if not ensure_weixin_version():
            return ("已中止：微信版本不是 " + WANTED_WEIXIN + "，装 hook 不会生效。\n"
                    "    想先排查可以先看 助手.bat → [8] → [5] 查看微信版本。")
    if not _confirm(f"确认「{what}」？可能要管理员权限。(y/N) "):
        return "已取消。"
    ok, msg = run_ps1(script)
    return ("[√] " if ok else "[!] ") + msg


def act_status_page():
    enabled, host, port, err = _status_page()
    if err:
        return f"读不到 config.yaml 的 status 段：{err}"
    if not enabled:
        return ("状态页是**关着的**（config.yaml 里 `status.enabled: false`）。\n"
                "要开：把那一项改成 true，然后重启助手。\n"
                "（它只绑回环地址，页面上有 wxid/群名，别往局域网上开。）")
    if not botctl.is_running():
        return (f"配置里是开着的（{host}:{port}），但**助手没在跑**，页面不会有人响应。"
                f"先启动助手。")
    url = f"http://{host}:{port}"
    print(f"打开 {url} …")
    try:
        os.startfile(url)          # 只有 Windows 有；这是 Windows 项目
    except (AttributeError, OSError) as e:
        return f"打不开浏览器（{e}），自己访问：{url}"
    return None


# ── 配套服务：网上搜索后端（SearXNG）────────────────────────────────────
# 「启/停/看」的实现在 botctl.py（和 bot 自己的启停同一个所有者），这里只显示菜单、调它。
# config 由 botctl.load_cfg() 读（它已经处理了「yaml 延迟导入 + 读不出来不抛」）。

def act_search_status():
    return botctl.search_status_text(botctl.load_cfg())


def act_search_start():
    cfg = botctl.load_cfg()
    print()
    print(botctl.search_status_text(cfg, probe=False))     # 先给现状，再看拉起来的结果
    print("\n启动中（要等它能真查出来才算成功，最多 40 秒）…")
    ok, msg = botctl.search_start(cfg=cfg)
    return ("[√] " if ok else "[!] ") + msg


def act_search_stop():
    cfg = botctl.load_cfg()
    print(botctl.search_stop(cfg=cfg, dry_run=True)[1])
    if not _confirm("确认停止搜索服务？(y/N) "):
        return "已取消。"
    ok, msg = botctl.search_stop(cfg=cfg)
    return ("[√] " if ok else "[!] ") + msg


def _enable_runtime_switch(section, what):
    """把某个功能的**运行时开关**（`<section>.enabled`）写进 `settings.json`，返回一句人话。

    为什么走 settings.json 而不是 config.yaml：项目铁律 —— **程序绝不回写带注释的
    config.yaml**（回写会把注释和用户手改的那份一起抹掉）。而 `settings.effective()`
    会把 settings.json 里的键**盖在** config.yaml 之上，所以「装完就能用」和
    「config.yaml 仍是用户的地盘」两件事同时成立。

    ⚠️ 必须**读-改-写**整段：直接 `set_value(section, {"enabled": True})` 会把用户
    在 settings.json 里已有的同段键（比如 search.max_results）冲掉。
    ⚠️ 返回的话必须**说清改了哪个文件的哪个键**：静默改用户配置在这个项目里是禁止的。
    """
    try:
        d = settings.load()
        sec = dict(d.get(section) or {})
        before = sec.get("enabled")
        sec["enabled"] = True
        d[section] = sec
        settings.save(d)
    except Exception as e:                        # noqa: BLE001
        return (f"[!] 开关没写成（{type(e).__name__}: {e}）——手动把 {section}.enabled "
                f"设成 true 也能用。")
    was = "" if before is True else (f"（原来是 {before!r}）" if before is not None else "")
    return (f"[√] 已打开{what}：把 **settings.json** 的 `{section}.enabled` 设成 true{was}；"
            f"**没有动** config.yaml 里那份带注释的配置（想关回来就删掉 settings.json 这一项，"
            f"或在 config.yaml 改成 false 之前先删它）。")


def act_search_install():
    cfg = botctl.load_cfg()
    home = botctl.search_home(cfg)
    print(f"搜索后端目录：{home}")
    print("它会在这个目录里建一份**自己专用的** .venv 并装依赖（和助手的 venv 分开，")
    print("免得 flask/lxml 这些互相顶版本）。第一次要联网下载，可能几分钟。")
    if not _confirm("现在装？(Y/n) ", default_no=False):
        return "已取消。"
    ok, msg = botctl.search_install(cfg=cfg, home=home)
    if not ok:
        return "[!] " + msg
    _set_opt("search", True)
    out = ["[√] " + msg,
           # 装完**顺手打开开关**（2026-10-05 用户拍板）：否则装了也用不了——
           # `web_read.enabled()` 要求 effective 配置里 search.enabled 严格为 True，
           # 而它默认是 false；用户只会看到「装了却搜不了」。
           _enable_runtime_switch("search", "网上搜索")]
    # 再顺手起一下：装了却要等下次重启助手才生效，同样会被当成「装了没用」。
    try:
        cfg2 = botctl.load_cfg()                # 开关刚写进 settings.json，重读一次
        ok2, m2 = botctl.search_start(cfg=cfg2, home=home, wait=20)
        out.append(("[√] " if ok2 else "[!] ") + m2)
    except Exception as e:                      # noqa: BLE001
        out.append(f"[!] 起搜索服务时出错（{type(e).__name__}: {e}）——"
                   f"下次启动助手会自动把它带起来。")
    return "\n".join(out)


# ── 可选组件：语音转文字 / 网上搜索 / 文件格式增强包 / 本地语义检索 ───────
# 为什么要有这一段（2026-10-05）：这些能力**代码都在、包里也都在**，但依赖与模型都不随包
# （它们在 requirements.txt 里只能写成注释行；语音模型、torch、SearXNG 的 venv 更不能跨机器拷）
# ——于是「装完就能用」在别人机器上并不成立，而 README 把语音条转文字、压缩包、视频
# 都写在功能卖点里。这里补的就是那个**安装入口**，并把「这一项要不要装」记进 settings.json
# （按项目约定：程序绝不回写带注释的 config.yaml）。
#
# 每项一个 owner，这里只做菜单与编排，不实现第二份：
#   * 语音：envsetup.install_optional（pip）+ audio_read.py --setup（下模型）
#   * 搜索：botctl.search_install（建 venv + pip）
#   * 格式包：envsetup.install_optional（一条 pip 装齐）+ archive_read.find_rar_tool（.rar 的壳）
#   * 语义：envsetup.install_optional + semantic.py --setup（下模型）--build（建索引）
OPTIONAL_ITEMS = (
    ("voice", "语音转文字（装依赖 + 下模型）"),
    ("search", "网上搜索后端（建 venv + 装依赖）"),
    ("formats", "文件格式增强包（视频 / 邮件 / 压缩包 / 老 Office / PDF 图）"),
    ("semantic", "本地语义检索（装 torch 系依赖 + 下模型 + 建索引）"),
)

# 哪些项在「一键部署」里**默认不装**（回车=跳过，要手打 y 才装）。
# 2026-10-05 用户拍板：**本地语义检索也自动装**（此前它是这里唯一一项）——
# 他要的就是「一键配置走完、四项全齐」。代价他认了：torch 几百 MB + 下模型；
# 唯一保留的人工确认是**建索引要停一下助手**（那件事仍在 `_install_semantic` 里单独问）。
# 机制留着：以后再有「重到不该一路回车就装」的项，把名字加进这个集合即可。
_HEAVY = set()


def _opt_wanted(name):
    """这一项「一键部署要不要自动装」。settings.json 的 `optional.<name>`；
    **没写过 = 要**（用户 2026-10-03 的决定就是一键部署自动装齐、每项可关）。"""
    try:
        v = (settings.load().get("optional") or {}).get(name)
    except Exception:
        v = None
    return v is None or v is True


def _set_opt(name, on):
    """把开关写进 settings.json（读-改-写，**不动 config.yaml**）。"""
    try:
        d = settings.load()
        opt = dict(d.get("optional") or {})
        opt[name] = bool(on)
        d["optional"] = opt
        settings.save(d)
        return True
    except Exception as e:
        print(f"[!] 开关没写成：{type(e).__name__}: {e}")
        return False


def _install_voice():
    """装语音转文字：先 pip 装依赖，再下模型（两步都不是同一件事，分开说）。"""
    ok, msg = env.install_optional("voice")
    print(("[√] " if ok else "[!] ") + msg)
    if not ok:
        return "语音转文字的**依赖**没装成，模型先不下了（下了也用不了）。"
    print("[可选组件] 接下来下模型（大小看 config.yaml 的 `audio.model`，默认 small ≈464MB，"
          "走 hf-mirror 镜像）…")
    run_venv("audio_read.py", ["--setup"])
    ok2, msg2 = _voice_state()
    if ok2:
        _set_opt("voice", True)
    return ("[√] " if ok2 else "[!] ") + msg2


def _voice_state():
    """语音转文字现在能不能用——**判据走 audio_read.available()**（用 venv 的 python 问它）。"""
    py = env.venv_python()
    if py is None:
        return False, "虚拟环境还没建好，语音转文字用不了（先装依赖）。"
    try:
        r = subprocess.run([py, "audio_read.py", "--status"], cwd=BASE,
                           capture_output=True, timeout=60, text=True,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"问不动语音那一侧（{type(e).__name__}: {e}）"
    out = (r.stdout or "").strip().splitlines()
    return r.returncode == 0, (out[-1] if out else "（没有输出）")


def _search_state():
    """搜索后端现在能不能用（依赖装没装 / 服务在不在跑）。"""
    try:
        cfg = botctl.load_cfg()
        home = botctl.search_home(cfg)
        port = botctl.search_port(cfg)
    except Exception as e:
        return False, f"读不出搜索配置（{type(e).__name__}: {e}）"
    if not os.path.isdir(home):
        return False, f"没有 SearXNG 目录（{home}）——包不完整，或 search.home 写错了。"
    if not botctl.search_ready(home):
        return False, f"依赖还没装（{home}）——用 [{_opt_index('search')}] 装。"
    pid = botctl.search_owner(port)
    if pid:
        return True, f"依赖已装，服务在跑（pid {pid}，端口 {port}）。"
    return True, (f"依赖已装，但**服务没在跑**（端口 {port}）——"
                  f"助手.bat → [8] 更多 → [9] 搜索服务 → [1] 启动。")


def _formats_state():
    """文件格式增强包：六个小依赖装了没 + `.rar` 的外部解压器在不在。

    ⚠️ 两件事**必须分开说**：`rarfile` 只是个壳，`.rar` 真正要的是外部解压器
    （unrar / 7z / bsdtar）。装完 rarfile 却报「.rar 能读了」就是**假成功**。
    """
    left = env.missing_optional("formats")
    try:
        import archive_read                       # 纯 stdlib，系统 python 也能导
        tool = archive_read.find_rar_tool()
    except Exception as e:                        # noqa: BLE001
        tool, _err = None, e
    if left:
        return False, f"还缺 {len(left)} 个：{', '.join(left)}——用 [{_opt_index('formats')}] 装。"
    rar = f"有（{tool}）" if tool else "**没有** → .rar 仍读不了（装个 7-Zip 或 WinRAR 就行）"
    return True, (f"六个依赖都在（视频 / .msg 邮件 / .7z / .rar / 老 Office / PDF 内嵌图）。\n"
                  f"       `.rar` 的外部解压器：{rar}")


def _install_formats():
    """装文件格式增强包（一条 pip 装齐）。装完**必须**把 .rar 那件事说清楚。"""
    ok, msg = env.install_optional("formats")
    print(("[√] " if ok else "[!] ") + msg)
    if not ok:
        return "文件格式增强包没装成（上面那段 pip 输出里是原因）。"
    _st, why = _formats_state()
    if _st:
        _set_opt("formats", True)
    return ("[√] 装好了。\n" if _st else "[!] ") + why


def _semantic_state():
    """本地语义检索现在能不能用——**判据走 `semantic.py --ready`**（纯探针，只回退出码）。

    另外把 `--status` 给人看的那几行也带出来（模型/索引分别什么状态），
    免得用户只看到一个 ❌ 不知道缺哪一步。
    """
    py = env.venv_python()
    if py is None:
        return False, "虚拟环境还没建好，语义检索用不了（先装依赖）。"
    try:
        r = subprocess.run([py, "semantic.py", "--ready"], cwd=BASE,
                           capture_output=True, timeout=60)
        ready = (r.returncode == 0)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"问不动语义检索那一侧（{type(e).__name__}: {e}）"
    if ready:
        return True, "模型 + 索引都就位（`semantic.enabled` 还要是 true 才会在聊天里用）。"
    # 不 ready：把状态里「模型 / 索引」那两行摘出来，用户才知道差哪一步
    lines = []
    try:
        r2 = subprocess.run([py, "semantic.py", "--status"], cwd=BASE,
                            capture_output=True, timeout=60, text=True,
                            encoding="utf-8", errors="replace")
        for ln in (r2.stdout or "").splitlines():
            if ln.startswith("本地模型：") or ln.startswith("  ") or ln.startswith("索引："):
                lines.append(ln.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    detail = "；".join(lines[:4]) or "模型/索引还不齐"
    return False, f"{detail}\n       装它：[{_opt_index('semantic')}]（会下模型并建索引）"


def _install_semantic():
    """装本地语义检索：pip → 下模型 → 建索引（**建索引前先停助手**）。"""
    ok, msg = env.install_optional("semantic")
    print(("[√] " if ok else "[!] ") + msg)
    if not ok:
        return "语义检索的**依赖**没装成（torch 那套很大，上面是原因），后面两步先不做。"

    print("[可选组件] 下模型（BAAI/bge-small-zh-v1.5，走 hf-mirror 镜像）…")
    run_venv("semantic.py", ["--setup"])

    print("[可选组件] 最后一步：建索引。它要遍历历史消息，**必须先停掉助手**")
    print("            （挂在轮询线程上跑会把 hook/微信拖住，项目为这类事崩过）。")
    if not _confirm("    现在「停助手 → 建索引 → 再把助手起回来」？(y/N) "):
        return ("模型下好了，**索引还没建**（没有索引 = 聊天里搜不了）。要建的话：\n"
                "  1) 助手.bat → [4] 停助手\n"
                "  2) .venv\\Scripts\\python.exe semantic.py --build --days 90\n"
                "  3) 再把助手启动起来")
    was_running = botctl.is_running()
    if was_running:
        ok_stop, m_stop = botctl.stop()
        print(("[√] " if ok_stop else "[!] ") + m_stop)
        if not ok_stop:
            return "停不掉助手，索引先不建（绝不在它跑着的时候建）。"
    run_venv("semantic.py", ["--build", "--days", "90"])
    if was_running:
        ok_start, m_start = botctl.start()
        print(("[√] " if ok_start else "[!] ") + m_start)
    ok2, why2 = _semantic_state()
    if ok2:
        _set_opt("semantic", True)
    out = ("[√] " if ok2 else "[!] ") + why2
    if ok2:
        # 装齐了就顺手打开开关（同 search 那条理由）：否则「装了却搜不了」。
        out += "\n" + _enable_runtime_switch("semantic", "本地语义检索")
    return out


def _opt_index(name):
    """这一项在菜单里是第几号（提示里要能照着按）。"""
    for i, (n, _l) in enumerate(OPTIONAL_ITEMS, start=1):
        if n == name:
            return i
    return 0


OPTIONAL_ACTIONS = {
    "voice": _install_voice,
    "search": act_search_install,
    "formats": _install_formats,
    "semantic": _install_semantic,
}
OPTIONAL_STATE = {
    "voice": _voice_state,
    "search": _search_state,
    "formats": _formats_state,
    "semantic": _semantic_state,
}


def optional_menu():
    """可选组件：装 / 看 / 决定「一键部署要不要自动装」。"""
    while True:
        # ⚠️ 开关那一项的按键**不能写死**：项数一多就会和某个组件撞号
        # （加到 4 项时 `[3]` 同时是「格式包」和「开关」，按 3 会去切开关、装不了）。
        # 所以它恒取「最后一个数字」= 项数 + 1。
        toggle_key = str(len(OPTIONAL_ITEMS) + 1)
        print()
        print("=" * 46)
        print("  可选组件（不随主程序装；不装也不影响聊天/发消息/读文件/定时）")
        print("=" * 46)
        for i, (name, label) in enumerate(OPTIONAL_ITEMS, start=1):
            ok, why = OPTIONAL_STATE[name]()
            mark = "✅" if ok else "❌"
            auto = "开" if _opt_wanted(name) else "关"
            heavy = "（**重**，一键部署默认不装）" if name in _HEAVY else ""
            print(f"   {mark} {label}{heavy}")
            print(f"       {why}")
            print(f"       一键部署时自动装：**{auto}**")
            print(f"       [{i}] 现在装 / 重装")
        print(f"   [{toggle_key}] 切换「一键部署时自动装」的开关")
        print("   [0] 返回")
        print("=" * 46)
        c = _clean(input("请输入数字选择："))
        if c == "0":
            return None
        if c == toggle_key:
            _submenu("自动安装开关（写 settings.json）", [
                (str(i), f"{name}：现在{'关掉' if _opt_wanted(name) else '打开'}",
                 (lambda n=name: _toggle_opt(n)))
                for i, (name, _label) in enumerate(OPTIONAL_ITEMS, start=1)
            ])
            continue
        hit = next((a for i, (name, _l) in enumerate(OPTIONAL_ITEMS, start=1)
                    if c == str(i) for a in [OPTIONAL_ACTIONS[name]]), None)
        if hit is None:
            print("无效选择。")
            continue
        msg = hit()
        if msg:
            print(msg)
        input("\n按回车继续 ... ")


def _toggle_opt(name):
    """切换一项的开关。**关掉不等于卸载**——已装的依赖不动，只是以后不再自动装。"""
    if _opt_wanted(name):
        _set_opt(name, False)
        return f"已关：以后「一键部署」不再自动装 {name}。已装的东西**不动**（要装回来再切一次）。"
    _set_opt(name, True)
    return f"已开：以后「一键部署」会自动装 {name}。"


def _install_optional_all():
    """一键部署的第 3 步：把**打开的**那几项一次装齐。返回要补的说明（没有就 None）。

    两条规矩：
      * 关掉的那几项**跳过并说明**（不许静默少装）——用户看到「跳过了」才知道怎么补；
      * `_HEAVY` 里那几项（语义检索）**默认不装**（回车=跳过，要手打 y）：它要下 torch、
        下模型、还得停一下助手，不该让人一路回车就装上。
    """
    skipped = []
    failed = []
    for name, label in OPTIONAL_ITEMS:
        if not _opt_wanted(name):
            skipped.append(label)
            continue
        print()
        print(f"--- {label} ---")
        print(OPTIONAL_STATE[name]()[1])
        if not _confirm("    现在装？" + ("(y/N) " if name in _HEAVY else "(Y/n) "),
                        default_no=(name in _HEAVY)):
            skipped.append(label)
            continue
        msg = OPTIONAL_ACTIONS[name]()
        print(msg)
        if not OPTIONAL_STATE[name]()[0]:
            failed.append(label)
    if not (skipped or failed):
        return None
    out = []
    if skipped:
        out.append("跳过了：" + "、".join(skipped) + "（之后想装：双击「可选组件.bat」）")
    if failed:
        out.append("这几项**没装成**：" + "、".join(failed) + "（原因看上面；不影响聊天等功能）")
    return "\n".join(out)


# ── 装 hook 之前的版本闸（微信版本 = 整件事的前置条件）──────────────────
# 为什么要有这一段（2026-10-04 真机）：另一台电脑上微信是 4.1.15.13，用户按 [9] 一键配置
# 走完一遍——version.dll 放进了微信目录、hook-install-log.txt 写着「已放置，SHA256 = …」，
# 看起来全部成功，可 30001 从来没有被监听，bot 就一直重试「连不上 30001」。
# 根因：hook 只支持 4.1.10.27；版本不对时 DLL 会被**正常加载**却挂钩失败，**不报错**。
# 所以「装 hook」前面必须先过这一关，而且要能顺手把版本换对（包里自带官方安装程序）。

def weixin_version_action(version):
    """纯函数：按检测到的微信版本决定「装 hook」之前该做什么。

      "ok"        —— 就是 WANTED_WEIXIN，直接装
      "downgrade" —— 明确是别的版本：必须先换成 WANTED_WEIXIN，否则装了也不生效
      "unknown"   —— 读不出版本（没装 / 拿不到文件版本资源）。**不许当成 mismatch**：
                     读不出不等于版本不对，一律拦住会挡住本来能装的机器，交给调用方如实问。
    """
    v = str(version or "").strip().split(" ")[0]
    if not v:
        return "unknown"
    return "ok" if v == WANTED_WEIXIN else "downgrade"


def parse_install_log(text):
    """纯函数：从 do_install.ps1 写的 install-log.txt 里读回 (exit code, 装完的版本)。

    为什么非读日志不可：do_install.ps1 是在**另一个提权窗口**里跑的，输出回不来、
    退出码也拿不到。「装上了」只能靠它自己写下的证据：exit code 是 0，**且**装完的
    ProductVersion 真的等于目标版本。读不到就返回 (None, None)，调用方必须如实说
    「没验成」——退出码 0 也可能是「跑完了但还是旧版本」，只看退出码就是假成功。
    """
    code, ver = None, None
    for line in str(text or "").splitlines():
        s = line.strip()
        m = re.search(r"exit code:\s*(-?\d+)", s)
        if m:
            code = int(m.group(1))
        m = re.search(r"Weixin\.exe ProductVersion\s*=\s*(\S+)", s)
        if m:
            ver = m.group(1)
    return code, ver


def detect_weixin():
    """探测本机微信版本，返回 `(version, install, err)`。

    ⚠️ `wechat_version` **延迟导入**：本文件顶层只许有标准库 + envsetup（见模块头注释）
    ——顶层一旦 import 崩了，「装依赖」这条自救路自己就先死了。
    探测失败也绝不抛：这里只是「先看一眼」，不该拦住控制台。
    """
    try:
        import wechat_version as wv
        info = wv.detect()
    except Exception as e:                  # noqa: BLE001 —— 探测失败一律降级成「不知道」
        return "", "", f"{type(e).__name__}: {e}"
    if not info:
        return "", "", ""
    return str(info.get("version") or ""), str(info.get("install") or ""), ""


def _installer_exe():
    return os.path.join(HOOK_DIR, "WeChatWin_" + WANTED_WEIXIN + ".exe")


def ensure_weixin_version(ask=True):
    """装 hook 前的版本闸。True = 版本对（或用户明确要继续），False = 别往下走。

    两级守卫里的**外面那一级**：拦在「装之前」，并且能顺手把版本换对。
    `do_hook_install.ps1` 里那道闸是**里面那一级**，管手敲命令 / 直接双击脚本的人。
    两级都要有——只留 .ps1 那道的话，用户在一个提权新窗口里看不到结果，还是会以为装完了。
    """
    ver, install, err = detect_weixin()
    action = weixin_version_action(ver)

    if action == "ok":
        print(f"    [√] 微信版本 {ver} —— 正是本 hook 唯一支持的版本。")
        return True

    if action == "unknown":
        print("    [!] 没检测到微信，或读不出微信版本号。")
        if err:
            print(f"        探测出错：{err}")
        if not install:
            print("        这台电脑看起来还没装微信电脑版。")
        print(f"        hook 是按微信 **{WANTED_WEIXIN}** 编译的；版本不对时它**不报错**，"
              "只是 30001 永远没人监听。")
        exe = _installer_exe()
        if os.path.isfile(exe):
            # 「没装微信」和「版本不对」要走的其实是同一条路：装包里那份 4.1.10.27。
            print("        包里有官方安装程序，可以顺手装上：")
            print("          " + exe)
            if ask and _confirm(f"        现在静默安装 {WANTED_WEIXIN} 吗？(Y/n) ",
                                default_no=False):
                return _install_weixin()
        else:
            print(f"        ⚠️ 包里没有安装程序（{exe}）——得先自己装好 {WANTED_WEIXIN}。")
        return _confirm("        跳过检查、仍然继续装 hook？(y/N) ")

    # action == "downgrade"
    print(f"    [!] 这台电脑的微信是 **{ver}**，而 hook 只支持 **{WANTED_WEIXIN}**。")
    print("        版本不对时 DLL 会被正常加载，但挂钩失败——**不报错、不崩**，")
    print("        只是 30001 永远没人监听（bot 就一直「连不上 30001」）。")
    print("        包里自带官方安装程序，可以就地换成 " + WANTED_WEIXIN + "：")
    print("          " + _installer_exe())
    print("        ⚠️ 它先结束微信进程，装完**要重新扫码登录**（会掉一次登录态）。")
    if not ask:
        return False
    if not _confirm(f"        现在静默安装 {WANTED_WEIXIN} 吗？(Y/n) ", default_no=False):
        print("        已跳过。版本没换之前，装 hook 这一步不会有意义。")
        return False
    return _install_weixin()


def _install_weixin():
    """静默装包里那份 4.1.10.27，并**复核**结果。True = 确实装上了。

    「版本不对」和「压根没装」走同一条路，所以两个分支共用它，别各写一份。
    """
    ok, msg = run_ps1("do_install.ps1")
    print(("[√] " if ok else "[!] ") + msg)
    if not ok:
        return False
    return _verify_downgrade(time.time())


def _wait_install_done(t0, timeout=300):
    """等 `do_install.ps1` 在新窗口里真的跑完；返回它的日志正文，超时返回 None。

    ⚠️ **必须等**：`run_ps1` 是「拉起一个新窗口就返回」的异步动作，而那次安装要几十秒
    （239MB 的安装包，本机实测约 1 分钟）。立刻去读日志，读到的必然是上一轮的旧日志或
    写了一半的日志 —— 于是「还在装」会被误报成「没换成」。判据用日志里的 `=== DONE ===`
    并且文件 mtime 要晚于我们发起的那一刻（否则读到的就是上一次运行留下的那份）。
    """
    p = os.path.join(HOOK_DIR, "install-log.txt")
    print("        安装中（几十秒到几分钟；它自己写完了才会往下走）…", end="", flush=True)
    t = time.time()
    last_dot = t
    while time.time() - t < timeout:
        try:
            if os.stat(p).st_mtime >= t0:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                if "=== DONE ===" in text:
                    print(" 完成")
                    return text
        except OSError:
            pass
        time.sleep(3)
        if time.time() - last_dot >= 15:
            print(".", end="", flush=True)
            last_dot = time.time()
    print("\n    [!] 等了 %d 秒还没看到安装完成（日志也没更新）。" % timeout)
    print("        可能：UAC 被点了「否」、安装器被安全软件拦住，或安装包放的位置不对。")
    return None


def _verify_downgrade(t0):
    """换完版本之后**复核**：读 do_install.ps1 留下的 install-log.txt。

    提权窗口里的输出回不来，「跑过了」不等于「装上了」——唯一的凭据是那份日志。
    验不过就如实说没换成，**绝不往后走**（否则又是一个「装了半天、端口不通」）。
    """
    text = _wait_install_done(t0)
    if text is None:
        return False
    p = os.path.join(HOOK_DIR, "install-log.txt")
    code, ver = parse_install_log(text)
    print(f"    安装日志：exit code = {code}，装完版本 = {ver or '（没读到）'}")
    if code == 0 and ver == WANTED_WEIXIN:
        print(f"    [√] 已经是 {WANTED_WEIXIN} 了。接下来装 hook，装完**打开微信扫码登录**。")
        return True
    print(f"    [X] **没换成 {WANTED_WEIXIN}**（日志：{p}）。")
    print("        常见原因：UAC 被点了「否」、被安全软件拦住，或安装程序弹了")
    print("        「你已安装新版本的微信，安装更早的版本？」而没人点「继续安装」。")
    print("        修好之后按一次 [9]，或自己双击那个 exe 选「继续安装」。")
    return False


def _hook_dll_state():
    """`(微信目录里那份 version.dll 的字节数, 人话)`；读不到返回 `(0, 为什么)`。

    ⚠️ 为什么需要这一句（2026-10-06 真机，用户卡了一整个下午）：**「包里那份是新的」和
    「微信里装的那份是新的」是两个文件。** 一键配置在版本闸没过时会**静默 return**，
    于是装 hook 一步没做、微信目录里躺的还是旧 DLL，而用户看到的是"打了一堆警告然后结束"，
    以为已经装完了。真实症状：助手每 10 秒刷「hook 已加载，但数据库打不开（微信没登录？）」
    —— 文案把人往「扫码登录」上带，真相却是 hook 从没换过。
    所以：只要跟 hook 安装有关，**就把微信目录里那份的字节数摆出来**（519168=旧 / 527360=新）。
    """
    try:
        wx = _find_weixin_dir()
    except Exception as e:                          # noqa: BLE001 —— 诊断不许反过来炸掉流程
        return 0, f"找不到微信目录（{type(e).__name__}: {e}）"
    if not wx:
        return 0, "找不到微信安装目录（微信没装？）"
    p = os.path.join(wx, "version.dll")
    if not os.path.isfile(p):
        return 0, f"{p} 不存在（hook 还没装过）"
    n = os.path.getsize(p)
    known = {519168: "**旧的**（要求核心库连续一直在写，安静时永不放行）",
             527360: "**新的**（读保存位置 + 25 秒窗口，零主动扫描）"}
    return n, f"{n} 字节 —— {known.get(n, '未知构建')}"


def _find_weixin_dir():
    """微信安装目录（有 Weixin.exe 的那个）。找不到返回空串。

    与 `installers/wechat-4.1.10.27/_common.ps1` 的 `Find-Weixin` 同源：
    HKCU/HKLM 的 `SOFTWARE\\Tencent\\Weixin` → `%ProgramFiles%\\Tencent\\Weixin`。
    """
    import winreg
    cands = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Tencent\Weixin") as k:
                for name in ("InstallPath", "InstallDir"):
                    try:
                        v, _ = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    if v:
                        cands.append(str(v).rstrip("\\/"))
        except OSError:
            continue
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_name)
        if root:
            cands.append(os.path.join(root, "Tencent", "Weixin"))
    for d in cands:
        if d and os.path.isfile(os.path.join(d, "Weixin.exe")):
            return d
    return ""


def _stop_after_version_gate():
    """版本闸没过 → **明说「后面几步都没做」**，并给出下一步命令。

    ⚠️ 这里的每一句都是 2026-10-06 真机换来的：旧实现只打一句「后面的步骤先不做了」就
    `return None`，用户以为一键配置跑完了，实际**连 hook 都没装**，微信目录里还是旧 DLL。
    所以必须说清三件事：① 停在第几步；② **哪些步骤没做**（尤其装 hook）；
    ③ 现在微信目录里那份 hook 是什么（字节数能一眼分辨新旧）。
    """
    print()
    print("=" * 46)
    print("  ⛔ 一键配置**没做完**：停在第 0 步（微信版本），后面的步骤**一步都没执行**")
    print("=" * 46)
    print("  没做的：1) 装 hook   2) 装依赖   3) 可选组件   4) 启动 + 配模型")
    size, note = _hook_dll_state()
    print(f"  现在微信目录里的 hook：{note}")
    if size and size != 527360:
        print("  ⚠️ 注意：**包里那份是新的、微信里装的那份是旧的** —— 解压新包不会替换它。")
    print()
    print("  下一步（三条路，任选一条）：")
    print(f"    ① 把微信版本换成 {WANTED_WEIXIN}，然后再按一次 [9]；")
    print(f"    ② 只是想先装 hook：菜单 [8] → [7] → [1]（会弹 UAC）；")
    print("    ③ 手工核对：")
    print('         (Get-Item "C:\\Program Files\\Tencent\\Weixin\\version.dll").Length')


def _report_hook_install():
    """装 hook 之后**把结果读回来**：微信目录里那份是什么 + 脚本日志的尾巴。

    ⚠️ 为什么必须有这一句（2026-10-06 真机）：`do_hook_install.ps1` 是在**提权新窗口**里
    跑的，那个窗口一关输出就没了；用户看到的是"一闪而过"，于是**根本不知道装没装上**
    （真机那次脚本其实压根没跑：`hook-install-log.txt` 都不存在）。所以控制台这边必须
    主动去读它的落盘证据，并把微信目录里那份 DLL 的**字节数**摆出来。
    """
    size, note = _hook_dll_state()
    print(f"    [i] 微信目录里的 hook：{note}")
    logp = os.path.join(HOOK_DIR, "hook-install-log.txt")
    if not os.path.isfile(logp):
        print("    [!] 那个脚本的日志**不存在** ⇒ 它这次**没有真的跑起来**")
        print("        （日志是它第一件事就写的；没有 = 没运行。常见原因：UAC 被点了「否」。）")
        return
    try:
        with open(logp, "r", encoding="utf-8", errors="replace") as fh:
            tail = [ln.rstrip() for ln in fh.read(4000).splitlines() if ln.strip()][-6:]
    except OSError as e:
        print(f"    [!] 日志读不出来：{e}")
        return
    print(f"    [i] 它的日志（{logp}）最后几行：")
    for ln in tail:
        print(f"        {ln}")
    print("    → 装完**重启微信**（version.dll 只在微信启动时加载），再确认通了：浏览器打")
    print("      http://127.0.0.1:30001/QueryDB/status   （返回 JSON 就成）")


def first_run():
    """**第一次装**：把「别人想用的话该点哪儿」变成一次点击。

    ⚠️ 为什么要有这个入口：装 hook 是**整件事的第一步**（不做后面全白搭），
    可它原来藏在 `[8] 更多… → [7] Hook → [1] 装 hook` 里——
    第一次拿到这个包的人根本不会翻到那儿。用户问「别人想用的话点哪个呢」才暴露出来。
    所以把它摆到顶层，按真实顺序走一遍。

    ⚠️ 第 0 步（2026-10-04 加）**不是可有可无的**：钩子是**版本锁死**的，微信不是
   4.1.10.27 时装 hook 会"成功"但永不生效（不报错、端口不通）。排在装 hook 之前，
   是因为版本不对时后面每一步都是白做。
    """
    print()
    print("=" * 46)
    print("  一键配置（查微信版本 → 装 hook → 装依赖 → 可选组件 → 启动 → 配模型；一路回车即可）")
    print("=" * 46)
    print("  0) 查微信版本（不对就用包里自带的那份换成 " + WANTED_WEIXIN + "）")
    print("  1) 把 hook 装进微信（要管理员，会弹 UAC）")
    print("  2) 装 Python 依赖")
    print("  3) 可选组件（语音转文字 / 网上搜索；不装也不影响其它功能）")
    print("  4) 启动助手，然后在微信里配 API Key")
    print()
    print("⚠️ 前提：这台电脑要装了 **64 位 Python**（3.11 推荐）。")
    print("   没有的话先去 python.org 装（勾上 Add to PATH），或：")
    print("     winget install -e --id Python.Python.3.11")
    print()

    # ── 0 · 微信版本 ──
    # 为什么放在最前面：hook 只支持 4.1.10.27，版本不对时**装 hook 会"成功"但永不生效**
    # （DLL 被正常加载、挂钩失败、30001 一直没人监听），用户只看到 bot 反复「连不上 30001」。
    # 2026-10-04 真机：另一台电脑微信是 4.1.15.13，[9] 走完一遍日志全绿、端口从没通。
    print("--- 第 0 步：微信版本 ---")
    if not ensure_weixin_version():
        _stop_after_version_gate()
        return None
    print()

    # ── 1 · 装 hook ──
    ps1 = os.path.join(HOOK_DIR, "do_hook_install.ps1")
    print("--- 第 1 步：装 hook ---")
    if not os.path.isfile(ps1):
        print(f"[!] 包里没有装 hook 的脚本（{ps1}）——包可能不完整。")
    else:
        print("    它会把 version.dll 放进微信目录，并挡住微信自动更新把版本顶掉。")
        print("    要求微信版本是 **" + WANTED_WEIXIN + "**（上面那步已经确认过了）。")
        if _confirm("    现在装？(Y/n) ", default_no=False):
            ok, msg = run_ps1("do_hook_install.ps1")
            print(("[√] " if ok else "[!] ") + msg)
            _report_hook_install()
        else:
            print("    已跳过。以后想装：菜单 [8] → [7] → [1]。")

    # ── 2 · 装依赖 ──
    print()
    print("--- 第 2 步：装 Python 依赖 ---")
    if env.venv_ready():
        print("    虚拟环境已经就绪，跳过。")
    elif _confirm("    现在装？（要联网下载，第一次可能几分钟）(Y/n) ", default_no=False):
        run("installer.py")
    else:
        print("    已跳过。以后想装：菜单 [2]。")

    # ── 3 · 可选组件 ──
    print()
    print("--- 第 3 步：可选组件（语音转文字 / 网上搜索）---")
    print("    这两样**不随主程序装**：语音要下模型（几百 MB），搜索要它自己一份 venv。")
    print("    现在装齐，之后就不用管了；跳过也不影响聊天、发消息、读文件、定时。")
    note = _install_optional_all()
    if note:
        print()
        print(note)

    # ── 4 · 启动 + 配模型 ──
    print()
    print("--- 第 4 步：启动 + 配置模型 ---")
    if _confirm("    现在启动助手（后台）？(Y/n) ", default_no=False):
        ok, msg = botctl.start()
        print(("[√] " if ok else "[!] ") + msg)
    else:
        print("    已跳过。以后想启动：菜单 [3]。")

    print()
    print("    接下来配「用哪个模型 + API Key」——**全程在这里，不用去微信里打字**。")
    print("    没有 key 就去对应平台领一个：DeepSeek https://platform.deepseek.com，")
    print("    智谱 GLM https://open.bigmodel.cn（其中 glm-4.7-flash 官方免费）。")
    if _confirm("    现在配？(Y/n) ", default_no=False):
        run("setup_llm.py")
        print("    → 配完直接去微信「文件传输助手」发一句话就能用了。")
    else:
        print("    已跳过。以后想配：菜单 [8] → [1]，或双击「配置模型.bat」。")
        print("    （也可以启动后在微信里发 /provider 1，再发 /api <key>。）")
    return None


def menu():
    while True:
        print()
        print("=" * 46)
        print("           微信 AI 助手 · 控制台")
        print("=" * 46)
        print("  第一次用？直接按 [9]「一键配置」，然后**连按回车**走完")
        print("-" * 46)
        print("   [1] 降级微信 4.x -> 3.9.x")
        print("   [2] 安装依赖（自动识别版本）")
        print("   [3] 启动助手（后台，无窗口）")
        print("   [4] 停止 / 重启助手")
        print("   [5] 看状态（进程 + 健康快照）")
        print("   [6] 看日志")
        print("   [7] 一键开始（检测 -> 装依赖 -> 启动）")
        print("   [8] 更多…（配模型 / 真机自检 / 跑自测 / hook / 自启 / 状态页）")
        print("   [9] 一键配置（装 hook + 装依赖 + 可选组件 + 启动 + 配模型）")
        print("   [0] 退出")
        print("=" * 46)

        c = _clean(input("请输入数字选择："))

        if c == "1":
            act_downgrade()
        elif c == "2":
            run("installer.py")
        elif c == "3":
            print(act_start())
        elif c == "4":
            _submenu("停止 / 重启助手", [
                ("1", "停止助手", act_stop),
                ("2", "重启助手", act_restart),
            ])
        elif c == "5":
            health_screen()
        elif c == "6":
            _submenu("看日志", [
                ("1", "最近 40 行", act_log_tail),
                ("2", "实时跟随（Ctrl+C 返回）", lambda: botctl.follow()),
            ])
        elif c == "7":
            auto()
        elif c == "8":
            _submenu("更多", [
                ("1", "配置模型（选服务商 + 填 key）", lambda: run("setup_llm.py")),
                ("2", "真机自检（只读；需先停 bot，会问你）", _verify_real_flow),
                ("3", "跑全部自测（不用真微信）", lambda: run("selftest_all.py")),
                ("4", "启动助手（前台，看日志）", act_foreground),
                ("5", "查看微信版本", lambda: run("wechat_version.py")),
                ("6", "开机自启（开 / 关 / 看）", lambda: _submenu("开机自启", [
                    ("1", "开启", lambda: run("autostart.py", ["on"])),
                    ("2", "关闭", lambda: run("autostart.py", ["off"])),
                    ("3", "查看状态", lambda: run("autostart.py", ["status"])),
                ])),
                ("7", "Hook（装 / 摘 / 装回 / 修）", lambda: _submenu("Hook 与微信", [
                    ("1", "装 hook（放 version.dll + 禁用微信自动更新）",
                     lambda: act_hook("do_hook_install.ps1", "装 hook")),
                    ("2", "摘 hook（改名 .disabled，会强杀卡死的微信）",
                     lambda: act_hook("do_remove_hook.ps1", "摘 hook")),
                    ("3", "装回 hook（并重启微信）",
                     lambda: act_hook("do_restore_hook.ps1", "装回 hook")),
                    ("4", "★ 只替换 version.dll（微信里那份是旧的时用这个）",
                     act_fix_hook),
                ])),
                ("8", "打开状态页（本地只读网页）", act_status_page),
                ("9", "搜索服务（网上搜索后端 启 / 停 / 看 / 装）", lambda: _submenu(
                    "搜索服务（SearXNG，网上搜索的后端）", [
                        ("1", "启动搜索服务", act_search_start),
                        ("2", "停止搜索服务", act_search_stop),
                        ("3", "看状态（进程 / 能不能查 / 开关 / 自启）", act_search_status),
                        ("4", "装 / 修依赖（在它自己的目录里建 venv）", act_search_install),
                    ])),
            ])
        elif c == "9":
            first_run()
        elif c == "0":
            print("再见！")
            break
        else:
            print("无效选择，请输入 0~9。")

        input("\n按回车返回菜单 ... ")


def main():
    """入口。带 `first` 参数就直接进「一键部署」，走完就结束（便于脚本化 / 自动化）。

    两个入口，各有明确分工：
      * `助手.bat` —— 日常菜单（状态 / 日志 / 起停 / 真机自检 / 全部自测）；
      * `一键部署.bat` —— **给别的电脑装**：它只调 `python console.py first`，
        流程全在本文件的 `first_run()` 里，自己不实现任何东西。

    ⚠️ 别把它变成第二个「实现」：多一个入口的成本不在那一行调用，而在第二份逻辑
    会跟第一份分叉（这个项目被「两份实现只改了一份」咬过好几次：`send_asset` 的指导、
    `_wechat_save_roots` 与 C++ 侧判据都是）。也**别**把 `first` 改成还会进菜单——
    部署完就该结束，菜单是 `助手.bat` 的事。

    历史（别再来回改）：2026-10-02 撤掉过一个「一键配置.bat」，因为它当时只是
    `助手.bat` 的重复入口（用户口径：「在助手.bat里面有个选项就行」）；2026-10-04
    重新加回来是为「把包给别人的电脑，双击一个文件就装完」——那是独立需求，
    所以名字叫「部署」而不是「配置」，而且它必须保持是个**薄壳**。
    """
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    _eok, _emsg, _elaunched = admin.ensure_elevated(capture=True)
    if not _eok:
        print(f"\n[admin] ❌ {_emsg}")
        input("\n按回车退出 ... ")
        return
    if _elaunched:
        # 刚在另一个提权窗口里把控制台拉起来了 → 这一份退出（别开两个菜单）
        return
    try:
        if arg in ("first", "--first-run", "setup", "一键配置", "一键部署"):
            first_run()
            return                      # 部署完就结束；菜单是 助手.bat 的事
        if arg in ("optional", "--optional", "可选组件"):
            optional_menu()             # 「可选组件.bat」只调它，自己不实现任何东西
            return
        menu()
    except KeyboardInterrupt:
        print("\n已退出。")
    except EOFError:
        print("\n输入结束，已退出。")


if __name__ == "__main__":
    main()
