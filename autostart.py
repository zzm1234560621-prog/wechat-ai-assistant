"""设置/取消 开机自启动：让 bot 在后台一直跑，你在微信文件传输助手里直接对话。

用法：
  python autostart.py on       # 开启开机自启（隐藏运行，无窗口）
  python autostart.py off      # 取消自启
  python autostart.py status   # 查看状态

原理：写 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，
开机后用 pythonw（无窗口）启动 bot.py；bot.py 内部用绝对路径，不依赖工作目录。
"""
import os
import sys
import winreg

import envsetup as env

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "WeChatAIAssistant"


def _pyw():
    return env.VENV_PYW if os.path.exists(env.VENV_PYW) else env.VENV_PY


def enable():
    if not env.venv_ready():
        print("[!] 虚拟环境未就绪或依赖缺失，请先双击 install.bat 完成安装。")
        print("    （如果刚移动过文件夹，重跑 install.bat 会自动修复。）")
        return
    py = _pyw()
    cmd = f'"{py}" "{os.path.join(env.BASE, "bot.py")}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, cmd)
    print("[√] 已开启开机自启。")
    print(f"    命令：{cmd}")
    print("    开机后自动在后台连接微信（会等待微信启动）；")
    print("    之后你只需在微信文件传输助手里对话。日志见 bot.log。")
    print("    关闭自启：python autostart.py off")
    # ⚠️ 提权这事必须在这里说清（2026-10-06 用户定的硬约束）：自启命令是**普通权限**
    # 拉起的，而开机那一刻**没人点 UAC**，所以 bot.py 里那条提权（assume=True）
    # **只告警不弹窗**。结果是：语音条（要读微信进程内存）**开机自启后可能用不了**。
    print("    ⚠️ 权限：开机自启是普通权限拉起来的，开机时没人点 UAC。")
    print("        要让自启也带管理员权限，正道是**计划任务（RunLevel=Highest）**，")
    print("        不是 RunAs —— 后者在开机那一刻没人点，等于起不来。")


def disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, NAME)
        print("[√] 已取消开机自启。")
    except FileNotFoundError:
        print("当前没有开启自启。")


def status():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, NAME)
            print("自启状态：已开启")
            print(f"  {val}")
    except FileNotFoundError:
        print("自启状态：未开启")


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if arg == "on":
        enable()
    elif arg == "off":
        disable()
    else:
        status()


if __name__ == "__main__":
    main()
