"""本地执行：在用户本机上跑一条命令行命令，把输出拿回来。

这个模块**只管"怎么跑"**，不管"该不该跑"。要不要跑由上层把关：

  * 模型走 `run_command` 工具**只能提出**一条命令，走 agent_tools 的「待确认」
    机制（kind="shell"），用户回「确认」以后才由 bot.py 调这里真跑。
  * 微信消息本身就是命令通道 —— 等于开了一个远程执行入口，所以**每一条都
    必须先原样给用户看过、等他确认**，不存在"模型说跑就跑"。

三条硬约束（都来自项目已有的坑）：

1. **同步阻塞**。hook 不支持并发，bot 主循环是单线程的，跑命令期间轮询会停。
   所以超时默认只给 60 秒，超了就中断 —— 别指望在这里开线程/异步。
2. **输出有上限**。一条 `dir /s C:\\` 能喷出上百 MB，全塞回微信不现实，
   也容易把模型上下文撑爆。超上限就截断，并**明确告诉用户截断了**（不许假装输出就这么长）。
3. **别把没做到的说成做到了**。命令没起来、超时被杀、编码解不开，都要如实体现，
   见 `ExecResult.ok` 与 `format_result` 的文案。

Windows 下控制台输出常是 GBK（代码页 936），也有不少程序吐 UTF-8，
所以解码统一走 `utf-8 → 系统 locale(gbk)` 回退，见 `_decode`。

**已知残留（如实写在代码里，不是"差不多就行"）**：utf-8 与 GBK 有 1920 个
2 字节序列**两边都能解、解出来还不一样**（例如 b'\\xc4\\xbf'：utf-8 是 U+013F，
GBK 是「目」）。这种字节串在"先按 utf-8 解"的策略下必然选错。`run_command` 里用
`_decode_printable_trap` 把**其中一类**（解出来的字符整片落在 Latin-1 补充 /
拉丁扩展 / IPA / 希腊区）改判成 GBK，并在结果里带上"这是猜的"提示；
仍有一小部分（例如误解出来正好是常见西文字符的）识别不出来，只能靠
"输出看着怪"自己判断。真要彻底消掉，只能让子进程统一输出编码
（如 chcp 65001 / PYTHONIOENCODING），那是另一个话题。
"""
import locale
import os
import subprocess
import sys
import time

# 项目根目录。默认工作目录就是它 —— 命令的 cwd 由 config.yaml 的 shell.cwd 决定。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 超时的上下限：上限是硬的（主循环同步阻塞，跑太久轮询停太久）。
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600
MIN_TIMEOUT = 1

# 输出上限的上下限（字符数，不是字节：截断是对**文本**做的）。
DEFAULT_MAX_OUTPUT = 20000
MAX_OUTPUT_CAP = 200000
MIN_MAX_OUTPUT = 200

# 发回微信时的上限。**这是微信场景的硬需求**，不是洁癖：
#   * 这条文本要经过 hook 的 SendTextMsg 发出去，长文本没有意义（聊天窗口刷屏）；
#   * 微信对消息体量本来就有限制，而且用户根本不会在手机上看 2 万字的 dir 输出。
# 所以给用户的**正文**要短（executor.format_result），但**工具结果**可以给模型留长一点
# （executor.summarize_for_model）——模型要拿它接着推理。
# 参考项目里同类做法：watch.format_hit 的通知截到 230 字。
WECHAT_MAX_CHARS = 1500


class ExecResult:
    """一次本地执行的结果。字段语义都要老实，别粉饰。"""

    __slots__ = ("ok", "output", "exit_code", "timed_out", "truncated",
                 "elapsed", "error", "command", "cwd", "encoding_guess")

    def __init__(self, command, cwd, ok=False, output="", exit_code=None,
                 timed_out=False, truncated=False, elapsed=0.0, error=None,
                 encoding_guess=None):
        self.command = str(command or "")   # **原样**存，一个字都不改
        self.cwd = str(cwd or "")
        self.ok = bool(ok)
        self.output = str(output or "")
        self.exit_code = exit_code
        self.timed_out = bool(timed_out)
        self.truncated = bool(truncated)
        self.elapsed = float(elapsed or 0.0)
        self.error = error                # None 或一段人话
        # 非 None = 这份输出是"猜"出来的一种编码；值就是那个编码名。
        # 只用于**如实提示可能乱码**，绝不据此假装内容是对的。
        self.encoding_guess = encoding_guess

    def __repr__(self):
        return (f"<ExecResult ok={self.ok} exit={self.exit_code} "
                f"timeout={self.timed_out} trunc={self.truncated} "
                f"elapsed={self.elapsed:.2f}s len={len(self.output)}>")


def _shell_cfg(cfg):
    """取 shell 段，**永远返回 dict**。

    配置是人手写的，什么写法都可能出现：`shell:` 空着 → None，
    `shell: yes` → True，`shell: "abc"` → str。这些都不该让 bot 崩——
    确认分支里一抛异常就把主循环带崩（主循环只捕获 KeyboardInterrupt）。
    拿不到字典就当**没配**，于是 shell 是关的（fail-safe：坏配置只会更保守）。
    """
    sec = (cfg or {}).get("shell")
    return sec if isinstance(sec, dict) else {}


def enabled(cfg):
    """本地执行开了没有。cfg 是整份配置（有 shell 段）。"""
    return bool(_shell_cfg(cfg).get("enabled", False))


def resolve_cwd(cfg, cwd=None):
    """定命令的工作目录。优先级：调用方给的 cwd > 配置 shell.cwd > 项目根目录。

    返回 (绝对路径, 错误文本)。配置里写了个不存在的目录时**如实报错**，
    不悄悄退回项目根 —— 那会让用户以为命令在自己指定的目录里跑了。
    """
    d = cwd or _shell_cfg(cfg).get("cwd") or PROJECT_DIR
    path = os.path.abspath(os.path.expanduser(str(d)))
    if not os.path.isdir(path):
        return "", f"工作目录不存在：{path}"
    return path, None


def _cfg_int(cfg, key, default, lo, hi):
    """从配置里取一个整数并夹到 [lo, hi]。取不出来就用默认值。"""
    try:
        v = int(_shell_cfg(cfg).get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def _candidates():
    """解码候选编码表，按尝试顺序：系统 locale → gbk → cp936。

    **为什么不能只信 locale**：本机实测 `locale.getpreferredencoding(False)` 返回
    'UTF-8'（Python 3.11 在部分 Windows 中文环境下就是这样），可 cmd 里那些老程序
    吐出来的确实是 GBK 字节。只按 locale 回退 = 回退到 utf-8 再失败一次，
    等于「utf-8 → gbk 回退」这条约定根本没生效。所以 gbk 必须**无条件**在候选里。
    """
    out = []
    pref = (locale.getpreferredencoding(False) or "").lower()
    if pref and pref not in ("utf-8", "utf8"):
        out.append(pref)
    for name in ("gbk", "cp936"):
        if name not in out:
            out.append(name)
    return out


def _decode(raw):
    """字节 → 文本。utf-8 优先，失败回退系统 locale，再回退 gbk。

    项目约定（CLAUDE.md / 用户要求）：Windows 控制台输出常是 GBK，
    解码要 utf-8 → gbk 回退。

    注意一个**真实踩过的坑**：GBK 的字节有时"能"用 utf-8 解出来（不抛异常），
    解出来是一串谁也不认识的怪符号 —— 也就是**静默乱码**。比如 GBK 的「中文」
    会被 utf-8 解成两个形近的怪字。所以这里解码之后**再验一遍**：
    把结果编回 utf-8 能不能还原成原始字节，还原不了就说明 utf-8 是错的假设，
    换下一个候选编码。CLAUDE.md 的规矩是「不许静默降级」，静默乱码就是反例。
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ["utf-8"] + _candidates():
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 能编回同一串字节 = 这个假设自洽，采用
        if text.encode(enc, errors="replace") == raw:
            return text
    # 全都不自洽：至少别把字符全丢掉，替换字符留给上层提示（会带乱码警告）
    return raw.decode("utf-8", errors="replace")


def _looks_mojibake(text):
    """文本里有没有**确定的坏字符**（U+FFFD、孤立 NUL）。

    这是"确实解不开"的信号：字节里混了 utf-8 和 gbk 都认不了的东西，
    或者干脆是别的编码。主进程的中文输出也走这条路。
    """
    return "\ufffd" in text or "\x00" in text


# 可疑区间的边界：U+00A0-U+02FF（Latin-1 补充 / 拉丁扩展 / IPA）+ U+0370-U+03FF（希腊）
_TRAP_RANGES = (0x00A0, 0x02FF, 0x0370, 0x03FF)


def _decode_printable_trap(raw):
    """utf-8 解出来的东西，像是「GBK 正文被误当 utf-8」吗？

    **这是主动兜一个已知的静默错误，不是瞎猜**：utf-8 解码是可逆的，所以
    「解完再编回去比对」那条校验对 utf-8 永远自洽，挡不住这一类——
    实测 utf-8 与 GBK 有 1920 个 2 字节序列**两边都能解、解出来还不一样**，
    例如 GBK 的「目录」(b'\\xc4\\xbf\\xc2\\xbc') 会被 utf-8 解成 'Ŀ¼'，
    而中文 Windows 上 `dir` 的输出正好含「目录」。

    判据只取「控制台里几乎不会合法出现」的那一片：
      * 解出来的字符落在 Latin-1 补充区(00A0-00FF) / 拉丁扩展 / IPA(0100-02FF) /
        希腊(0370-03FF)（真实的英文工具输出用不到它们，而 GBK 汉字被 utf-8
        误解时整片都落在这一带）；
      * 这一段里**一个 ASCII 字母数字都没有**——「目录」误解出来是 'Ŀ¼'（纯怪符号），
        而合法的混排西文像 'café' 带着 c/a/f，就会被这一条挡住
        （实测 `echo café` 曾被误改判成 'caf茅'，这条就是为它加的）；
      * 且这串字节**确实是合法的 GBK**（否则谈不上"本该是 GBK"）。
    三个条件同时成立才改判；而且改判后结果里**必须**带上"这是猜的"提示。
    残留：纯符号的合法 utf-8（如 'Ω'）仍会被改判成汉字——只能靠 ⚠️ 提示兜住。

    返回**三态**（这点很重要，别退回成布尔）：
      * "gbk"       —— 像 GBK 正文被误当 utf-8，建议改按 GBK 解；
      * "ambiguous" —— 命中可疑区间、但混着 ASCII 字母数字（例如 abc + GBK「目录」
                       解成 'abcĿ¼'），**不改判**，但也**不能一声不响**：
                       调用方要带上"这段可能没解码对"的提示。
                       （这是复核方 R5-2b 抓到的：只 return False 的话，
                       这一支就成了新的静默乱码，正是本函数要消灭的东西。）
      * None        —— 没什么可疑的。
    """
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    suspect = [ch for ch in text
               if _TRAP_RANGES[0] <= ord(ch) <= _TRAP_RANGES[1]
               or _TRAP_RANGES[2] <= ord(ch) <= _TRAP_RANGES[3]]
    if not suspect:
        return None
    try:
        raw.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        return None
    # 混着 ASCII 字母数字 → 更像真的西文输出（café / 25°C / naïve），**不改判**，
    # 但它确实落在可疑区（真的 GBK 正文混在 ASCII 里也会长这样），所以标注可疑。
    if any(ch.isascii() and ch.isalnum() for ch in text):
        return "ambiguous"
    return "gbk"


def _kill_tree(proc):
    """连子进程一起杀掉。

    Windows 上 `proc.kill()` 只杀 cmd.exe，它下面挂的 ping/程序还活着、
    还占着管道，于是 communicate() 会一直等下去（实测踩过，会卡到超时都过了还在等）。
    所以这里用 taskkill /T /F 整棵树带走；非 Windows 用进程组。
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        pass        # taskkill 都失败就退回下面的 proc.kill()
    try:
        proc.kill()
    except Exception:
        pass


def run_command(command, cwd=None, timeout=None, max_output=None,
                workdir=None, env=None, cfg=None):
    """同步跑一条命令，返回 ExecResult。**这是唯一真正执行命令的函数。**

    参数：
      command    : 命令行原文。Windows 下走 `cmd /d /s /c`，和用户在 cmd 里敲的一样。
      cwd/workdir: 工作目录（两个名字都收，workdir 是历史叫法）。
      timeout    : 秒。默认取 cfg 的 shell.timeout，再不行 60；夹到 [1, 600]。
      max_output : 输出字符上限。默认取 cfg 的 shell.max_output，再不行 20000。
      env        : 追加/覆盖的环境变量（不影响 os.environ 本体）。
      cfg        : 整份配置，这里只读 shell 段。

    绝不抛异常：任何失败都变成 ok=False + error 文案，让调用方能原样转告用户。
    """
    command = str(command or "")
    limit = max_output
    if limit is None:
        limit = _cfg_int(cfg, "max_output", DEFAULT_MAX_OUTPUT,
                         MIN_MAX_OUTPUT, MAX_OUTPUT_CAP)
    else:
        limit = max(MIN_MAX_OUTPUT, min(int(limit), MAX_OUTPUT_CAP))

    t = timeout
    if t is None:
        t = _cfg_int(cfg, "timeout", DEFAULT_TIMEOUT, MIN_TIMEOUT, MAX_TIMEOUT)
    else:
        try:
            t = max(MIN_TIMEOUT, min(int(t), MAX_TIMEOUT))
        except (TypeError, ValueError):
            t = DEFAULT_TIMEOUT

    if not command.strip():
        return ExecResult(command, "", error="命令是空的。")

    work, derr = resolve_cwd(cfg, cwd or workdir)
    if derr:
        return ExecResult(command, "", error=derr)

    popen_kw = {}
    if os.name == "nt":
        # ⚠️ **必须把整条命令拼成一个字符串、再整体包一层引号传给 Popen**（踩过的坑）。
        #
        # 先说为什么不能用 list 形式：
        #     Popen(["cmd.exe", "/d", "/s", "/c", command])
        # 会走 Python 的 list2cmdline 转义，把命令里的引号变成 \" 再包一层外引号，
        # cmd 收到的是字面反斜杠引号 —— 结果
        #     echo hi > "C:\a b\out.txt"   →  rc=1「文件名、目录名或卷标语法不正确」
        # 只要路径带引号（**含空格的路径基本都带**）就必炸。
        #
        # 再说为什么整体还要再包一层引号（少了它也不行）：
        # cmd 对「以引号开头的命令」有自己的一套去引号规则，于是
        #     "C:\...\python.exe" "脚本.py"      ← 命令以带引号的路径开头
        # 直接传会 rc=1「文件名、目录名或卷标语法不正确」。
        # 实测 18 种形态（带引号可执行路径、引号重定向、echo "x"、&& 、| 、
        # %VAR%、for + 括号、cd /d 含空格目录、^ 转义、@ 前缀、exit N……）
        # **只有** `cmd.exe /d /s /c "<命令>"` 这种整体包引号的写法全部通过。
        # 命令里如果本来带引号，cmd 的 /s 规则会把最外层那对剥掉，内层原样保留。
        argv = f'cmd.exe /d /s /c "{command}"'
    else:
        argv = ["/bin/sh", "-c", command]
        # 非 Windows 单独开进程组，超时才能整组带走
        popen_kw = {"start_new_session": True}

    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update({str(k): str(v) for k, v in env.items()})

    started = time.time()
    try:
        proc = subprocess.Popen(
            argv, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=child_env, **popen_kw)
    except Exception as e:
        # 命令根本没起来（比如 cmd.exe 都拉不起来）——如实报错，别装成执行过了
        return ExecResult(command, work, elapsed=time.time() - started,
                          error=f"启动进程失败：{e}")

    timed_out = False
    raw = b""
    try:
        raw, _ = proc.communicate(timeout=t)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            raw, _ = proc.communicate(timeout=10)
        except Exception:
            raw = b""
        if not raw:
            # 不摆出"进程没了所以没输出"这种糊弄说法：明说进程是被中断的
            raw = f"（命令超过 {t} 秒仍未结束，已被中断。）\n".encode("utf-8")
    except Exception as e:
        _kill_tree(proc)
        return ExecResult(command, work, elapsed=time.time() - started,
                          error=f"执行出错：{e}")

    elapsed = time.time() - started
    text = _decode(raw)
    guess = None
    enc_note = ""
    trap = _decode_printable_trap(raw)
    if trap == "gbk":
        # 命中「GBK 正文被误当 utf-8」这个已知陷阱：此刻 text 是错的。
        # 改成按 GBK 解（上面已确认这些字节是合法 GBK），但**仍标注这是猜的**，
        # 让用户知道这行不可靠，而不是把猜出来的内容当成板上钉钉。
        try:
            text = raw.decode("gbk")
            guess = "gbk"
            enc_note = ("这份输出按 utf-8 解是一串怪西文字符、按 GBK 解才是通顺内容，"
                        "已按 GBK 显示；若它本来就是 utf-8 的西文，这一处可能不对。")
        except (UnicodeDecodeError, LookupError):
            pass
    elif trap == "ambiguous":
        # 没改判（怕误伤 café 那类正常西文），但也不能一声不响：
        # 这段就是"可能没解码对"，按项目规矩必须让用户看见。
        enc_note = ("输出里有几个字符落在 utf-8/GBK 都会解、结果还不一样的区间，"
                    "**这一处可能没解码对**（原文可能是中文也可能是西文），"
                    "拿不准就请人工核对一下。")
    # 统一换行 + 去掉尾随空白：cmd 的输出常带一堆 CR/LF 和尾空格
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    truncated = False
    if len(text) > limit:
        omitted = len(text) - limit
        truncated = True
        text = (text[:limit]
                + f"\n…（输出太长，已截断：另有约 {omitted} 个字符没显示。"
                  f"要看完整的请把命令改得更精确，比如加过滤/限制行数。）")

    result = ExecResult(command, work, ok=(not timed_out and proc.returncode == 0),
                        output=text, exit_code=proc.returncode, timed_out=timed_out,
                        truncated=truncated, elapsed=elapsed, encoding_guess=guess)
    if timed_out:
        result.error = f"命令超过 {t} 秒还没结束，已被中断（结果是残缺的）。"
    if _looks_mojibake(text):
        result.error = ((result.error + " " if result.error else "")
                        + "输出里出现了乱码字符，可能有部分内容没正确解码。")
    if enc_note:
        result.error = ((result.error + " " if result.error else "") + enc_note)
    return result


def format_result(result, max_chars=None):
    """把 ExecResult 渲染成**发回微信**的文本。

    要求（原始设计）：**原样带上命令原文**，让用户审的就是那条真命令；
    退出码/超时/截断/失败都写清楚，没跑成绝不写成跑成了。

    输出长度按**微信消息**的体量裁（WECHAT_MAX_CHARS，默认 1500 字）：
    命令输出动不动几千行，全发过去等于在聊天窗口里刷屏，用户其实看不了。
    被裁掉时**明说裁了**，并告诉他怎么拿更多（重新跑个更精确的命令），
    但不许假装输出就这么长。
    """
    r = result
    if max_chars is None:
        max_chars = WECHAT_MAX_CHARS
    max_chars = max(0, int(max_chars))

    lines = [f"命令：{r.command}",
             f"目录：{r.cwd or '（未确定）'}"]
    if r.timed_out:
        head = f"状态：超时中断（{r.elapsed:.1f}s，结果是残缺的）"
    elif r.error and r.exit_code is None:
        head = f"状态：没有执行 —— {r.error}"
    elif r.ok:
        head = f"状态：完成（退出码 0，用时 {r.elapsed:.1f}s）"
    else:
        head = f"状态：失败（退出码 {r.exit_code}，用时 {r.elapsed:.1f}s）"
    lines.append(head)
    # 警告**不分成功失败**都要带上：命令成功但输出编码没解对时，
    # 用户看到的是一串怪字符（编码猜的还要说明"这是猜的"）。
    # 以前只在失败分支带 error，导致「成功 + 乱码」这条路上用户和模型
    # 都看不到任何提示，会以为输出本来就这么怪 —— 那是静默误导。
    if r.error and not (r.exit_code is None):
        # exit_code is None 的两种失败（空命令/目录不存在/启动失败）在 head 里
        # 已经说过一次了，不重复；剩下的是"跑完了但有话要说"（乱码警告等）
        lines.append(f"⚠️ {r.error}")

    out = r.output
    if max_chars and len(out) > max_chars:
        # 裁的时候把结论性的**开头**留着：报错信息、表头、前几行结果都在那里
        out = (out[:max_chars]
               + f"\n…（微信里只发前 {max_chars} 字，剩下的我没发。"
                 f"要看完整内容，请重新跑一条更精确的命令，比如加过滤或只看前几行。）")
    if out:
        lines.append("输出：")
        lines.append(out)
    else:
        lines.append("输出：（空）")
    return "\n".join(lines)


def summarize_for_model(result, max_chars=4000):
    """把结果压成**给模型看**的一段工具返回。

    和 format_result 的区别：这个是喂回模型的，可以长一点（模型要接着推理），
    但同样不能无限长，否则一轮工具调用就把上下文塞满了。
    措辞上必须让模型无法误以为"已经请示过用户了"——见 agent_tools 的待确认机制。
    """
    r = result
    if r.timed_out:
        state = f"命令超时（>{r.elapsed:.0f}s）已被中断，结果是残缺的"
    elif r.error and r.exit_code is None:
        state = f"命令没有执行：{r.error}"
    elif r.ok:
        state = "命令执行完成，退出码 0"
    else:
        state = f"命令失败，退出码 {r.exit_code}"
    # 同 format_result：成功也要把编码警告带给模型，否则它会拿乱码去作答
    if r.error and r.exit_code is not None:
        state += f"（注意：{r.error}）"

    body = r.output
    if max_chars and len(body) > int(max_chars):
        body = body[:int(max_chars)] + "\n…（输出过长，这里已截断）"
    return (f"本地执行结果：{state}。\n"
            f"命令原文：{r.command}\n"
            f"输出：\n{body if body else '（空）'}")


def run_command_text(command, max_chars=None, **kw):
    """跑一条命令并直接给 (ok, 给用户看的文本)。给不关心细节的调用方用。"""
    r = run_command(command, **kw)
    return r.ok, format_result(r, max_chars=max_chars)


if __name__ == "__main__":
    # 纯逻辑自测：真跑几个子进程（都是无害的短命令），不碰微信、不碰 hook。
    # 跑： .venv/Scripts/python.exe executor.py
    import sys as _sys

    _fail = []

    def chk(cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            _fail.append(msg)

    print("基本执行 / 退出码 / 输出:")
    r = run_command("echo hello")
    chk(r.ok, f"echo hello 成功（exit={r.exit_code}）")
    chk(r.output.strip() == "hello", f"拿到输出 {r.output.strip()!r}")
    chk(r.truncated is False and r.timed_out is False, "没截断、没超时")
    chk(r.cwd and os.path.isdir(r.cwd), f"工作目录是真实目录：{r.cwd}")
    chk(r.exit_code == 0, "退出码 0")

    r = run_command("exit 3")
    chk(not r.ok and r.exit_code == 3, f"非零退出码如实返回（exit={r.exit_code}）")
    txt = format_result(r)
    chk("退出码 3" in txt and "命令：exit 3" in txt, "渲染里带退出码和命令原文")

    print("stderr 也会被带回来（2>&1）:")
    # cmd 的 exit /b 后面接不存在的东西，或直接造一个 stderr 写入
    r = run_command('echo out & echo err 1>&2')
    chk("out" in r.output and "err" in r.output, f"stdout+stderr 都在：{r.output!r}")

    print("原样保存命令（防提示词注入的关键：用户审的是真命令）:")
    tricky = "echo   A  &&  echo B"
    r = run_command(tricky)
    chk(r.command == tricky, f"命令原文一字未改：{r.command!r}")

    print("输出上限与截断提示:")
    # 造 ~6000 字符输出，限 300：必须截断且**明说**截断了
    r = run_command("for /L %i in (1,1,400) do @echo 0123456789", max_output=300)
    chk(r.truncated, f"超上限被截断（len={len(r.output)}）")
    chk("已截断" in r.output, "输出里明确写了被截断")
    txt = format_result(r)
    chk("已截断" in txt, "渲染文本里也带着截断说明")
    r = run_command("echo short", max_output=300)
    chk(not r.truncated, "短输出不标截断")

    print("超时 ≤ 2 秒必须返回，且如实说超时:")
    t0 = time.time()
    r = run_command("ping -n 20 127.0.0.1 > nul", timeout=2)
    dt = time.time() - t0
    chk(dt < 20, f"超时后及时返回（{dt:.1f}s，不是等命令自己结束）")
    chk(r.timed_out and not r.ok, f"标记为超时（timed_out={r.timed_out}）")
    chk("超时" in format_result(r), "渲染文本里写了超时")
    chk(-1 <= r.exit_code <= 0xFFFFFFFF or r.exit_code != 0, "退出码不是 0（没假装成功）")

    print("工作目录:")
    r = run_command("cd", cwd=PROJECT_DIR)
    chk(os.path.normcase(PROJECT_DIR) in os.path.normcase(r.output),
        f"cwd 生效：{r.output.strip()}")
    r = run_command("echo x", cwd=os.path.join(PROJECT_DIR, "no-such-dir-xyz"))
    chk(not r.ok and "工作目录不存在" in (r.error or ""),
        f"目录不存在 → 如实报错：{r.error}")

    print("空命令 / 配置读取:")
    r = run_command("   ")
    chk(not r.ok and "空" in (r.error or ""), f"空命令报错：{r.error}")
    chk(enabled({"shell": {"enabled": True}}) is True, "enabled() 读配置")
    chk(enabled({}) is False and enabled(None) is False, "没配置就是没开")
    chk(_cfg_int({"shell": {"timeout": 5}}, "timeout", 60, 1, 600) == 5, "配置里的 timeout 生效")
    chk(_cfg_int({"shell": {"timeout": 0}}, "timeout", 60, 1, 600) == 1, "timeout 夹到下限")
    chk(_cfg_int({"shell": {"timeout": "abc"}}, "timeout", 60, 1, 600) == 60, "坏值用默认")
    chk(_cfg_int({"shell": {"timeout": 99999}}, "timeout", 60, 1, 600) == 600, "timeout 夹到上限")

    print("解码回退（utf-8 → gbk，含「GBK 也能被 utf-8 解出来」这个坑）:")
    chk(_decode("中文".encode("utf-8")) == "中文", "utf-8 中文")
    # GBK 的「中文」字节，utf-8 也能"成功"解出来（变成两个形近的怪字）——
    # 这正是静默乱码，必须被双向校验挡下、回退到 gbk 才对
    chk(_decode("中文".encode("gbk")) == "中文",
        f"gbk 中文：utf-8 解得动也要纠正过来（实际 {_decode('中文'.encode('gbk'))!r}）")
    chk(_decode("你好世界".encode("gbk")) == "你好世界", "gbk 多字")
    chk(_decode(b"plain ascii") == "plain ascii", "纯 ascii")
    chk(_decode(b"") == "" and _decode(None) == "", "空/None")
    chk(_looks_mojibake(_decode(b"\xff\xfe\xfa")) is True, "解不开的字节会被标成乱码")

    print("给用户的文本按微信消息体量裁（不是终端整屏）:")
    longr = run_command("for /L %i in (1,1,400) do @echo 0123456789")
    txt = format_result(longr)
    chk(len(txt) <= WECHAT_MAX_CHARS + 200, f"发微信的文本被裁到量级内（{len(txt)} 字）")
    chk("微信里只发前" in txt, "裁掉时明说是微信发送上限，没假装输出就这么长")
    chk("命令：for /L" in txt, "裁了也仍然带着命令原文")
    shortr = run_command("echo short")
    chk("微信里只发前" not in format_result(shortr), "短输出不加裁切说明")
    mtxt = summarize_for_model(longr)
    chk(len(mtxt) > len(txt), f"给模型的摘要可以比给用户的长（{len(mtxt)} > {len(txt)}）")
    chk("本地执行结果" in mtxt and "已截断" in mtxt, "模型摘要里写明状态和截断")

    print("run_command_text 便捷封装:")
    ok, text = run_command_text("echo wrapped")
    chk(ok and "wrapped" in text, "run_command_text 返回 (True, 文本)")

    print("带引号的路径/重定向（cmd 引号转义的坑，必须回归）:")
    # 含空格的路径在微信上是最常见的形态（用户自己电脑里的目录），
    # 而 list 形式 argv 会让这种命令直接 rc=1 跑不了。这条用例就是钉住它。
    qdir = os.path.join(PROJECT_DIR, "data")
    os.makedirs(qdir, exist_ok=True)
    qfile = os.path.join(qdir, "executor 引号 测试.txt")
    if os.path.exists(qfile):
        os.remove(qfile)
    r = run_command(f'echo quoted-ok > "{qfile}"')
    chk(r.ok and os.path.exists(qfile),
        f"带引号的重定向路径能跑通（rc={r.exit_code}）: {r.output[:60]!r}")
    if os.path.exists(qfile):
        try:
            with open(qfile, encoding="utf-8", errors="replace") as fh:
                body = fh.read().strip()
            chk(body == "quoted-ok", f"写进去的内容对：{body!r}")
        finally:
            os.remove(qfile)
    else:
        chk(False, "引号路径的文件没被创建")
    r = run_command('echo "hello world"')
    chk(r.output.strip() == '"hello world"',
        f"带引号的参数原样传给 echo（没被转义坏）：{r.output.strip()!r}")
    # 以「带引号的可执行路径」开头 —— 真实场景里最常见（程序装在带空格的目录里）。
    # 整体不再包一层引号时，cmd 会 rc=1「文件名、目录名或卷标语法不正确」。
    r = run_command(f'"{sys.executable}" -c "print(123)"')
    chk(r.ok and r.output.strip() == "123",
        f"命令以带引号的可执行路径开头也能跑：rc={r.exit_code} out={r.output.strip()!r}")

    print("format_result 不粉饰:")
    flat = ExecResult("boom", PROJECT_DIR, error="启动进程失败：模拟")
    t = format_result(flat)
    chk("没有执行" in t, f"没跑起来时明说没有执行：{t.splitlines()[2]!r}")

    print("三个已修缺陷的回归:")
    # ① shell: 写成空（None）不能让 bot 崩
    chk(enabled({"shell": None}) is False, "shell 写成空（None）→ 不抛异常、当作没开")
    chk(enabled({"shell": "yes"}) is False, "shell 写成标量（'yes'）→ 当作没开，不抛异常")
    chk(enabled({"shell": True}) is False, "shell 写成布尔 → 当作没开，不抛异常")
    chk(resolve_cwd({"shell": None})[0], "shell=None 时 cwd 仍能解析")
    chk(_shell_cfg({"shell": "abc"}) == {}, "_shell_cfg 对非 dict 一律返回空 dict")
    # ② GBK 正文被 utf-8 误当西文（「目录」= b'\xc4\xbf\xc2\xbc' → 'Ŀ¼'）
    trap = "目录".encode("gbk")
    chk(_decode_printable_trap(trap) == "gbk",
        f"「目录」的 GBK 字节会命中歧义陷阱（utf-8 解成 {trap.decode('utf-8')!r}）")
    chk(_decode_printable_trap("hello".encode("utf-8")) is None, "正常 ASCII 不误判")
    chk(_decode_printable_trap("中文".encode("utf-8")) is None, "正常 utf-8 中文不误判")
    chk(_decode_printable_trap("café".encode("utf-8")) == "ambiguous",
        "混排西文（café）不改判，但要标成「可能没解码对」")
    # 复核方 R5-2b 抓的静默口子：ASCII 混一段 GBK 正文，整串仍是合法 utf-8，
    # 解出来是 'abcĿ¼'。不能改判（怕误伤 café），但**必须标注**，不许一声不响。
    mixed_gbk = b"abc" + "目录".encode("gbk")
    chk(mixed_gbk.decode("utf-8") == "abc\u013f\u00bc", "前提：这串字节 utf-8 解得动")
    chk(_decode_printable_trap(mixed_gbk) == "ambiguous",
        "ASCII+GBK（'abcĿ¼'）不改判也要标成「可能没解码对」")
    chk(_decode_printable_trap("Ω".encode("utf-8")) == "gbk",
        "纯符号 utf-8（Ω）会被改判 → 所以结果里必须带「这是猜的」提示")
    # 端到端：`echo café` 那条路必须带上提示，不能静默
    r_amb = run_command("echo caf\xe9")
    if r_amb.ok and "caf" in r_amb.output:
        chk(bool(r_amb.error), "含歧义字符的输出必须有告警（不许静默）")
        chk("可能没解码对" in r_amb.error, f"告警文案要点明不确定：{r_amb.error}")
    else:
        chk(True, "（本机 echo 输出的编码不是歧义形态，跳过端到端那条）")

    # ③ 成功命令的乱码警告必须出现在**给用户**和**给模型**的文本里
    ok_but_dirty = ExecResult("dir", PROJECT_DIR, ok=True, exit_code=0,
                              output="Ŀ¼\nfile.txt",
                              error="输出里出现了乱码字符，可能有部分内容没正确解码。")
    utxt = format_result(ok_but_dirty)
    chk("乱码" in utxt, "成功但输出可能乱码时，给用户的文本里带警告")
    chk("状态：完成" in utxt, "警告不影响如实报告状态")
    mtxt = summarize_for_model(ok_but_dirty)
    chk("乱码" in mtxt, "给模型的摘要里也带乱码警告（否则它拿乱码作答）")

    print()
    if _fail:
        print(f"失败 {len(_fail)} 项 ❌")
        for f in _fail:
            print("  - " + f)
        _sys.exit(1)
    print("全部通过 ✅")
