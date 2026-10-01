"""一键安装：自动识别微信版本 -> 建虚拟环境 -> 装匹配的 wcferry -> 生成启动脚本。

用法：python installer.py （或双击 install.bat）
不写死任何机器相关路径，项目文件夹可以放在任意位置、任意盘符。
如果文件夹被移动过导致 .venv 失效，重跑本脚本会自动删掉重建。
"""
import os
import shutil
import subprocess
import sys
import venv

import envsetup as env
from wechat_version import detect, match_wcferry

BASE = env.BASE
VENV = env.VENV
VENV_PY = env.VENV_PY
LAUNCHER = os.path.join(BASE, "启动助手.bat")

# 启动脚本一律用 %~dp0 定位自身，不写绝对路径，这样移动文件夹也不会失效。
# 依赖缺失时（例如换了电脑、挪了目录）自动回头调 install.bat 重装。
# 内容保持纯 ASCII，避免 cmd 在 chcp 65001 下解析多字节字符出问题。
LAUNCHER_TEMPLATE = '''@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

".venv\\Scripts\\python.exe" -c "import importlib.util as u,sys;sys.exit(1 if [m for m in ('wcferry','anthropic','yaml') if u.find_spec(m) is None] else 0)" >nul 2>nul
if errorlevel 1 (
    echo [setup] venv missing or broken, running installer ...
    echo.
    call "%~dp0install.bat"
    goto :eof
)

".venv\\Scripts\\python.exe" bot.py
pause
'''


def run(cmd):
    print("  > " + " ".join(cmd))
    return subprocess.run(cmd, check=False)


def ensure_venv():
    """确保有一个能用的 venv；已失效（如移动过文件夹）就删掉重建。"""
    existing = env.venv_python()
    if existing:
        print(f"  venv 可用：{existing}")
        return True
    if os.path.exists(VENV):
        print("  检测到 .venv 已失效（常见原因：整个文件夹被移动或拷贝过），删除后重建 ...")
        shutil.rmtree(VENV, ignore_errors=True)
    print("  创建 Python 虚拟环境 ...")
    try:
        venv.create(VENV, with_pip=True)
    except Exception as e:
        print(f"  创建失败：{e}")
        return False
    return env.venv_python() is not None


def install_deps(wcfer):
    print(f"  安装依赖（wcferry=={wcfer} / anthropic / PyYAML）...")
    run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"])
    r = subprocess.run([
        VENV_PY, "-m", "pip", "install",
        f"wcferry=={wcfer}", "anthropic", "setuptools<81", "PyYAML",
    ])
    if r.returncode != 0:
        return False
    missing = env.missing_packages()
    if missing:
        print(f"  装完后仍缺少：{', '.join(missing)}")
        return False
    return True


def write_launcher():
    with open(LAUNCHER, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(LAUNCHER_TEMPLATE)
    print(f"  已生成：{LAUNCHER}")


def main():
    print("=" * 56)
    print("  微信 AI 助手 一键安装")
    print("  注意：本工具注入微信进程，违反微信协议，有封号风险，建议小号使用。")
    print("=" * 56)

    ok, hint = env.check_interpreter()
    if not ok:
        print(f"\n[!] {hint}")
        input("按回车退出 ...")
        return 1
    if hint:
        print(f"\n[i] {hint}")

    # 1) 检测微信版本
    info = detect()
    if not info:
        print("\n[!] 未检测到已安装的微信电脑版。")
        print("    请先安装并登录微信 PC 版（3.9.x），再运行本安装。")
        input("按回车退出 ...")
        return 1
    wver = info["version"]
    wcfer, msg = match_wcferry(wver)
    print(f"\n[1/4] 检测到微信版本：{wver}（来源：{info['source']}）")
    print(f"      匹配：{msg}")
    if not wcfer:
        print("\n[!] 无法自动匹配可用的 wcferry，安装中止。")
        print("    建议先降级微信到 3.9.12.17 或 3.9.12.51（双击「降级.bat」），再重跑本安装。")
        input("按回车退出 ...")
        return 1

    # 2) 虚拟环境
    print("\n[2/4] 准备 Python 虚拟环境 ...")
    if not ensure_venv():
        print("\n[!] 虚拟环境不可用，安装中止。")
        input("按回车退出 ...")
        return 1

    # 3) 依赖
    print("\n[3/4] 安装依赖 ...")
    if not install_deps(wcfer):
        print("\n[!] 依赖安装失败。常见原因：网络不通，或当前 Python 版本太新。")
        print("    请改用 Python 3.11 重跑 install.bat。")
        input("按回车退出 ...")
        return 1

    # 4) 启动脚本
    print("\n[4/4] 生成启动脚本 ...")
    write_launcher()

    print("\n" + "=" * 56)
    print("  安装完成！")
    print(f"  微信 {wver} 已匹配 wcferry {wcfer}。")
    print("  以后双击「启动助手.bat」即可运行；")
    print("  挪动文件夹后它会自动检测到 venv 失效并重新安装。")
    print("  首次运行后，在微信文件传输助手里发 /api <你的key> 设置密钥。")
    print("=" * 56)
    input("按回车退出 ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
