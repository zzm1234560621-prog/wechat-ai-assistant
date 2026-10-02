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
import subprocess
import sys

import botctl
import envsetup as env

BASE = env.BASE

# hook 相关脚本（都在 installers\wechat-4.1.10.27\ 下，**都要管理员**）
HOOK_DIR = os.path.join(BASE, "installers", "wechat-4.1.10.27")


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
    print("[自动] 前台启动（Ctrl+C 停止）...")
    subprocess.run([env.VENV_PY, "bot.py"], cwd=BASE)
    return None


def act_log_tail():
    print()
    print(botctl.tail(40))
    return None


def act_hook(script, what):
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


def first_run():
    """**第一次装**：把「别人想用的话该点哪儿」变成一次点击。

    ⚠️ 为什么要有这个入口：装 hook 是**整件事的第一步**（不做后面全白搭），
    可它原来藏在 `[8] 更多… → [7] Hook → [1] 装 hook` 里——
    第一次拿到这个包的人根本不会翻到那儿。用户问「别人想用的话点哪个呢」才暴露出来。
    所以把它摆到顶层，按真实顺序走一遍。
    """
    print()
    print("=" * 46)
    print("  一键配置（装 hook → 装依赖 → 启动 → 配模型；一路回车即可）")
    print("=" * 46)
    print("  1) 把 hook 装进微信（要管理员，会弹 UAC）")
    print("  2) 装 Python 依赖")
    print("  3) 启动助手，然后在微信里配 API Key")
    print()
    print("⚠️ 前提：这台电脑要装了 **64 位 Python**（3.11 推荐）。")
    print("   没有的话先去 python.org 装（勾上 Add to PATH），或：")
    print("     winget install -e --id Python.Python.3.11")
    print()

    # ── 1 · 装 hook ──
    ps1 = os.path.join(HOOK_DIR, "do_hook_install.ps1")
    print("--- 第 1 步：装 hook ---")
    if not os.path.isfile(ps1):
        print(f"[!] 包里没有装 hook 的脚本（{ps1}）——包可能不完整。")
    else:
        print("    它会把 version.dll 放进微信目录，并挡住微信自动更新把版本顶掉。")
        print("    要求微信版本是 **4.1.10.27**（微信里「设置 → 关于微信」看一眼）。")
        if _confirm("    现在装？(Y/n) ", default_no=False):
            ok, msg = run_ps1("do_hook_install.ps1")
            print(("[√] " if ok else "[!] ") + msg)
            print("    → 装完**重启微信**，再确认通了：浏览器打")
            print("      http://127.0.0.1:30001/QueryDB/status   （返回 JSON 就成）")
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

    # ── 3 · 启动 + 配模型 ──
    print()
    print("--- 第 3 步：启动 + 配置模型 ---")
    if _confirm("    现在启动助手（后台）？(Y/n) ", default_no=False):
        ok, msg = botctl.start()
        print(("[√] " if ok else "[!] ") + msg)
    else:
        print("    已跳过。以后想启动：菜单 [3]。")

    print()
    print("    接下来配「用哪个模型 + API Key」——**全程在这里，不用去微信里打字**。")
    print("    没有 key 就去 https://platform.deepseek.com 领一个（有免费额度）。")
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
        print("   [9] 一键配置（装 hook + 装依赖 + 启动 + 配模型）")
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
                ("7", "Hook（装 / 摘 / 装回）", lambda: _submenu("Hook 与微信", [
                    ("1", "装 hook（放 version.dll + 禁用微信自动更新）",
                     lambda: act_hook("do_hook_install.ps1", "装 hook")),
                    ("2", "摘 hook（改名 .disabled，会强杀卡死的微信）",
                     lambda: act_hook("do_remove_hook.ps1", "摘 hook")),
                    ("3", "装回 hook（并重启微信）",
                     lambda: act_hook("do_restore_hook.ps1", "装回 hook")),
                ])),
                ("8", "打开状态页（本地只读网页）", act_status_page),
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
    """入口。带 `first` 参数就直接进「一键配置」，不用先看菜单（便于脚本化 / 自动化）。

    日常口径只有一个：**双击 `助手.bat` 看菜单**，第一次用按 `[9]`。
    （我一度另外扔了一个根目录的 `一键配置.bat`，用户指出「在助手.bat里面有个选项就行」——
    所以撤掉了：**多一个入口就是多一处要维护、也多一个「到底点哪个」的疑问**。）
    """
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    try:
        if arg in ("first", "--first-run", "setup", "一键配置"):
            first_run()
        menu()
    except KeyboardInterrupt:
        print("\n已退出。")
    except EOFError:
        print("\n输入结束，已退出。")


if __name__ == "__main__":
    main()
