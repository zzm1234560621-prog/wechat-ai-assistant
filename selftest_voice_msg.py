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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
