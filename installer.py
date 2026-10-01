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
#
# {pkg_repr} 在 write_launcher() 里被替换成 requirements.txt 派生出的包名元组，
# 所以这份自检清单**不会**和 requirements.txt 走散（别在这里再手写名单）。
LAUNCHER_TEMPLATE = '''@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

".venv\\Scripts\\python.exe" -c "import importlib.util as u,sys;sys.exit(1 if [m for m in {pkg_repr} if u.find_spec(m) is None] else 0)" >nul 2>nul
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


def build_pip_specs(wcfer):
    """要交给 pip 安装的需求串列表。

    **唯一真源是 requirements.txt**（envsetup.requirements_specs 读它），
    本函数只做两件事：把 wcferry 的版本覆盖成检测到的微信版本对应的版本，
    并在 requirements.txt 被清空/读不到时明确报错——不在这里另抄一份包名单。
    """
    specs = env.requirements_specs()
    if not specs:
        raise RuntimeError(
            f"{env.REQUIREMENTS_TXT} 里没有可安装的依赖行，安装无法继续。"
            "请检查该文件是否被清空或改坏。")
    return env.set_version(specs, "wcferry", wcfer)


def _write_pip_requirements(specs):
    """把解析出来的需求串落到一个临时文件，pip 用 -r 装（便于报错时回看装了什么）。"""
    path = os.path.join(BASE, ".pip-deps.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(specs) + "\n")
    return path


def install_deps(wcfer=None, dry_run=False):
    """按 requirements.txt 装依赖。wcfer 为空 = 本次不装 wcferry（微信 4.x 主线）。"""
    print(f"  安装依赖（清单来自 requirements.txt，wcferry：{wcfer or '不安装'}）...")
    try:
        specs = build_pip_specs(wcfer) if wcfer else [
            s for s in env.requirements_specs()
            if env.requirement_name(s).lower() != "wcferry"]
    except (OSError, RuntimeError) as e:
        print(f"  读取依赖清单失败：{e}")
        return False
    if not specs:
        print(f"  依赖清单为空（{env.REQUIREMENTS_TXT} 被清空或改坏？），安装中止。")
        return False
    for s in specs:
        print(f"    - {s}")
    req_file = _write_pip_requirements(specs)

    if dry_run:
        # 自测用：只问 pip「会装什么」，不真装、不改 venv
        print("  [dry-run] 只解析依赖，不实际安装。")
        r = subprocess.run([VENV_PY, "-m", "pip", "install", "--dry-run", "-r", req_file])
        if r.returncode != 0:
            print("  [dry-run] pip 解析依赖失败，请先修好依赖清单。")
        return True

    run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"])
    r = subprocess.run([VENV_PY, "-m", "pip", "install", "-r", req_file])
    if r.returncode != 0:
        # wcferry 只服务 wcferry(微信 3.9.x) 后端；主线是 4.x + aixed hook，不需要它。
        # 所以它装不上时**如实报出来**并重试一次「不带 wcferry」的安装，而不是整单失败。
        rest = [s for s in specs if env.requirement_name(s).lower() != "wcferry"]
        if len(rest) != len(specs):
            print("  [!] 上面这单里 wcferry 装失败了。它只在 wcferry 后端（微信 3.9.x）才需要，")
            print("      主线（微信 4.x + aixed hook）不需要它，正在重试安装其余依赖 ...")
            r2 = subprocess.run([VENV_PY, "-m", "pip", "install", "-r",
                                 _write_pip_requirements(rest)])
            if r2.returncode == 0:
                print("  [!] 其余依赖装好了，但 **wcferry 没装上**（这不是静默失败：它装不上"
                      "只影响 wcferry 后端）。")
                print("      如果你想用微信 3.9.x + wcferry 那条后端，请先降到 3.9.12.17 / 3.9.12.51")
                print("      并手动装对应版本："
                      f"{VENV_PY} -m pip install wcferry=={wcfer}")
                missing = [m for m in env.missing_packages() if m != "wcferry"]
                if missing:
                    print(f"  仍缺少：{', '.join(missing)}")
                    print(f"  请手动补装：{VENV_PY} -m pip install " + " ".join(missing))
                    return False
                return True
        print("  依赖安装失败。常见原因：网络不通，或当前 Python 版本太新"
              "（wcferry 依赖的 pynng 在 3.13+ 上常没有预编译轮子）。")
        return False
    missing = env.missing_packages()
    if missing:
        print(f"  装完后仍缺少：{', '.join(missing)}")
        print(f"  请手动补装：{VENV_PY} -m pip install " + " ".join(missing))
        return False
    return True


def launcher_text():
    """按 requirements.txt 派生出**当前后端真正需要**的自检清单，渲染出 启动助手.bat。

    ⚠️ 必须按后端过滤（`env.required_pkgs`），不能直接用全量 `required_import_names()`：
    4.x 主线**故意不装 wcferry**（见 install_deps 里的过滤），自检清单里要是还写着
    wcferry，自检就**永远**失败 → 每次都去 `install.bat` → 装完回来还是失败 →
    **永远进不了 bot**（死循环）。本机之所以没暴露，只是因为 venv 里恰好有 wcferry。

    {pkg_repr} 渲染成 `('a','b','c')` 这种形态（逗号后不留空格）：
    它要嵌进 `python -c "..."` 的双引号串里，越紧凑越不容易被 cmd 的引号解析碰上。
    """
    try:
        backend = env.backend_from_config() or "aixed"
        pkgs_list = env.required_pkgs(backend)
    except Exception as e:
        # 读不出后端配置时，按**主线（aixed）**处理：宁可少要一个 wcferry
        # （wcferry 后端下读配置不会失败，配置坏了本来就起不来）。
        print(f"[installer] ⚠️ 按后端算依赖清单失败（{e}），按 aixed 主线处理")
        pkgs_list = [n for n in env.required_import_names() if n != "wcferry"]
    pkgs = ",".join(repr(n) for n in pkgs_list)
    return LAUNCHER_TEMPLATE.replace("{pkg_repr}", f"({pkgs})")


def write_launcher():
    with open(LAUNCHER, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(launcher_text())
    print(f"  已生成：{LAUNCHER}（依赖自检清单来自 requirements.txt）")


def resolve_specs_for_current_wechat():
    """检测微信并算出这次要装的需求串。

    返回 (specs, 说明文字, wcferry版本或None)。**不读也不装任何东西**，供 dry-run 与自测用。
    """
    info = detect()
    if not info:
        return None, "未检测到已安装的微信电脑版。", None
    wver = info["version"]
    wcfer, msg = match_wcferry(wver)
    if not wcfer:
        # 微信 4.x 走 aixed hook，不需要 wcferry；装依赖时把 wcferry 从清单里去掉，
        # 而不是「整单中止」——否则 4.x 用户按官方路径装出来是缺依赖的。
        rest = [s for s in env.requirements_specs()
                if env.requirement_name(s).lower() != "wcferry"]
        return rest, f"微信 {wver}，{msg}；本次跳过 wcferry，只装其余依赖。", None
    return build_pip_specs(wcfer), f"微信 {wver}，{msg}", wcfer


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    print("=" * 56)
    print("  微信 AI 助手 一键安装" + ("（dry-run：只解析依赖，不安装）" if dry_run else ""))
    print("  注意：本工具注入微信进程，违反微信协议，有封号风险，建议小号使用。")
    print("=" * 56)

    if dry_run:
        # 自测/排错入口：打印依赖清单后直接退出，不建 venv、不跑 pip、不生成任何文件
        try:
            specs, msg, wcfer = resolve_specs_for_current_wechat()
        except (OSError, RuntimeError) as e:
            print(f"\n[!] 读取依赖清单失败：{e}")
            return 1
        if specs is None:
            print(f"\n[!] {msg}")
            return 1
        print(f"\n[1/1] {msg}")
        print("  本次将安装（唯一真源：requirements.txt）：")
        for s in specs:
            print(f"    - {s}")
        print(f"  wcferry：{wcfer or '（不安装）'}")
        return 0
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
        print("    请先安装并登录微信 PC 版（3.9.x 或 4.x），再运行本安装。")
        input("按回车退出 ...")
        return 1
    wver = info["version"]
    wcfer, msg = match_wcferry(wver)
    print(f"\n[1/4] 检测到微信版本：{wver}（来源：{info['source']}）")
    print(f"      匹配：{msg}")
    if not wcfer:
        print("\n[i] 这条版本不需要 wcferry：依赖按 requirements.txt 安装，"
              "但会跳过 wcferry。")
        print("    若你确实要用 wcferry 后端，请先降级微信到 3.9.12.17 或 3.9.12.51"
              "（双击「降级.bat」），再重跑本安装。")

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
    if wcfer:
        print(f"  微信 {wver} 已匹配 wcferry {wcfer}。")
    else:
        print(f"  微信 {wver}：已按 requirements.txt 装好依赖（未装 wcferry）。")
    print("  以后双击「启动助手.bat」即可运行；")
    print("  挪动文件夹后它会自动检测到 venv 失效并重新安装。")
    print("  首次运行后，在微信文件传输助手里发 /api <你的key> 设置密钥。")
    print("=" * 56)
    input("按回车退出 ...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
