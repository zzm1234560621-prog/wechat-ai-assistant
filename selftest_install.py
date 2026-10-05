"""安装/环境链路的回归自测：依赖清单单一真源、控制台提权命令、版本探测、绕过更新。

跑：.venv/Scripts/python.exe selftest_install.py

不联网、不真的安装、不碰 30001、不改任何注册表、不执行降级。
覆盖的是 2026 年那次外部审计报出来的 4 个必修项：

  T1 官方安装路径装不全依赖（requirements.txt 里的 pypdf 从来没被装过）
  T2 console.py 管理员分支必然参数绑定失败（-ArgumentList '' 是空串）
  T3 bypass_update.py 对微信 4.x 静默无效（缺 xwechat 路径、找不到还退出码 0）
  T4 wechat_version.py 把路径拼进 PowerShell 单引号串（含 ' 就坏 / 可注入）
  T5 装 hook 前的微信版本闸：别的版本上 hook 会**静默失效**（2026-10-04 真机事故）

风格照抄 selftest_aixed.py / selftest_executor_chain.py：ok/FAIL + 结尾汇总 + 失败 sys.exit(1)。
"""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bypass_update
import console
import envsetup as env
import settings
import wechat_version as wv

_FAIL = []
REQ = os.path.join(HERE, "requirements.txt")


def chk(cond, msg):
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        _FAIL.append(msg)


def grab(fn, *a, **kw):
    """跑一个函数并捕获它的 stdout（用来断言「明确报错」而不是静默）。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = fn(*a, **kw)
    return ret, buf.getvalue()


def load_installer():
    """加载 installer.py。

    installer.py 里 `import envsetup as env` 指的是**顶层 envsetup.py**，
    所以先把它塞进 sys.modules，installer 就不会再去解析（也就不会真去建 venv）。
    """
    sys.modules.setdefault("envsetup", env)
    spec = importlib.util.spec_from_file_location("installer_under_test",
                                                 os.path.join(HERE, "installer.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 从需求串里拆出原生 argv（只用于断言，不是产品代码） ──────────────
def split_args(s):
    out, cur, in_q = [], [], False
    for ch in s:
        if ch == '"':
            in_q = not in_q
            cur.append(ch)
        elif ch == " " and not in_q:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [a[1:-1].replace('\\"', '"') if a.startswith('"') and a.endswith('"') else a
            for a in out]


def cmd_to_native(cmd):
    """从 build_admin_command 生成的命令行里反解出 -FilePath / -ArgumentList。"""
    spans = [(m.start(), m.end(), m.group(1))
             for m in re.finditer(r"'((?:[^']|'')*)'", cmd)]
    tokens = []
    i = 0
    while i < len(cmd):
        if cmd[i] == "'":
            for s, e, val in spans:
                if s == i:
                    tokens.append(val.replace("''", "'"))
                    i = e
                    break
            else:
                tokens.append("'")
                i += 1
        elif cmd[i] == " ":
            i += 1
        else:
            j = i
            while j < len(cmd) and cmd[j] not in " '":
                j += 1
            tokens.append(cmd[i:j])
            i = j
    fpath = argv = workdir = None
    verb = False
    for k, t in enumerate(tokens):
        if t == "-FilePath" and k + 1 < len(tokens):
            fpath = tokens[k + 1]
        elif t == "-ArgumentList" and k + 1 < len(tokens):
            argv = split_args(tokens[k + 1])
        elif t == "-WorkingDirectory" and k + 1 < len(tokens):
            workdir = tokens[k + 1]
        elif t == "-Verb" and k + 1 < len(tokens):
            verb = tokens[k + 1] == "RunAs"
    return fpath, argv, workdir, verb


def _no_elevation(line):
    """把 `-Verb RunAs` 去掉再执行。

    这几条验的是**参数绑定 / 参数有没有原样送到子进程**，跟提权无关；而真去执行提权那条
    会**弹 UAC 对话框等人点** —— 在自动化跑的机器上必然卡满 60 秒超时，全量自测就会偶发变红
    （2026-10-02 实测抓到的就是这个：单跑有时过、全量跑有时挂，看着像"偶发"，其实是等 UAC）。
    提权本身不靠执行验证：那些用 `build_admin_command` 的字符串断言 `-Verb RunAs` 就够。
    """
    return line.replace(" -Verb RunAs", "")


def main():
    print("=" * 60)
    print("安装 / 环境链路自测（不联网、不安装、不碰微信、不改注册表）")
    print("=" * 60)

    # ── T1 依赖清单单一真源 ────────────────────────────────────────────
    print("\n1) T1 依赖清单以 requirements.txt 为唯一真源")
    specs_file = env.requirements_specs()          # 默认读的就是 requirements.txt
    direct = env.requirements_specs(REQ)
    names_file = [env.requirement_name(s) for s in specs_file]
    lower = [n.lower() for n in names_file]
    chk(specs_file == direct, "requirements_specs() 默认读的就是项目根 requirements.txt")
    chk("pypdf" in lower, "requirements.txt 里有 pypdf（读收到的 PDF 要用）")
    chk("wcferry" in lower, "requirements.txt 里有 wcferry")
    chk(all(s and not s.startswith("#") for s in specs_file),
        f"注释/空行都被过滤掉（解析出 {len(specs_file)} 条：{names_file}）")
    chk("yaml" in env.REQUIRED_PKGS, "REQUIRED_PKGS 含 yaml（PyYAML 的导入名）")
    chk("pypdf" in env.REQUIRED_PKGS, "★ REQUIRED_PKGS 含 pypdf（审计报的漏项）")
    chk("wcferry" in env.REQUIRED_PKGS, "REQUIRED_PKGS 仍含 wcferry（wcferry 后端要它）")
    chk(set(env.REQUIRED_PKGS) == set(env.required_import_names()),
        "REQUIRED_PKGS 等于「从 requirements.txt 派生」的结果（不是手写名单）")
    chk(env.required_import_names(REQ) == list(env.REQUIRED_PKGS),
        "★ 直接解析 requirements.txt 的结果与 REQUIRED_PKGS 完全一致（单一真源的硬证据）")
    chk("PyYAML" not in env.REQUIRED_PKGS and "yaml" in env.REQUIRED_PKGS,
        "PyYAML 映射成 import 名 yaml（find_spec 查的是 yaml）")

    # requirements.txt 里的 wcferry 说明要讲清「只有 3.9.x 后端才需要」
    raw = open(REQ, "r", encoding="utf-8").read()
    chk("只有走 wcferry 后端" in raw and "aixed" in raw,
        "requirements.txt 的 wcferry 注释说清「只有 wcferry(3.9.x) 后端才需要，主线 4.x+aixed 不需要」")
    chk("3.9.12.51" in raw and "39.5.2" in raw and "39.4.4" in raw,
        "requirements.txt 写明 wcferry 与微信版本的对应表（和 wechat_version.py 对齐）")

    # 空清单不许静默通过
    empty = os.path.join(tempfile.gettempdir(), "_selftest_empty_reqs.txt")
    open(empty, "w", encoding="utf-8").write("# 只有注释\n\n")
    chk(env.requirements_specs(empty) == [], "全是注释的清单解析成空列表（不报错、不臆造）")

    # wcferry 只在 wcferry 后端必需：否则「wcferry 装不上」会把 4.x 主线整个卡住
    print("\n1b) wcferry 只在 wcferry 后端必需")
    aixed_pkgs = env.required_pkgs("aixed")
    wcf_pkgs = env.required_pkgs("wcferry")
    chk("wcferry" not in aixed_pkgs, "★ aixed 主线下 wcferry 不算必需（装不上也不挡启动）")
    chk("pypdf" in aixed_pkgs and "yaml" in aixed_pkgs and "anthropic" in aixed_pkgs,
        f"aixed 主线仍然必需 anthropic / yaml / pypdf：{aixed_pkgs}")
    chk(set(wcf_pkgs) == set(env.REQUIRED_PKGS), "wcferry 后端下必需集合 = REQUIREMENTS 全量")
    chk(set(aixed_pkgs) <= set(env.REQUIRED_PKGS), "过滤只做减法：不会凭空多出包里没有的依赖")
    chk(env.backend_from_config() in ("aixed", "wcferry"),
        f"能从 config.yaml 读出 backend：{env.backend_from_config()}")

    # ── T1b installer 的依赖清单 ──────────────────────────────────────
    print("\n2) T1b installer.py 读 requirements.txt 来装（不执行安装）")
    inst = load_installer()
    wcfer = "39.5.2"
    pip_specs = inst.build_pip_specs(wcfer)
    pip_names = [env.requirement_name(s) for s in pip_specs]
    chk(len(pip_specs) == len(specs_file),
        f"装的需求条目数与 requirements.txt 相同（{len(pip_specs)} 条）")
    chk(set(pip_names) == set(names_file), "包集合与 requirements.txt 完全一致（没漏 pypdf、没多塞）")
    chk(f"wcferry=={wcfer}" in pip_specs, f"wcferry 版本被覆盖成 =={wcfer}（保持原逻辑）")
    chk(all(s == p for s, p in zip(pip_specs, specs_file) if "wcferry" not in s.lower()),
        "除 wcferry 外，其余需求串一字不动地来自 requirements.txt")
    chk("pypdf>=4.0" in pip_specs, "★ 安装清单里有 pypdf>=4.0")

    # requirements.txt 被清空时必须明确报错，不许静默装个空单
    real_req = env.REQUIREMENTS_TXT
    try:
        env.REQUIREMENTS_TXT = empty
        err = ""
        try:
            inst.build_pip_specs(wcfer)
        except RuntimeError as e:
            err = str(e)
        chk("安装无法继续" in err, "requirements.txt 空/坏时 build_pip_specs 明确报错（不静默）")
    finally:
        env.REQUIREMENTS_TXT = real_req

    # ── T1c 启动助手.bat 模板 ────────────────────────────────────────
    print("\n3) T1c installer 生成的 启动助手.bat 自检清单含全部依赖")
    text = inst.launcher_text()
    chk("{pkg_repr}" not in text, "模板占位符已被替换")
    chk("'pypdf'" in text, "★ 启动脚本的自检清单里有 pypdf（漏了就会「缺依赖还照跑」）")
    # ⚠️ 自检清单必须**按当前后端过滤**：4.x 主线故意不装 wcferry，清单里若还写着它，
    # 自检永远失败 → 每次启动都去装依赖 → 永远进不了 bot。
    _backend = env.backend_from_config() or "aixed"
    for m in env.required_pkgs(_backend):
        chk(f"'{m}'" in text, f"启动脚本自检清单含 {m}（后端 {_backend} 下必需的）")
    chk(("'wcferry'" in text) == (_backend == "wcferry"),
        f"★ 自检清单里的 wcferry 与后端匹配（当前后端 {_backend}）："
        f"4.x 主线**必须不含** wcferry，否则全新机器「装完还是起不来」")
    chk("bot.py" in text and "install.bat" in text, "启动脚本仍然是「缺依赖自动装 + 起 bot」")
    real = open(os.path.join(HERE, "启动助手.bat"), "r", encoding="utf-8").read()
    chk([l.rstrip("\r") for l in real.splitlines()] ==
        [l.rstrip("\r") for l in text.splitlines()],
        "★ 仓库里现成的 启动助手.bat 与模板渲染结果逐行一致（未漂移）")
    chk(real == text, "两份内容连行尾也一致（模板 newline='\\r\\n'）")

    # install.bat 里不应有第二份依赖清单（它只负责挑解释器然后交给 installer.py）
    bat = open(os.path.join(HERE, "install.bat"), "r", encoding="utf-8").read()
    chk("installer.py" in bat and "py -%%V" in bat,
        "install.bat 只负责挑解释器 → 转给 installer.py")
    chk(not re.search(r"pip install", bat) and not re.search(r"^(anthropic|PyYAML|setuptools|pypdf)",
                                                             bat, re.M),
        "★ install.bat 里没有第二份依赖清单（没有 pip install、没有包名行）")

    # ── T2 console.py 提权命令 ────────────────────────────────────────
    print("\n4) T2 console.py 管理员分支的命令拼接")
    line = console.build_admin_command("downgrade.py", None)
    chk("-ArgumentList" not in line or "-ArgumentList ''" not in line,
        "★ 无参数时不再出现 `-ArgumentList ''`（原来 PowerShell 直接拒收）")
    chk("-Verb RunAs" in line, "仍然只用 -Verb RunAs 提权（没换别的方式）")
    fp, argv, wd, verb = cmd_to_native(line)
    chk(fp == sys.executable, f"-FilePath 是本解释器：{fp}")
    chk(argv == ["downgrade.py"], f"无参数时 ArgumentList 就是脚本名：{argv}")
    chk(wd == HERE, f"-WorkingDirectory 是项目根：{wd}")
    chk(verb, "-Verb 解析出来是 RunAs")

    line2 = console.build_admin_command("autostart.py", ["on"])
    _, argv2, _, _ = cmd_to_native(line2)
    chk(argv2 == ["autostart.py", "on"], f"带参数时逐个传：{argv2}")

    # ── T2b 提权跑 .ps1（hook 那几个脚本）────────────────────────────
    # ⚠️ .ps1 **不能**走 build_admin_command：那个是 `-FilePath <python> '<script>'`，
    #    等于让 python 去解释 PowerShell，必然失败。所以单独立了一条函数。
    psline = console.build_ps1_admin_command(r"C:\a b\installers\do_hook_install.ps1")
    chk("-Verb RunAs" in psline, "ps1 也用 -Verb RunAs 提权")
    chk("powershell.exe" in psline, f".ps1 走 powershell 而不是 python：{psline[:60]}")
    chk("python" not in psline.split("-Verb")[0].lower().replace("powershell", ""),
        "命令行里不许出现用 python 跑 ps1 的写法")
    chk("'-File'" in psline or "-File" in psline, f"要用 -File 传脚本路径：{psline[:90]}")
    # 路径含空格时必须是**一个**单引号字面量（不能被拆开）
    chk("'C:\\a b\\installers\\do_hook_install.ps1'" in psline,
        f"含空格的路径整体包成单引号字面量：{psline}")

    pretty = r"C:\Program Files\a b\downgrade.py"
    line3 = console.build_admin_command(pretty, ["a b", "it's"])
    chk("''" in line3, "参数里的单引号被 PowerShell 单引号串规则转义成 ''")
    fp3, argv3, _, _ = cmd_to_native(line3)
    chk(fp3 == sys.executable,
        "脚本路径不是 -FilePath（-FilePath 永远是解释器，脚本是第一个参数）")
    chk(argv3 == [pretty, "a b", "it's"],
        f"★ ArgumentList 里含空格的脚本路径与含单引号的参数都还是独立一项：{argv3}")

    # 脚本路径里带单引号：'' 转义要能原样还原
    quote_path = r"C:\Program Files\a b\it's\downgrade.py"
    _, argv_q, _, _ = cmd_to_native(console.build_admin_command(quote_path, []))
    chk(argv_q == [quote_path], f"脚本路径含单引号也能原样还原：{argv_q}")

    # 真让 PowerShell 解析（只解析不执行；不碰降级、不提权）
    env2 = dict(os.environ)
    env2["WX_SELFTEST_CMD"] = line
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "[void][scriptblock]::Create($env:WX_SELFTEST_CMD); Write-Output PARSE_OK"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env2, timeout=60)
    chk(r.returncode == 0 and "PARSE_OK" in (r.stdout or ""),
        f"★ PowerShell 能解析这条命令（rc={r.returncode}）")

    # 空参数这条是审计实测的复现点：确认「空 ArgumentList」确实会被 PowerShell 拒掉
    bad = "Start-Process -FilePath 'cmd.exe' -ArgumentList '' -Verb RunAs"
    env3 = dict(os.environ)
    env3["WX_SELFTEST_CMD"] = bad
    r_bad = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$ErrorActionPreference='Stop'; try { [void][scriptblock]::Create($env:WX_SELFTEST_CMD);"
         " Write-Output PARSE_OK } catch { Write-Output ('PARSE_FAIL: ' + $_.Exception.Message) }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env3, timeout=60)
    chk("PARSE_FAIL" not in (r_bad.stdout or ""),
        "语法层面 `-ArgumentList ''` 能通过（所以必须靠参数校验，见下条）")

    # 等价绑定验证：-FilePath 换成无害的自写「脚本」，只看 Start-Process 的参数校验过不过、
    # 以及参数到底有没有原样送到子进程（不含 RunAs，不提权，不执行任何真流程）。
    cmdexe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    work = tempfile.mkdtemp(prefix="wx selftest console ")   # 名字故意带空格
    try:
        safe = console.build_admin_command("/c", ["exit", "0"], python=cmdexe, base=work)
        r2 = subprocess.run(["powershell", "-NoProfile", "-Command", _no_elevation(safe)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
        err = r2.stderr or ""
        chk("ParameterBindingException" not in err and "Cannot validate argument" not in err,
            f"★ 等价验证（cmd.exe /c exit 0，无害）参数绑定通过：rc={r2.returncode}")
        r3 = subprocess.run(["powershell", "-NoProfile", "-Command", _no_elevation(bad)],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=60)
        chk("ArgumentList" in (r3.stderr or ""),
            "对照组：旧写法 `-ArgumentList ''` 确实被 PowerShell 报 ArgumentList 错（复现审计结论）")

        # 参数送达验证：worker 把收到的 argv 写成 JSON，Start-Process 是异步的所以轮询等它
        worker = os.path.join(work, "argv_worker.py")
        with open(worker, "w", encoding="utf-8") as f:
            f.write("import json,sys\n"
                    "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(sys.argv[2:]))\n")
        for label, extra in (("普通参数", ["one", "two"]),
                             ("带空格与单引号的参数", ["a b", "it's"])):
            out_json = os.path.join(work, "argv.json")
            if os.path.exists(out_json):
                os.remove(out_json)
            line_ok = console.build_admin_command(worker, [out_json] + extra,
                                                 python=sys.executable, base=work)
            subprocess.run(["powershell", "-NoProfile", "-Command", _no_elevation(line_ok)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
            got = None
            for _ in range(100):                      # 最多等 ~20 秒
                if os.path.exists(out_json):
                    try:
                        import json
                        got = json.load(open(out_json, encoding="utf-8"))
                        break
                    except ValueError:
                        pass
                import time
                time.sleep(0.2)
            chk(got == extra, f"★ 参数原样送达子进程（{label}）：{got}")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)

    # ── T3 bypass_update ──────────────────────────────────────────────
    print("\n5) T3 bypass_update.py：4.x 路径 / 非零退出码 / 只对当前用户")
    cands = bypass_update.candidate_paths()
    chk(any("xwechat" in p for p in cands), "★ 候选路径含 Tencent\\xwechat\\Weixin.exe（微信 4.x 实际路径）")
    chk(any(p.endswith(r"Tencent\Weixin\Weixin.exe") for p in cands), "候选路径也含 Tencent\\Weixin\\Weixin.exe")
    chk(any(p.endswith(r"Tencent\WeChat\WeChat.exe") for p in cands), "3.9.x 的 WeChat.exe 路径保留")
    chk(len(cands) == len(set(cands)), "候选路径无重复")

    real_paths = bypass_update.COMMON_PATHS
    fake = os.path.join(tempfile.gettempdir(), "wx-selftest-no-such-dir") + r"\nope.exe"
    try:
        bypass_update.COMMON_PATHS = [fake]
        ret, out = grab(bypass_update.find_wechat, [fake], False)
        chk(ret is None, "★ find_wechat() 找不到时不抛异常、返回 None（不看 PATH、没副作用）")
        chk(out == "", "find_wechat() 自己不打印（打印交给调用方，便于复用）")
        chk(len(bypass_update.candidate_paths()) > 1,
            "candidate_paths() 还会从 ProgramFiles 环境变量补充候选（不只是硬编码表）")
        chk(bypass_update.find_wechat([fake], False) is None
            and bypass_update.find_wechat([__file__], False) == __file__,
            "find_wechat(路径表) 是真的按「文件在不在」判断")

        for fn, label in ((bypass_update.on, "on"), (bypass_update.off, "off"),
                          (bypass_update.status, "status")):
            ret2, out2 = grab(fn, [fake], False)
            chk(ret2 is False and "未找到微信主程序" in out2,
                f"{label}() 找不到微信时明确报错并返回 False（不是静默成功）")
            chk("AppCompatFlags" in out2 and "新建字符串值" in out2,
                f"{label}() 的提示里给了可操作建议（手改注册表的位置）")

        argv_backup = sys.argv
        find_backup = bypass_update.find_wechat
        try:
            # main() 不接受注入参数，这里直接把探测函数换掉（等价于这台机器上没装微信）
            bypass_update.find_wechat = lambda *a, **k: None
            sys.argv = ["bypass_update.py", "on"]
            code, out3 = grab(bypass_update.main)
            chk(code == 1, "★ main() 在找不到微信时返回退出码 1（原来恒为 0）")
            chk("退出码 1" in out3, "main() 明说了退出码 1")
            sys.argv = ["bypass_update.py", "status"]
            code_s, out_s = grab(bypass_update.main)
            chk(code_s == 1 and "退出码 1" in out_s,
                "status 在找不到微信时也不是「假装成功」")
            sys.argv = ["bypass_update.py", "在看吗"]
            code2, out4 = grab(bypass_update.main)
            chk(code2 == 1 and "未知参数" in out4, "未知参数也明确报错 + 退出码 1")
        finally:
            bypass_update.find_wechat = find_backup
            sys.argv = argv_backup
    finally:
        bypass_update.COMMON_PATHS = real_paths

    src = open(os.path.join(HERE, "bypass_update.py"), "r", encoding="utf-8").read()
    chk("HKEY_LOCAL_MACHINE" in src and "不动 HKEY_LOCAL_MACHINE" in src,
        "注释里写明只写 HKCU、不动 HKLM")
    chk("只对当前用户生效" in src, "输出/注释里说清「只对当前用户生效」")

    # ── T4 wechat_version 取文件版本 ──────────────────────────────────
    print("\n6) T4 wechat_version.py：不拼字符串、路径含空格/单引号都能用")
    p_plain = r"C:\Program Files\Tencent\Weixin\Weixin.exe"
    p_quote = r"C:\Program Files\Wei'xin\Weixin.exe"
    p_cn = "C:\\程序 文件\\微信 目录\\Weixin.exe"
    argv_p = wv._file_version_cmd(p_plain)
    chk(argv_p[0] == "powershell" and argv_p[1] == "-NoProfile", "用的是 powershell")
    chk("-EncodedCommand" in argv_p, "脚本走 -EncodedCommand（命令行里看不到路径、无法被解析坏）")
    chk(argv_p[-1] == wv._PS_ENC_CMD, "路径不在命令行里，最后一个参数就是编码后的脚本")
    chk(all(p_plain not in a and p_quote not in a and p_cn not in a for a in argv_p),
        "★ argv 里任何一项都不含路径原文（不做字符串拼接）")
    for a in argv_p:
        chk(a.isascii(), f"argv 项全是 ASCII，cmd 解析不会碰多字节字符：{a[:24]}…")
    chk(wv._file_version_cmd(p_plain) == wv._file_version_cmd(p_quote) == wv._file_version_cmd(p_cn),
        "三个不同路径构造出的 argv 完全一样（路径根本不进命令行）")
    chk("LiteralPath" in wv._PS_SCRIPT and "$env:" in wv._PS_SCRIPT,
        "脚本用 Get-Item -LiteralPath $env:WX_VER_PATH（-LiteralPath 不做通配符展开）")

    # 不存在 -> 返回 None 且打印原因
    ret, out = grab(wv._file_version, r"C:\这个目录不存在\nope.exe")
    chk(ret is None, "不存在的路径返回 None（返回语义不变）")
    chk("文件不存在" in out, "★ 并且打印了明确原因（不是静默 None）")

    # 存在但 powershell 失败 -> 返回 None 且打印 stderr
    class _R:
        returncode = 1
        stdout = ""
        stderr = "At line:1 报错啦"
    real_exists, real_run = os.path.exists, subprocess.run
    seen = {}
    try:
        os.path.exists = lambda p: True
        subprocess.run = lambda argv, **kw: (seen.update(argv=argv, env=kw.get("env") or {}),
                                             _R())[1]
        ret2, out2 = grab(wv._file_version, p_quote)
    finally:
        os.path.exists, subprocess.run = real_exists, real_run
    chk(ret2 is None and "powershell 退出码 1" in out2,
        "powershell 失败时返回 None 并打印退出码（原来异常被吞成静默 None）")
    chk(seen["env"].get("WX_VER_PATH") == p_quote,
        f"★ 路径是通过环境变量 {wv._PS_PATH_ENV} 传进去的，值原样（含单引号也不变形）")
    chk(seen["argv"] == wv._file_version_cmd(p_quote), "真正跑的就是 _file_version_cmd 构造的 argv")

    # 文件存在时真实跑一次（不碰微信进程，只读一个文件版本号）
    if os.path.exists(p_plain):
        v = wv._file_version(p_plain)
        chk(isinstance(v, str) and v.count(".") >= 2,
            f"真实安装的 Weixin.exe 能读出文件版本：{v!r}")

    # ── T5 装 hook 前的微信版本闸（2026-10-04 真机事故）─────────────────
    # 事故形态：另一台电脑微信是 4.1.15.13，[9] 一键配置把 version.dll 放进微信目录、
    # 日志写着「已放置，SHA256 = …」，一切看着成功，但 30001 从没被监听 —— hook 是按
    # 4.1.10.27 的函数偏移编译的，版本不对时 DLL 被正常加载却挂钩失败，**不报错**。
    print("\n5) T5 装 hook 前的微信版本闸")
    chk(console.WANTED_WEIXIN == "4.1.10.27",
        f"目标版本常量是 4.1.10.27（实际 {console.WANTED_WEIXIN!r}）")
    chk(console.weixin_version_action("4.1.10.27") == "ok", "目标版本 -> ok")
    chk(console.weixin_version_action("  4.1.10.27 ") == "ok", "两侧空白也认（注册表/文件版本都可能带）")
    chk(console.weixin_version_action("4.1.10.27.0") == "downgrade",
        "★ 多一位就按 mismatch 处理（宁可多问一次，也不静默装到不匹配的版本上）")
    chk(console.weixin_version_action("4.1.15.13") == "downgrade", "别的版本 -> downgrade")
    chk(console.weixin_version_action("") == "unknown", "读不出版本 -> unknown")
    chk(console.weixin_version_action(None) == "unknown",
        "★ None 也是 unknown，**不许当成 mismatch**（读不出不等于版本不对，"
        "一律拦会挡住本来能装的机器）")

    # 安装日志读回：提权窗口的输出回不来，「装上了」只能靠日志里的两个事实
    log_ok = ("=== 2026-10-04 23:00:00 ===\n"
              "[3] 静默安装 WeChatWin_4.1.10.27.exe /S\n"
              "  exit code: 0\n"
              "[5] 主程序版本\n"
              "  Weixin.exe ProductVersion = 4.1.10.27\n"
              "=== DONE ===")
    chk(console.parse_install_log(log_ok) == (0, "4.1.10.27"), "读回 (exit code 0, 4.1.10.27)")
    chk(console.parse_install_log("  exit code: 0\n  Weixin.exe ProductVersion = 4.1.15.13")
        == (0, "4.1.15.13"),
        "★ 退出码 0 但版本没换 —— 调用方必须判失败（只看退出码就是假成功）")
    chk(console.parse_install_log("") == (None, None),
        "★ 日志读不出来时返回 (None, None)，不许当成功")

    # 等安装完成：run_ps1 是「拉起新窗口就返回」的异步动作，而安装要几十秒 ——
    # 立刻读日志必然读到旧的/写了一半的那份，于是「还在装」被误报成「没换成」。
    with tempfile.TemporaryDirectory() as td:
        real_hook = console.HOOK_DIR
        try:
            console.HOOK_DIR = td
            logp = os.path.join(td, "install-log.txt")
            with open(logp, "w", encoding="utf-8") as f:
                f.write("=== x ===\n[3] 静默安装 WeChatWin_4.1.10.27.exe /S\n"
                        "  exit code: 0\n[5] 主程序版本\n"
                        "  Weixin.exe ProductVersion = 4.1.10.27\n=== DONE ===\n")
            got = console._wait_install_done(0, timeout=5)
            chk(bool(got) and "=== DONE ===" in got, "日志写完了就立刻拿到结果（不白等）")
            with open(logp, "w", encoding="utf-8") as f:
                f.write("=== x ===\n[3] 静默安装 WeChatWin_4.1.10.27.exe /S\n")  # 没写完
            chk(console._wait_install_done(time.time() - 10, timeout=3) is None,
                "★ 日志里没有 DONE 就超时返回 None（不许把「还在装」当「装好了」）")
        finally:
            console.HOOK_DIR = real_hook

    # ── T6 可选组件（语音 / 网上搜索 / 格式包 / 语义检索）的安装入口 ──────
    # 2026-10-05：这些**代码在、包里也在**，但依赖和模型都不随包（它们只能在
    # requirements.txt 里写成注释行；语音模型、torch、SearXNG 的 venv 更不能跨机器拷）——
    # 于是「装完就能用」在别人机器上并不成立，而 README 把这些都写在功能卖点里。
    # T6 钉的就是那个**安装入口**，以及它最容易被改坏的四条。
    print("\n6) T6 可选组件的安装入口")
    chk(set(env.OPTIONAL_PIP) >= {"voice", "formats", "semantic"},
        f"注册表里有这几项：{sorted(env.OPTIONAL_PIP)}")

    # ★ 最要紧的一条：这些包**绝不能**变成 requirements.txt 的正式行。
    # 写成正式行 → required_pkgs() 要求它们 → 没装的人「装完还是起不来」死循环，
    # installer 还会去拖重包（faster-whisper 真实踩过，selftest_audio 也钉着）。
    # 每一项都要查——只查 voice 的话，后加的项漏成正式行就没人拦。
    formal = {env.requirement_name(s).lower() for s in env.requirements_specs()}
    for name, comp in env.OPTIONAL_PIP.items():
        chk(comp.get("specs") and comp.get("imports"),
            f"{name}：写清了「装什么」和「装完 import 什么」")
        leaked = sorted(p.lower() for p in comp["specs"] if p.lower() in formal)
        chk(not leaked, f"★ {name} 的依赖不许出现在 requirements.txt 正式行里：{leaked}")

    # 没建 venv / 探不动 → 一律当**全缺**（宁可让上层重装一次，也不假装齐全）
    real_py = env.venv_python
    try:
        env.venv_python = lambda: None
        chk(env.missing_optional("voice") == list(env.OPTIONAL_PIP["voice"]["imports"]),
            "venv 不可用时 missing_optional 当全缺")
        chk(env.missing_optional("formats") == list(env.OPTIONAL_PIP["formats"]["imports"]),
            "格式包也一样（六个都当缺）")
        chk(env.install_optional("voice")[0] is False
            and "虚拟环境" in env.install_optional("voice")[1],
            "venv 没建好时装可选组件 → 如实拒绝，不假装成功")
    finally:
        env.venv_python = real_py
    chk(env.install_optional("根本没有这一项")[0] is False,
        "组件名写错 → 如实报「没有这个可选组件」")
    chk(env.missing_optional("根本没有这一项") == [],
        "未知组件不抛异常（菜单列错了不该把控制台炸掉）")

    # console 那一侧：菜单项必须是**同一个清单**驱动的，不许菜单里有、实现里没有。
    # ⚠️ 是**包含**不是相等：`search` 的安装 owner 在 `botctl`（它有自己一份 venv），
    # 所以它出现在菜单里、但**不在** `envsetup.OPTIONAL_PIP`。别把这条写成相等。
    items = {n for n, _label in console.OPTIONAL_ITEMS}
    chk(items == set(console.OPTIONAL_ACTIONS) == set(console.OPTIONAL_STATE),
        f"菜单 / 动作 / 状态三张表一一对应：{sorted(items)}")
    chk(set(env.OPTIONAL_PIP) <= items,
        f"注册表里的每一项菜单里都得有：{sorted(set(env.OPTIONAL_PIP) - items)}")
    chk("search" in items and "search" not in env.OPTIONAL_PIP,
        "search 的安装 owner 在 botctl.search_install（不在 OPTIONAL_PIP 里）")
    chk(callable(console.optional_menu) and callable(console._install_optional_all),
        "可选组件菜单与「一键部署第 3 步」都存在")
    chk(console._HEAVY <= items,
        f"「默认不装」的项（若有）都必须来自清单：{sorted(console._HEAVY)}")
    chk("semantic" not in console._HEAVY,
        "★ 语义检索**不再**默认跳过（2026-10-05 用户拍板：一键配置要装齐四项）")
    chk(not console._HEAVY,
        f"★ 现在没有任何一项默认跳过（一键部署一路回车＝四项全装）：{sorted(console._HEAVY)}")

    # ★ 菜单按键不能撞号：2026-10-05 加到第 4 项时真撞过——`[3]` 同时是「格式包」和
    #   「自动装开关」，按 3 会去切开关、装不了东西。这里真跑一遍菜单（输入 0 返回）
    #   然后数按键。开关那一项必须取「项数 + 1」。
    import builtins
    buf = io.StringIO()
    real_input = builtins.input
    try:
        builtins.input = lambda *a, **k: "0"
        with redirect_stdout(buf):
            console.optional_menu()
    finally:
        builtins.input = real_input
    # ⚠️ 只数**可选的那几个键**：状态文案里也会引用按键（「用 [3] 装」），
    # 那是提示、不是可选项，用整屏正则会把它们一起数进来、报一个假撞号。
    key_lines = re.findall(r"^\s+\[(\d+)\] (?:现在装|切换|返回)", buf.getvalue(), re.M)
    chk(len(key_lines) == len(set(key_lines)),
        f"★ 可选数字键不许重复（撞号＝按下去做的是另一件事）：{key_lines}")
    chk(str(len(console.OPTIONAL_ITEMS) + 1) in key_lines,
        f"「自动装」开关取了项数 + 1 这个键：{len(console.OPTIONAL_ITEMS) + 1}")
    for _n, _label in console.OPTIONAL_ITEMS:
        chk(f"[{console._opt_index(_n)}]" in buf.getvalue(), f"{_n} 在菜单里有按键")

    # `.rar`：rarfile 只是壳，判据在 archive_read.find_rar_tool()（唯一一份）。
    # 这里**不断言找没找到**（换台电脑就不一样），只要求它别抛、别返回假路径。
    try:
        import archive_read
        tool = archive_read.find_rar_tool()
        chk(tool is None or os.path.isabs(str(tool)),
            f".rar 外部解压器的判定可用（本机结果：{tool!r}）")
    except Exception as e:                          # noqa: BLE001
        chk(False, f"archive_read.find_rar_tool() 不该抛：{type(e).__name__}: {e}")

    # `semantic.py --ready`：给控制台用的**纯探针**，一个字都不打（好让调用方只看退出码）。
    # 打不出东西这件事本身可验：未识别参数那条路会打印「没认出来」。
    py = env.venv_python()
    if py:
        r = subprocess.run([py, "semantic.py", "--ready"],
                           capture_output=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        chk(r.stdout.strip() == b"", f"★ semantic.py --ready 是纯探针（无输出）：{r.stdout[:60]!r}")
        chk(r.returncode in (0, 1), f"--ready 只回 0/1：{r.returncode}")
    else:
        chk(True, "（本机没有 venv，跳过 --ready 实跑）")

    # 开关写 settings.json（**不回写带注释的 config.yaml**），且「没写过 = 要装」
    with tempfile.TemporaryDirectory() as td:
        real_settings_path = settings.SETTINGS_PATH
        try:
            settings.SETTINGS_PATH = os.path.join(td, "settings.json")
            chk(console._opt_wanted("voice") is True, "★ 没写过 = 要装（一键部署默认装齐）")
            console._set_opt("voice", False)
            chk(console._opt_wanted("voice") is False, "关掉之后记住")
            chk((settings.load().get("optional") or {}).get("voice") is False,
                "开关真的落在 settings.json 的 optional 段")
            console._set_opt("search", True)
            chk((settings.load().get("optional") or {}).get("voice") is False,
                "★ 改一项不许把另一项冲掉（读-改-写，不是整段覆盖）")
        finally:
            settings.SETTINGS_PATH = real_settings_path

    # 装成功后**顺手打开运行时开关**（2026-10-05 用户拍板）：写 settings.json，不动 config.yaml。
    with tempfile.TemporaryDirectory() as td:
        real_settings_path = settings.SETTINGS_PATH
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        cfg_before = (os.path.getmtime(cfg_path), os.path.getsize(cfg_path)) \
            if os.path.exists(cfg_path) else None
        try:
            settings.SETTINGS_PATH = os.path.join(td, "settings.json")
            # 先放一个「同段别的键」进去，验证是读-改-写、不是整段覆盖
            settings.save({"search": {"max_results": 7}, "optional": {"search": True}})
            msg = console._enable_runtime_switch("search", "网上搜索")
            saved = settings.load()
            chk(saved.get("search", {}).get("enabled") is True,
                "★ 装完把 search.enabled 写进 settings.json（运行时开关）")
            chk(saved.get("search", {}).get("max_results") == 7,
                "★ 写开关不许冲掉同段已有的键（读-改-写）")
            chk(saved.get("optional", {}).get("search") is True, "别的段也不许动")
            chk("settings.json" in msg and "config.yaml" in msg,
                f"★ 那句人话要说清改了哪个文件、没动哪个文件：{msg[:60]}")
            chk("true" in msg, "顺带告诉用户设成了 true")
        finally:
            settings.SETTINGS_PATH = real_settings_path
        if cfg_before is not None:
            chk((os.path.getmtime(cfg_path), os.path.getsize(cfg_path)) == cfg_before,
                "★ 全程没碰 config.yaml（mtime/大小都没变）")

    # settings.example.json 里要**带上**这个段：包里那份是它拷过去的，少了用户就不知道有开关
    ex_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.example.json")
    with open(ex_path, encoding="utf-8") as f:
        example = json.load(f)
    chk(isinstance(example.get("optional"), dict) and "voice" in example["optional"],
        f"settings.example.json 里有 optional 段：{example.get('optional')}")

    # ── 汇总 ──────────────────────────────────────────────────────────
    print()
    if _FAIL:
        print(f"失败 {len(_FAIL)} 项 ❌")
        for f in _FAIL:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
