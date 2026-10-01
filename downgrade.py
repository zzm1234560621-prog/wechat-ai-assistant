"""微信降级脚本：从 4.x 降级到 wcferry 支持的 3.9.x。

用法：python downgrade.py

流程：检测当前版本 -> 选择目标版本 -> 找安装包 -> 关微信 -> 卸载 -> 安装 -> 校验。

⚠️ 降级前务必：
  1. 备份聊天记录（4.x 与 3.9.x 数据不互通，可能丢记录）
  2. 记住微信账号密码（降级后需重新登录）
"""
import os
import subprocess
import sys
import time

from wechat_version import WX_TO_WCFER, detect, match_wcferry

BASE = os.path.dirname(os.path.abspath(__file__))
INSTALLERS_DIR = os.path.join(BASE, "installers")


# 版本对应关系**只有一份真源**：`wechat_version.WX_TO_WCFER`。
# 这里以前手抄了一份，而且抄成了三位短号（39.5.2 / 39.4.4）——那在 PyPI 上根本不存在，
# `pip install wcferry==39.4.4` 会直接 No matching distribution。
# 以后加微信版本**只改 WX_TO_WCFER 那张表**，别再在这里抄第二份。
TARGETS = {
    "1": {"ver": "3.9.12.51", "wcferry": WX_TO_WCFER.get("3.9.12.51", "")},
    "2": {"ver": "3.9.12.17", "wcferry": WX_TO_WCFER.get("3.9.12.17", "")},
}


def ask(prompt):
    return input(prompt).strip()


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def list_installers():
    if not os.path.isdir(INSTALLERS_DIR):
        return []
    return [os.path.join(INSTALLERS_DIR, n)
            for n in os.listdir(INSTALLERS_DIR) if n.lower().endswith(".exe")]


def kill_wechat():
    for exe in ("Weixin.exe", "WeChat.exe"):
        subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True)
    time.sleep(2)


def get_uninstall_exe():
    info = detect()
    install = ((info or {}).get("install") or "").strip('"')
    if install and os.path.isdir(install):
        for cand in (os.path.join(install, "Uninstall.exe"),
                     os.path.join(install, "Uninstall", "Uninstall.exe")):
            if os.path.exists(cand):
                return cand
    return None


def main():
    print("=" * 56)
    print("  微信降级脚本（4.x -> 3.9.x）")
    print("=" * 56)

    if not is_admin():
        print("[!] 建议以管理员身份运行（卸载/安装需要权限）。")
        print("    可右键命令行/PowerShell 选择“以管理员身份运行”后重跑。")

    info = detect()
    if not info:
        print("未检测到微信，请先安装微信。")
        return
    ver = info["version"]
    wcfer, msg = match_wcferry(ver)
    print(f"\n当前微信版本：{ver}")
    print(f"匹配结果：{msg}")

    if wcfer:
        print("\n当前版本已被 wcferry 支持，无需降级。直接双击 install.bat 即可。")
        return

    print("\n可降级到的版本：")
    print("  [1] 3.9.12.51  （对应 wcferry 39.5.2）")
    print("  [2] 3.9.12.17  （对应 wcferry 39.4.4）")
    print("  [3] 取消")
    choice = ask("请选择（1/2/3）：")
    if choice not in TARGETS:
        print("已取消。")
        return
    target = TARGETS[choice]
    print(f"\n目标版本：{target['ver']}")

    installers = list_installers()
    if not installers:
        print("\n[!] 没有找到安装包。")
        print(f"    请下载微信 {target['ver']} 的安装包，放到：\n    {INSTALLERS_DIR}")
        print("    （官方只推送 4.x，历史版本需自行获取并核实来源，别用来路不明的镜像）")
        print("    放好后重跑本脚本。")
        return

    installer = installers[0]
    if len(installers) > 1:
        print("\n检测到多个安装包，默认用第一个：")
        for p in installers:
            print("  -", p)
    print(f"\n将使用安装包：{os.path.basename(installer)}")

    print("=" * 56)
    print("  ⚠️ 降级前请务必：")
    print("  1. 已备份聊天记录（4.x 与 3.9.x 数据不互通）")
    print("  2. 已记住微信账号密码（降级后需重新登录）")
    print("=" * 56)
    if ask('\n确认降级？输入“确认降级”继续，其他任意键取消：') != "确认降级":
        print("已取消。")
        return

    print("\n[1/4] 关闭微信 ...")
    kill_wechat()

    print("[2/4] 卸载当前微信 ...")
    uninst = get_uninstall_exe()
    if uninst:
        print(f"  启动卸载程序：{uninst}（请在弹窗里完成卸载，别勾选删除聊天记录）")
        subprocess.Popen([uninst])
    else:
        print("  未找到卸载程序，请手动在“设置 -> 应用”里卸载微信。")
    ask("  卸载完成后按回车继续 ...")

    print("[3/4] 安装目标版本 ...")
    subprocess.Popen([installer])
    print("  请在安装向导里完成安装（默认路径即可）。")
    ask("  安装完成后按回车继续 ...")

    print("[4/4] 校验 ...")
    info2 = detect()
    if info2:
        print(f"  现在检测到：{info2['version']}")
        wcfer2, msg2 = match_wcferry(info2["version"])
        print(f"  {msg2}")
        if wcfer2:
            print("\n✅ 降级成功！接下来：")
            print("  1. 登录微信，并在 设置 -> 通用 里关闭自动更新")
            print("  2. 双击 install.bat 自动安装匹配的 wcferry")
            print("  3. python autostart.py on 开启开机自启")
            print("  4. 跑起来后在微信文件传输助手里发 /api <key> 设置密钥")
        else:
            print("  ⚠️ 版本仍未匹配，可能安装未生效或版本不对，请检查。")
    else:
        print("  未检测到微信，安装可能未完成。")


if __name__ == "__main__":
    main()
