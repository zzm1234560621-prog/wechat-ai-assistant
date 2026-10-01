"""绕过微信「版本过低，请升级」强制更新提示。

原理：在注册表给 WeChat.exe 加兼容层 ~ ARM64WOWONAMD64，让微信跳过版本检查。
用法：
  python bypass_update.py on      开启绕过
  python bypass_update.py off     关闭绕过
  python bypass_update.py status  查看状态
"""
import os
import sys
import winreg

COMPAT_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
COMPAT_VALUE = "~ ARM64WOWONAMD64"

COMMON_PATHS = [
    r"C:\Program Files\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files\Tencent\Weixin\Weixin.exe",
]


def find_wechat():
    for p in COMMON_PATHS:
        if os.path.exists(p):
            return p
    return None


def on():
    path = find_wechat()
    if not path:
        print("[!] 未找到 WeChat.exe，请确认微信安装路径。")
        return
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, COMPAT_KEY, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, path, 0, winreg.REG_SZ, COMPAT_VALUE)
    print("[√] 已开启绕过强制更新。")
    print(f"    微信路径：{path}")
    print("    请【完全退出微信】（含右下角托盘图标）后重新启动微信才会生效。")
    print("    关闭绕过：python bypass_update.py off")


def off():
    path = find_wechat()
    if not path:
        print("[!] 未找到 WeChat.exe。")
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, COMPAT_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, path)
        print("[√] 已关闭绕过。")
    except FileNotFoundError:
        print("当前没有开启绕过。")


def status():
    path = find_wechat()
    if not path:
        print("未找到 WeChat.exe。")
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, COMPAT_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, path)
            print("绕过状态：已开启")
            print(f"  {path} -> {val}")
    except FileNotFoundError:
        print("绕过状态：未开启")


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if arg == "on":
        on()
    elif arg == "off":
        off()
    else:
        status()


if __name__ == "__main__":
    main()
