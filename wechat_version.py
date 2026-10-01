"""自动识别已安装的微信 PC 版本，并匹配对应的 wcferry 版本。

识别来源：
  1. 注册表卸载信息（同时读 64 位和 32 位视图）
  2. 兜底：WeChat.exe / Weixin.exe 的文件版本

可独立运行测试：python wechat_version.py
"""
import os
import subprocess
import winreg

# 已知的 微信版本 -> wcferry 版本 对应表（wcferry 发版后这里需要更新）
WX_TO_WCFER = {
    "3.9.12.51": "39.5.2",
    "3.9.12.17": "39.4.4",
    "3.9.11.25": "39.3.0",
    "3.9.10.27": "39.2.0",
    "3.9.10.19": "39.1.0",
    # "3.9.2.23": "39.0.14",  # 此对应关系未核实，先注释掉
}

# 常见安装路径（兜底用）
COMMON_PATHS = [
    r"C:\Program Files\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files\WeChat\WeChat.exe",
    r"C:\Program Files\Tencent\Weixin\Weixin.exe",
    r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
]

UNINSTALL_PATHS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def _enum_uninstall(root, path, access):
    out = []
    try:
        key = winreg.OpenKey(root, path, 0, access)
    except OSError:
        return out
    try:
        n = winreg.QueryInfoKey(key)[0]
    except OSError:
        return out
    for i in range(n):
        try:
            name = winreg.EnumKey(key, i)
        except OSError:
            continue
        try:
            with winreg.OpenKey(key, name, 0, access) as sub:
                def qv(v):
                    try:
                        return winreg.QueryValueEx(sub, v)[0]
                    except OSError:
                        return ""
                display = qv("DisplayName")
                if not display:
                    continue
                if any(s in display for s in ("微信", "WeChat", "Weixin")):
                    out.append({
                        "name": display,
                        "version": qv("DisplayVersion"),
                        "install": qv("InstallLocation"),
                    })
        except OSError:
            continue
    return out


def _read_registry():
    out = []
    for root, path in UNINSTALL_PATHS:
        # 同时枚举 64 位与 32 位注册表视图（微信 4.x 是 64 位程序）
        out += _enum_uninstall(root, path, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        out += _enum_uninstall(root, path, winreg.KEY_READ | winreg.KEY_WOW64_32KEY)
    return out


def _file_version(path):
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{path}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=30,
        )
        v = out.stdout.strip()
        return v or None
    except Exception:
        return None


def detect():
    """返回 {"version", "install", "source"} 或 None。"""
    for c in _read_registry():
        if c["version"]:
            return {"version": str(c["version"]).strip(), "install": c["install"], "source": "注册表"}
    for p in COMMON_PATHS:
        if os.path.exists(p):
            v = _file_version(p)
            if v:
                return {"version": v, "install": os.path.dirname(p), "source": "WeChat/Weixin.exe 文件"}
    return None


def match_wcferry(version):
    """返回 (wcferry版本或None, 说明文字)。"""
    if not version:
        return None, "未检测到微信版本"
    v = str(version).split(" ")[0].strip()
    if v in WX_TO_WCFER:
        return WX_TO_WCFER[v], f"微信 {v} -> wcferry {WX_TO_WCFER[v]}（精确匹配）"
    if v.startswith("4."):
        return None, f"微信 {v} 是 4.x，wcferry 暂不支持，请降级到 3.9.12.17 或 3.9.12.51"
    if v.startswith("3.9."):
        return "39.5.2", f"微信 {v} 不在映射表里，尝试用最新 wcferry 39.5.2（可能不兼容，需实测）"
    return None, f"微信 {v} 版本过旧或未知，建议用 3.9.12.17 或 3.9.12.51"


if __name__ == "__main__":
    info = detect()
    if not info:
        print("未检测到微信电脑版。")
    else:
        print(f"检测到微信版本：{info['version']}（来源：{info['source']}）")
        print(f"安装位置：{info['install'] or '未知'}")
        wcfer, msg = match_wcferry(info["version"])
        print(f"匹配结果：{msg}")
        print(f"wcferry 版本：{wcfer or '（无）'}")
