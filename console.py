"""微信 AI 助手 - 统一控制台（单一入口，控制所有功能）。

双击 助手.bat 就会进入本菜单。
  一键走完：选 [7] 自动；手动分步：1 降级 -> 2 安装 -> 3 启动
"""
import subprocess
import sys

import envsetup as env

BASE = env.BASE


def run(script, args=None, admin=False):
    cmd = [script] + (args or [])
    if admin:
        # 用系统 python 在新管理员窗口里跑（降级需要提权）
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             f"Start-Process -FilePath '{sys.executable}' "
             f"-ArgumentList '{' '.join(cmd)}' "
             f"-WorkingDirectory '{BASE}' -Verb RunAs"])
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
        print("\n[自动] 当前版本不能用，必须先降级到 3.9.x。")
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
            print("无效选择，请输入 0~7。")

        input("\n按回车返回菜单 ...")


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        print("\n已退出。")
    except EOFError:
        print("\n输入结束，已退出。")
