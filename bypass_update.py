"""绕过微信「版本过低，请升级」强制更新提示。

原理：在当前用户注册表里给 WeChat.exe / Weixin.exe 加兼容层 ~ ARM64WOWONAMD64，
让微信跳过版本检查。

⚠️ 只写 HKEY_CURRENT_USER（当前用户），**不动 HKEY_LOCAL_MACHINE**：
全局键会影响这台机器上所有用户，风险太大；当前用户这一层对我们的场景已经够用。

用法：
  python bypass_update.py on      开启绕过
  python bypass_update.py off     关闭绕过
  python bypass_update.py status  查看状态

退出码：0 = 操作成功（status 只表示「查得动」）；
        1 = 没找到微信程序 / 操作失败（**不再是静默的 0**）。
"""
import os
import sys
import winreg

COMPAT_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
COMPAT_VALUE = "~ ARM64WOWONAMD64"

# 常见安装路径（按 3.9.x 的 WeChat.exe 和 4.x 的 Weixin.exe 两种命名）
COMMON_PATHS = [
    # 3.9.x
    r"C:\Program Files\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files\WeChat\WeChat.exe",
    # 4.x：安装目录随版本变过，三种都列上
    r"C:\Program Files\Tencent\xwechat\Weixin.exe",
    r"C:\Program Files (x86)\Tencent\xwechat\Weixin.exe",
    r"C:\Program Files\Tencent\Weixin\Weixin.exe",
    r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
]


def candidate_paths(paths=None, include_env=True):
    """COMMON_PATHS 加上环境变量里能问出来的位置（去重，保持顺序）。

    paths / include_env 只为自测注入用；正常调用不传，就用内置表 + 环境变量。
    """
    out = list(COMMON_PATHS if paths is None else paths)
    if not include_env:
        return out
    for env, sub in (("ProgramFiles", r"Tencent\xwechat\Weixin.exe"),
                     ("ProgramFiles(x86)", r"Tencent\xwechat\Weixin.exe"),
                     ("ProgramFiles", r"Tencent\Weixin\Weixin.exe"),
                     ("ProgramFiles(x86)", r"Tencent\Weixin\Weixin.exe"),
                     ("ProgramFiles", r"Tencent\WeChat\WeChat.exe"),
                     ("ProgramFiles(x86)", r"Tencent\WeChat\WeChat.exe")):
        root = os.environ.get(env)
        if root:
            p = os.path.join(root, sub)
            if p not in out:
                out.append(p)
    return out


def find_wechat(paths=None, include_env=True):
    """返回找到的微信主程序路径；没找到返回 None（本函数不打印、不退出）。

    注意看的是**文件在不在**，不看 PATH：微信不会把自己加进 PATH。
    """
    for p in candidate_paths(paths, include_env):
        if os.path.exists(p):
            return p
    return None


def _not_found_hint():
    print("[!] 未找到微信主程序（WeChat.exe / Weixin.exe）。")
    print("    已找过这些位置：")
    for p in candidate_paths():
        print(f"      - {p}")
    print("    微信装在别处的话，把安装目录里的 WeChat.exe / Weixin.exe 路径告诉我，"
          "或者自己加兼容层：")
    print(f"      注册表 HKCU\\{COMPAT_KEY}")
    print(f"      新建字符串值 = 微信 exe 全路径，数据 = {COMPAT_VALUE}")


def on(paths=None, include_env=True):
    path = find_wechat(paths, include_env)
    if not path:
        _not_found_hint()
        return False
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, COMPAT_KEY, 0, winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, path, 0, winreg.REG_SZ, COMPAT_VALUE)
    print("[√] 已开启绕过强制更新（只对当前用户生效）。")
    print(f"    微信路径：{path}")
    print("    请【完全退出微信】（含右下角托盘图标）后重新启动微信才会生效。")
    print("    关闭绕过：python bypass_update.py off")
    return True


def off(paths=None, include_env=True):
    path = find_wechat(paths, include_env)
    if not path:
        _not_found_hint()
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, COMPAT_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, path)
        print("[√] 已关闭绕过（当前用户）。")
    except FileNotFoundError:
        print("当前用户下没有为这个路径开启绕过，无需关闭。")
    return True


def status(paths=None, include_env=True):
    path = find_wechat(paths, include_env)
    if not path:
        _not_found_hint()
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, COMPAT_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, path)
            print("绕过状态：已开启（当前用户）")
            print(f"  {path} -> {val}")
    except FileNotFoundError:
        print("绕过状态：未开启")
    return True


def main():
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if arg == "on":
        ok = on()
    elif arg == "off":
        ok = off()
    elif arg == "status":
        ok = status()
    else:
        print(f"[!] 未知参数：{arg}")
        print("    用法：python bypass_update.py on|off|status")
        return 1
    if not ok:
        print("[!] 操作未完成（退出码 1）。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
