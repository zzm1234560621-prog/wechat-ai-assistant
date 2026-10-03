"""语音条逆向工具（voice_msg）的回归自测。

**不联网、不需要真实语音文件、不碰微信**：
  * XML 解析用**合成**的 XML（形状照真机抓到的那条 1:1 复刻，但里面的
    aeskey / wxid / roomid **全部换成假的**——真机的那些是私事，不进仓库）；
  * 解密用**自己加密再解回来**（验的是我的代码，不是微信的格式：格式要等真实文件才能定）。

用法：`.venv/Scripts/python.exe selftest_voice_msg.py`
"""
import binascii
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import voice_msg      # noqa: E402
import voice_mem      # noqa: E402  # 扫内存的超时保护（见 t8）

_PASS = 0
_OK = True


def check(label, cond, extra=""):
    global _PASS, _OK
    cond = bool(cond)
    _PASS += 1 if cond else 0
    _OK = _OK and cond
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {extra}" if extra and not cond else ""))
    return cond


def sec(t):
    print(f"\n── {t} ──")


# 形状照真机抓到的那条复刻；aeskey / fromusername / clientmsgid 都是假的
REAL_SHAPE_XML = (
    'wxid_fake___sender:\n'
    '<msg><voicemsg endflag="1" cancelflag="0" forwardflag="0" voiceformat="4" '
    'voicelength="10880" length="20934" bufid="0" '
    'aeskey="0123456789abcdef0123456789abcdef" '
    'voiceurl="7f0c00060220b99d6f8932" voicemd5="" '
    'clientmsgid="abcdef@chatroom_195826_1790616577" '
    'fromusername="wxid_fake___sender" silklength="0" /></msg>')


def t1_parse():
    sec("解析语音消息 XML（真机形状）")
    info = voice_msg.parse_voicemsg(REAL_SHAPE_XML)
    check("认得出这是语音", bool(info), info)
    check("aeskey 抠出来了", info.get("aeskey") == "0123456789abcdef0123456789abcdef", info)
    check("aeskey → 16 字节 key", isinstance(info.get("key_bytes"), bytes)
          and len(info["key_bytes"]) == 16, info.get("key_bytes"))
    check("length → 20934 字节", info.get("length_bytes") == 20934, info.get("length_bytes"))
    check("voicelength → 10880 毫秒（≈10.9 秒）", info.get("duration_ms") == 10880,
          info.get("duration_ms"))
    check("voiceformat=4 显示成 silk（社区通说，仅用于显示）",
          info.get("format_name") == "silk", info.get("format_name"))
    check("附带的 wxid 也抠出来了（判「谁发的」用得上）",
          info.get("fromusername") == "wxid_fake___sender", info.get("fromusername"))

    check("不是语音 → 空 dict", voice_msg.parse_voicemsg("<msg><img /></msg>") == {})
    check("空输入不炸", voice_msg.parse_voicemsg("") == {} and voice_msg.parse_voicemsg(None) == {})
    check("aeskey 长度不对 → key 为 None（**不猜、不补零**）",
          voice_msg.key_of("abc") is None and voice_msg.key_of("") is None)
    check("aeskey 合法 → 16 字节", len(voice_msg.key_of("00" * 16)) == 16)


def t2_magic():
    sec("按魔数认容器")
    cases = [
        (b"#!SILK_V3" + b"\0" * 20, "silk-v3"),
        (b"#!AMR\n" + b"\0" * 20, "amr"),
        (b"#!AMR-WB\n", "amr-wb"),
        (b"RIFF" + b"\0" * 20, "wav"),
        (b"ID3\x04", "mp3"),
        (b"\xff\xd8\xff\xe0", "jpeg"),
    ]
    for data, want in cases:
        got = voice_msg.magic_of(data)
        check(f"{want} 认得出", got == want, got)
    check("SILK 前面多一个长度字节也认得出（偏移 1）",
          voice_msg.magic_of(b"\x02#!SILK_V3" + b"\0" * 8) == "silk-v3（偏移 1）",
          voice_msg.magic_of(b"\x02#!SILK_V3" + b"\0" * 8))
    check("随机字节 → 认不出就是 None（不硬猜）",
          voice_msg.magic_of(bytes(range(32))) is None)
    check("空输入 → None", voice_msg.magic_of(b"") is None)


def t3_decrypt():
    sec("解密：自己加密再解回来（验代码，不验微信格式）")
    try:
        from Crypto.Cipher import AES
    except ImportError:
        check("pycryptodome 可用（逆向要用）", False)
        return
    key = bytes.fromhex("0123456789abcdef0123456789abcdef")
    # 造一段「SILK 明文」，按 16 字节对齐
    silk = b"#!SILK_V3" + bytes(range(64))
    silk = silk[:len(silk) // 16 * 16]

    for name, mk in (("ECB", lambda: AES.new(key, AES.MODE_ECB)),
                     ("CBC-零IV", lambda: AES.new(key, AES.MODE_CBC, b"\0" * 16))):
        enc = mk().encrypt(silk)
        cands = voice_msg.identify(enc, key)
        hit = [c for c in cands if c[2] == "silk-v3" and c[0].startswith(name.split("-")[0])]
        check(f"AES-{name} 加密的 SILK：候选里有 {name} 且魔数对上",
              bool(hit), cands)

    # ★ 魔数分不出模式（真机自测当场抓到）：CBC 零 IV 与 ECB 的第一块一模一样，
    #   所以 CBC 的载荷在 ECB 方案下首块也能对上魔数 —— 必须让调用方用
    #   「完整解码是否成功」来裁决，identify 因此返回列表而不是单个方案。
    cbc_enc = AES.new(key, AES.MODE_CBC, b"\0" * 16).encrypt(silk)
    cands = voice_msg.identify(cbc_enc, key)
    schemes = [c[0] for c in cands]
    check("CBC 载荷会同时命中 ECB 与 CBC 两个候选（**魔数分不出模式**，所以返回列表）",
          any(s.startswith("ECB") for s in schemes) and any(s.startswith("CBC") for s in schemes),
          schemes)
    check("……而且不同候选的明文**不全相同**（整段解出来才分得开）",
          len({c[1] for c in cands}) > 1, len({c[1] for c in cands}))

    # 换一把错的 key：必须**判不出来**，而不是硬报一个格式
    wrong = bytes.fromhex("ff" * 16)
    cands = voice_msg.identify(AES.new(key, AES.MODE_ECB).encrypt(silk), wrong)
    check("key 不对 → 一个候选都没有（不硬报格式）", cands == [], cands)

    check("空/短输入不炸", voice_msg.decrypt_variants(b"", key) == []
          and voice_msg.decrypt_variants(silk, b"short") == [])
    check("非 16 倍数长度也不炸（会按整块截）",
          isinstance(voice_msg.decrypt_variants(silk + b"\x01", key), list))


def t4_to_pcm_honest():
    sec("解码：认不出/缺解码器都要**如实报错**")
    pcm, err = voice_msg.to_pcm(b"not audio at all" * 4)
    check("认不出的格式 → 报错、不放行", pcm == b"" and "认不出音频格式" in err, err)
    pcm, err = voice_msg.to_pcm(b"#!SILK_V3" + b"\0" * 64)
    check("SILK：要么解码成功，要么给出照做指引（**不返回空音频假装成功**）",
          (pcm != b"" and err == "") or (pcm == b"" and err != ""), (len(pcm), err))
    if pcm == b"":
        check("……缺 pilk 时的指引里有 pip install", "pilk" in err, err)
    _ = binascii


def t5_find_payload(tmp):
    sec("按 length 在磁盘上找音频（语音 XML 里**没有文件名**，只能按大小找）")
    h = tmp
    exact = os.path.join(h, "a_exact.bin")
    plus16 = os.path.join(h, "b_plus16.bin")
    far = os.path.join(h, "c_far.bin")
    open(exact, "wb").write(b"\0" * 20934)
    open(plus16, "wb").write(b"\0" * 20950)      # AES 填充多 16 字节很常见
    open(far, "wb").write(b"\0" * 30000)
    sub = os.path.join(h, "nested")
    os.makedirs(sub, exist_ok=True)
    nested = os.path.join(sub, "d_nested.bin")
    open(nested, "wb").write(b"\0" * 20935)

    hits = voice_msg.find_payload([h], 20934, tol=64)
    paths = [x["path"] for x in hits]
    check("找到大小对得上的候选（含子目录）", exact in paths and nested in paths, paths)
    check("按差距排序：完全相同的排第一", paths and paths[0] == exact, paths)
    check("差 16 字节的也算候选", plus16 in paths, paths)
    check("差太远的不进候选", far not in paths, paths)
    check("每个候选带 size 和 diff",
          all("size" in x and "diff" in x for x in hits), hits)

    check("length<=0 → 不找（不返回一堆无关文件）",
          voice_msg.find_payload([h], 0) == [])
    check("目录不存在 → 空结果不炸", voice_msg.find_payload([os.path.join(h, "nope")], 20934) == [])

    # md5 是确定的：filehelper 的 attach 目录名可以直接对（不是私事，是公开字符串的哈希）
    check("hash_of('filehelper') 与真机一致",
          voice_msg.hash_of("filehelper") == "9e20f478899dc29eb19741386f9343c8",
          voice_msg.hash_of("filehelper"))

    acct = os.path.join(h, "acct")
    att = os.path.join(acct, "msg", "attach", voice_msg.hash_of("filehelper"))
    os.makedirs(att, exist_ok=True)
    os.makedirs(os.path.join(acct, "msg", "file"), exist_ok=True)
    dirs = voice_msg.candidate_dirs("filehelper", [acct])
    check("candidate_dirs 给出该会话的 attach 目录", att in dirs, dirs)
    check("只列真实存在的目录", all(os.path.isdir(d) for d in dirs), dirs)


class _FakeClient:
    """假客户端：`sqlite_master` 探测一律成功，`Msg_` 查询返回给定行。"""

    def __init__(self, rows):
        self.rows = rows
        self.sql_log = []

    def query_sql(self, db, sql):
        self.sql_log.append((db, sql))
        if not str(db).startswith("message_"):
            return []
        if "sqlite_master" in sql:
            return [{"name": "Msg_9e20f478899dc29eb19741386f9343c8"}]
        if "FROM Msg_" in sql:
            return self.rows
        return []


def t6_voice_info():
    sec("自动路径第一步：从 DB 取某条语音的 aeskey/时长/格式")
    import binascii
    import live_history
    try:
        import zstandard
    except ImportError:
        check("zstandard 可用（真机 message_content 是 zstd）", False)
        return

    xml = ('<msg><voicemsg voiceformat="4" voicelength="1470" length="2315" '
           'aeskey="' + "ab" * 16 + '" fromusername="wxid_fake" /></msg>')
    blob = zstandard.ZstdCompressor().compress(xml.encode("utf-8"))
    row = {"local_type": 34, "message_content": binascii.hexlify(blob).decode()}

    info = live_history.voice_info(_FakeClient([row]), "filehelper", 12345)
    check("认得出这是语音并解析出字段", bool(info) and info.get("format_name") == "silk", info)
    check("aeskey → 16 字节 key", len(info.get("key_bytes") or b"") == 16, info.get("key_bytes"))
    check("length / 时长都对", info.get("length_bytes") == 2315
          and info.get("duration_ms") == 1470, (info.get("length_bytes"), info.get("duration_ms")))
    check("带上会话和 local_id（后面定位文件要用）",
          info.get("talker") == "filehelper" and info.get("local_id") == "12345", info)

    # 不是语音：**必须返回 {}**，不许硬当成语音
    info = live_history.voice_info(_FakeClient([{"local_type": 1, "message_content": ""}]),
                                   "filehelper", 1)
    check("普通文本消息 → {}（不硬当语音）", info == {}, info)
    check("查不到 → {}（不抛异常）",
          live_history.voice_info(_FakeClient([]), "filehelper", 1) == {})
    check("参数不全 → {}",
          live_history.voice_info(_FakeClient([]), "", 0) == {})
    check("解不出 XML（不是语音格式）→ {}（不硬当语音）",
          live_history.voice_info(
              _FakeClient([{"local_type": 34,
                            "message_content": binascii.hexlify(
                                zstandard.ZstdCompressor().compress(b"<msg><x/></msg>")).decode()}]),
              "filehelper", 1) == {})


def t7_probe(tmp):
    """--probe：先拍基线 → 播放 → 看新增。**判不出来就如实说判不出来。**

    这一条守的是「不猜」：工具的最后一步必须是「要么给出**完整解码**过的方案，
    要么明说没试出来」，绝不能给出一个「大概是这个」的结论。
    """
    sec("T7 · --probe 两阶段探测（基线 → 新增 → 试解密/如实说判不出）")
    acct = os.path.join(tmp, "acct")
    sub = os.path.join(acct, "msg", "file", "2026-10")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, "旧文件.bin"), "wb") as f:
        f.write(b"old")
    state = os.path.join(tmp, "probe_state.json")

    # ① 第一次：只拍基线，并告诉用户下一步做什么
    text1, res1 = voice_msg.probe([acct], state_path=state)
    check("第一次跑 → phase=baseline", res1.get("phase") == "baseline", res1)
    check("第一次跑 → 报告里说清基线记了多少个文件",
          "已记录基线" in text1 and res1.get("files") == 1, text1[:80])
    check("第一次跑 → 明确告诉你「先播放一条、再跑一遍」",
          "播放" in text1 and "再跑一遍" in text1, text1[-160:])
    check("第一次跑 → 基线真的落盘了", os.path.isfile(state))

    # ② 什么都没变 → 必须得出「没落盘」这个**结论**，而不是含糊
    text2, res2 = voice_msg.probe([acct], state_path=state)
    check("没有新文件 → changed=0", res2.get("changed") == 0, res2)
    check("没有新文件 → 明说「没有任何新文件落盘」",
          "没有任何新文件落盘" in text2, text2[:80])
    check("没有新文件 → 给出可执行的结论（别往这条路投入 / 如实说做不到）",
          "如实告诉用户做不到" in text2, text2[-160:])

    # ③ 播放之后新落盘一个文件（大小刚好对得上 length）
    payload = os.path.join(sub, "新落盘的语音.bin")
    body = b"\x00" * 64
    with open(payload, "wb") as f:
        f.write(body)
    text3, res3 = voice_msg.probe([acct], length=len(body), state_path=state)
    check("检测到新增", res3.get("changed") == 1 and res3.get("candidates") == 1, res3)
    check("报告里列出了那个新文件", "新落盘的语音.bin" in text3, text3[:200])
    check("没给 aeskey 时明说「只能看它是不是明文」",
          "没给" in text3 and "明文" in text3, text3)

    # ④ 给了 aeskey、但解不出来 → **必须如实说判不出来**（不许给「大概」）
    text4, res4 = voice_msg.probe([acct], length=len(body),
                                  aeskey="00" * 16, state_path=state)
    honest = ("没试出能用的方案" in text4 or "一个都没对上魔数" in text4
              or "pycryptodome" in text4)
    check("解不出来时给出**诚实**的结论（不是「大概就是这个」）", honest, text4[-220:])
    check("解不出来 → decoded 记 0", res4.get("decoded") == 0, res4)
    check("解不出来 → 明确说「不要编一个结论」",
          "不要编" in text4 or "pycryptodome" in text4, text4[-160:])

    # ⑤ length 对不上时，过滤要说清「剩几个」，而不是硬凑一个候选
    text5, res5 = voice_msg.probe([acct], length=len(body) + 99999, tol=8,
                                  state_path=state)
    check("length 差太多 → 候选被过滤掉、并说清过滤后剩几个",
          res5.get("candidates") == 0 and "剩 **0**" in text5, text5[:200])

    # ⑥ 基线的读写坏掉不许炸（这是诊断工具，不能因为一个坏文件就崩）
    with open(state, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    text6, res6 = voice_msg.probe([acct], state_path=state)
    check("基线文件坏了 → 当作没有基线、重新拍（不抛异常）",
          res6.get("phase") == "baseline", res6)

    # ⑦ 目录不存在 → 不炸，报 0 个文件
    text7, res7 = voice_msg.probe([os.path.join(tmp, "根本没有")], state_path=state)
    check("目录不存在 → 不炸", isinstance(text7, str) and bool(text7), res7)


def t8_scan_deadline():
    """扫微信内存**必须有硬时间上限**——否则一次卡住就让整个助手停摆。

    **为什么这条最重要**（2026-10-03 真机踩的）：`voice_mem.read()` 同步跑在
    **收消息那条线程**上，而 `scan_silk` 在 128TB 地址空间里逐段
    `VirtualQueryEx` + `ReadProcessMemory`。原先它**没有任何时间上限**，
    微信让某次读取一卡，`read()` 就永远不返回：日志停在
    「处理自己的消息: [语音条…]」，之后**再无心跳**、再发语音也没人接。

    正确行为：到点就放弃，**如实说这条没读出来**（并给出调大的配置名），
    让轮询继续。宁可少读一条语音，也不能让整台助手哑掉。
    """
    sec("扫内存的硬时间上限（防止助手卡死）")
    import time as _time
    check("有默认上限且不为 0", voice_mem.DEFAULT_SCAN_SECONDS > 0,
          voice_mem.DEFAULT_SCAN_SECONDS)
    # ⚠️ 默认预算**必须覆盖实测的完整扫描上界**（2026-10-03 真机 6 次：
    # 4.0 / 4.9 / 16.0 / 2.1 / 2.3 / 2.0 秒）。以前默认 8 秒正好落在抖动区间里，
    # 真机开始随机「扫不完就放弃」—— 那是"代码没错、预算不够"，
    # 当晚 3 条新失败里 2 条就是它。这条断言就是防有人把它改回去。
    check("默认预算 ≥ 实测完整扫描上界（16 秒），不许改回打进抖动区间的 8",
          voice_mem.DEFAULT_SCAN_SECONDS >= 16.0, voice_mem.DEFAULT_SCAN_SECONDS)

    # deadline 已过 → 立刻返回 (空的, False)，绝不继续扫
    t = _time.monotonic()
    hits, complete = voice_mem.scan_silk(None, deadline=_time.monotonic() - 1)
    dt = _time.monotonic() - t
    check("deadline 已过 → 立即返回", dt < 0.5, dt)
    check("……且标成「没扫完」", hits == [] and complete is False, (hits, complete))

    # 兼容：不传 deadline 仍返回二元组
    r = voice_mem.scan_silk(None)
    check("不传 deadline → 仍返回 (hits, complete)", isinstance(r, tuple) and len(r) == 2,
          type(r).__name__)

    # read() 侧：扫不完 → 空文本 + 如实说明 + 给出配置名（绝不给半截结果）
    class _H:
        pass

    _real_proc, _real_k32, _real_scan = (voice_mem.weixin_main_process,
                                         voice_mem._k32, voice_mem.scan_silk)
    try:
        voice_mem.weixin_main_process = lambda: (1234, _H())
        voice_mem._k32 = lambda: type("K", (), {"CloseHandle": staticmethod(lambda h: None)})()
        voice_mem.scan_silk = lambda h, deadline=None, **kw: ([], False)
        texts, why = voice_mem.read(1400, cfg={"voice": {"scan_seconds": 8}})
        check("扫不完 → 不给文本（绝不拿半截结果当答案）", texts == [], texts)
        check("……如实说明超时了", "秒还没扫完" in why, why)
        check("……并告诉用户改哪个配置", "voice.scan_seconds" in why, why)
        check("……且明说没有编内容", "别编" in why, why)

        # 扫完了、只是没命中 → 仍走原来那句（两种失败不能混）
        voice_mem.scan_silk = lambda h, deadline=None, **kw: ([], True)
        texts2, why2 = voice_mem.read(1400, cfg={})
        check("扫完但没命中 → 走原来的文案（两种失败分开）",
              texts2 == [] and "没搜到 SILK" in why2 and "秒还没扫完" not in why2, why2)
        check("配置非法（scan_seconds='abc'）→ 退回默认，不静默",
              voice_mem._cfg_float({"voice": {"scan_seconds": "abc"}}, "voice",
                                   "scan_seconds", 8.0) == 8.0)
    finally:
        voice_mem.weixin_main_process, voice_mem._k32, voice_mem.scan_silk = (
            _real_proc, _real_k32, _real_scan)


def _mk_silk(frames, payload=8):
    """造一段**结构合法**的假 SILK：`#!SILK_V3` + 每帧 `[uint16 长度][载荷]`。

    真 SILK 音频没法在自测里合成（那得编码器），但 `frame_ends()` 走的就是这个块结构，
    而"帧数 → 时长"这条链正是根因所在，所以合成结构足够把 bug 钉死。
    """
    body = b"".join(int(payload).to_bytes(2, "little") + b"A" * payload
                    for _ in range(int(frames)))
    return voice_mem.MAGIC + body


def t9_frame_estimate_gate(tmp):
    """⚠️ 根因回归：`voicelength` 比真实音频长一点时，**必须照样能裁出来**。

    2026-10-03 真机侦察（`_audit/probe_voice_bytes.py`，8/8 命中）：
    微信报的 `voicelength` 比内存里那段真实音频**长 20~40 毫秒**，而旧代码

        est = round(voicelength / 20)
        if est > len(ends): return "", …, "帧数不够，到不了目标时长"   ← 直接扔掉

    于是 `est` 只比真实帧数大 1~2 帧，**内存里明明完整存在**的那条就被丢掉了；
    上层看到的是"内存里搜到 SILK，但没有一条时长接近 7180 毫秒"
    （日志里 3720/3400/4440/7180 四次全是这个）。真机那条 7180ms 的语音其实
    好好躺在内存里：357 帧 = 7140ms，只差 40ms。

    修法是把 `est` **夹到末帧**、让实测时长说话。**关键是不能顺手放松"拒答"**：
    差得远（1800ms vs 7180ms）时仍须一个字都不给 —— 见下面第二条断言。
    """
    sec("帧数估算闸（长语音读不出来的根因）")
    # 「真解码」替身：帧数 × 20ms（这就是 SILK 的时长定义，扫描时也是这么估的）
    _real = voice_mem._silk_ms
    voice_mem._silk_ms = (lambda s: len(voice_mem.frame_ends(s)) * voice_mem.FRAME_MS
                          if s.startswith(voice_mem.MAGIC) else None)
    try:
        # ① 真机那条：7140ms 的真实音频，微信报 7180ms
        blob = _mk_silk(357)
        silk, ms, err = voice_mem.silk_for_duration(blob, 7180)
        check("voicelength 比真实长 40ms → **照样裁得出来**（旧代码在这里扔掉）",
              bool(silk) and not err and round(ms) == 7140, (len(silk), ms, err))
        check("……裁出来的是**完整那段**（357 帧全要，不是少一帧）",
              len(voice_mem.frame_ends(silk)) == 357, len(voice_mem.frame_ends(silk)))

        # ② 不许因为 clamp 就把"差得远"的音频当这条：1800ms vs 7180ms
        short = _mk_silk(90)
        silk2, ms2, err2 = voice_mem.silk_for_duration(short, 7180)
        check("差得远（1800ms 目标 7180ms）→ 仍然**如实拒绝**，不给半截",
              silk2 == b"" and bool(err2), (len(silk2), ms2, err2))

        # ③ 常规路径没被改坏：目标正好落在末帧
        exact = _mk_silk(200)
        silk3, ms3, err3 = voice_mem.silk_for_duration(exact, 4000)
        check("目标正好等于真实时长 → 正常裁出 4000ms",
              bool(silk3) and not err3 and round(ms3) == 4000, (len(silk3), ms3, err3))

        # ④ 帧数不够但**在容差内**（差 20ms）也要能出来 —— 真机最常见的形态
        silk4, ms4, err4 = voice_mem.silk_for_duration(_mk_silk(90), 1820)
        check("差 20ms（1800 vs 1820）→ 能裁出（真机 8/8 都是这种差）",
              bool(silk4) and not err4 and round(ms4) == 1800, (len(silk4), ms4, err4))

        # ⑤ ⚠️ **够不着的必须不解码就判死**：clamp 之后如果每个短 blob 都去解码，
        # 长语音那一轮就是几百次 pilk 解码 —— 同步卡在收消息线程上（真机卡死过的坑）。
        calls = []
        voice_mem._silk_ms = (lambda s: (calls.append(1),
                                         len(voice_mem.frame_ends(s)) * voice_mem.FRAME_MS)[1])
        silk5, ms5, err5 = voice_mem.silk_for_duration(_mk_silk(50), 7180)   # 1000ms vs 7180ms
        check("整段都够不着目标 → **一次解码都不做**就拒（不许把轮询线程拖死）",
              silk5 == b"" and bool(err5) and len(calls) == 0,
              (len(silk5), ms5, err5, len(calls)))

        # ⑥ 但"刚好够得着"的仍然要真解码，不能靠估算糊弄过去
        calls[:] = []
        voice_mem._silk_ms = (lambda s: (calls.append(1),
                                         len(voice_mem.frame_ends(s)) * voice_mem.FRAME_MS)[1])
        silk6, ms6, _e6 = voice_mem.silk_for_duration(_mk_silk(90), 1820)
        check("够得着的那条**必须真解码验证**（不是拿估算当结论）", len(calls) >= 1, len(calls))
    finally:
        voice_mem._silk_ms = _real


def t10_length_fingerprint(tmp):
    """⚠️ 回归：用消息自带的 `length` 把**同时长**的两条语音分开。

    2026-10-03 真机侦察：内存里那条 SILK 的真实长度和 XML 的 `length` 精确对应
    （自己发出的样本 8/8 差 **−1**），而按时长挑出来的错误候选差 200~1900 字节。
    真实场景是"3 条候选时长一样、置信度也接近"→ 以前只能拒答。

    这里用两段**时长相同、字节数不同**的假 SILK 复现那个撞车，然后验证：
      * 给了 `target_bytes` ⇒ 只挑长度对得上的那一条；
      * **不给**（或没有一条对得上）⇒ 一个字节都不放宽，原来的"分不出来就拒答"照旧。
    """
    sec("length 指纹（同时长的两条语音怎么分开）")
    import audio_read
    import hashlib as _hl

    a_blob, b_blob = _mk_silk(200, 8), _mk_silk(200, 9)      # 都是 4000ms，字节数不同
    hits = [0x1000, 0x2000]
    blobs = {0x1000: a_blob, 0x2000: b_blob}

    _saved = (voice_mem._silk_ms, voice_mem._read_mem, voice_mem.available,
              voice_mem.weixin_main_process, voice_mem._k32, voice_mem.scan_silk,
              voice_mem.silk_to_wav, audio_read.transcribe_scored)
    try:
        voice_mem._silk_ms = (lambda s: len(voice_mem.frame_ends(s)) * voice_mem.FRAME_MS
                              if s.startswith(voice_mem.MAGIC) else None)
        voice_mem._read_mem = lambda h, addr, n: blobs.get(addr, b"")[:n]
        voice_mem.available = lambda: (True, "")
        voice_mem.weixin_main_process = lambda: (1, object())
        voice_mem._k32 = lambda: type("K", (), {"CloseHandle": staticmethod(lambda h: None)})()
        voice_mem.scan_silk = lambda h, deadline=None, **kw: (hits, True)
        voice_mem.silk_to_wav = lambda silk, wav: (1.0, 24000, "")

        dig = {_hl.sha256(b).hexdigest()[:12]: name for name, b in
               (("A", a_blob), ("B", b_blob))}

        # 两段文本故意做得**明显不同但都很通顺**（照抄真机那次撞车的样子：
        # 「帮我看看所有玻璃妮可跟我聊了什么」vs「Вау, это не суперпой, Ники.」）——
        # 只有真的不同，`_similar()` 才判成"两条不同语音"，tie 拒答才成立。
        _TALK = {"A": "帮我看看所有玻璃妮可跟我聊了什么",
                 "B": "Вау, это не суперпой, Ники."}

        def _fake_tr(wav, cfg):
            # 文件名形如 <digest>_<ms>.wav —— 用它反查是 A 还是 B
            for d, name in dig.items():
                if d in os.path.basename(wav):
                    # 分数故意做得**很接近**（模拟真机那个"置信度也接近"）
                    return (_TALK[name], -0.80 if name == "A" else -0.90, "")
            return ("", None, "认不出 wav")
        audio_read.transcribe_scored = _fake_tr

        # ① 候选要带上 length 指纹差（B 的真实长度 = len(b)+? → 用 len(b)+1 当 length）
        out = voice_mem.candidates(None, hits, tmp, target_ms=4000, target_bytes=len(b_blob) + 1)
        deltas = sorted(c["bytes_delta"] for c in out if c.get("bytes_delta") is not None)
        check("候选带上了 length 指纹差（命中那条 = −1）",
              deltas == sorted([len(a_blob) - (len(b_blob) + 1), -1]), deltas)

        # ② 有指纹命中 ⇒ 只挑那一条（B），同长度的 A 出局
        texts, err = voice_mem.read(4000, out_dir=tmp, cfg={},
                                   target_bytes=len(b_blob) + 1)
        check("有指纹命中 → **只挑那一条**（B）。同长度的 A 当场出局",
              texts == [_TALK["B"]] and not err, (texts, err))

        # ③ 指纹对不上任何候选 ⇒ **完全退回**时长逻辑：两条都留，分数太近就拒答
        texts2, err2 = voice_mem.read(4000, out_dir=tmp, cfg={}, target_bytes=999999)
        check("没有候选命中指纹 → 不复用指纹、退回时长逻辑（仍然如实拒答）",
              texts2 == [] and "认不出" in err2, (texts2, err2))

        # ④ 压根不给 target_bytes ⇒ 行为与改动前一致（老调用方不受影响）
        texts3, err3 = voice_mem.read(4000, out_dir=tmp, cfg={})
        check("不传 target_bytes → 行为不变（还是拒答，不放宽）",
              texts3 == [] and "认不出" in err3, (texts3, err3))
    finally:
        (voice_mem._silk_ms, voice_mem._read_mem, voice_mem.available,
         voice_mem.weixin_main_process, voice_mem._k32, voice_mem.scan_silk,
         voice_mem.silk_to_wav, audio_read.transcribe_scored) = _saved


def main():
    print("=" * 60)
    print("语音条逆向工具 voice_msg 回归自测（不联网、不需真实语音、不碰微信）")
    print("=" * 60)
    tmp = tempfile.mkdtemp(prefix="selftest_voice_msg_")
    try:
        t1_parse()
        t2_magic()
        t3_decrypt()
        t4_to_pcm_honest()
        t5_find_payload(tmp)
        t6_voice_info()
        t7_probe(tmp)
        t8_scan_deadline()
        t9_frame_estimate_gate(tmp)
        t10_length_fingerprint(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
