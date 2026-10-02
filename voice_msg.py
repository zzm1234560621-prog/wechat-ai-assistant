"""微信语音条：从消息 XML 到音频字节（逆向落地部分）。

## 现在确定的事实（2026-10-01 实测，见 docs/voice-msg-feasibility.md）

语音条的 `message_content`（zstd 解出来后）是这种 XML：：

    <msg><voicemsg endflag="1" cancelflag="0" forwardflag="0" voiceformat="4"
      voicelength="10880" length="20934" bufid="0"
      aeskey="0123456789abcdef0123456789abcdef"
      voiceurl="7f0c0006..." voicemd5="" clientmsgid="...newSendVoice_amr_..."
      fromusername="wxid_xxxxxxxxxxxx" silklength="0" /></msg>

    （上面是**形状**示例，aeskey/wxid **不是**真机值——真机的那些属于聊天内容，
    不许进仓库，测试里也一律用假值。）

两条关键：
  * **`aeskey` 就在 XML 里**（32 个 hex = 16 字节）——和图片一个套路。
    所以**不需要**去破 `docs/wechat4-dat-image-notes.md` 里那个「全局固定 AES 密钥」。
  * `length` 是加密后的字节数，`voicelength` 是时长（毫秒），`voiceformat` 是编码。

## 还没确定、且**必须拿真实文件才能确定**的两点

1. **字节在哪**：这台机器上搜遍全盘**没有任何 size≈`length` 的候选音频**，而
   `msg\\attach\\<md5>\\<月>\\Rec\\` 那两个有语音的群是空的。最合理的解释是
   **微信按需下载**（你不点开播放，它就不落盘）——所以要**先播一条语音**再看有没有新文件。
2. **加密方案**：key 是那把 16 字节没跑，但**模式/偏移/填充**未知（ECB？CBC？首个 16 字节当 IV？
   前面有没有容器头？）。所以这里不猜死，而是**把几种合理方案都试一遍，让魔数说话**
   （解出来能对上 `#!SILK_V3` / `#!AMR` / `RIFF` 的那个就是）。
   这是逆向该有的写法：不把猜测写成结论。

## 不做什么
* 不动 `Decode_Pic`（它在 4.1.10.27 上偏移是错的，见笔记），不重编译 hook。
* 拿不到真实文件之前，**不写「解码成功」的假路径**——`decrypt_variants()` 返回的全部是候选，
  由调用方看魔数判定；判不出来就如实说判不出来。
"""
import binascii
import json
import os
import re

# voiceformat 的取值（社区通说；**待真实文件验证**，所以只用于显示，不用于分支）
VOICE_FORMAT = {0: "amr", 1: "speex", 2: "mp3", 3: "amr-wb", 4: "silk", 5: "silk"}

# 各容器/编码的魔数
_MAGIC = (
    (b"#!SILK_V3", "silk-v3"),
    (b"#!SILK", "silk"),
    (b"\x02#!SILK_V3", "silk-v3-len前缀"),
    (b"#!AMR-WB", "amr-wb"),
    (b"#!AMR", "amr"),
    (b"RIFF", "wav"),
    (b"OggS", "ogg"),
    (b"ID3", "mp3"),
    (b"fLaC", "flac"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG", "png"),
)


def magic_of(data):
    """按魔数认容器/编码；认不出返回 None。"""
    if not data:
        return None
    for pat, name in _MAGIC:
        if bytes(data).startswith(pat):
            return name
        # SILK 文件有时前面带一个长度字节
        if len(data) > len(pat) + 1 and bytes(data)[1:1 + len(pat)] == pat:
            return name + "（偏移 1）"
    return None


def parse_voicemsg(xml):
    """从语音消息 XML 里抠出字段。返回 dict；不是语音就返回 {}。

    只认 `<voicemsg ...>` 那一坨属性，不依赖 XML 库（微信的 XML 常不带闭合、属性顺序也不保证）。
    """
    s = str(xml or "")
    m = re.search(r"<voicemsg\b([^>]*)/?>", s, re.S)
    if not m:
        return {}
    attrs = dict(re.findall(r'(\w+)\s*=\s*"([^"]*)"', m.group(1)))
    for k, v in re.findall(r"(\w+)\s*=\s*'([^']*)'", m.group(1)):
        attrs.setdefault(k, v)
    out = {
        "aeskey": (attrs.get("aeskey") or "").strip(),
        "voiceformat": attrs.get("voiceformat") or "",
        "length": attrs.get("length") or "",
        "voicelength": attrs.get("voicelength") or "",
        "voiceurl": attrs.get("voiceurl") or "",
        "fromusername": attrs.get("fromusername") or "",
    }
    try:
        out["length_bytes"] = int(out["length"])
    except (TypeError, ValueError):
        out["length_bytes"] = 0
    try:
        out["duration_ms"] = int(out["voicelength"])
    except (TypeError, ValueError):
        out["duration_ms"] = 0
    out["format_name"] = VOICE_FORMAT.get(_int(out["voiceformat"]), "")
    out["key_bytes"] = key_of(out["aeskey"])
    return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def key_of(aeskey_hex):
    """`aeskey` 字符串 → 16 字节 key；不是 32 个 hex 就返回 None（**不猜**）。"""
    s = str(aeskey_hex or "").strip()
    if len(s) != 32:
        return None
    try:
        return binascii.unhexlify(s)
    except Exception:
        return None


def decrypt_variants(buf, key):
    """把几种**合理但未证实**的解密方案都试一遍：返回 [(方案名, 明文), ...]。

    为什么要「都试」而不是选一个：模式/偏移/IV 目前**没有真实文件可验证**，
    把猜测写成单一结论就是在编。调用方用 `magic_of()` 挑出对的那个；
    一个都对不上，就说明方案不在这个集合里（或字节还没下载）。
    """
    out = []
    if not buf or not key or len(key) != 16:
        return out
    try:
        from Crypto.Cipher import AES
    except ImportError:
        return [("__NEED_PYCRYPTODOME__", b"")]
    data = bytes(buf)
    for off in (0, 1, 15, 16):                   # 可能前面有容器头/长度前缀
        body = data[off:]
        n = len(body) // 16 * 16
        if n < 16:
            continue
        body = body[:n]
        variants = [
            (f"ECB(off={off})", AES.new(key, AES.MODE_ECB)),
            (f"CBC-零IV(off={off})", AES.new(key, AES.MODE_CBC, b"\0" * 16)),
        ]
        for name, c in variants:
            try:
                out.append((name, c.decrypt(body)))
            except Exception:
                pass
        # 「前 16 字节当 IV」：IV 是明文里的前一块
        if off == 0 and n >= 32:
            try:
                out.append(("CBC-首块当IV(off=0)",
                            AES.new(key, AES.MODE_CBC, body[:16]).decrypt(body[16:n])))
            except Exception:
                pass
    return out


def hash_of(talker):
    """会话名 → `Msg_<md5>` 里那个 md5（= 微信的 attach 目录名）。"""
    import hashlib
    return hashlib.md5(str(talker).encode("utf-8")).hexdigest()


def candidate_dirs(talker, account_dirs):
    """语音**可能**落在哪些目录。不知道确切位置时，就把这几个都找一遍。

    已知：`msg\\attach\\<md5(会话)>\\<年-月>\\Rec\\…` 是媒体目录（图片/富文本在这），
    但语音**不在那里**（实测那两个有语音的群，`Rec\\` 是空的）。
    所以这里把「该会话的 attach」「msg\\file」都列上，让按大小的搜索去覆盖。
    """
    out = []
    h = hash_of(talker)
    for acct in account_dirs or []:
        for sub in (os.path.join("msg", "attach", h), os.path.join("msg", "attach"),
                    os.path.join("msg", "file")):
            p = os.path.join(acct, sub)
            if os.path.isdir(p) and p not in out:
                out.append(p)
    return out


def find_payload(dirs, length, tol=64, limit=8):
    """按 `length`（消息 XML 里的加密字节数）在目录里找候选音频文件。

    为什么按大小找：语音 XML 里**没有文件名**（图片有 `packed_info_data` 可解），
    所以「大小对得上」是唯一的关联线索。差几字节很正常（容器头 / AES 填充），
    因此给容差；返回按差距从小到大排序，调用方逐个去 `identify()` + 解码试。
    """
    want = int(length or 0)
    if want <= 0 or not dirs:
        return []
    hits = []
    for d in dirs:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                diff = abs(sz - want)
                if diff <= int(tol):
                    hits.append({"path": p, "size": sz, "diff": diff})
    hits.sort(key=lambda h: h["diff"])
    return hits[:int(limit)]


def identify(buf, key):
    """试解密，返回**所有**明文能对上魔数的候选：`[(方案名, 明文, 魔数名), ...]`。

    ⚠️ **魔数只能证明「第一块」解对了，分不出模式**：CBC 零 IV 与 ECB 的第一块
    密文/明文完全一样，所以同一份数据在两种方案下首块都能对上 `#!SILK_V3`。
    （2026-10-01 自测当场抓到这个：CBC 加密的载荷被认成 ECB。）
    真正的裁决是**完整解码是否成功**——调用方对每个候选跑 `to_pcm()`，
    能解出 PCM 的那个才是对的。所以这里返回列表，**不替调用方挑一个**。
    """
    out = []
    for name, plain in decrypt_variants(buf, key):
        if name == "__NEED_PYCRYPTODOME__":
            return [("__NEED_PYCRYPTODOME__", b"", None)]
        mg = magic_of(plain)
        if mg:
            out.append((name, plain, mg))
    return out


def to_pcm(data):
    """把认出来的容器解成 PCM（16k 单声道）。返回 (pcm_bytes, err)。

    SILK 走 `pilk`（`pip install pilk`），AMR/其它走 PyAV（faster-whisper 会带）。
    **没装解码器就如实报错**，不返回空音频假装成功。
    """
    mg = magic_of(data)
    if mg is None:
        return b"", "认不出音频格式（魔数对不上 SILK/AMR/WAV…），不解码。"
    if mg.startswith("silk"):
        try:
            import pilk
        except ImportError:
            return b"", ("SILK 要 pilk 解码：.venv\\Scripts\\python.exe -m pip install pilk"
                         "（或把 codec 交给微信自己：见下）")
        import tempfile
        try:
            fi = tempfile.NamedTemporaryFile(suffix=".silk", delete=False)
            fi.write(data)
            fi.close()
            fo = fi.name + ".pcm"
            pilk.silk_to_pcm2(fi.name, fo, rate=16000)
            with open(fo, "rb") as fh:
                return fh.read(), ""
        except Exception as e:
            return b"", f"pilk 解码失败：{type(e).__name__}: {str(e)[:120]}"
    # 其它容器交给 PyAV
    try:
        import av
        import io as _io
        with av.open(_io.BytesIO(data)) as c:
            out = bytearray()
            for frame in c.decode(audio=0):
                s = frame.to_ndarray()
                out += s.tobytes()
            return bytes(out), ""
    except Exception as e:
        return b"", f"解码失败（{mg}）：{type(e).__name__}: {str(e)[:120]}"


# ── --probe：先拍基线 → 你播放一条语音 → 再看新增（**用证据说话，不猜**）──────
#
# 为什么必须先拍基线：`msg\attach\<md5(会话)>\<月>\Rec\` 实测是**空的**，
# 最合理的解释是**微信按需下载**——你不点开播放，它就不落盘。
# 所以「直接全盘搜一个大小接近的音频」会捞到一堆无关命中（上一次实验就是这样，
# 结论也因此被推翻过一次）。而「播放这条语音之后**新出现/变化**的文件」才是真候选。
#
# 判据仍然是**魔数 + 完整解码**：魔数只能证明第一块解对了（CBC 零 IV 与 ECB 首块相同，
# 见 identify 的说明），所以最终要看 to_pcm 能不能出东西。**判不出来就如实说判不出来。**

def _snapshot(dirs):
    """目录树 → `{路径: [大小, mtime]}`。读不到的文件跳过（不炸）。"""
    out = {}
    for d in dirs or []:
        for root, _sub, files in os.walk(d):
            for fn in files:
                p = os.path.join(root, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out[p] = [st.st_size, int(st.st_mtime)]
    return out


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _save_json(path, data):
    """返回 `(是否成功, 错误文本)`。失败**不抛**——这只是个诊断工具。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return True, ""
    except OSError as e:
        return False, str(e)


def probe(dirs, length=0, aeskey="", state_path=None, tol=256, limit=20):
    """两次调用完成一次探测。

    第一次（没有基线）→ 拍基线并告诉你下一步做什么。
    第二次 → 列出相对基线的**新增/变化**文件，按 `length` 过滤（给了的话），
    再逐个试解密，**报出所有能完整解码的方案**（不替调用方挑一个）。

    返回 `(给用户看的报告文本, 结构化结果)`。**任何一步失败都如实写在文本里。**
    """
    state_path = state_path or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "voice_probe.json")
    cur = _snapshot(dirs)
    before = _load_json(state_path)

    if before is None:
        ok, err = _save_json(state_path, cur)
        lines = [f"📸 已记录基线：{len(cur)} 个文件"
                 + (f"（存在 {state_path}）" if ok else f"，但**没能存下来**：{err}"),
                 "",
                 "接下来（顺序别反）：",
                 "  1. 在微信里**把那条语音条点开播放一次**——目的是让它落盘；",
                 "  2. 再跑一遍**同样的命令**。",
                 "",
                 "为什么要先拍基线：这个数据目录里 `Rec/` 是空的，微信大概率"
                 "**按需下载**；直接全盘搜「大小接近的音频」会捞到一堆无关命中。"]
        if not ok:
            lines.append("⚠️ 基线存不下来，第二次跑仍会被当成「第一次」——"
                         "先确认 data/ 可写。")
        return "\n".join(lines), {"phase": "baseline", "files": len(cur), "saved": ok}

    changed = sorted(p for p, meta in cur.items() if before.get(p) != meta)
    lines = [f"🔍 与基线相比：**新增/变化 {len(changed)} 个文件**"
             f"（基线 {len(before)} → 现在 {len(cur)}）"]
    if not changed:
        lines += ["",
                  "**没有任何新文件落盘。**",
                  "这说明微信确实**没有**把语音条的音频写到磁盘上——哪怕你播放过。",
                  "那就别再往这条路投入了：按 docs/voice-msg-feasibility.md 的结论，"
                  "**语音条读不了，应当如实告诉用户做不到**。",
                  "",
                  "（想再确认一次的话，跑 `--probe-reset` 清掉基线重新走一遍。）"]
        return "\n".join(lines), {"phase": "changes", "changed": 0}

    cands = [{"path": p, "size": cur[p][0]} for p in changed]
    if length:
        want = int(length)
        cands = [c for c in cands if abs(c["size"] - want) <= int(tol)]
        lines.append(f"按 `length={want}`（±{tol} 字节）过滤后剩 **{len(cands)}** 个")
        if not cands:
            lines += ["", "除了数量对不上，没有别的线索了。把 `--tol` 放宽一点再试，"
                          "或者先看看 `--probe` 列出来的全部新增文件。"]
    cands.sort(key=lambda c: abs(c["size"] - (int(length) if length else 0)))
    cands = cands[:int(limit)]

    decoded = []
    for c in cands:
        try:
            with open(c["path"], "rb") as f:
                data = f.read()
        except OSError as e:
            lines.append(f"  · {os.path.basename(c['path'])}（{c['size']}B）读不动：{e}")
            continue
        raw_mg = magic_of(data)
        head = (f"  · {os.path.basename(c['path'])}（{c['size']}B）"
                f"原样魔数={raw_mg or '不认得'}")
        if not aeskey:
            lines.append(head + "   ← 没给 `--aeskey`，只能看它是不是明文")
            if raw_mg:
                decoded.append((c["path"], "（未解密）", raw_mg, 0))
            continue
        try:
            cands_scheme = identify(data, key_of(aeskey))
        except Exception as e:
            lines.append(head + f"   ← 试解密时出错：{type(e).__name__}: {e}")
            continue
        if not cands_scheme:
            lines.append(head + "   ← 试过的那几种方案**一个都没对上魔数**")
            continue
        lines.append(head)
        for name, plain, mg in cands_scheme:
            if name == "__NEED_PYCRYPTODOME__":
                lines.append("      ⚠️ 需要 pycryptodome 才能试 AES（pip install pycryptodome）")
                break
            pcm, perr = to_pcm(plain)
            if pcm:
                lines.append(f"      ✅ {name}：魔数={mg}，**完整解码出 {len(pcm)} 字节 PCM**")
                decoded.append((c["path"], name, mg, len(pcm)))
            else:
                lines.append(f"      ❌ {name}：魔数={mg}，解码失败：{perr[:70]}")

    lines.append("")
    if decoded:
        lines.append("👉 结论（**按完整解码判定**，不是只看魔数）：")
        for p, name, mg, n in decoded:
            lines.append(f"   · {os.path.basename(p)}：方案 **{name}**，魔数 {mg}"
                         + (f"，PCM {n} 字节" if n else "（未解密，只是明文）"))
        lines.append("   把命中的那个方案记进 docs/voice-msg-feasibility.md，"
                     "再决定要不要接进聊天通路。**别只看魔数就下结论**"
                     "（CBC 零 IV 与 ECB 的首块相同，会同时命中）。")
    else:
        lines.append("👉 **没试出能用的方案。** 如实说：这一轮没能确定语音条的字节在哪 / "
                     "怎么解，不要编一个「大概就是这个」的结论。")
        lines.append("   可以把 `--tol` 放宽、或去掉 `--length` 先看全部新增文件，"
                     "再拿候选去 `--file <路径> <aeskey>` 单独试。")
    return "\n".join(lines), {"phase": "changes", "changed": len(changed),
                              "candidates": len(cands), "decoded": len(decoded)}


if __name__ == "__main__":
    # 现场小工具：给一个 XML（文件或原串）或一个候选文件，看能认出什么
    import sys
    if "--xml-file" in sys.argv:
        i = sys.argv.index("--xml-file")
        p = sys.argv[i + 1]
        # ⚠️ 用文件而不是内联字符串：cmd/PowerShell 会把内联 XML 里的引号吃掉，
        #    传进来就变成属性全空——这不是解析器的问题，是 shell 的引号规则。
        info = parse_voicemsg(open(p, encoding="utf-8", errors="replace").read())
        print(info)
    elif "--xml" in sys.argv:
        i = sys.argv.index("--xml")
        info = parse_voicemsg(sys.argv[i + 1] if len(sys.argv) > i + 1 else "")
        print(info)
        if not info.get("aeskey"):
            print("（提示：内联 XML 里的引号常被 shell 吃掉，改用 --xml-file <文件>）")
    elif "--file" in sys.argv:
        i = sys.argv.index("--file")
        p = sys.argv[i + 1]
        key = key_of(sys.argv[i + 2]) if len(sys.argv) > i + 2 else None
        data = open(p, "rb").read()
        print(f"{p}: {len(data)}B  原样魔数 = {magic_of(data)}")
        if key:
            cands = identify(data, key)
            print(f"  解密候选（魔数只能证明第一块，多方案会同时命中）：{len(cands)} 个")
            for name, plain, mg in cands:
                pcm, perr = to_pcm(plain)
                verdict = f"✅ 解码出 {len(pcm)} 字节 PCM" if pcm else f"❌ {perr[:60]}"
                print(f"    {name:22s} 魔数={mg:12s} {verdict}")
            if not cands:
                print("    一个都没对上 → 字节可能还没下载，或加密方案不在试过的那几种里。")
    elif "--probe-reset" in sys.argv:
        sp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "voice_probe.json")
        if "--state" in sys.argv:
            sp = sys.argv[sys.argv.index("--state") + 1]
        try:
            os.remove(sp)
            print(f"已清掉基线：{sp}（下次 --probe 会重新拍一次）")
        except FileNotFoundError:
            print(f"本来就没有基线：{sp}")
        except OSError as e:
            print(f"删不掉基线（{e}）——先确认文件没被占用。")
    elif "--probe" in sys.argv:
        def _opt(name, default=""):
            if name in sys.argv and len(sys.argv) > sys.argv.index(name) + 1:
                return sys.argv[sys.argv.index(name) + 1]
            return default

        i = sys.argv.index("--probe")
        acct = ""
        if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("--"):
            acct = sys.argv[i + 1]
        if not acct:
            print("用法：python voice_msg.py --probe <微信账号数据目录> "
                  "[--talker <会话>] [--length <字节数>] [--aeskey <32位hex>] "
                  "[--tol 256] [--state <基线文件>]")
            print("      python voice_msg.py --probe-reset        # 清掉基线重来")
            print("  <微信账号数据目录> 形如 ...\\xwechat_files\\wxid_xxxx_1234")
            print("  第一次跑只拍基线；**播放一条语音条**之后再跑同样的命令。")
            raise SystemExit(0)

        talker = _opt("--talker")
        dirs = candidate_dirs(talker, [acct]) if talker else []
        if not dirs:
            # 没给会话就把整个账号目录当扫描面（范围更大 = 更容易捞到，但噪音也更多）
            dirs = [acct] if os.path.isdir(acct) else []
        if not dirs:
            print(f"这个目录不存在或读不到：{acct}")
            raise SystemExit(1)

        try:
            length = int(_opt("--length", "0") or 0)
        except ValueError:
            print("--length 要是数字（就是消息 XML 里 voicemsg 的 length 属性）")
            raise SystemExit(1)
        try:
            tol = int(_opt("--tol", "256") or 256)
        except ValueError:
            tol = 256

        text, _res = probe(dirs, length=length, aeskey=_opt("--aeskey"),
                           state_path=_opt("--state") or None, tol=tol)
        print(text)
    else:
        print(__doc__.split("##")[0])
        print("用法：")
        print("  python voice_msg.py --xml '<msg><voicemsg aeskey=... /></msg>'")
        print("  python voice_msg.py --file <候选文件> <aeskey>")
        print("  python voice_msg.py --probe <微信账号数据目录> [--talker <会话>] "
              "[--length N] [--aeskey <hex>]   # 先拍基线，播放一条再跑一次")
        print("  python voice_msg.py --probe-reset")
