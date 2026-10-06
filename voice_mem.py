"""从**微信进程内存**里读语音条（`local_type=34`），转成文字。

## 为什么走内存

语音条的音频**不在磁盘上**。2026-10-03 拿用户刚发的那条真样本（1812 字节 /
1.24 秒 / silk）把三处都翻遍了：

  * `msg\\attach\\<md5(会话)>\\<月>\\{Rec,Img}\\` —— 按大小命中的全是明文富文本和图片缩略图；
  * `cache\\<月>\\Message\\<md5(会话)>\\{Bubble,Thumb,ImageTemp,SendTemp}` —— 只有图和缩略图；
  * 20 个 `VoiceTemp` 目录 —— 只有 2 个 **0 字节**占位文件。

但微信**必须**持有音频才能播放它。实测在 Weixin.exe 内存里能搜到 70+ 处
`#!SILK_V3`，而且都是**明文 SILK**（紧跟 uint16 帧长），连 aeskey 都不用。

## 边界（必须如实说，不许含糊）

* 只读：`ReadProcessMemory`，一个字节都不写微信（和改代码段的字节补丁不是一类东西）；
* 只能读到微信**最近碰过**的语音（刚发的 / 刚播的）。很久没碰过的可能已经不在内存里了；
  **2026-10-03 侦察实测**：两遍扫描之间内存里的 SILK 从 148 处掉到 99 处，
  12 条语音里有 4 条的音频已经不在内存 —— 这部分**修不了**，只能如实说；
* 定位有两级信号：
  ① **消息 XML 的 `length`**（加密字节数）—— 内存里那条 SILK 的真实长度精确对应它
     （自己发出的样本实测 8/8 差 −1；错误候选差 200~1900），这一级能分开"同时长的两条"；
  ② **时长**（`voicelength`）+ 容忍度 —— 指纹没命中时（例如别人发来的语音）用它。
  ⚠️ **绝不能只在时长上做文章**：微信报的 `voicelength` 比真实音频长 20~40ms，
  按它估算帧数会**多算 1~2 帧**，把内存里完整存在的候选误判成"帧数不够"
  （2026-10-03 长语音全读不出来的根因，见 `silk_for_duration` 的注释）。
* 命不中指纹、又有多条时长接近时，靠 whisper 置信度分辨；**分不出来就拒答**，
  绝不替调用方挑 —— 挑错了就是**把别人的话安到他头上**；
* 扫 600MB 要 ~5 秒，**会占住收消息那条线程**（hook 不支持并发，见 CLAUDE.md：
  任何并发加速的想法都会让微信崩）。所以调用方要清楚这个代价；
* 扫不到 / 解不出 / 转写空 —— **一律如实说，绝不拿别的语音顶上**。

## 依赖

`pilk`（SILK 解码，纯本地）+ `faster-whisper`（转写，本地模型，音频不出本机）。
`pilk` 有**两代 API**，见 `voice_msg.to_pcm` 里的说明 —— 只写老 API 会一条都解不出来。
"""
import ctypes
import hashlib
import os
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MAGIC = b"#!SILK_V3"
WINDOW = 64 * 1024          # 每个 magic 往后最多看多少
MATCH_TOL_MS = 150
MAX_CANDIDATES = 8
FRAME_MS = 20               # 微信 SILK 每帧 20 毫秒（用来按帧数估时长，省掉解码探测）

# 扫内存的**硬时间上限**（秒）。默认 **20**。
#
# 它是安全线，不是性能参数：这个调用**同步跑在收消息那条线程**上，而 `scan_silk`
# 在 128TB 地址空间里逐段读 —— 没有上限时，微信让某次 `ReadProcessMemory` 一卡，
# `read()` 就永远不返回，整个 bot 停摆（2026-10-03 真机：日志停在
# 「处理自己的消息: [语音条…]」，之后再无心跳）。
#
# ⚠️ **为什么从 8 提到 20**（2026-10-03 晚真机实测，不是估的）：一次**完整**扫描
# 实测 4.0 / 4.9 / 16.0 / 2.1 / 2.3 / 2.0 秒 —— 抖动极大，而且**耗时与扫了多少字节
# 不成比例**（1071MB 用 4.0 秒、430MB 却要 2.1 秒、758MB 要 11.1 秒）：时间主要花在
# **冷页缺页**上（微信被换出去的页，读它要等磁盘）。所以 8 秒正好落在抖动区间里，
# 真机上开始随机「扫不完就放弃」—— 当晚 3 条新失败里 2 条就是它，而这两条**不是**
# 代码算错，是预算不够。提到 20 秒可以覆盖实测上界（16.0）。
# 代价说清楚：最坏情况下这条语音会让轮询停 ~20 秒（再加转写 5~8 秒），
# 这期间消息**不丢**、排着队回来照收 —— 和以前群发/跑命令是同一档代价。
# 用户嫌停太久就把 `voice.scan_seconds` 调小（或 `voice.auto_read: false` 直接关掉）。
DEFAULT_SCAN_SECONDS = 20.0


def _cfg_float(cfg, sect, key, default):
    """从配置读一个浮点数；读不到/非法就用默认（**告警，不静默**）。"""
    sec = ((cfg or {}).get(sect) or {})
    if not isinstance(sec, dict) or key not in sec:
        return default
    try:
        return float(sec.get(key))
    except (TypeError, ValueError):
        print(f"⚠️ voice_mem: {sect}.{key} 不是数字（{sec.get(key)!r}），"
              f"用默认 {default}", file=sys.stderr, flush=True)
        return default


# 裁剪必须贴到目标时长多近。**固定收紧**，不跟调用方的"留多少候选"走：
# 放宽它只会把音频裁歪（2026-10-03 踩过：tol 一大就早停在 1400ms，
# 一段 1600ms 的语音被裁短，转出来的字全变了）。
# 真机实测（2026-10-03 侦察，8/8）：微信报的 `voicelength` 比真实音频**长 20~40ms**，
# 所以这个 60ms 的余量刚好够；**但绝不能**再拿它当"帧数够不够"的硬判据（见
# `silk_for_duration` 的 `est` 闸 —— 那个才是"长语音读不出来"的根因）。
SILK_TOL_MS = 60

# `length` 指纹的容差（字节）。为什么是它：2026-10-03 真机侦察（`_audit/probe_voice_bytes.py`）
# 实测 8/8 条**自己发出的**语音，内存里那条 SILK 的真实长度（裁到帧边界）
# **精确等于 XML 的 `length` − 1**；而按时长挑出来的错误候选差 200~1900 字节。
# 所以"接近 length"能把同时长的不同语音分开 —— 这就是那个一直缺的"唯一标识"。
#
# ⚠️ **只当优先信号，绝不当硬门槛**：−1 这个关系只在"自己发出的"样本上验证过；
# **别人发来的**语音走的是加密载荷长度，关系可能不同（2026-10-03 那条 2.38 秒的
# 手机语音就是）。当硬门槛会把 incoming 全判死 —— 一条都读不出来。
# 所以规则是：**有候选命中指纹就用它，一个都没命中就完全退回原来的时长逻辑**。
BYTE_TOL = 2

# 置信度闸门（**经验值，不是校准过的概率**）：
#   POOR_SCORE 以下 = 听不清；前两名文本不同且分差小于 TIE_MARGIN = 分不出来。
# 两个闸门的方向都是"**宁可拒答，也不给一句可能是别人的话**"——
# 在这里猜错的代价是**拿别人的话去执行**，比读不出来严重得多。
POOR_SCORE = -1.8
TIE_MARGIN = 0.35

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010


def available():
    """返回 `(能不能用, 一句人话)`。缺什么要说清怎么补。"""
    if not sys.platform.startswith("win"):
        return False, "读微信内存这条路只在 Windows 上成立。"
    try:
        import pilk  # noqa: F401
    except ImportError:
        return False, ("SILK 解码要 pilk：.venv\\Scripts\\python.exe -m pip install pilk")
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False, ("转写要 faster-whisper："
                       ".venv\\Scripts\\python.exe -m pip install faster-whisper")
    return True, "可以用（读微信内存里的 SILK，本地转写）"


# ---------------- 进程 / 内存 ----------------

def _k32():
    return ctypes.windll.kernel32


class ProcessProbe:
    """`weixin_main_process(probe=...)` 的诊断出口（给人看的失败原因）。

    为什么要有它：`read()` 以前把"找不到进程"和"进程在、但打不开"**混成同一句**
    ——「微信没在跑？或者权限不够」。2026-10-06 真机就是这么骗人的：微信好好跑着
    （进程表里 5 个 `Weixin.exe`），只是它**以 High 完整性启动**（用户提权开了微信），
    而助手是 Medium，于是 `OpenProcess(QUERY_INFORMATION|VM_READ)` 被系统拒绝
    （`GetLastError=5`）。用户照着那句提示去查「微信是不是没开」，方向完全错了。

    所以把两件事分开记：`saw_any` = 进程表里有没有 `Weixin.exe`；`denied` = 有进程、
    但一个 `OpenProcess` 都没成功（跨完整性级别 / 权限不足 / 被安全软件拦）。
    """

    __slots__ = ("saw_any", "denied")

    def __init__(self):
        self.saw_any = False
        self.denied = False


def weixin_main_process(probe=None):
    """找主微信进程（**有 Weixin.dll 的那个**）。找不到返回 `(None, "")`。

    ⚠️ 判据是「模块表里有没有 Weixin.dll」，不是「内存最大的那个」：
    后者要先问 `GetProcessMemoryInfo`，而它在 kernel32 里不存在（真名
    `K32GetProcessMemoryInfo`），ctypes 抛 AttributeError 再被吞掉，
    就变成「一个进程都没找到、却什么都不报」——踩过。

    `probe`（可选，见 `ProcessProbe`）：只填诊断字段，**不改返回值** ——
    既有的两元组契约和自测都不动。
    """
    import ctypes.wintypes as wt
    k32 = _k32()
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pids = []
    if snap and snap != -1:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(pe)
        if k32.Process32First(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.decode("mbcs", "ignore").lower() == "weixin.exe":
                    pids.append(pe.th32ProcessID)
                if not k32.Process32Next(snap, ctypes.byref(pe)):
                    break
        k32.CloseHandle(snap)
    if probe is not None:
        probe.saw_any = bool(pids)

    for pid in pids:
        h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            if probe is not None:
                probe.denied = True
            continue
        if _has_weixin_dll(pid):
            return pid, h
        k32.CloseHandle(h)
    return None, ""


def _has_weixin_dll(pid):
    import ctypes.wintypes as wt
    k32 = _k32()
    TH32CS_SNAPMODULE = 0x00000008
    TH32CS_SNAPMODULE32 = 0x00000010

    class MODULEENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
                    ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
                    ("ProccntUsage", wt.DWORD),
                    ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                    ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
                    ("szModule", ctypes.c_char * 256),
                    ("szExePath", ctypes.c_char * 260)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == -1:
        return False
    me = MODULEENTRY32()
    me.dwSize = ctypes.sizeof(me)
    found = False
    ok = k32.Module32First(snap, ctypes.byref(me))
    while ok:
        if me.szModule.decode("mbcs", "ignore").lower() == "weixin.dll":
            found = True
            break
        ok = k32.Module32Next(snap, ctypes.byref(me))
    k32.CloseHandle(snap)
    return found


def _read_mem(h, addr, n):
    k32 = _k32()
    buf = ctypes.create_string_buffer(n)
    got = ctypes.c_size_t(0)
    if k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
        return buf.raw[:got.value]
    return b""


def scan_silk(h, chunk=4 * 1024 * 1024, deadline=None):
    """扫出一段进程里所有 `#!SILK_V3` 的地址。**只读**。

    `deadline`（`time.monotonic()` 的绝对时刻）是**硬时间上限**：到了就停下，
    返回 `(已经找到的地址, 是否扫完)`。

    ⚠️ **为什么必须有这个上限**（2026-10-03 真机踩的）：这个函数跑在**收消息那条
    线程**上（`bot.read_voice_message` 是同步的），而它在 128TB 地址空间里逐段
    `VirtualQueryEx` + 每段 `ReadProcessMemory` 4MB —— **全程没有任何时间上限**。
    微信进程一旦让某次 `ReadProcessMemory` 卡住，这里就**永远不返回**，
    整个 bot 停摆：不轮询、不回复、再发语音也没人接。
    真机表现就是日志停在 `处理自己的消息: [语音条…]` 那一行，之后再无心跳。
    """
    import ctypes.wintypes as wt
    import time
    k32 = _k32()
    MEM_COMMIT = 0x1000
    PAGE_GUARD = 0x100
    PAGE_NOACCESS = 0x01
    READABLE = {0x02, 0x04, 0x20, 0x40, 0x80}

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_ulonglong),
                    ("AllocationBase", ctypes.c_ulonglong),
                    ("AllocationProtect", wt.DWORD), ("__a1", wt.DWORD),
                    ("RegionSize", ctypes.c_ulonglong),
                    ("State", wt.DWORD), ("Protect", wt.DWORD),
                    ("Type", wt.DWORD), ("__a2", wt.DWORD)]

    mbi = MBI()
    addr, hits = 0, []
    complete = True
    while addr < 0x7FFFFFFFFFFF:
        if deadline is not None and time.monotonic() > deadline:
            complete = False
            break
        if not k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)):
            break
        base, size, state, prot = mbi.BaseAddress, mbi.RegionSize, mbi.State, mbi.Protect
        if (state == MEM_COMMIT and size > 0 and (prot & PAGE_NOACCESS) == 0
                and (prot & PAGE_GUARD) == 0 and (prot & 0xFF) in READABLE):
            off = 0
            while off < size:
                n = min(chunk, size - off)
                data = _read_mem(h, base + off, n)
                if data:
                    i = data.find(MAGIC)
                    while i >= 0:
                        hits.append(base + off + i)
                        i = data.find(MAGIC, i + 1)
                off += n
        addr = base + size or addr + 0x1000
    return sorted(set(hits)), complete


# ---------------- SILK ----------------

def trim_silk(d, limit=None):
    """按 SILK v3 的块结构裁到真结尾；`limit` 是调用方给的硬上界。

    ⚠️ **不能只用块结构**：内存里 SILK 后面跟着别的堆数据，那些字节也能凑成
    合法长度，于是一段 1.2 秒的语音会被裁成 55KB，尾部被当成帧去解，
    解码器刷一屏 `SKP_Silk_SDK_Decode returned -12`。
    ⚠️ **也不能只用「到下一个 magic 的距离」**：实测会把一条语音切成
    一堆 0.02~0.04 秒的小包，那条 1.24 秒的语音直接消失。
    **两种都要试**（见 `candidates()`）。
    """
    end = len(d) if limit is None else min(len(d), int(limit))
    pos = len(MAGIC)
    while pos + 2 <= end:
        n = int.from_bytes(d[pos:pos + 2], "little")
        if n == 0 or pos + 2 + n > end:
            break
        pos += 2 + n
    return d[:pos]


def frame_ends(d, limit=None):
    """返回每个完整 SILK 帧**结束**的偏移（含 9 字节头）。"""
    end = len(d) if limit is None else min(len(d), int(limit))
    pos, out = len(MAGIC), []
    while pos + 2 <= end:
        n = int.from_bytes(d[pos:pos + 2], "little")
        if n == 0 or pos + 2 + n > end:
            break
        pos += 2 + n
        out.append(pos)
    return out


def _silk_ms(silk):
    """SILK 字节 → 时长（毫秒）；读不出返回 None。

    ⚠️ **复用同一个临时文件**，不要每次 `mkdtemp`：裁剪时要做几十次时长探测
    （帧数估算 + 邻居校验），每次都建目录 + 写盘能吃掉几十秒
    （2026-10-03 实测：改成复用之后 `read()` 从 64 秒降到个位数）。
    """
    import pilk
    global _MS_TMP
    try:
        p = _MS_TMP
    except NameError:
        p = _MS_TMP = os.path.join(tempfile.gettempdir(), "voicemem_probe.silk")
    try:
        with open(p, "wb") as f:
            f.write(silk)
        if hasattr(pilk, "get_duration"):
            return float(pilk.get_duration(p))
    except Exception:
        return None
    return None


def silk_for_duration(raw, target_ms, tol_ms=60):
    """按**消息自己给的时长**（XML 里的 `voicelength`）裁出这段语音。

    为什么要按时长裁，而不是「裁到块结构结束」或「裁到下一个 magic」：

      * 内存里 SILK 后面紧跟着别的堆数据，那两刀都只能给一个**近似**的边界 ——
        同一段音频会裁出「少一帧」「多一堆垃圾」好几个变体，转出来的字各不相同
        （2026-10-03 实测：同一条 1.24 秒的语音，一次转出「你好」、另一次转出
        「刚抓外」）。这种**不稳定**在功能上等于随机。
      * 消息 XML 里就写着 `voicelength=1240`。所以正确的问题是：
        **「解出来最接近 1240 毫秒的前缀是哪一段」** —— 这个答案唯一。

    ⚠️ 找的必须是**最接近的**，不能"够到 target−tol 就停"：那样 1600 毫秒的语音
    会被裁到 1400 毫秒（tol 一大就早停），同一段音频裁出不同音频、转出不同的字
    —— 2026-10-03 真机取证时就撞上过（`local_id=615` 两条候选都停在同一帧）。

    ⚠️ `est` 超出真实帧数时**夹到末帧再说**（不许直接放弃）—— 理由见下面的长注释，
    那是"长语音一条都读不出来"的根因。够不够得上目标由**实测时长**判定。

    返回 `(silk, 实际毫秒, 错误)`。
    """
    ends = frame_ends(raw)
    if not ends:
        return b"", 0.0, "这个 SILK 头后面没有完整帧"
    # ⚠️ **便宜的先判死**（不解码）：任何前缀的时长都不可能超过 `帧数 × 20ms`
    # （SILK 每帧恒 20ms，真机实测吻合），所以"整段都够不着目标"可以在**解码之前**
    # 就拒掉。为什么必须有这一条：`est` 闸改成 clamp 之后，以前**不进解码**的那些
    # 短 blob 会逐个去探测（每个最多 5 次 pilk 解码），长语音那一轮就是几百次解码
    # —— 这个函数同步跑在**收消息那条线程**上，那就是几十秒的停顿（真机卡死过的同一个坑）。
    total_ms = len(ends) * FRAME_MS
    if total_ms + tol_ms < float(target_ms):
        return b"", float(total_ms), "帧加起来也离目标时长太远"
    # 先按**帧数**估一刀（微信的 SILK 每帧 20 毫秒），解码验证只在附近几个帧数上做。
    # 别一上来就二分：每次验证都要写盘 + 解码，几十次探测就是几十秒。
    #
    # ⚠️⚠️ 这里**必须 clamp，绝不能**在 `est` 超出真实帧数时直接放弃 ——
    # 那是 2026-10-03 真机「长语音一条都读不出来」的**根因**（`_audit/probe_voice_bytes.py`
    # 侦察，8/8 命中）：微信报的 `voicelength` 比真实音频**长 20~40ms**，
    # 而 `est = round(voicelength / 20)` ⇒ `est` 比真实帧数**大 1~2 帧** ⇒
    # 以前那句 `if est > len(ends): return …"帧数不够"` 把**内存里明明完整存在**的
    # 那一条当场扔掉；上层于是只看到"没有一条时长接近 X 毫秒"（日志里 3720/3400/
    # 4440/7180 四次全是这个），而那条语音其实好好地躺在内存里（7140ms vs 7180ms，差 40ms）。
    # 现在把 `est` 夹到末帧、让它去**实测**：够不上目标时下面的 `tol_ms` 判定照样会拒，
    # 所以"绝不拿别的语音顶上"这条没有被放松 —— 拒答的依据从"估算帧数"换成了"实测时长"。
    # （`ends[k]` 是**第 k+1 帧**的结束偏移，所以"整段"是 `ends[len-1]`；`est` 只是个
    #   靠近末尾的索引，±1/±2 的探针本来就会把它兜住。）
    est = max(1, int(round(float(target_ms) / FRAME_MS)))
    est = min(est, len(ends) - 1)
    best = None
    for idx in (est, est - 1, est + 1, est - 2, est + 2):
        if not (0 <= idx < len(ends)):
            continue
        ms = _silk_ms(raw[:ends[idx]])
        if ms is None:
            continue
        if best is None or abs(ms - target_ms) < abs(best[1] - target_ms):
            best = (raw[:ends[idx]], ms)
        if abs(ms - target_ms) <= tol_ms:
            break
    if best is None:
        return b"", 0.0, "这个 SILK 头后面没有可解码的帧"
    if abs(best[1] - target_ms) > tol_ms:
        # **唯一**的"不是这条"出口：整段帧加起来**实测**都离目标太远
        # （以前这里是"估算帧数不够"的第二个出口，那个把真候选也误杀了）。
        return b"", best[1], "帧加起来也离目标时长太远"
    return best[0], best[1], ""


def silk_to_wav(silk, wav_path):
    """SILK 字节 → WAV 文件。返回 `(时长秒, 采样率, 错误)`。"""
    import tempfile
    import pilk
    tmp = tempfile.mkdtemp(prefix="voicemem_")
    src = os.path.join(tmp, "in.silk")
    with open(src, "wb") as f:
        f.write(silk)
    try:
        if hasattr(pilk, "silk_to_wav"):
            pilk.silk_to_wav(src, wav_path)
        else:
            import voice_msg
            pcm, err = voice_msg.to_pcm(silk)
            if err or not pcm:
                return 0.0, 0, err or "SILK 解不出 PCM"
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm)
        with wave.open(wav_path) as w:
            return (w.getnframes() / float(w.getframerate() or 1),
                    w.getframerate(), "")
    except Exception as e:
        return 0.0, 0, f"{type(e).__name__}: {str(e)[:150]}"


def candidates(h, hits, out_dir, tol_ms=MATCH_TOL_MS, target_ms=None, max_n=MAX_CANDIDATES,
               target_bytes=None):
    """把内存里的 SILK 解成候选语音。返回 `[{silk, silk_len, ms, addr}, ...]`。

    **给了 `target_ms` 就只认「按时长裁出来的那一段」**（见 `silk_for_duration`），
    不再退回那两种近似裁剪 —— 那两种会给同一段音频裁出好几个变体、转出不同的字，
    在功能上等于随机。

    `target_bytes`（消息 XML 里的 `length`）**只用来标注**，不做门槛：每条候选多带一个
    `bytes_delta = 真实长度 − target_bytes`，由 `read()` 决定要不要按它优先挑。
    为什么不在这里直接过滤：那个 −1 关系只在**自己发出的**样本上验证过，
    别人发来的可能是加密载荷长度（见 `BYTE_TOL` 的注释）——在这里硬过滤会把
    incoming 一条不剩地判死。

    ⚠️ **这一步故意不落 WAV**：完整解码一次要几百毫秒到秒级，而最终只会转写其中
    1~2 条。调用方要用哪条自己调 `silk_to_wav()`（`read()` 就是这么做的）。
    2026-10-03 实测：每条候选都解码，`read()` 要 31 秒；改成按需解码后大幅下降。

    去重按 SILK 内容的 sha256（同一段音频在内存里常有多份）＋**前缀去重**
    （同一个 magic 起裁出来的多个长短，短的必然是长的前缀）。"""
    os.makedirs(out_dir, exist_ok=True)
    seen, out = set(), []
    for i, addr in enumerate(hits):
        nxt = hits[i + 1] if i + 1 < len(hits) else addr + WINDOW
        limit = max(64, min(WINDOW, nxt - addr))
        raw = _read_mem(h, addr, limit)
        if target_ms:
            # 内侧容差**固定收紧**（见 SILK_TOL_MS 的注释）：它决定"这一刀裁得准不准"
            silk, cms, _err = silk_for_duration(raw, target_ms, tol_ms=SILK_TOL_MS)
            cands = [silk] if silk else []
        else:
            cms = 0.0
            cands = [c for c in (trim_silk(raw), trim_silk(raw, limit)) if len(c) >= 32]
        for cand in cands:
            digest = hashlib.sha256(cand).hexdigest()[:12]
            if digest in seen:
                continue
            seen.add(digest)
            if target_ms:
                ms = float(cms)
                if abs(ms - target_ms) > tol_ms:
                    continue
            else:
                ms = _silk_ms(cand) or 0.0
            out.append({"silk": cand, "silk_len": len(cand), "addr": addr,
                        "ms": ms, "digest": digest, "wav": "",
                        # 只有"按时长精确裁过"的那条长度才可信；近似裁剪那条不算
                        "bytes_delta": (len(cand) - int(target_bytes))
                        if (target_bytes and target_ms) else None})
            if len(out) >= max_n * 3:      # 先去重再截断 —— 同一段音频常有多份
                break
    # 同一段音频被裁出多个长短：**短的必然是长的前缀**（都是从同一个 magic 起裁的）。
    # 只留最贴目标时长的那条，否则同一段音频要白转好几次（每次 ~5 秒）——
    # 2026-10-03 实测：4 条候选里 3 条是同一段音频。
    if target_ms:
        out.sort(key=lambda c: abs(c["ms"] - target_ms))
        kept = []
        for c in out:
            if any(k["silk"].startswith(c["silk"]) or c["silk"].startswith(k["silk"])
                   for k in kept):
                continue
            kept.append(c)
        out = kept
    return out[:max_n]


# ---------------- 对外 ----------------

def _similar(a, b, threshold=0.5):
    """两段转写是不是「同一段音频被裁出了不同长短」。

    **为什么必须有这一条**（2026-10-03 真机取证）：同一条语音在内存里有多份，
    裁出来会有微差，whisper 于是给两个很像但不一样的版本 ——

        「我最近得好了什么」 vs 「我最近准备好了什么」

    那是**同一个人在说同一句话**，不是"两条不同的语音"。把它们判成不同，
    就会**白白拒答**（实测就是这么拒的）。用字符序列相似度判
    （stdlib `difflib`，不引第三方依赖）。
    """
    import difflib
    a, b = str(a or "").strip(), str(b or "").strip()
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def no_text_reason(n_cands, errs):
    """候选一条文字都没转出来时，**如实说原因**（纯函数，自测直接钉）。

    ⚠️ 2026-10-06 另一台电脑真机踩到：转写每一次都被拒（那台是**本地模型没下**），
    而用户看到的是「可能是没人声/太短」——**把自己发的语音说成"没人声"，还把已知原因
    换成了猜测**。原因本来就在 `audio_read.transcribe_scored` 返回的 `err` 里（含
    「跑 `.venv\\Scripts\\python.exe audio_read.py --setup`」这种**可照做**的指令），
    以前被 `continue` 一起丢掉了。

    规矩：**有 `err` 就报 `err`**（去重、压平空白、最多两条）；真的一条 `err` 都没有
    （纯空结果那种）才退回那句猜测。两句都保留「没有文本，别编」——那是给模型看的。
    """
    uniq = []
    for e in (errs or []):
        t = " ".join(str(e).split())
        if t and t not in uniq:
            uniq.append(t)
    if uniq:
        return (f"找到 {int(n_cands)} 条时长接近的语音，但**转写全部失败**："
                f"{'；'.join(uniq[:2])}。**没有文本**，别编。")
    return (f"找到 {int(n_cands)} 条时长接近的语音，但一条都没转出文字"
            f"（可能是没人声/太短）。**没有文本**，别编。")


def read(duration_ms, out_dir=None, tol_ms=MATCH_TOL_MS, transcribe=True, cfg=None,
         max_try=3, ambiguous_limit=6, scan_seconds=None, target_bytes=None):
    """按**时长**（＋消息自带的 `length` 指纹）找那条语音并转文字。
    返回 `(文本列表, 错误说明)`。

    为什么要回**列表**：时长匹配本身没有唯一 id，1.24 秒的语音旁边可能站着
    1.20 / 1.22 秒的另外几条。**绝不替调用方挑**（挑错＝把别人的话安到他头上，
    和「重名不许静默取第一个」同一条铁律）。

    ## 挑哪一条：**先看 `length` 指纹，再看置信度，分不出来就拒答**

    2026-10-03 真机侦察（`_audit/probe_voice_bytes.py`，8/12 条能唯一锁定）：
    消息 XML 里的 `length`（加密字节数）和内存里那条 SILK 的真实长度**精确对应**
    （自己发出的样本 8/8 差 −1），而**按时长**挑出来的错误候选差 200~1900 字节。
    所以 `target_bytes` 给了的话：

      1. **有候选命中指纹**（`|bytes_delta| <= BYTE_TOL`）⇒ 只留这些，
         同长度的"别人的语音"当场被排除（这就是以前那个"2 条候选时长一样、
         置信度也接近"认不出来的根因之一）；
      2. **一个都没命中** ⇒ **完全退回原来的时长逻辑**，一个字节都不放宽
         （别人发来的语音 `length` 语义可能不同，硬门槛会把 incoming 全判死）。

    历史取证（`local_id=615`，用户说的是「我最近聊了什么」）：

        1400ms  SILK 2730 字节（更接近加密字节数 2804）  no_speech 0.17  logprob −0.97  →「要饿了吗呢」✗
        1400ms  SILK 2547 字节                          no_speech 0.06  logprob −0.76  →「我最近得好了什么」✓

    两条候选**时长一模一样**，按时长分不出来；当时只能靠 whisper 置信度，
    所以规则是：**转写前 `max_try` 条 → 按分数排序 → 只有"明显最好"才给文本，
    否则如实说分不出来。** 这条命比速度重要得多：在这里猜错＝拿别人的话去执行。
    （`length` 指纹能解决的正是这种同长度撞车；指纹没命中时上面这条规矩原样保留。）

    ⚠️ 三个和"慢"有关的参数（都是实测逼出来的）：

      * `cfg` —— **必须传**！不传就走 `audio_read.transcribe(wav)` 的默认后端
        （本地 whisper-small），于是 `audio.backend: cloud` 和 `audio.model: tiny`
        **全都不生效**。
      * `max_try`（默认 **3**）—— 最多转写几条候选。要按置信度挑就至少得比两条，
        所以默认 3；单条候选的正常情况下只会转 1 条。
      * `ambiguous_limit`（默认 6）—— 时长相近的候选**太多**时直接**如实说认不出**：
        那是"同长度站着一堆别人的语音"，转出来也是瞎猜。

    `target_bytes` 是**新加的最后一个参数**（`live_history.voice_info()` 的
    `length_bytes`）：放最后是为了不动任何既有的位置参数调用。
    """
    if not duration_ms or duration_ms <= 0:
        return [], "没有时长，认不出是哪条语音（消息 XML 里的 voicelength 没读到）。"
    ok, why = available()
    if not ok:
        return [], why

    import audio_read
    import time as _time
    probe = ProcessProbe()
    pid, h = weixin_main_process(probe=probe)
    if not h:
        # ⚠️ 这两种失败**必须分开说**（2026-10-06 真机踩到）：
        #   ① 进程表里压根没有 Weixin.exe = 微信真的没开；
        #   ② 有 Weixin.exe、但一个都打不开 = **跨完整性级别**（微信被提权打开 →
        #      High；助手是 Medium → OpenProcess 被拒，GetLastError=5）。
        # 混成一句「微信没在跑？或者权限不够」，用户会去查「微信开没开」，
        # 而真相正好相反：微信开着，是**权限不对**，要重启微信（不提权）或提权跑助手。
        if probe.denied:
            return [], ("读到微信进程了，但**打不开它的内存**——最快的解释是"
                        "**微信是用管理员权限开的**，而助手不是，Windows 不允许低权限进程读"
                        "高权限进程的内存。**不是「微信没在跑」**。两条路：① 关掉微信、"
                        "从开始菜单**普通双击**重开（推荐，助手不用动）；② 或者让助手也以"
                        "管理员身份运行。这条语音没读出来，别编。")
        if probe.saw_any:
            return [], ("微信进程在，但没有一个能读内存（可能被安全软件拦、或进程刚要退出）。"
                        "这条语音没读出来，别编。")
        return [], ("进程表里没有 Weixin.exe —— 微信**没在跑**（或者跑的是旧版 "
                    "WeChat.exe）。这条语音没读出来，别编。")
    out_dir = out_dir or os.path.join(HERE, "data", "voice_mem")
    # 扫内存的**硬时间上限**：超了就放弃并如实说（`voice.scan_seconds`，默认 20 秒）。
    # 为什么默认是这个数：完整扫描实测 4~16 秒（见 `DEFAULT_SCAN_SECONDS` 的注释），
    # 而它同步跑在**收消息那条线程**上。宁可这一条读不出来，也不能让 bot 停摆
    # ——读不出来只是少一条语音，停摆是整台助手哑掉。
    lim = scan_seconds
    if lim is None:
        lim = _cfg_float(cfg, "voice", "scan_seconds", DEFAULT_SCAN_SECONDS)
    lim = max(1.0, float(lim))
    deadline = _time.monotonic() + lim
    try:
        hits, complete = scan_silk(h, deadline=deadline)
        if not complete:
            return [], (f"扫微信内存超过 {lim:.0f} 秒还没扫完，**放弃了**（不想让助手一直卡着）。"
                        f"这条语音**没读出来**，别编。要给它更多时间就调大 "
                        f"`voice.scan_seconds`（当前 {lim:.0f}）。")
        if not hits:
            return [], ("微信内存里没搜到 SILK —— 这条语音微信最近没碰过"
                        "（可能既没播过也没刚发过）。**没有文本**，别编。")
        cands = candidates(h, hits, out_dir, tol_ms=tol_ms, target_ms=duration_ms,
                           target_bytes=target_bytes)
    finally:
        _k32().CloseHandle(h)

    if not cands:
        return [], (f"内存里搜到 SILK，但没有一条时长接近 {duration_ms:.0f} 毫秒"
                    f"（容忍 {tol_ms} 毫秒）。**没有文本**，别编。")
    if not transcribe:
        return [f"[{c['ms']:.0f}ms] {c.get('digest', '')}" for c in cands], ""

    # ── `length` 指纹优先级（**只在有候选中命中时生效**）──────────────────
    # 命中了就是"这一条"：同长度站着的别人的语音当场出局（那种以前只能靠置信度
    # 硬分，分不出来就拒答）。没命中就一个字都不改，原样走时长 + 置信度那条路——
    # 见 BYTE_TOL 的注释：incoming 的 `length` 语义可能不同，不能当硬门槛。
    if target_bytes:
        hit = [c for c in cands
               if c.get("bytes_delta") is not None
               and abs(c["bytes_delta"]) <= BYTE_TOL]
        if hit:
            cands = hit

    cands.sort(key=lambda c: abs(c["ms"] - duration_ms))
    if len(cands) > int(ambiguous_limit):
        return [], (f"内存里有 {len(cands)} 条时长都接近 {duration_ms:.0f} 毫秒的语音，"
                    f"**认不出是哪一条**（这条大概已经不在内存里了）。"
                    f"没有文本 —— 别猜，也别拿别的语音顶上。")

    scored = []
    errs = []
    for c in cands[:max(1, int(max_try))]:
        # 到这一步才落 WAV（`candidates()` 故意不解码，见那边的注释）
        wav = c.get("wav") or ""
        if not wav:
            wav = os.path.join(out_dir, f"{c.get('digest', 'cand')}_{c['ms']:.0f}.wav")
            secs, _rate, derr = silk_to_wav(c["silk"], wav)
            if derr or secs <= 0:
                if derr:
                    errs.append(derr)
                continue
            c["wav"] = wav
        try:
            txt, score, err = audio_read.transcribe_scored(wav, cfg)
        except Exception as e:
            txt, score, err = "", None, f"{type(e).__name__}: {e}"
        if err or not txt:
            if err:
                errs.append(err)
            continue
        scored.append({"text": txt, "score": score, "ms": c["ms"],
                       "silk": c["silk_len"]})
    if not scored:
        return [], no_text_reason(len(cands), errs)

    # 云端转写拿不到置信度：多条候选时**没有依据可挑** → 拒绝，不赌
    if scored[0]["score"] is None:
        if len(cands) == 1:
            return [scored[0]["text"]], ""
        return [], (f"{len(cands)} 条候选时长相同，而当前是**云端转写**"
                    f"（返回里没有置信度），认不出是哪一条。**没有文本** —— 别猜。")

    scored.sort(key=lambda s: -s["score"])
    best = scored[0]
    second = scored[1] if len(scored) > 1 else None
    if best["score"] < POOR_SCORE:
        return [], (f"这条语音听不清（置信度 {best['score']:.2f} 低于 {POOR_SCORE}），"
                    f"**没有文本** —— 宁可说听不清，也不给一句可能是别人的话。")
    if (second and second["text"] != best["text"]
            and (best["score"] - second["score"]) < TIE_MARGIN
            and not _similar(best["text"], second["text"])):
        return [], (f"{len(scored)} 条候选时长一样、置信度也接近"
                    f"（{best['score']:.2f} vs {second['score']:.2f}），"
                    f"而且**文本明显不同**（{best['text']!r} vs {second['text']!r}）——"
                    f"认不出是哪一条。**没有文本** —— 别猜。")
    if len(scored) > 1:
        # 同一段音频的多个裁剪版本会给出很像但不一样的文本：**按相似度投票**，
        # 取票数最多那一簇里分数最高的那条 —— 比"取最高分"稳（最高分那条可能
        # 正好是裁歪的那个版本）。
        groups = []
        for s in scored:
            for g in groups:
                if _similar(g[0]["text"], s["text"]):
                    g.append(s)
                    break
            else:
                groups.append([s])
        groups.sort(key=lambda g: (-len(g), -max(x["score"] for x in g)))
        best_group = groups[0]
        best_group.sort(key=lambda s: -s["score"])
        return [best_group[0]["text"]], ""
    return [best["text"]], ""


def _cli(argv):
    if "--duration" in argv:
        dur = float(argv[argv.index("--duration") + 1])
    else:
        print(__doc__)
        return 2
    # `--bytes <length>`：消息 XML 里的加密字节数（指纹）。不给就只按时长（老行为）。
    tby = None
    if "--bytes" in argv:
        try:
            tby = int(argv[argv.index("--bytes") + 1])
        except (IndexError, ValueError):
            print("--bytes 要跟一个整数（消息 XML 里的 length）")
            return 2
    texts, err = read(dur, target_bytes=tby)
    print(f"目标时长 {dur:.0f} 毫秒" + (f" / length={tby}B" if tby else ""))
    if err:
        print("  ❌", err)
    for t in texts:
        print("  ✅", t)
    return 0 if texts else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
