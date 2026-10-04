"""便携性自测：把项目装到**别的电脑**上，不能带着我本机的路径和编码假设。

为什么要有这份自测（2026-10-02 真踩过）：

* `installers/wechat-4.1.10.27/do_*.ps1` 八个脚本各自把「项目目录 / 微信安装目录 /
  用户名」写死在文件开头，README 只能要求用户「换机器先手工改三行」。于是
  「给别的电脑装的产品」其实**装不上**——而且它不报错：脚本会去找一个不存在的目录，
  然后静默地把事情做错（hook 没装上，用户以为装上了）。
* 同一批 `.ps1` 还没有 UTF-8 BOM。Windows PowerShell 5.1 在没有 BOM 时按**系统代码页**
  读脚本，GBK（936）机器上中文全变乱码；而本机代码页恰好是 65001，所以**本机看不出问题**
  ——只能靠这份自测盯住。

只查**会执行的**文件（.py / .ps1 / .bat）：注释和文档里提一句历史写法是正常的，不该拦，
所以先把行注释剥掉再匹配。

用法：
    .venv\\Scripts\\python.exe selftest_portable.py
"""
import os
import re
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.basename(os.path.abspath(__file__))

# 不查的目录：虚拟环境 / 缓存 / 运行期数据 / 旧打包产物 / 自测图片
# / 第三方 hook 源码（那是别人仓库的快照，不归我们管，也不该改）
SKIP_DIR_NAMES = {".venv", "__pycache__", "data", "dist", "test_images", ".git",
                  "src-4.1.10.27"}
CODE_EXT = (".py", ".ps1", ".bat")

# 「会执行的代码里绝不该出现」的本机身份。注释里提到是允许的（先剥注释再匹配）。
#
# ⚠️ 这里**故意不拦** `C:\Program Files\Tencent\Weixin` 这类**厂商默认**安装路径：
# 它在 `wechat_version.COMMON_PATHS` / `bypass_update.COMMON_PATHS` 里是**兜底候选**，
# 而它们的正经来源是注册表和 `$env:ProgramFiles`——默认路径不是「本机身份」，
# 换台电脑照样成立。要拦的是**只在这台机器上成立**的东西：项目所在盘符和用户名。
FORBIDDEN = [
    (r"[A-Za-z]:[\\/]+wechat-ai-assistant", "本机上的项目绝对路径"),
    (r"[A-Za-z]:[\\/]+Users[\\/]+zzm12", "本机用户名下的绝对路径"),
    (r"\bzzm12\b", "本机用户名"),
]

_ok = True


def check(label, cond, detail=""):
    global _ok
    if cond:
        print(f"  ✅ {label}")
    else:
        _ok = False
        print(f"  ❌ {label}  {detail}")
    return cond


def code_files():
    """扫出所有属于本项目的、会执行的源文件。"""
    out = []
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in files:
            if name == SELF:
                continue
            if name.lower().endswith(CODE_EXT):
                out.append(os.path.join(root, name))
    return sorted(out)


def strip_line_comments(text, ext):
    """剥掉行注释。

    够用就好：这些文件里 `#` / `rem` 不出现在字符串中间的关键位置。
    剥注释是为了「注释里写一句以前写死过什么」不被误判——那种提法是有价值的。
    """
    out = []
    for line in text.splitlines():
        if ext in (".py", ".ps1"):
            i = line.find("#")
            if i != -1:
                line = line[:i]
        elif ext == ".bat":
            s = line.strip().lower()
            if s.startswith("rem ") or s.startswith("rem\t") or s.startswith("::"):
                line = ""
        out.append(line)
    return "\n".join(out)


def has_utf8_bom(path):
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"


def ps1_appdata_user(common, appdata=r"C:\Users\someuser\AppData\Roaming"):
    """真的跑一次 PowerShell，问 `Get-AppDataUserName` 要结果。

    没有 PowerShell（或它起不来）就返回 None，调用方按「跳过」处理——
    不能因为环境缺件就把这份自测判成失败。
    """
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps or not os.path.isfile(common):
        return None
    script = ". '{}'; Get-AppDataUserName -AppData '{}'".format(
        common.replace("'", "''"), appdata.replace("'", "''"))
    try:
        r = subprocess.run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", script],
                           capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or b"").decode("utf-8", "ignore").strip()


def main():
    files = code_files()
    print(f"便携性自测（扫描 {len(files)} 个可执行源文件）")
    print("=" * 64)

    # ── 1 · 会执行的代码里不许出现本机路径 / 用户名 ──────────────────────
    print("── 1 · 不携带本机的绝对路径与用户名 ──")
    hits = []
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        try:
            text = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError as e:
            hits.append((path, 0, f"读不了：{e}"))
            continue
        stripped = strip_line_comments(text, ext)
        for lineno, line in enumerate(stripped.splitlines(), 1):
            for pat, why in FORBIDDEN:
                if re.search(pat, line, re.IGNORECASE):
                    hits.append((os.path.relpath(path, BASE), lineno, f"{why}：{line.strip()[:90]}"))
    check("没有把本机路径/用户名写进可执行代码", not hits,
          "\n      " + "\n      ".join(f"{p}:{n} {w}" for p, n, w in hits[:10]) if hits else "")

    # ── 2 · 微信安装目录必须是探出来的，不是猜死的 ──────────────────────
    print("── 2 · 安装脚本能自己找到微信目录 ──")
    inst_dir = os.path.join(BASE, "installers", "wechat-4.1.10.27")
    common = os.path.join(inst_dir, "_common.ps1")
    check("有共用的定位脚本 _common.ps1", os.path.isfile(common))
    if os.path.isfile(common):
        c = open(common, "r", encoding="utf-8").read()
        check("_common.ps1 提供 Find-Weixin（注册表 + Program Files 两条路）",
              "function Find-Weixin" in c and "Tencent\\Weixin" in c)
        check("_common.ps1 会认「登录用户」而不是提权后的管理员",
              "function Get-LoginUserAppData" in c and "Win32_ComputerSystem" in c)

    do_scripts = [f for f in os.listdir(inst_dir)
                  if f.lower().startswith("do_") and f.lower().endswith(".ps1")] \
        if os.path.isdir(inst_dir) else []
    check(f"8 个 do_*.ps1 都在（实际 {len(do_scripts)} 个）", len(do_scripts) == 8,
          f"找到：{sorted(do_scripts)}")
    bad = []
    for name in sorted(do_scripts):
        t = open(os.path.join(inst_dir, name), "r", encoding="utf-8").read()
        if "_common.ps1" not in t:
            bad.append(name)
    check("每个 do_*.ps1 都点源引入了 _common.ps1", not bad, f"没引入的：{bad}")

    # ── 3 · .ps1 必须带 UTF-8 BOM（PS 5.1 在 GBK 机器上才不乱码）────────
    print("── 3 · PowerShell 脚本带 UTF-8 BOM ──")
    ps1 = [p for p in files if p.lower().endswith(".ps1")]
    no_bom = [os.path.relpath(p, BASE) for p in ps1 if not has_utf8_bom(p)]
    check(f"{len(ps1)} 个 .ps1 全部带 BOM", not no_bom, f"缺 BOM：{no_bom}")

    # ── 4 · 从 AppData 路径里解出的是「用户名」，不是 AppData 这一层 ──────
    print("── 4 · 解析「登录用户名」（cacls 要用它）──")
    got = ps1_appdata_user(common)
    if got is None:
        print("  ⏭  跳过：本机没有可用的 PowerShell")
    else:
        check(f"Get-AppDataUserName 返回用户名（得到 {got!r}）", got == "someuser",
              "往上只退一级会得到 'AppData'——于是「禁用微信自动更新」去拒绝一个"
              "不存在的账户，静默失效")

    # ── 5 · 打包脚本要收齐「代码要用到的目录」──────────────────────────
    print("── 5 · build_package.ps1 的目录清单是不是齐的 ──")
    # 为什么要有这一条（2026-10-04 差点漏出去）：`build_package.ps1` 里那段
    # `foreach ($d in @('docs','tools', ...))` 是**显式清单**。加漏一个目录不会报错，
    # 而是「开发机上好用、发布包里静默失效」—— 这次是 `plugins/`：
    # 少了它，README 里「复制 `plugins/_example.py`」成了死指令，
    # 而 `selftest_plugins.py` 的「模板存在」那条会在**朋友的机器上**失败。
    bp = os.path.join(BASE, "tools", "build_package.ps1")
    if not os.path.isfile(bp):
        check("找得到 tools/build_package.ps1", False, bp)
    else:
        text = open(bp, "r", encoding="utf-8", errors="replace").read()
        m = re.search(r"foreach\s*\(\s*\$d\s+in\s+@\(([^)]*)\)\s*\)", text)
        listed = set()
        if m:
            listed = {s.strip().strip("'\"") for s in m.group(1).split(",") if s.strip()}
        check("build_package.ps1 里有目录清单，且解析得到它", bool(listed), f"解析到：{listed}")

        # 代码真正要用到的目录（相对仓库根）。**新增目录就加到这里** ——
        # 然后上面那份清单忘了加，这份自测就会红。
        need_dirs = {
            "docs": "规格与笔记（README/CLAUDE.md 到处在指它们）",
            "tools": "打包/自测用到的脚本（resize.ps1、office2text.ps1…）",
            "plugins": "插件目录（`_example.py` 模板在这儿，README 让人复制它）",
        }
        missing = sorted(d for d in need_dirs
                         if os.path.isdir(os.path.join(BASE, d)) and d not in listed)
        check("每个代码要用到的目录都在打包清单里", not missing,
              "漏了就会「开发机好用、发布包静默失效」：" +
              "；".join(f"{d}（{need_dirs[d]}）" for d in missing))

        # 反向：清单里写了、仓库里却没有的目录（无害，但说明清单过时了）
        stale = sorted(d for d in listed if not os.path.isdir(os.path.join(BASE, d)))
        if stale:
            print(f"  ℹ️  清单里有仓库里不存在的目录（只是提示）：{stale}")

    print("=" * 64)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
