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
# / 第三方源码快照（别人仓库的东西，不归我们管，也不该改）：
#   * `src-4.1.10.27` —— 随包携带的 WeChat-Hook 源码；
#   * `searxng`       —— 2026-10-05 起随包携带的搜索后端（1000+ 个 .py）。
# 第三方树整棵跳过是**必须的**：它们是别人写的东西，里面出现本机盘符/用户名是他们的自由，
# 拿我们的「不许写死本机路径」去查它们，只会在用户的机器上报一条假失败
# （本项目修过同类坑：包里少放一个文件就让用户看到假失败）。
SKIP_DIR_NAMES = {".venv", "__pycache__", "data", "dist", "test_images", ".git",
                  "src-4.1.10.27", "searxng"}
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
    """扫出所有属于**本项目**的、会执行的源文件。

    ⚠️ `plugins/` 里放的是**用户自己写的插件**（2026-10-04 加的插件契约）——
    他在自己的插件里写自己机器的路径完全合理，拿它去查「有没有本机路径」会在
    **他的电脑上**报一条假失败。本项目修过同类坑（包里少放 `config.example.yaml`
    导致用户跑自测看到假失败），所以这里只扫 `plugins/_*.py`：`_` 开头的是
    **我们自己的模板**（也正好是加载器**不会加载**的那些）。
    """
    out = []
    for root, dirs, files in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        rel = os.path.relpath(root, BASE)
        in_user_plugins = rel.split(os.sep)[0] == "plugins" and rel != "."
        for name in files:
            if name == SELF:
                continue
            if in_user_plugins and not name.startswith("_"):
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
    # 部署/卸载脚本的**集合**（2026-10-06：加「只替换 version.dll」的 do_fix_hook.ps1 后为 10 个）。
    # 这条钉的是「别把随包脚本弄丢」；个数变化时**必须回来一起改**，
    # 免得新增一个脚本忘了带 BOM/不引入 _common.ps1。
    EXPECTED_DO_SCRIPTS = 10
    check(f"{EXPECTED_DO_SCRIPTS} 个 do_*.ps1 都在（实际 {len(do_scripts)} 个）",
          len(do_scripts) == EXPECTED_DO_SCRIPTS, f"找到：{sorted(do_scripts)}")
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
            "searxng": "随包携带的搜索后端（少了它，别人机器上 web_search 永远用不了）",
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

    # ── 6 · 装 hook 之前必须校验微信版本 ────────────────────────────────
    print("── 6 · 装 hook 前会校验微信版本 ──")
    # 为什么有这一条（2026-10-04 真机）：另一台电脑的微信是 4.1.15.13，[9] 一键配置把
    # version.dll 放进了微信目录、hook-install-log.txt 写着「已放置，SHA256 = …」，
    # 看着全部成功，但 30001 从来没有被监听 —— hook 是按 4.1.10.27 的函数偏移编译的，
    # 版本不对时 DLL 被**正常加载**却挂钩失败、**不报错**。用户只看到 bot 一直
    # 「连不上 30001」。所以「装 hook」之前必须先过版本闸，而且**两级都要有**：
    # .ps1 里那级管手敲命令的人，控制台那级才能顺手把版本换对。
    common_txt = open(common, "r", encoding="utf-8").read() if os.path.isfile(common) else ""
    check("_common.ps1 里有唯一一份目标微信版本常量",
          "$WX_WANTED_VERSION" in common_txt)
    check("_common.ps1 提供 Test-WeixinVersion（区分 mismatch 与 unknown）",
          "function Test-WeixinVersion" in common_txt and "'unknown'" in common_txt)
    hook_txt = ""
    hook = os.path.join(inst_dir, "do_hook_install.ps1")
    if os.path.isfile(hook):
        hook_txt = open(hook, "r", encoding="utf-8").read()
    check("do_hook_install.ps1 放 DLL 之前过版本闸", "Test-WeixinVersion" in hook_txt)
    check("版本不对时**根本不放 DLL**（明确退出，而不是继续放）",
          "version mismatch" in hook_txt and "exit 2" in hook_txt,
          "只警告不拦 = 用户仍然会看到「已放置，成功」这种假成功")

    # Python 控制台与 .ps1 必须是**同一个版本号**（两处写死的字面量，只能靠这条对齐）
    m_ps = re.search(r"\$WX_WANTED_VERSION\s*=\s*'([^']+)'", common_txt)
    cons_path = os.path.join(BASE, "console.py")
    cons_txt = open(cons_path, "r", encoding="utf-8").read() if os.path.isfile(cons_path) else ""
    m_py = re.search(r'WANTED_WEIXIN\s*=\s*"([^"]+)"', cons_txt)
    check("console.py 与 _common.ps1 的目标微信版本一致",
          bool(m_ps and m_py and m_ps.group(1) == m_py.group(1)),
          f"ps1={m_ps.group(1) if m_ps else None!r} py={m_py.group(1) if m_py else None!r}")
    check("一键配置真的会去装 4.1.10.27（不是只打印一句提醒）",
          "ensure_weixin_version" in cons_txt and 'run_ps1("do_install.ps1")' in cons_txt)

    # 包里那份 version.dll 必须是**带登录门禁**的构建。厂商原版（483840 / 5ABB5002）是
    # 2026-10-04 真机事故的根因之一：它的源码快照里 `g_IsLogin` 恒为 0，装上去的现象是
    # 「hook 通了、查询全失败、IsLogin 恒 0」，而日志一切正常。判据用编译进二进制的
    # 宽字符串（`xwechat_files` / `db_storage` 只有「找新鲜库」那套判据才会用到）。
    dll = os.path.join(inst_dir, "version.dll")
    gated = False
    if os.path.isfile(dll):
        raw = open(dll, "rb").read()
        gated = (("xwechat_files".encode("utf-16-le") in raw)
                 and ("db_storage".encode("utf-16-le") in raw))
    check("包里的 version.dll 是带登录门禁的构建（不是厂商原版）", gated,
          "厂商原版没有这套判据 → 装上去 IsLogin 恒 0、查询全失败。"
          "别拿 version_old_backup.dll 覆盖它")

    # ── 7 · 给别的电脑装：只有「一个 .bat」这一条路 ─────────────────────
    print("── 7 · 一键部署入口（一个 .bat）──")
    # 为什么有这条（2026-10-04）：用户口径是「部署在别的电脑上，用一个 .bat」。
    # 它必须是个**薄壳**：真流程全在 console.py 的 first_run() 里。
    # 从这里抄一份出去的那天起就会分叉——这个项目被「两份实现只改了一份」咬过好几次
    # （send_asset 的模型指导、`_wechat_save_roots` 与 C++ 侧判据都是）。
    deploy = os.path.join(BASE, "一键部署.bat")
    dtxt = open(deploy, "r", encoding="utf-8", errors="replace").read() \
        if os.path.isfile(deploy) else ""
    check("一键部署.bat 在包里（双击就装完）", os.path.isfile(deploy))
    check("它是薄壳：只调 console.py first", "console.py first" in dtxt)
    copied = [k for k in ("do_hook_install", "do_install", "pip install", "WeChatWin_")
              if k in dtxt]
    check("它没有把部署流程抄第二份", not copied, f"抄了：{copied}")
    bp_txt = open(bp, "r", encoding="utf-8", errors="replace").read() \
        if os.path.isfile(bp) else ""
    check("打包脚本的「从这里开始.txt」首推它",
          "一键部署.bat" in bp_txt and "助手.bat**，按 **[9]" not in bp_txt)

    # ── 7b · 诊断工具必须随包走（2026-10-06 真机踩出来的）──────────────────
    print("── 7b · 出问题时有工具可用：hook_doctor 随包 ──")
    # 为什么钉这一条：真机上用户拿到新包却起不来（微信里那份 hook 是旧的），
    # 而**包里没有任何诊断工具**——诊断脚本当时只存在于开发机的 `_audit\`（打包时被排除）。
    # 于是只能靠来回猜。规矩：诊断工具必须在包根，且它能 import 到判据模块 hook_check。
    doctor_py = os.path.join(BASE, "tools", "hook_doctor.py")
    hc_py = os.path.join(BASE, "hook_check.py")
    check("tools/hook_doctor.py 存在（hook/闸门的一站式诊断）", os.path.isfile(doctor_py))
    check("hook_check.py 存在（安装目录与包内 DLL 的判据唯一真源）", os.path.isfile(hc_py))
    check("打包脚本会把 hook_doctor.py 放到**包根**（用户不用 cd 进 tools）",
          "hook_doctor.py" in bp_txt and "$pkg 'hook_doctor.py'" in bp_txt)
    # 判据逻辑只能有一份：doctor 必须 import hook_check，而不是自己再写一遍找目录/比哈希
    doc_txt = open(doctor_py, "r", encoding="utf-8").read() if os.path.isfile(doctor_py) else ""
    check("doctor 复用 hook_check（不另写第二份判据）",
          "import hook_check" in doc_txt)
    check("「从这里开始.txt」里首推先跑 doctor（排错第一站）",
          "hook_doctor.py" in bp_txt and "先跑" in bp_txt)

    # ── 8 · .bat 必须是纯 ASCII ────────────────────────────────────────
    print("── 8 · .bat 全是纯 ASCII（GBK 控制台才不会乱码）──")
    # 为什么（2026-10-04，写 `一键部署.bat` 时当场踩到）：cmd 按系统代码页（中文机是 936）
    # 读 .bat，一个带 UTF-8 中文的脚本在控制台上是乱码。四个老 .bat 都是刻意写成纯 ASCII 的，
    # 新加的也必须守着——这条自测就是那次踩坑的回归。
    bats = [p for p in files if p.lower().endswith(".bat")]
    dirty = []
    for p in bats:
        n = sum(1 for byte in open(p, "rb").read() if byte > 127)
        if n:
            dirty.append(f"{os.path.relpath(p, BASE)}({n} 字节)")
    check(f"{len(bats)} 个 .bat 全是纯 ASCII", not dirty, f"含非 ASCII：{dirty}")

    print("=" * 64)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
