"""自动识别已安装的微信 PC 版本，并匹配对应的 wcferry 版本。

识别来源：
  1. 注册表卸载信息（同时读 64 位和 32 位视图）
  2. 兜底：WeChat.exe / Weixin.exe 的文件版本

可独立运行测试：python wechat_version.py
"""
import base64
import os
import subprocess
import winreg

# ── 「取 exe 文件版本」的 PowerShell 脚本 ─────────────────────────────────
# 脚本本体写死、纯 ASCII，用 -EncodedCommand 传，路径走环境变量（见 _file_version）。
# ⚠️ 别改回 f"(Get-Item '{path}')..."：路径含单引号会被拼坏甚至注入。
_PS_PATH_ENV = "WX_VER_PATH"
_PS_SCRIPT = r"(Get-Item -LiteralPath $env:WX_VER_PATH).VersionInfo.ProductVersion"
_PS_ENC_CMD = base64.b64encode(_PS_SCRIPT.encode("utf-16-le")).decode("ascii")

# 已知的 微信版本 -> wcferry 版本 对应表（wcferry 发版后这里需要更新）
#
# ⚠️ **版本号要写 PyPI 上的完整形式（四段），不是文档里的三位短号。**
# wcferry 的版本是 `w.x.y.z`（w=微信大版本 39=3.9，x=适配的微信小版本，
# y=WeChatFerry 自己的版本，z=客户端）。`pip install wcferry==39.4.4` 会直接
# 「No matching distribution」——PyPI 上没有三位号。
#
# 已核实的对应关系（来源：wechatferry/wechatferry 的官方 release notes，
# 里面每条都写着「WeChatFerry: vX / WeChat: Y」）：
#   微信 3.9.12.17 <-> WeChatFerry **v39.4.5**（release v0.0.26 明写）→ PyPI 有 `39.4.5.0` ✅
#   微信 3.9.12.17 <-> WeChatFerry v39.4.4（3.9.12.17 的安装包挂在 v39.4.4 那个 tag 下）
#                               → 但 PyPI 上**没有** 39.4.4.x，只有 39.4.2.2 / 39.4.5.0
#   微信 3.9.10.27 <-> WeChatFerry **39.2.4**（release v0.0.19/v0.0.22~v0.0.24 明写）
#                               → PyPI 上没有 39.2.4.x，最近的 39.2.3.1
#   微信 3.9.12.51 <-> wcferry `39.5.2.0`（PyPI 存在；配对有旁证）
# **没有核实过**的（PyPI 上连近似版本都没有）：3.9.11.25、3.9.10.19。
# 结论：这张表本质上是「微信版本 ↔ WeChatFerry **release** 版本」，
# 而 PyPI 的 wheel 版本是另一条号；两者**只在部分版本上重合**。
# 所以**别凭猜把短号补成 `.0`**——补错了 pip 一样装不上，还把一个「已知的坏值」
# 伪装成「看起来对的值」。要修就先去
# https://pypi.org/project/wcferry/#history 和
# https://github.com/wechatferry/wechatferry/releases 两边对完再改。
WX_TO_WCFER = {
    "3.9.12.51": "39.5.2.0",                # ✅ PyPI 存在 + 配对有旁证
    "3.9.12.17": "39.4.5.0",                # ✅ 官方 release notes 配 v39.4.5；PyPI 存在
    "3.9.11.25": "39.3.0",                  # ⚠️ 未核实（PyPI 上只有 39.3.2.0/39.3.3.0/39.3.3.1）
    "3.9.10.27": "39.2.0",                  # ⚠️ 官方配的是 39.2.4，PyPI 无 39.2.4.x
    "3.9.10.19": "39.1.0",                  # ⚠️ 未核实（PyPI 上一个 39.1.x 都没有）
    # "3.9.2.23": "39.0.14",  # 此对应关系未核实，先注释掉
}

# 常见安装路径（兜底用）
COMMON_PATHS = [
    r"C:\Program Files\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
    r"C:\Program Files\WeChat\WeChat.exe",
    # 4.x：安装目录名换过（xwechat / Weixin），两种都列上
    r"C:\Program Files\Tencent\xwechat\Weixin.exe",
    r"C:\Program Files (x86)\Tencent\xwechat\Weixin.exe",
    r"C:\Program Files\Tencent\Weixin\Weixin.exe",
    r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe",
]

# 要在安装目录里找的主程序名（4.x 是 Weixin.exe，3.9.x 是 WeChat.exe）
EXE_NAMES = ("Weixin.exe", "WeChat.exe")

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


def _file_version_cmd(path):
    """构造「取 exe 文件版本」的 PowerShell argv 列表（纯函数，便于自测）。

    **不做任何字符串拼接**，也没有任何位置给路径内容变成 PowerShell 代码：
      * 脚本本体是写死的 ASCII 常量，用 -EncodedCommand(base64/UTF-16LE) 传进去，
        绕开 cmd/PowerShell 的命令行引号解析；
      * 路径通过环境变量 WX_VER_PATH 传进去，脚本里用 `Get-Item -LiteralPath $env:WX_VER_PATH`
        取（-LiteralPath 不做通配符展开）。

    这样安装路径带空格 / 中文 / 单引号 / 方括号都不会坏，也不可能注入。
    （实测过两条弯路：`-Command` 后面再跟位置参数在 Windows PowerShell 5.1 里
     会被当成脚本文本的一部分拼进去直接语法报错；`-EncodedCommand` 后面也不许再跟参数。）
    """
    return [
        "powershell", "-NoProfile", "-NonInteractive",
        "-EncodedCommand", _PS_ENC_CMD,
    ]


def _file_version(path):
    """读 exe 的文件版本号；读不到返回 None（保持原来的返回语义），但会打印原因。"""
    if not os.path.exists(path):
        print(f"[wechat_version] 取文件版本：文件不存在，跳过 —— {path}")
        return None
    env = dict(os.environ)
    env[_PS_PATH_ENV] = str(path)
    try:
        out = subprocess.run(
            _file_version_cmd(path), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[wechat_version] 取文件版本失败（无法调用 powershell：{e}）—— {path}")
        return None
    if out.returncode != 0:
        err = (out.stderr or "").strip() or (out.stdout or "").strip() or "（无输出）"
        print(f"[wechat_version] 取文件版本失败（powershell 退出码 {out.returncode}）：{err}")
        print(f"[wechat_version]   目标：{path}")
        return None
    v = (out.stdout or "").strip()
    if not v:
        print("[wechat_version] 取文件版本失败：powershell 没返回版本号"
              "（该文件可能没有版本资源）")
        print(f"[wechat_version]   目标：{path}")
        return None
    return v


def _exe_in(install_dir, display_name=""):
    """在注册表给的安装目录里找微信主程序，返回 exe 全路径；找不到返回 None。

    注册表的 InstallLocation 有的是 `"C:\\Program Files\\Tencent\\Weixin"` 这种带引号的，
    有的干脆是空的——所以这里容忍引号、也容忍目录不存在。
    """
    d = (install_dir or "").strip().strip('"').rstrip("\\")
    if not d or not os.path.isdir(d):
        return None
    for name in EXE_NAMES:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def detect():
    """返回 {"version", "install", "source"} 或 None。"""
    regs = [c for c in _read_registry() if c["version"]]
    # 1) 注册表优先；但 4.x 换过安装目录（Tencent\xwechat\Weixin.exe）和 install 值可能带引号，
    #    所以取到的 exe 不存在时**不轻信注册表版本号**，继续往下试。
    for c in regs:
        exe = _exe_in(c["install"], c["name"])
        if exe:
            return {"version": str(c["version"]).strip(),
                    "install": os.path.dirname(exe), "source": "注册表"}
    # 2) 兜底：常见路径上的 WeChat.exe / Weixin.exe 文件版本
    for p in COMMON_PATHS:
        if os.path.exists(p):
            v = _file_version(p)
            if v:
                return {"version": v, "install": os.path.dirname(p),
                        "source": "WeChat/Weixin.exe 文件"}
    # 3) 文件全都对不上时，退回注册表报的版本（原来就是这个行为，别丢）
    if regs:
        c = regs[0]
        return {"version": str(c["version"]).strip(),
                "install": (c["install"] or "").strip('"'), "source": "注册表"}
    return None


def match_wcferry(version):
    """返回 (wcferry版本或None, 说明文字)。"""
    if not version:
        return None, "未检测到微信版本"
    v = str(version).split(" ")[0].strip()
    if v in WX_TO_WCFER:
        return WX_TO_WCFER[v], f"微信 {v} -> wcferry {WX_TO_WCFER[v]}（精确匹配）"
    if v.startswith("4."):
        # 4.x 是**主线**（aixed hook），不是「需要降级」的东西。
        # 以前这里写「请降级到 3.9.12.17 或 3.9.12.51」——那是把主线用户带沟里，
        # 而且降级会掉登录态、还要重新扫码。调用方 installer/console 现在都按主线走。
        return None, (f"微信 {v} 是 4.x：走 **aixed hook** 主线（version.dll 注入 + "
                      f"本地 HTTP :30001），不需要 wcferry、也**不用降级**；"
                      f"装法见 README 的「微信 4.x 主线」一节")
    if v.startswith("3.9."):
        # 兜底也用**四段号**：三位短号在 PyPI 上不存在，pip 会直接装不上
        return "39.5.2.0", f"微信 {v} 不在映射表里，尝试用最新 wcferry 39.5.2.0（可能不兼容，需实测）"
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
