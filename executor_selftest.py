"""executor.py（本地执行）的**独立**自测：只跑无害的本地短命令。

跑法：
    .venv/Scripts/python.exe executor_selftest.py

覆盖范围（判据都是本脚本自己设计的，不抄 executor.py 里已有的 __main__ 自测）：

  * 执行 / 退出码 / stdout+stderr 合并 / env / 命令不存在
  * **命令原文一字不差**（中文、双引号、反斜杠、带引号的「含空格路径」重定向）
  * 工作目录：显式 cwd > 配置 shell.cwd > 项目根；目录不存在如实报错、不静默退回
  * 空命令、配置读取与上下限夹取（shell.timeout / shell.max_output）
  * 超时：1 秒极小值、配置写 0 被夹到 1、超时后**及时返回**而不是等命令自己跑完
  * 输出上限：**恰好等于输出长度不截、少 1 个字符就截**；上限按**字符**算不按字节
  * 编码：utf-8 / gbk 回退 / 纯 ascii / 解不开的字节带乱码标记，含真子进程吐字节的端到端
  * 微信体量：format_result 受 WECHAT_MAX_CHARS 约束、裁了要明说、短输出不加裁切说明
  * summarize_for_model 比 format_result 给得多，并写明状态
  * 成功/失败都要把「⚠️ <error>」带进给用户和给模型的文本（不再只在失败时才带）
  * 歧义编码三态：`"gbk"` 改判（「目录」）/ `"ambiguous"` 不改判但必须告警（`abcĿ¼`、café）
    / `None` 无嫌疑；以及 encoding_guess 与 ⚠️ 提示有没有如实落地

不碰微信、不碰 hook（30001 端口）、不联网、不写仓库里的任何文件。
唯一的落盘动作：一条「带引号重定向」用例在系统临时目录里建一个文件，跑完立刻删掉。

末尾的 warn 段是**已知残留探针**：命中就说明还有 executor docstring 里写明的边界
（比如纯符号的合法 utf-8「Ω」会被误改判成 GBK），不算 FAIL —— FAIL 只留给
「模块没做到它自己声明的行为」。
"""
import os
import shutil
import sys
import tempfile
import time

import executor as ex

PROJ = ex.PROJECT_DIR
PARENT = os.path.dirname(PROJ)
PY = sys.executable
# 用本机 venv 的 python 往 stdout 写死字节，拿到「确定无疑的原始字节」。
# 可执行路径**带引号**（含空格的路径也能跑，见 §2 的回归），代码段不带空格、不带双引号，
# 这样 cmd /d /s /c 那一层的去引号规则不会改写它。
_RAW_OK = '"' not in PY

_pass = 0
_fail = []
_warn = []


def sec(title):
    print()
    print(title)


def chk(cond, msg):
    """打印一项判据。失败不中断，最后统一 exit 1（一次性看到全部问题）。"""
    global _pass
    if cond:
        _pass += 1
        print("  ok    " + msg)
    else:
        _fail.append(msg)
        print("  FAIL  " + msg)


def warn(msg):
    _warn.append(msg)
    print("  warn  " + msg)


def raw_bytes_cmd(expr):
    """拼一条「让本机 python 往 stdout 写死原始字节」的命令。

    expr 形如 "bytes([228,184,173])" 或 r"b'\\xe4\\xb8\\xad'*300"。
    """
    code = "__import__('sys').stdout.buffer.write(" + expr + ")"
    return f'"{PY}" -c {code}'


# --------------------------------------------------------------------------
sec("【1】执行 / 退出码 / 合并输出 / env")
# --------------------------------------------------------------------------
r = ex.run_command("echo hello")
chk(r.ok and r.exit_code == 0, f"echo hello 成功且退出码 0（exit={r.exit_code}）")
chk(r.output == "hello", f"输出正好是 hello（实际 {r.output!r}）")
chk(r.truncated is False and r.timed_out is False, "既没截断也没超时")
chk(r.error is None, "成功时 error 字段为空（没乱塞告警）")
chk(0.0 <= r.elapsed < 30.0, f"elapsed 是合理的秒数（{r.elapsed:.3f}s）")

r = ex.run_command("exit 0")
chk(r.ok and r.exit_code == 0 and r.output == "", "退出码 0 就算成功，输出为空")
chk("（空）" in ex.format_result(r), "空输出在给用户的文本里写「（空）」，不是留白")

r = ex.run_command("exit 7")
chk((not r.ok) and r.exit_code == 7, f"非零退出码如实返回（exit={r.exit_code}）")
chk(r.error is None, "非零退出码走 ok/exit_code 表达，不算 error（error 只放跑不起来这类）")
chk("失败（退出码 7" in ex.format_result(r), "给用户的文本里是「失败（退出码 7」")

r = ex.run_command("dir /b", cwd=PROJ)
chk(r.ok and "executor.py" in r.output, "dir /b 能列出项目根目录（真的跑起来了）")
chk("bot.py" in r.output, "同一份目录清单里有 bot.py（判据不是只看一条）")

r = ex.run_command("echo from-stdout & echo from-stderr 1>&2")
chk("from-stdout" in r.output and "from-stderr" in r.output,
    "stderr 被 2>&1 合进 stdout，两边都能拿到")

r = ex.run_command("这个命令肯定不存在_exec_selftest_xyz")
chk((not r.ok) and r.exit_code not in (0, None),
    f"命令不存在时如实失败，不是假装成功（exit={r.exit_code}）")
chk(r.error is None and r.output != "", "这种失败是 cmd 报的（有输出），没被包装成启动异常")

r = ex.run_command("echo %EXEC_SELFTEST_VAR%", env={"EXEC_SELFTEST_VAR": "env-ok-123"})
chk(r.output == "env-ok-123", f"env 参数传进子进程（实际 {r.output!r}）")
chk("EXEC_SELFTEST_VAR" not in os.environ, "env 参数不污染 os.environ 本体")
chk(ex.run_command("echo %EXEC_SELFTEST_UNDEFINED%").output == "%EXEC_SELFTEST_UNDEFINED%",
    "没传的变量原样回显（说明上一条不是 cmd 的默认行为撑起来的）")


# --------------------------------------------------------------------------
sec("【2】命令原文一字不差（用户审的就是要跑的那条）")
# --------------------------------------------------------------------------
tricky = 'echo 中文 & echo "带 引号" & echo C:\\Windows\\System32'
r = ex.run_command(tricky)
chk(r.command == tricky, "ExecResult.command 与给的原文完全相等（中文/引号/反斜杠都在）")
chk(("命令：" + tricky) in ex.format_result(r), "format_result 里的命令原文一字不差")
chk(("命令原文：" + tricky) in ex.summarize_for_model(r), "summarize_for_model 里的命令原文一字不差")

r = ex.run_command('echo "hello world"')
chk(r.output == '"hello world"',
    f"双引号原样进 cmd，没被转义成带反斜杠的怪物（实际 {r.output!r}）")

r = ex.run_command(f'dir "{PROJ}" /b')
chk(r.ok and "executor.py" in r.output, "带引号的路径能真的跑通（引号不做二次转义）")

# 命令以**带引号的可执行路径**开头（含空格路径的常见形态）：少了整体那层引号，
# cmd 会把 `"C:\Program Files\..."` 当成坏路径 → rc=1「文件名、目录名或卷标语法不正确」
r = ex.run_command(f'"{PY}" -c "print(123)"')
chk(r.ok and r.output == "123",
    f"命令以带引号的可执行路径开头也能跑通（ok={r.ok}，输出 {r.output!r}）")
chk(r.exit_code == 0 and r.error is None, "这条回归退出码 0、没有附加告警")

tail_cmd = "echo 尾部反斜杠\\"
r = ex.run_command(tail_cmd)
chk(r.command == tail_cmd and r.error is None, "命令末尾带反斜杠也不会被吞掉/报错")

made = ex.ExecResult("  echo   A  ", "D:\\x")
chk(made.command == "  echo   A  ", "ExecResult 构造函数不 trim、不规范化命令原文")

r = ex.run_command("set X=42 && echo %X%")
chk(r.output == "%X%", "cmd /c 的 %VAR% 是立即展开的（回显 %X% 而不是 42，别当 bug 修）")

# 关键回归：带空格路径的重定向。list 形式传参会走 list2cmdline 把引号变成 \"，
# cmd 收到字面反斜杠引号 → rc=1「文件名、目录名或卷标语法不正确」，文件根本建不出来。
_tmp = tempfile.mkdtemp(prefix="exec_selftest_")
try:
    _sd = os.path.join(_tmp, "含 空格")
    os.makedirs(_sd)
    _target = os.path.join(_sd, "a b.txt")
    redir_cmd = f'echo ok > "{_target}"'
    r = ex.run_command(redir_cmd)
    chk(r.ok and r.exit_code == 0, f"带引号（含空格）的重定向能跑通（exit={r.exit_code}，err={r.error}）")
    chk(os.path.isfile(_target), f"文件真的被创建了：{_target}")
    if os.path.isfile(_target):
        with open(_target, "rb") as fh:
            chk(fh.read().strip() == b"ok", "文件内容就是 echo 进去的 ok")
    else:
        chk(False, "文件不存在，内容无从校验")
    chk(r.command == redir_cmd, "重定向命令的原文同样一字不差")
    chk(("命令：" + redir_cmd) in ex.format_result(r), "发给用户的文本里带着这条带引号的原文")
finally:
    shutil.rmtree(_tmp, ignore_errors=True)


# --------------------------------------------------------------------------
sec("【3】工作目录")
# --------------------------------------------------------------------------
r = ex.run_command("cd", cwd=PARENT)
chk(r.ok and os.path.normcase(PARENT) in os.path.normcase(r.output),
    f"显式 cwd 生效（{r.output.strip()}）")
chk(os.path.normcase(r.cwd) == os.path.normcase(PARENT), "ExecResult.cwd 是解析后的那个目录")

r = ex.run_command("cd", workdir=PARENT)
chk(os.path.normcase(PARENT) in os.path.normcase(r.output), "workdir 是 cwd 的等效别名")

r = ex.run_command("cd", cfg={"shell": {"cwd": PARENT}})
chk(os.path.normcase(PARENT) in os.path.normcase(r.output), "配置 shell.cwd 生效")

r = ex.run_command("cd", cwd=PROJ, cfg={"shell": {"cwd": PARENT}})
chk(os.path.normcase(PROJ) in os.path.normcase(r.output), "显式 cwd 压过配置里的 shell.cwd")

r = ex.run_command("cd")
chk(os.path.normcase(PROJ) in os.path.normcase(r.output), "什么都不给就落在项目根目录")

p, e = ex.resolve_cwd({})
chk(p == PROJ and e is None, "resolve_cwd({}) → 项目根")
chk(ex.resolve_cwd(None)[0] == PROJ, "resolve_cwd(None) 也是项目根")
chk(ex.resolve_cwd({"shell": {"cwd": "~"}})[0] == os.path.expanduser("~"), "配置里写 ~ 会展开")

_bad = r"D:\no-such-dir-exec-selftest"
p, e = ex.resolve_cwd({}, _bad)
chk(p == "" and _bad in e, f"不存在的目录在 resolve_cwd 阶段就报错（{e}）")

r = ex.run_command("echo x", cwd=_bad)
chk((not r.ok) and r.cwd == "" and _bad in (r.error or ""),
    f"目录不存在 → 如实报错（{r.error}）")
chk(os.path.normcase(PROJ) not in os.path.normcase(r.error or ""),
    "**没有**静默退回项目根（退回就等于骗用户说命令在他指定的目录跑了）")
chk(r.output == "" and r.exit_code is None, "没跑起来就没有输出、也没有退出码")


# --------------------------------------------------------------------------
sec("【4】空命令 / 配置读取与夹取")
# --------------------------------------------------------------------------
for _c in ("", "   ", "\t", None):
    r = ex.run_command(_c)
    chk((not r.ok) and "空" in (r.error or "") and r.cwd == "" and r.exit_code is None,
        f"空命令如实报错、不执行（{_c!r} → {r.error}）")

chk(ex.enabled({"shell": {"enabled": True}}) is True, "enabled：True 就是开")
chk(ex.enabled({"shell": {"enabled": 1}}) is True, "enabled：1 也当开")
chk(ex.enabled({"shell": {"enabled": 0}}) is False, "enabled：0 就是关")
chk(ex.enabled({"shell": {"enabled": "yes"}}) is True, "enabled：非空字符串按 bool 语义算开")
chk(ex.enabled({"shell": {}}) is False and ex.enabled({}) is False and ex.enabled(None) is False,
    "enabled：缺段/缺 key/整份 None 都算没开（fail-safe）")
chk(ex.enabled({"shell": None}) is False,
    "enabled：shell 段写成空值（yaml 解出来是 None）也当没开，不抛 AttributeError")
# 复核方 R5-1：`shell:` 写成标量（yaml 的 yes/true/字符串）时曾抛 AttributeError，
# 确认分支里一抛就把主循环带崩（主循环只捕获 KeyboardInterrupt）——现在一律当没配。
chk(ex.enabled({"shell": "yes"}) is False, "enabled：shell 段写成字符串标量 → False，不抛异常")
chk(ex.enabled({"shell": True}) is False, "enabled：shell 段写成布尔标量 → False，不抛异常")
chk(ex.enabled({"shell": 1}) is False, "enabled：shell 段写成数字标量 → False，不抛异常")
chk(ex._shell_cfg({"shell": "abc"}) == {}, "_shell_cfg：非 dict 的 shell 段一律返回 {}")
chk(ex._shell_cfg({"shell": None}) == {} and ex._shell_cfg(None) == {},
    "_shell_cfg：None 段/整份 None 也返回 {}")
chk(ex._shell_cfg({"shell": {"enabled": True}}) == {"enabled": True}, "_shell_cfg：正常的 dict 原样返回")
chk(ex.run_command("echo scalar-shell", cfg={"shell": "yes"}).ok,
    "整条链路：cfg 的 shell 段是标量时，run_command 仍按默认跑完而不是抛异常")

chk(ex.MIN_TIMEOUT <= ex.DEFAULT_TIMEOUT <= ex.MAX_TIMEOUT,
    "超时三个常量的大小关系成立")
chk(ex.MIN_MAX_OUTPUT <= ex.DEFAULT_MAX_OUTPUT <= ex.MAX_OUTPUT_CAP,
    "输出上限三个常量的大小关系成立")
chk(0 < ex.WECHAT_MAX_CHARS <= ex.DEFAULT_MAX_OUTPUT,
    "发微信的体量上限是正数，且不高于工具输出上限")

# 下限/上限本身不被改，超出去才夹
chk(ex._cfg_int({"shell": {"timeout": 1}}, "timeout", 60, 1, 600) == 1, "timeout 下限本身保留")
chk(ex._cfg_int({"shell": {"timeout": 600}}, "timeout", 60, 1, 600) == 600, "timeout 上限本身保留")
chk(ex._cfg_int({"shell": {"timeout": 5}}, "timeout", 60, 1, 600) == 5, "区间内的值原样保留")
chk(ex._cfg_int({"shell": {"timeout": 0}}, "timeout", 60, 1, 600) == 1, "timeout 0 夹到下限 1")
chk(ex._cfg_int({"shell": {"timeout": -5}}, "timeout", 60, 1, 600) == 1, "timeout 负数夹到下限 1")
chk(ex._cfg_int({"shell": {"timeout": 601}}, "timeout", 60, 1, 600) == 600, "超一点也夹回上限")
chk(ex._cfg_int({"shell": {"timeout": 99999}}, "timeout", 60, 1, 600) == 600, "大得离谱夹到上限")
chk(ex._cfg_int({"shell": {"timeout": "abc"}}, "timeout", 60, 1, 600) == 60, "坏值用默认")
chk(ex._cfg_int({"shell": {"timeout": None}}, "timeout", 60, 1, 600) == 60, "None 用默认")
chk(ex._cfg_int({}, "timeout", 60, 1, 600) == 60, "没配置用默认")
chk(ex._cfg_int({"shell": None}, "timeout", 60, 1, 600) == 60, "shell 段是 None 也退回默认")

chk(ex._cfg_int({"shell": {"max_output": 200}}, "max_output", 20000, 200, 200000) == 200,
    "max_output 下限本身保留")
chk(ex._cfg_int({"shell": {"max_output": 200000}}, "max_output", 20000, 200, 200000) == 200000,
    "max_output 上限本身保留")
chk(ex._cfg_int({"shell": {"max_output": 100}}, "max_output", 20000, 200, 200000) == 200,
    "max_output 低于下限被夹到 200")
chk(ex._cfg_int({"shell": {"max_output": 999999}}, "max_output", 20000, 200, 200000) == 200000,
    "max_output 高于上限被夹到 200000")
chk(ex._cfg_int({"shell": {"max_output": "xxx"}}, "max_output", 20000, 200, 200000) == 20000,
    "max_output 坏值用默认")

chk(ex.run_command("echo cfg-shell-none", cfg={"shell": None}).ok,
    "shell 段为 None 时 run_command 也能按默认跑完（不再依赖任何 None 容错的巧合）")
chk(ex.run_command("echo fallback-ok", timeout="abc").ok,
    "timeout 传坏值回退默认 60，不是抛异常")
chk(ex.run_command("echo fallback-ok", cfg={"shell": {"timeout": "abc"}}).ok,
    "配置里 timeout 是坏值也回退默认")


# --------------------------------------------------------------------------
sec("【5】超时：1 秒极小值、及时返回、配置夹取")
# --------------------------------------------------------------------------
_PING = "ping -n 6 127.0.0.1 > nul"

t0 = time.time()
_full = ex.run_command(_PING, timeout=20)
_wall_full = time.time() - t0
chk(_full.ok and not _full.timed_out, f"同一条命令给足时间能自己跑完（{_full.elapsed:.1f}s）")
chk(_full.elapsed > 3.0, f"这条命令天然要 3 秒以上（{_full.elapsed:.1f}s），超时判据才有意义")

t0 = time.time()
_to = ex.run_command(_PING, timeout=1)
_wall_to = time.time() - t0
chk(_to.timed_out and not _to.ok, f"timeout=1 触发超时（timed_out={_to.timed_out}）")
chk(_to.elapsed < 4.0 and _wall_to < 4.0,
    f"1 秒的超时在 1 秒附近就返回了（{_to.elapsed:.1f}s）")
chk(_wall_to < _full.elapsed - 1.5,
    f"不是等命令自己跑完才返回（{_wall_to:.1f}s << {_full.elapsed:.1f}s）")
chk(_to.exit_code not in (0, None), f"超时不算成功（exit={_to.exit_code}）")
chk("已被中断" in _to.output and "1 秒" in _to.output,
    "输出里明说进程是被中断的、并写了秒数，不糊弄成「没输出」")
chk("超过 1 秒" in (_to.error or ""), f"error 文案带着夹取后的秒数（{_to.error}）")
_txt = ex.format_result(_to)
chk("状态：超时中断" in _txt and "状态：完成" not in _txt, "给用户的文本写「超时中断」，不写「完成」")

t0 = time.time()
_cz = ex.run_command(_PING, cfg={"shell": {"timeout": 0}})
_wall_cz = time.time() - t0
chk(_cz.timed_out and _wall_cz < 4.0,
    f"配置写 timeout: 0 被夹到 MIN_TIMEOUT(1) 真生效（{_wall_cz:.1f}s 就断了，不是 60）")
chk("超过 1 秒" in (_cz.error or ""), "夹取后的秒数如实写进文案，不是原始配置值")


# --------------------------------------------------------------------------
sec("【6】输出上限与截断边界（恰好 / 多 1 / 少 1 / 按字符不按字节）")
# --------------------------------------------------------------------------
_LONG = "for /L %i in (1,1,40) do @echo 0123456789"
_base = ex.run_command(_LONG)
chk(_base.ok and not _base.truncated, "参照输出本身没被默认上限截掉")
_L = len(_base.output)
chk(_L == 439, f"40 行 × 10 字的输出长度是 439（实际 {_L}）")
chk(_base.output.count("\n") == 39, "行数判据：39 个换行 = 40 行（不是硬编码猜的）")

r = ex.run_command(_LONG, max_output=_L)
chk(not r.truncated and r.output == _base.output,
    "上限**恰好等于**输出长度：一个字都不该裁")

r = ex.run_command(_LONG, max_output=_L + 1)
chk(not r.truncated and r.output == _base.output, "上限比输出长 1：也不裁")

r = ex.run_command(_LONG, max_output=_L - 1)
chk(r.truncated and r.output.startswith(_base.output[:_L - 1]),
    "上限比输出短 1：截断，且保留的是**开头**（报错/表头都在开头）")
chk("另有约 1 个字符没显示" in r.output, "少掉几个字就写几个字（这里是 1）")
chk("已截断" in r.output, "截断时明确写出「已截断」")
chk(len(r.output) > _L - 1, "截断提示是**追加**在正文后面的，不吞掉正文")

r = ex.run_command(_LONG, max_output=300)
chk(r.truncated and r.output.startswith(_base.output[:300]), "上限 300：正文正好是原文前 300 字")
chk(f"另有约 {_L - 300} 个字符没显示" in r.output, "omitted 计数精确（439-300=139）")

r = ex.run_command(_LONG, max_output=10)
chk(r.truncated and r.output.startswith(_base.output[:ex.MIN_MAX_OUTPUT]),
    f"上限低于 MIN_MAX_OUTPUT 被夹到 {ex.MIN_MAX_OUTPUT}（正文是原文前 200 字）")

r = ex.run_command(_LONG, max_output=10 ** 9)
chk(not r.truncated, "上限大得离谱被夹到 MAX_OUTPUT_CAP，这条短输出仍然不截、不炸")

r = ex.run_command("echo short", max_output=200)
chk(not r.truncated, "短输出不标截断")

# cmd 的输出常带一堆尾随 CR/LF：末尾的多余空行要去掉，但**中间**的空行是内容，必须留着
r = ex.run_command("echo a&echo.&echo b&echo.&echo.")
chk(r.output == "a\n\nb",
    f"末尾多余空行被去掉、中间的空行保留（实际 {r.output!r}）")
chk(r.output.count("\n") == 2, "正好两个换行（a / 空行 / b），没把中间的也算成尾随")

if _RAW_OK:
    # 300 个「中」在 utf-8 下是 900 字节。上限按**字符**算就不该截，按字节算就会截。
    _zh = raw_bytes_cmd(r"b'\xe4\xb8\xad'*300")
    _zhbase = ex.run_command(_zh)
    chk(_zhbase.ok and len(_zhbase.output) == 300,
        f"参照输出：300 个中文字符 / 900 字节（实际 {len(_zhbase.output)} 字）")
    r = ex.run_command(_zh, max_output=300)
    chk(not r.truncated, "上限 300 = 300 个字符：900 字节也不截（说明按字符算，不按字节）")
    r = ex.run_command(_zh, max_output=299)
    chk(r.truncated and "另有约 1 个字符没显示" in r.output,
        "上限 299：按字符算少 1 就截，多字节字符不会被当成字节数砍半")
else:
    warn(f"本机 sys.executable 路径里含双引号（{PY}），跳过「按字符不按字节」的端到端用例")


# --------------------------------------------------------------------------
sec("【7】编码：utf-8 / gbk 回退 / ascii / 解不开的字节")
# --------------------------------------------------------------------------
chk(ex._decode("中文".encode("utf-8")) == "中文", "utf-8 中文原样解出")
chk(ex._decode("中文".encode("gbk")) == "中文",
    "gbk 中文：utf-8 抛异常 → 回退 gbk，结果必须是中文而不是乱码")
try:
    "中文".encode("gbk").decode("utf-8")
    _cn_utf8_ok = True
except UnicodeDecodeError:
    _cn_utf8_ok = False
chk(_cn_utf8_ok is False,
    "判据前提：GBK 的「中文」字节 b'\\xd6\\xd0\\xce\\xc4' 在 utf-8 下**必抛异常**（实测过，别想当然）")
chk(ex._decode(b"plain ascii") == "plain ascii", "纯 ascii 原样")
chk(ex._decode(b"") == "" and ex._decode(None) == "", "空字节/None 返回空串")
chk(ex._decode("已经是 str") == "已经是 str", "传进来的已经是 str 就原样返回")

_garbage = ex._decode(b"\xd6\xd0\xce")   # 奇数长度：utf-8 和 gbk 都解不动
chk("\ufffd" in _garbage and ex._looks_mojibake(_garbage) is True,
    f"非法字节 b'\\xd6\\xd0\\xce' 落到替换字符，并被标成乱码（{_garbage!r}）")
chk(ex._looks_mojibake("正常的中文和英文 ASCII") is False, "正常输出不会被误判成乱码")
chk(ex._looks_mojibake("坏\ufffd字") is True, "含 U+FFFD 判为乱码")
chk(ex._looks_mojibake("有\x00NUL") is True, "含孤立 NUL 判为乱码")

_cands = ex._candidates()
chk("gbk" in _cands and "cp936" in _cands,
    f"候选编码表里**无条件**带 gbk/cp936（只信 locale 在 cp65001 机器上会失效）：{_cands}")

if _RAW_OK:
    r = ex.run_command(raw_bytes_cmd("bytes([214,208,206,196])"))
    chk(r.ok and r.output == "中文", f"端到端：真子进程吐 GBK 字节，回来还是「中文」（{r.output!r}）")
    chk(r.error is None, "正确回退到 gbk 时不该报乱码警告")

    r = ex.run_command(raw_bytes_cmd("bytes([228,184,173,230,150,135])"))
    chk(r.ok and r.output == "中文", f"端到端：真子进程吐 utf-8 字节（{r.output!r}）")

    r = ex.run_command(raw_bytes_cmd("bytes([97,98,99])"))
    chk(r.ok and r.output == "abc", "端到端：纯 ascii 字节原样")

    _moji = ex.run_command(raw_bytes_cmd("bytes([255,254])"))
    chk(_moji.ok, "吐非法字节的子进程本身是成功的（问题只在解码）")
    chk("\ufffd" in _moji.output, "非法字节在输出里留下替换字符，没被悄悄丢掉")
    chk("乱码" in (_moji.error or ""), f"executor 自己补了乱码警告（{_moji.error}）")
    chk("⚠️" in ex.format_result(_moji),
        "成功但输出有乱码时，发回微信的文本里带 ⚠️ 警告（不再静默）")
    chk("（注意：" in ex.summarize_for_model(_moji),
        "成功但输出有乱码时，给模型的摘要里也带警告（模型不会拿乱码去作答）")
else:
    warn(f"本机 sys.executable 路径里含双引号（{PY}），跳过真子进程吐字节的端到端编码用例")


# --------------------------------------------------------------------------
sec("【8】format_result：发给微信的体量、状态、裁了要明说")
# --------------------------------------------------------------------------
_long_cmd = "for /L %i in (1,1,400) do @echo 0123456789"
_lres = ex.run_command(_long_cmd)
chk(_lres.ok and len(_lres.output) == 4399, f"长参照输出 4399 字（实际 {len(_lres.output)}）")

_txt = ex.format_result(_lres)
chk(len(_txt) <= ex.WECHAT_MAX_CHARS + 300,
    f"发微信的文本受 WECHAT_MAX_CHARS 约束（{len(_txt)} <= {ex.WECHAT_MAX_CHARS + 300}）")
chk(f"微信里只发前 {ex.WECHAT_MAX_CHARS} 字" in _txt,
    "被裁时明说「只发了前 N 字」，不假装输出就这么长")
chk(("命令：" + _long_cmd) in _txt, "裁了也仍然整条带着命令原文")
chk(_lres.output[:ex.WECHAT_MAX_CHARS] in _txt, "留着的是输出的**开头**那一段")

_short_txt = ex.format_result(ex.run_command("echo short"))
chk("微信里只发前" not in _short_txt, "短输出不加裁切说明")
chk("命令：echo short" in _short_txt and "输出：\nshort" in _short_txt,
    "短输出的文本带命令原文和输出正文")

_t120 = ex.format_result(_lres, max_chars=120)
chk(f"微信里只发前 120 字" in _t120 and _lres.output[:120] in _t120,
    "max_chars 参数能指定更小的体量，数字与正文都对得上")
chk("微信里只发前" not in ex.format_result(_lres, max_chars=0),
    "max_chars=0 是「不限」，不是「一个字节都不发」")

_st = [
    (ex.ExecResult("c", "D", ok=True, output="出", exit_code=0, elapsed=1.0), "状态：完成（退出码 0"),
    (ex.ExecResult("c", "D", ok=False, output="", exit_code=2, elapsed=1.0), "状态：失败（退出码 2"),
    (ex.ExecResult("c", "D", ok=False, output="", exit_code=None, error="没有执行啊"),
     "状态：没有执行 —— 没有执行啊"),
    (ex.ExecResult("c", "D", ok=False, output="半", exit_code=1, timed_out=True, elapsed=3.0),
     "状态：超时中断"),
]
for _r, _needle in _st:
    chk(_needle in ex.format_result(_r), f"状态文案如实：{_needle}")
_warn_fail = ex.format_result(
    ex.ExecResult("c", "D", ok=False, output="", exit_code=7, error="模拟错误"))
chk("⚠️ 模拟错误" in _warn_fail, "失败且带 error 时，有一行独立的「⚠️ <error>」")
chk("注：" not in _warn_fail, "失败分支不再另外加「注：」（已并成 ⚠️ 行，防文案回退）")

_ok_err = ex.ExecResult("dir", PROJ, ok=True, exit_code=0, output="Ŀ¼", error="编码是猜的")
_ok_err_txt = ex.format_result(_ok_err)
chk("⚠️ 编码是猜的" in _ok_err_txt,
    "**成功**结果的文本里也带 ⚠️ 警告（以前只在失败时才带，等于静默误导）")
chk("状态：完成" in _ok_err_txt and "输出：\nĿ¼" in _ok_err_txt,
    "成功了状态还是「完成」，警告只是额外一行，正文照发")
chk("（注意：编码是猜的）" in ex.summarize_for_model(_ok_err),
    "成功结果的模型摘要里也带这条警告")
chk("⚠️" not in ex.format_result(
        ex.ExecResult("c", "D", ok=False, exit_code=None, error="没跑成")),
    "exit_code 为 None 的失败在状态行里已经说清，不再重复一行 ⚠️")

_rbad = ex.run_command("echo x", cwd=r"D:\no-such-dir-exec-selftest")
_bad_txt = ex.format_result(_rbad)
chk("状态：没有执行" in _bad_txt and "状态：完成" not in _bad_txt,
    "目录不存在这种「压根没跑」的情况，给用户的文本写「没有执行」")
chk("命令：echo x" in _bad_txt, "没跑起来也要带着命令原文，用户才知道是哪条")


# --------------------------------------------------------------------------
sec("【9】summarize_for_model：给模型的摘要")
# --------------------------------------------------------------------------
_mtxt = ex.summarize_for_model(_lres)
chk(len(_mtxt) > len(_txt), f"给模型的比给用户的多（{len(_mtxt)} > {len(_txt)}）")
chk("本地执行结果：" in _mtxt, "摘要写明这是本地执行结果")
chk("命令原文：" + _long_cmd in _mtxt, "摘要里命令原文一字不差")
chk("输出过长，这里已截断" in _mtxt, "摘要自己也截断了，并写明截断")
chk(_lres.output[:4000] in _mtxt, "摘要正文默认留前 4000 字")
chk(("命令原文：" + _long_cmd) in ex.summarize_for_model(_lres, max_chars=100),
    "max_chars 压得很小时，命令原文仍然完整保留")

for _r, _needle in [
    (ex.ExecResult("c", "D", ok=True, output="出", exit_code=0, elapsed=1.0), "命令执行完成，退出码 0"),
    (ex.ExecResult("c", "D", ok=False, output="", exit_code=2, elapsed=1.0), "命令失败，退出码 2"),
    (ex.ExecResult("c", "D", ok=False, output="", exit_code=None, error="没跑成"), "命令没有执行：没跑成"),
    (ex.ExecResult("c", "D", ok=False, output="半", exit_code=1, timed_out=True, elapsed=3.0),
     "超时"),
]:
    chk(_needle in ex.summarize_for_model(_r), f"摘要状态如实：{_needle}")
chk("（空）" in ex.summarize_for_model(ex.ExecResult("c", "D", ok=True, output="", exit_code=0)),
    "摘要里空输出写「（空）」")


# --------------------------------------------------------------------------
sec("【10】run_command_text 便捷封装")
# --------------------------------------------------------------------------
_ok, _t = ex.run_command_text("echo wrapped")
chk(_ok is True and "wrapped" in _t, "run_command_text 成功路径返回 (True, 文本)")
chk("命令：echo wrapped" in _t, "便捷封装的文本同样带命令原文")
_ok, _t = ex.run_command_text("exit 7")
chk(_ok is False and "失败（退出码 7" in _t and "状态：完成" not in _t,
    "run_command_text 失败路径返回 (False, 写清失败的文本)")


# --------------------------------------------------------------------------
sec("【附】歧义编码改判（正式断言）与已知残留（warn）")
# --------------------------------------------------------------------------
# 背景：utf-8 解码是**可逆**的，所以 _decode 里「encode 回去比对原始字节」那条校验
# 对 utf-8 永远自洽 —— 挡不住「GBK 正文被当成 utf-8」这一类。先独立枚举印证这一点，
# 再验证 run_command 那一层的改判（_decode_printable_trap）真的生效。
_amb = 0
for _lead in range(0x81, 0xFF):
    for _trail in range(0x40, 0xFF):
        if _trail == 0x7F:
            continue
        _raw = bytes([_lead, _trail])
        try:
            _g = _raw.decode("gbk")
        except UnicodeDecodeError:
            continue
        try:
            _u = _raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _g != _u:
            _amb += 1
chk(_amb > 0,
    f"独立枚举确认：utf-8 与 gbk 有 {_amb} 个「两边都能解、解出来还不一样」的 2 字节序列")

# 检测函数本身是**三态**（"gbk" / "ambiguous" / None），下面对每一种都自己的判据
chk(ex._decode_printable_trap("目录".encode("gbk")) == "gbk",
    "歧义检测：GBK 的「目录」字节 → 'gbk'，可以改判")
chk(ex._decode_printable_trap("中文".encode("gbk")) is None,
    "歧义检测：utf-8 本来就解不动的 GBK 字节 → None（交给 _decode 的 gbk 回退，不走这条路）")
chk(ex._decode_printable_trap(b"plain ascii") is None, "歧义检测：纯 ascii → None")
chk(ex._decode_printable_trap(b"\xff\xfe") is None, "歧义检测：非法字节 → None")
chk(ex._decode_printable_trap(b"") is None, "歧义检测：空字节 → None")
# 复核方 R5-2b 抓的静默口子：整串是合法 utf-8、解出 'abcĿ¼'，
# 布尔版会 return False 让它一个提示都没有 —— 三态版必须是 'ambiguous'（不改判、但要告警）
chk(ex._decode_printable_trap(b"abc" + "目录".encode("gbk")) == "ambiguous",
    "歧义检测：ASCII 字母数字混着可疑区 → 'ambiguous'（不许静默）")
chk(ex._decode_printable_trap("caf\u00e9".encode("utf-8")) == "ambiguous",
    "歧义检测：合法 utf-8 西文 café → 'ambiguous'（不改判，但不许静默）")

# 改判发生在 run_command 那一层（_decode 单函数只负责"哪个编码自洽"），
# 所以端到端断言直接打 run_command，不去要求 _decode 猜对。
if _RAW_OK:
    _dir_run = ex.run_command(raw_bytes_cmd("bytes([196,191,194,188])"))
    chk(_dir_run.ok and _dir_run.output == "目录",
        f"端到端：真子进程吐 GBK 的「目录」字节，回来是「目录」而不是 'Ŀ¼'（实际 {_dir_run.output!r}）")
    chk(_dir_run.encoding_guess == "gbk",
        f"并如实标注 encoding_guess='gbk'（实际 {_dir_run.encoding_guess!r}）")
    chk(bool(_dir_run.error) and "GBK" in _dir_run.error,
        "error 里说清「这是按 GBK 猜的、可能是 utf-8 西文」，不把猜的当板上钉钉")
    chk("⚠️" in ex.format_result(_dir_run), "发回微信的文本里有 ⚠️ 提示，不静默")
    chk("（注意：" in ex.summarize_for_model(_dir_run),
        "给模型的摘要里同样标注了「这是猜的」")

    # 复核方那条静默口子的端到端：'abc' + GBK「目录」→ 不改判（留着 'abcĿ¼'），但必须带告警
    _amb_run = ex.run_command(raw_bytes_cmd("bytes([97,98,99,196,191,194,188])"))
    chk(_amb_run.ok and _amb_run.output == "abc\u013f\u00bc",
        f"端到端：'abc'+GBK「目录」→ 保留 utf-8 解读 'abcĿ¼'，不擅自改判（实际 {_amb_run.output!r}）")
    chk(_amb_run.encoding_guess is None, "这种「不改判」的情况不打 encoding_guess（没换编码）")
    chk("可能没解码对" in (_amb_run.error or ""),
        f"**但必须有告警**：error 里写「这一处可能没解码对」（实际 {_amb_run.error!r}）")
    chk("⚠️" in ex.format_result(_amb_run) and "（注意：" in ex.summarize_for_model(_amb_run),
        "这条告警同样进了给用户的文本和给模型的摘要，不是只躺在 error 字段里")

    # 合法 utf-8 西文 café：三态改动后不再被误改判，但要如实提示「可能没解码对」
    _cafe_run = ex.run_command("echo caf\u00e9")
    chk(_cafe_run.ok and _cafe_run.output == "caf\u00e9",
        f"端到端：café 保持 utf-8 原样，没被改成怪汉字（实际 {_cafe_run.output!r}）")
    chk(_cafe_run.encoding_guess is None, "café 这条路不改判，encoding_guess 保持 None")
    chk("可能没解码对" in (_cafe_run.error or ""),
        f"café 仍带「这一处可能没解码对」的如实告警（{_cafe_run.error!r}）")
    chk("⚠️" in ex.format_result(_cafe_run),
        "café 的告警进到发给微信的文本里（属于如实提示，不再是静默错误）")

    # 非歧义的正常输出不该被改判（别把干净输出也标成"猜的"）
    _norm = ex.run_command(raw_bytes_cmd("bytes([214,208,206,196])"))
    chk(_norm.output == "中文" and _norm.encoding_guess is None and _norm.error is None,
        "非歧义的 GBK 中文：不改判、不打 encoding_guess、不加警告")
else:
    warn(f"本机 sys.executable 路径里含双引号（{PY}），跳过歧义编码的端到端改判用例")

# 已知残留（executor docstring 已写明，不求 FAIL）：**纯符号**的合法 utf-8（Ω）没有 ASCII
# 字母数字，过不了"混排"那条闸，仍会被改判成汉字 —— 只能靠 ⚠️ 提示兜住。
_omega = ex.run_command("echo \u03a9")
if _omega.output == "\u03a9":
    chk(True, "合法 utf-8 纯符号西文（Ω）也没被误改判")
else:
    warn(f"已知残留：合法 utf-8 纯符号 'Ω' 被改判成 {_omega.output!r}"
         f"（encoding_guess={_omega.encoding_guess!r}）；已按 docstring 的约定带 ⚠️ 提示")


# --------------------------------------------------------------------------
print()
print(f"通过 {_pass} 项，失败 {len(_fail)} 项，已知残留提示 {len(_warn)} 条。")
if _warn:
    print("已知残留提示（不算失败，但请知悉）：")
    for _w in _warn:
        print("  - " + _w)
if _fail:
    print(f"失败 {len(_fail)} 项 ❌")
    for _f in _fail:
        print("  - " + _f)
    sys.exit(1)
print("全部通过 ✅")
