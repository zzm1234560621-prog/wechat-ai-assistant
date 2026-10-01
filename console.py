"""微信 AI 助手 - 统一控制台（单一入口，控制所有功能）。

双击 助手.bat 就会进入本菜单。
  一键走完：选 [7] 自动；手动分步：1 降级 -> 2 安装 -> 3 启动
"""
import subprocess
import sys

import envsetup as env

BASE = env.BASE


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


def menu():
    while True:
        print()
        print("=" * 38)
        print("        微信 AI 助手 · 控制台")
        print("=" * 38)
        print("  一键走完选 [7]；手动分步 1 降级→2 安装→3 启动")
        print("-" * 38)
        print("  [1] 降级微信 4.x -> 3.9.x")
        print("  [2] 安装依赖（自动识别版本）")
        print("  [3] 启动助手（前台，看日志）")
        print("  [4] 开机自启：开启")
        print("  [5] 开机自启：关闭")
        print("  [6] 查看状态（微信版本 / 自启）")
        print("  [7] 自动（一键：检测→装依赖→启动）")
        print("  [8] 配置模型（选服务商 + 填 key）")
        print("  [0] 退出")
        print("=" * 38)

        c = input("请输入数字选择：").strip()

        if c == "1":
            run("downgrade.py", admin=True)
            print("已在独立窗口启动降级程序（需管理员权限），请在那边操作。")
        elif c == "2":
            run("installer.py")
        elif c == "3":
            if not env.venv_ready():
                print("虚拟环境未就绪（不存在、已失效或依赖缺失），请先选 [2] 安装依赖。")
            else:
                subprocess.run([env.VENV_PY, "bot.py"], cwd=BASE)
        elif c == "4":
            run("autostart.py", ["on"])
        elif c == "5":
            run("autostart.py", ["off"])
        elif c == "6":
            print("\n--- 微信版本 ---")
            run("wechat_version.py")
            print("--- 开机自启 ---")
            run("autostart.py", ["status"])
        elif c == "7":
            auto()
        elif c == "8":
            run("setup_llm.py")
        elif c == "0":
            print("再见！")
            break
        else:
            print("无效选择，请输入 0~8。")

        input("\n按回车返回菜单 ...")


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\n已退出。")
    except EOFError:
        print("\n输入结束，已退出。")
