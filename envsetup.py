"""虚拟环境与目录布局的公共定义，供 installer / console / autostart 复用。

所有路径都从本文件所在目录推导，不写死任何用户目录或盘符，
因此整个项目文件夹可以放在任意位置，换台电脑也不用改代码。
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(BASE, ".venv")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe")
VENV_PYW = os.path.join(VENV, "Scripts", "pythonw.exe")

# 建好 venv 后要验证存在的运行时依赖
REQUIRED_PKGS = ("wcferry", "anthropic", "yaml")

# 建议用来建 venv 的 Python 版本，按优先级排列。
# wcferry 依赖 pynng 这个 C 扩展，3.13+ 上未必有预编译轮子，所以优先用 3.11。
PREFERRED_PY = ("3.11", "3.10", "3.12", "3.9")


def _run(args, timeout=60):
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def venv_python():
    """返回可用的 venv 解释器路径；venv 不存在或已失效时返回 None。

    venv 里记录的是绝对路径，整个文件夹被移动/拷贝后就会失效，
    这里实跑一次来确认，不能只看文件是否存在。
    """
    if not os.path.exists(VENV_PY):
        return None
    r = _run([VENV_PY, "-c", "import sys"], timeout=30)
    return VENV_PY if r is not None and r.returncode == 0 else None


def missing_packages():
    """返回 venv 里缺失的依赖包名。venv 不可用时返回全部依赖。

    用 find_spec 而不是直接 import：wcferry 导入时会加载 DLL，
    在微信没跑或版本不匹配时可能抛错，这里只查包在不在。
    """
    if venv_python() is None:
        return list(REQUIRED_PKGS)
    code = (
        "import importlib.util as u;"
        f"names={list(REQUIRED_PKGS)!r};"
        "print('\\n'.join(n for n in names if u.find_spec(n) is None))"
    )
    r = _run([VENV_PY, "-c", code], timeout=60)
    if r is None or r.returncode != 0:
        return list(REQUIRED_PKGS)
    out = r.stdout.decode("utf-8", "ignore")
    return [n for n in out.split() if n]


def venv_ready():
    """venv 存在、可运行、且依赖齐全。"""
    return venv_python() is not None and not missing_packages()


def check_interpreter():
    """检查当前解释器能否用来建 venv。返回 (是否可用, 提示文本)。"""
    ver = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info < (3, 8):
        return False, f"当前 Python 是 {ver}，wcferry 需要 3.8 及以上，请换一个解释器。"
    if sys.version_info >= (3, 13):
        return True, (
            f"当前 Python 是 {ver}。wcferry 依赖的 pynng 在 3.13+ 上可能没有预编译"
            "轮子，安装也许会失败；若失败请改用 Python 3.11 重跑 install.bat。"
        )
    return True, ""
