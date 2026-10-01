"""语音条逆向工具（voice_msg）的回归自测。

**不联网、不需要真实语音文件、不碰微信**：
  * XML 解析用**合成**的 XML（形状照真机抓到的那条 1:1 复刻，但里面的
    aeskey / wxid / roomid **全部换成假的**——真机的那些是私事，不进仓库）；
  * 解密用**自己加密再解回来**（验的是我的代码，不是微信的格式：格式要等真实文件才能定）。

用法：`.venv/Scripts/python.exe selftest_voice_msg.py`
"""
import binascii
import os
import sys

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


def main():
    print("=" * 60)
    print("语音条逆向工具 voice_msg 回归自测（不联网、不需真实语音、不碰微信）")
    print("=" * 60)
    t1_parse()
    t2_magic()
    t3_decrypt()
    t4_to_pcm_honest()
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
