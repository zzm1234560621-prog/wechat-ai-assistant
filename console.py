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


def _confirm(prompt):
    return _clean(input(prompt)).lower() in ("y", "yes", "是")


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


def menu():
    while True:
        print()
        print("=" * 46)
        print("           微信 AI 助手 · 控制台")
        print("=" * 46)
        print("  安装 / 首次配置")
        print("   [1] 降级微信 4.x -> 3.9.x")
        print("   [2] 安装依赖（自动识别版本）")
        print("   [3] 配置模型（选服务商 + 填 key）")
        print("   [4] 自动（一键：检测→装依赖→启动）")
        print("  运行控制")
        print("   [5] 启动助手（后台，无窗口）")
        print("   [6] 停止助手")
        print("   [7] 重启助手")
        print("   [8] 启动助手（前台，看日志）")
        print("   [9] 实时看日志（Ctrl+C 返回）")
        print("  诊断 / 排查")
        print("  [10] 看状态（进程 + 健康快照）")
        print("  [11] 看最近日志（40 行）")
        print("  [12] 真机自检（只读；**需先停 bot**，会问你）")
        print("  [13] 跑全部自测（24 份，不用真微信）")
        print("  Hook / 微信")
        print("  [14] 装 hook（放 version.dll + 禁用微信自动更新）")
        print("  [15] 摘 hook（改名 .disabled，会强杀卡死的微信）")
        print("  [16] 装回 hook（并重启微信）")
        print("  [17] 查看微信版本 / 开机自启状态")
        print("  其它")
        print("  [18] 开机自启：开启")
        print("  [19] 开机自启：关闭")
        print("  [20] 打开状态页（本地只读网页）")
        print("   [0] 退出")
        print("=" * 46)

        c = _clean(input("请输入数字选择："))

        if c == "1":
            run("downgrade.py", admin=True)
            print("已在独立窗口启动降级程序（需管理员权限），请在那边操作。")
        elif c == "2":
            run("installer.py")
        elif c == "3":
            run("setup_llm.py")
        elif c == "4":
            auto()
        elif c == "5":
            ok, msg = botctl.start()
            print(("[√] " if ok else "[!] ") + msg)
        elif c == "6":
            print(botctl.stop(dry_run=True)[1])
            if _confirm("确认停止？(y/N) "):
                ok, msg = botctl.stop()
                print(("[√] " if ok else "[!] ") + msg)
            else:
                print("已取消。")
        elif c == "7":
            if _confirm("确认重启助手？(y/N) "):
                ok, msg = botctl.restart()
                print(("[√] " if ok else "[!] ") + msg)
            else:
                print("已取消。")
        elif c == "8":
            if not env.venv_ready():
                print("虚拟环境未就绪（不存在、已失效或依赖缺失），请先选 [2] 安装依赖。")
            else:
                subprocess.run([env.VENV_PY, "bot.py"], cwd=BASE)
        elif c == "9":
            botctl.follow()
        elif c == "10":
            health_screen()
        elif c == "11":
            print()
            print(botctl.tail(40))
        elif c == "12":
            _verify_real_flow()
        elif c == "13":
            run("selftest_all.py")
        elif c in ("14", "15", "16"):
            which = {"14": ("do_hook_install.ps1", "装 hook"),
                     "15": ("do_remove_hook.ps1", "摘 hook（会强杀微信）"),
                     "16": ("do_restore_hook.ps1", "装回 hook（会重启微信）")}[c]
            if _confirm(f"确认「{which[1]}」？可能要管理员权限。(y/N) "):
                ok, msg = run_ps1(which[0])
                print(("[√] " if ok else "[!] ") + msg)
            else:
                print("已取消。")
        elif c == "17":
            print("\n--- 微信版本 ---")
            run("wechat_version.py")
            print("--- 开机自启 ---")
            run("autostart.py", ["status"])
        elif c == "18":
            run("autostart.py", ["on"])
        elif c == "19":
            run("autostart.py", ["off"])
        elif c == "20":
            enabled, host, port, err = _status_page()
            if err:
                print(f"读不到 config.yaml 的 status 段：{err}")
            elif not enabled:
                print("状态页是**关着的**（config.yaml 里 `status.enabled: false`）。")
                print("要开：把那一项改成 true，然后 [7] 重启助手。")
                print("（它只绑回环地址，页面上有 wxid/群名，别往局域网上开。）")
            elif not botctl.is_running():
                print(f"配置里是开着的（{host}:{port}），但**助手没在跑**，页面不会有人响应。")
                print("先 [5] 启动助手。")
            else:
                url = f"http://{host}:{port}"
                print(f"打开 {url} …")
                try:
                    os.startfile(url)          # 只有 Windows 有；这是 Windows 项目
                except (AttributeError, OSError) as e:
                    print(f"打不开浏览器（{e}），自己访问：{url}")
        elif c == "0":
            print("再见！")
            break
        else:
            print("无效选择，请输入 0~20。")

        input("\n按回车返回菜单 ...")


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\n已退出。")
    except EOFError:
        print("\n输入结束，已退出。")
