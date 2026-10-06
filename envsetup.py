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

# ── 依赖清单的唯一真源 ────────────────────────────────────────────────
# requirements.txt 是本项目依赖清单的**唯一真源**。
# installer.py 直接读它来决定装什么；本文件也从它派生出「装完要验哪些包」。
# ⚠️ 新增依赖只改 requirements.txt，不要在别处再抄一份名单。
REQUIREMENTS_TXT = os.path.join(BASE, "requirements.txt")

# 发行名(PyPI) -> 导入名(importlib.util.find_spec 用的名字)。
# 只列两者不一致的；没列到的按原样查（绝大多数包名 == 模块名，如 pypdf）。
IMPORT_NAME_OVERRIDE = {
    "pyyaml": "yaml",
}


def requirements_specs(path=None):
    """解析 requirements.txt，返回**非注释、非空行**的需求串原文列表。

    例如 ['wcferry>=39.4.4', 'anthropic>=0.40.0', 'setuptools<81',
          'PyYAML>=6.0', 'pypdf>=4.0']。

    读不到文件时抛 FileNotFoundError——**不许静默返回空清单**：
    空清单会让「装完校验」变成永远通过。
    """
    p = path or REQUIREMENTS_TXT
    with open(p, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    out = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # 去掉行尾注释（requirements.txt 里只在有空白的注释上这么做）
        if " #" in s:
            s = s.split(" #", 1)[0].strip()
        if s:
            out.append(s)
    return out


def requirement_name(spec):
    """从一条需求串里取出发行名。'PyYAML>=6.0' -> 'PyYAML'。"""
    for ch in " \t[<>=!~;":
        i = spec.find(ch)
        if i != -1:
            spec = spec[:i]
    return spec.strip()


def import_name(project):
    """发行名 -> 导入名。PyYAML -> yaml，pypdf -> pypdf。"""
    return IMPORT_NAME_OVERRIDE.get(project.lower(), project)


def required_import_names(path=None):
    """requirements.txt 派生的导入名列表（去重、保持文件顺序）。"""
    out = []
    for spec in requirements_specs(path):
        name = import_name(requirement_name(spec))
        if name and name not in out:
            out.append(name)
    return out


def set_version(specs, project, version):
    """把 specs 里某包的版本约束替换成 ==version（装 wcferry 时用）。

    不在这里另写一份包名单：其余包原样来自 requirements.txt，
    所以「以 requirements.txt 为准」这条约束不会被绕过。
    """
    return [f"{project}=={version}" if requirement_name(s).lower() == project.lower()
            else s for s in specs]


# 建好 venv 后要验证存在的依赖（从 requirements.txt 派生，别手写第几份名单）
REQUIRED_PKGS = tuple(required_import_names())

# 哪个包在什么后端下才是必需的：
#   wcferry 只服务 wcferry 后端（微信 3.9.x）；主线是 4.x + aixed hook，不装它也能跑。
# 其余依赖（anthropic / yaml / pypdf / setuptools）两条后端都要。
REQUIRED_ONLY_FOR_BACKEND = {"wcferry": "wcferry"}


def backend_from_config(path=None):
    """从 config.yaml 读 backend（读不到就当 wcferry —— 与 bot.py 的默认值一致）。"""
    p = path or os.path.join(BASE, "config.yaml")
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s.startswith("backend:"):
                    v = s.split(":", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
    except OSError:
        pass
    return "wcferry"


def required_pkgs(backend=None):
    """当前后端下真正必需的包（从 REQUIRED_PKGS 里按后端过滤，不另写名单）。"""
    b = backend or backend_from_config()
    return tuple(n for n in REQUIRED_PKGS
                 if REQUIRED_ONLY_FOR_BACKEND.get(n, b) == b)


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


def missing_packages(backend=None):
    """返回 venv 里缺失的**必需**依赖包名。venv 不可用时返回全部。

    用 find_spec 而不是直接 import：wcferry 导入时会加载 DLL，
    在微信没跑或版本不匹配时可能抛错，这里只查包在不在。

    aixed 主线下 wcferry 不算缺（见 REQUIRED_ONLY_FOR_BACKEND），否则
    「装不上 wcferry」会把 4.x 主线整个卡住——那正是审计报的「官方路径装不全」的另一面。
    """
    names = list(required_pkgs(backend))
    if venv_python() is None:
        return names
    code = (
        "import importlib.util as u;"
        f"names={names!r};"
        "print('\\n'.join(n for n in names if u.find_spec(n) is None))"
    )
    r = _run([VENV_PY, "-c", code], timeout=60)
    if r is None or r.returncode != 0:
        # 探测本身失败（解释器跑了但报错）：当成都缺，宁可让上层重装一次，也不假装齐全
        return names
    out = r.stdout.decode("utf-8", "ignore")
    return [n for n in out.split() if n]


def venv_ready(backend=None):
    """venv 存在、可运行、且**当前后端所需**的依赖齐全。"""
    return venv_python() is not None and not missing_packages(backend)


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


# ── 可选组件：不随主程序安装的能力 ─────────────────────────────────────
# 为什么单独有这么一段：
#   * 这些依赖**绝不能**写成 requirements.txt 的正式行——`required_pkgs()` 是从它派生的，
#     写成正式行，没装的人就会「装完还是起不来」死循环，installer 还会去拖重包
#     （faster-whisper 踩过这个坑，见 selftest_audio 那条钉死的用例）；
#   * 可只留注释又等于**没有任何安装入口**：2026-10-05 实测，包发给别人之后「语音转文字」
#     根本装不出来，而 README 把「语音条转文字」写在功能卖点里——功能是死的，还没人知道。
# 所以：清单仍在 requirements.txt（唯一真源）里保持**注释**形态，安装入口放在这里。
OPTIONAL_PIP = {
    "voice": {
        "label": "语音转文字（本地 faster-whisper，音频一个字节不出本机）",
        "specs": ["faster-whisper", "pilk"],
        "imports": ["faster_whisper", "pilk"],
    },
    "formats": {
        "label": "文件格式增强包（视频 / 邮件 / 压缩包 / 老 Office / PDF 内嵌图）",
        # 六个都小（最大的 av 几十 MB，wheel 自带 FFmpeg），装完立刻能用，所以合成一项。
        # ⚠️ `rarfile` 装上**并不等于 .rar 能读**：它只是个壳，真正解压要外部程序
        # （unrar / 7z / bsdtar）——那件事由 `archive_read.find_rar_tool()` 判定，
        # 装完必须如实说，不许报成「.rar 能读了」。
        "specs": ["av", "extract-msg", "py7zr", "rarfile", "xlrd", "olefile", "Pillow"],
        "imports": ["av", "extract_msg", "py7zr", "rarfile", "xlrd", "olefile", "PIL"],
    },
    "semantic": {
        "label": "本地语义检索（sentence-transformers，会拖进 torch，最重的一项）",
        # 比其它项多两步：下模型 + **建索引（建之前必须停 bot）**。
        # ⚠️ 它**也**在「一键部署」默认装的那一批里（2026-10-05 用户拍板，`console._HEAVY`
        # 因此清空）—— 唯一保留的人工确认是「建索引要停一下助手」，那一步单独问。
        # 别把这里再写回「默认不装」：那是 _HEAVY 还有它的时候的说法。回归：selftest_install。
        "specs": ["sentence-transformers"],
        "imports": ["sentence_transformers"],
    },
}


def missing_optional(name):
    """某个可选组件在 venv 里还缺哪些包。venv 不可用 / 探不动 → 当**全缺**。"""
    names = list((OPTIONAL_PIP.get(name) or {}).get("imports") or [])
    if not names:
        return []
    py = venv_python()
    if py is None:
        return names
    code = (
        "import importlib.util as u;"
        f"names={names!r};"
        "print('\\n'.join(n for n in names if u.find_spec(n) is None))"
    )
    r = _run([py, "-c", code], timeout=60)
    if r is None or r.returncode != 0:
        return names
    return [n for n in r.stdout.decode("utf-8", "ignore").split() if n]


def install_optional(name):
    """把某个可选组件的依赖装进 **bot 自己的 venv**。返回 `(ok, 一句人话)`。

    「装好了没」的判据是装完**再查一次 import**，不是 pip 的退出码——
    pip 说成功而 import 找不到是真会发生的（版本不匹配 / 装到了别的解释器）。
    """
    comp = OPTIONAL_PIP.get(name)
    if not comp:
        return False, f"没有这个可选组件：{name}（有的是：{', '.join(OPTIONAL_PIP)}）"
    py = venv_python()
    if py is None:
        return False, "虚拟环境还没建好或已失效——先跑 install.bat（或菜单 [2] 安装依赖）。"
    specs = list(comp["specs"])
    print(f"[可选组件] {comp['label']}")
    print(f"[可选组件] 装 {', '.join(specs)}（要联网下载，第一次几分钟）…")
    r = subprocess.run([py, "-m", "pip", "install", *specs])
    if r.returncode != 0:
        return False, ("pip 装不上（退出码非 0）——上面那段输出里是真正的原因。"
                       "faster-whisper 会带上 ctranslate2 / onnxruntime 这些轮子，"
                       "装不上多半是网络。")
    left = missing_optional(name)
    if left:
        return False, f"pip 说装完了，可 import 还是找不到：{', '.join(left)}——**不能算装好**。"
    return True, f"装好了：{', '.join(specs)}"
