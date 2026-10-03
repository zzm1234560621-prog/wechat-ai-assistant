"""项目里「几种临时目录」的统一入口（audio / image / video / archive 共用）。

## 为什么需要它（2026-10-03）

`audio_read.tmp_dir()` / `image_read.tmp_dir()` / `video_read.tmp_dir()` /
`archive_read._tmp_dir()` **各自硬编码**了 `data/tmp_audio`、`data/tmp_img`、
`data/tmp_video`、`data/tmp_unpack`。四个函数、四条重复的 `os.makedirs`，
而且**没有一处能改**——后果是：

* 在**受限环境**（例如只允许写工作区顶层的沙箱）里，这些子目录**建都建不了**：
  `selftest_audio` 的 t7（真跑 PyAV 切片）当场 `PermissionError` 崩掉，
  整份套件后面的用例一条都跑不到；`selftest_io_llm` 的压缩包用例同样写不进
  `data/tmp_unpack`。当时只能在**测试里 monkeypatch `tmp_dir`** ——
  测试内部按名字去替换生产函数的引用，是本文件最不希望留下的那种耦合。
* 换个部署形态（只读安装目录、多实例、CI 临时盘）也无处可配。

所以这里收成一处，并给一个**环境变量覆盖**：

    PROJ_TMP=<目录>   → 所有模块的临时文件都落到那里（测试用）

生产默认**一个字都不变**（仍是 `<项目>/data/tmp_*`）；只有显式设了环境变量才改道。
测试因此只要设环境变量，不必再碰生产函数的引用。

## 与 `.gitignore` / 清理的关系

`data/` 已被 `.gitignore` 忽略；清理仍由各模块的 `sweep_tmp()` 负责
（**删了什么要打日志**，这条规矩不变——`sweep()` 就是它的统一实现）。
"""
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))

#: 覆盖根目录的环境变量名。设了就改道，没设就用项目内的 `data/`。
ENV_VAR = "PROJ_TMP"


def root():
    """临时根目录。`PROJ_TMP` 设了就用它，否则 `<项目>/data`。

    ⚠️ **每次调用都读环境变量**（不缓存）：测试在进程内设了就要立刻生效，
    缓存会让"设了没用"变成又一个静默失效——正是本项目最忌讳的那种。
    """
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(HERE, "data")


def get(name, make=True):
    """某个用途的临时目录（如 `get("tmp_audio")`）。`make=True` 时顺手建出来。

    建不出来**不抛异常、不静默吞掉**：返回路径，由真正要写文件的那一步去报错
    ——那时错误信息里有确切的路径，比在这里抛一句更可诊断。
    """
    d = os.path.join(root(), str(name))
    if make:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"⚠️ tempdir: 建不出临时目录 {d}（{type(e).__name__}: {e}）。"
                  f"要用别的盘，就设环境变量 {ENV_VAR}=<目录>。", flush=True)
    return d


def sweep(name, max_age, label, unit="个"):
    """删掉 `get(name)` 下超过 `max_age` 秒的文件**或子目录**。返回删掉的名字列表。

    **删了什么要打日志**（本项目规矩）：一条汇总行，带模块名与数量。
    子目录用 `rmtree`（`archive_read` 会为每个成员建目录，正常路径用完就删，
    这里只是兜底）。
    """
    import shutil
    d = get(name, make=False)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    dead, now = [], time.time()
    for n in names:
        p = os.path.join(d, n)
        try:
            if now - os.path.getmtime(p) > max_age:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
                dead.append(n)
        except OSError:
            continue
    if dead:
        print(f"⚠️ {label}: 清理了 {len(dead)} {unit}临时文件（>{max_age / 3600:.0f} 小时）。",
              flush=True)
    return dead


#: 测试用的稳定临时根（**同一个路径**，不是每个进程 mkdtemp）。
#: ⚠️ 必须**稳定**：`selftest_audio.t7` 有一条断言「切片用完就删、目录里没有 .wav 残留」，
#: 而素材 wav 写在测试自己的临时目录里 —— 有进程号正好把它们分到不同目录，断言才成立。
TEST_ROOT = None  # 由 use_for_tests() 惰性算出（依赖 tempfile）


def _test_root():
    global TEST_ROOT
    if TEST_ROOT is None:
        import tempfile
        TEST_ROOT = os.path.join(tempfile.gettempdir(), "wechat-ai-assistant-tmp")
    return TEST_ROOT


def use_for_tests():
    """把临时根改到系统临时盘下的固定目录（测试用；**幂等**）。

    为什么测试需要它：生产临时目录都在 `<项目>/data/` 下面，而在**受限环境**
    （只允许写工作区顶层的沙箱）里连建文件都做不到 —— 于是
    `selftest_audio.t7`（真跑 PyAV 切片）、`selftest_archive`、`selftest_video`、
    `selftest_mail_db`、`selftest_io_llm` 的压缩包用例都会 `PermissionError`。
    以前只能在各套件里 monkeypatch 生产函数（替换函数引用，最脏的那种耦合）；
    现在统一走 `PROJ_TMP`，**生产默认一个字不变**。

    已在别的测试进程里设过 `PROJ_TMP` 就尊重它（不覆盖）。返回最终生效的根目录。
    """
    env = (os.environ.get(ENV_VAR) or "").strip()
    if not env:
        d = _test_root()
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            return root()
        os.environ[ENV_VAR] = d
    return root()
