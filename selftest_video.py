"""视频读取（`video_read` + `file_read` 视频分支）的回归自测。

**不联网、不需要微信**。视频样本是**现场用 PyAV 造**的（这也顺带证明了这条链不依赖
ffmpeg.exe / Pillow / numpy）：
  * 造一个 20 秒、带音轨的 mp4，**画面就是一张有字的图**（PowerShell + System.Drawing 画的），
    于是「抽帧 → 图片通道 → OCR 认出字」是**真跑**出来的；
  * 音轨是正弦音：本机没装 faster-whisper 时要**如实说**「转写要装什么」，不许假装听完了。

钉住的规矩：
  * `video.max_seconds` 分段：第一段给 cursor，`extract_page(cursor=…)` 从**下一段**接着读；
  * 读到末尾要**明说**"已到末尾"，不是给个空段；
  * `image.mode=off` / `video.frame_seconds=0` → **一张画面都不抽**，并明说"画面没看"；
  * 抽帧张数受 `video.max_frames` 约束，且**实际间隔**要写出来（不许悄悄少抽）；
  * PyAV 没装时给**能照做**的安装指引，绝不静默降级成"没内容"。

用法：`.venv/Scripts/python.exe selftest_video.py`
"""
import array
import io
import re
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import file_read  # noqa: E402
import image_read  # noqa: E402
import video_read  # noqa: E402

TMP = tempfile.mkdtemp(prefix="selftest_video_")
_ok = True
_SKIP = []


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


def skip(label):
    _SKIP.append(label)
    print(f"  ⏭️  {label}")


# ---------------- 造样本 ----------------
def _text_png(path, text="体检报告 2026 年 10 月", w=480, h=220):
    """用系统 System.Drawing 画一张**有字**的图（不装 Pillow）。"""
    ps = (
        'Add-Type -AssemblyName System.Drawing; '
        f'$b = New-Object System.Drawing.Bitmap({w},{h}); '
        '$g = [System.Drawing.Graphics]::FromImage($b); '
        '$g.Clear([System.Drawing.Color]::White); '
        '$f = New-Object System.Drawing.Font("Microsoft YaHei",20); '
        '$g.DrawString("' + text + '", $f, [System.Drawing.Brushes]::Black, 12, 80); '
        '$g.Dispose(); $b.Save("' + path + '", [System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()'
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=90)
    return path if os.path.getsize(path) > 0 else None


def _tone_wav(path, secs=20.0, rate=44100):
    n = int(secs * rate)
    s = array.array("h")
    for i in range(n):
        s.append(int(9000 * math.sin(2 * math.pi * 440 * i / rate)))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(s.tobytes())
    return path


def _make_video(path, png, wav, secs=20, fps=5, w=480, h=220):
    """把那张有字的图当画面、把正弦当音轨，合成一个 mp4。"""
    import av
    with wave.open(wav, "rb") as wf:
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    # 先把 png 解成一帧，之后重复用（不用 Pillow）
    with av.open(png) as pc:
        still = next(pc.decode(video=0)).reformat(format="yuv420p")

    out = av.open(path, "w")
    try:
        vs = out.add_stream("mpeg4", rate=fps)
        vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
        st = out.add_stream("aac", rate=rate)
        st.layout = "mono"
        for i in range(fps * secs):
            frame = still.reformat(format="yuv420p")
            frame.pts = i
            for pkt in vs.encode(frame):
                out.mux(pkt)
        for pkt in vs.encode():
            out.mux(pkt)
        step = 2048
        for off in range(0, len(raw), step):
            chunk = raw[off:off + step]
            if not chunk:
                break
            af = av.AudioFrame(format="s16", layout="mono", samples=len(chunk) // 2)
            af.sample_rate = rate
            af.planes[0].update(chunk)
            for pkt in st.encode(af):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)
    finally:
        out.close()
    return path


def t1_without_pyav():
    print("\n── 1 · 没装 PyAV 时：给能照做的指引，不静默降级 ──")
    real = video_read.available
    video_read.available = lambda cfg=None: (
        False, "读视频需要 PyAV（它自带 ffmpeg，不需要另装 ffmpeg）："
               "`.venv\\Scripts\\python.exe -m pip install av`")
    try:
        text, nxt, note = video_read.read_window(os.path.join(TMP, "x.mp4"), {})
    finally:
        video_read.available = real
    check("read_window 如实返回失败", text is None and nxt is None, (text, nxt))
    check("指引里有 pip install av", note and "pip install av" in note, note)

    p = os.path.join(TMP, "不是视频.bin")
    with open(p, "wb") as f:
        f.write(b"\x00\x01\x02" * 100)
    check("info() 对非视频返回 None（不猜）", video_read.info(p) is None)
    text, nxt, note = video_read.read_window(p, {})
    check("非视频 → 如实说打不开", text is None and note, (text, note))


def t2_real_video():
    print("\n── 2 · 真机：造带音轨的视频，抽帧走图片通道认出画面里的字 ──")
    ok, why = video_read.available()
    if not ok:
        skip("PyAV 没装，跳过真机那条")
        return
    png = _text_png(os.path.join(TMP, "帧.png"))
    if not png:
        skip("画不出带字的 PNG（System.Drawing 不可用），跳过真机那条")
        return
    wav = _tone_wav(os.path.join(TMP, "音.wav"), secs=20)
    mp4 = _make_video(os.path.join(TMP, "报告.mp4"), png, wav, secs=20)
    print(f"  （样本：{os.path.getsize(mp4)//1024}KB，20 秒，480x220@5fps，带音轨）")
    meta = video_read.info(mp4)
    check("能读出时长/分辨率/音轨", meta and 19 < (meta["duration"] or 0) < 21
          and meta["has_audio"] and meta["width"] == 480, meta)

    cfg = {"image": {"mode": "ocr"}, "file": {"max_bytes": 0},
           "video": {"max_seconds": 8, "frame_seconds": 2, "max_frames": 6}}
    t0 = time.time()
    text, err = file_read.extract(mp4, cfg)
    print(f"  （第一段用时 {time.time() - t0:.1f}s）")
    check("整条链读出来了", bool(text) and not err, err)
    check("结果里写了时长与这一段的范围",
          bool(text) and "总长" in text and "00:00~00:08" in text, (text or "")[:160])
    flat = re.sub(r"\s+", "", text or "")
    check("**画面里的字被认出来了**（真跑 OCR；OCR 会在字间加空格）",
          "体检报告" in flat and "2026" in flat, (text or "")[-400:])
    check("抽帧带了时间标签", "[00:0" in (text or ""), (text or "")[-300:])
    check("没装 faster-whisper 时**如实说**（不假装听完）",
          "faster-whisper" in (text or "") or "转写" in (text or ""), text[:600])
    check("长视频给了 cursor（要继续读）", "cursor=" in (text or ""), (text or "")[-200:])

    # ② 继续读下一段
    m = re.search(r"cursor=([0-9a-f]{16}:\d+)", text or "")
    cur = m.group(1) if m else None
    check("cursor 形状是 id:秒", bool(cur) and cur.count(":") == 1, cur)
    text2, err2 = file_read.extract_page(cfg=cfg, cursor=cur)
    check("「继续」读的是**下一段**（不是从头再来）",
          bool(text2) and not err2 and "00:08~00:16" in text2, (err2, (text2 or "")[:160]))
    check("第二段也有 cursor（还能再继续）", "cursor=" in (text2 or ""), (text2 or "")[-160:])

    # ③ 读到末尾
    m2 = re.search(r"cursor=([0-9a-f]{16}:\d+)", text2 or "")
    cur2 = m2.group(1) if m2 else None
    text3, err3 = file_read.extract_page(cfg=cfg, cursor=cur2)
    check("最后一段读完后**明说已到末尾**",
          bool(text3) and "末尾" in text3 and "cursor=" not in text3, (err3, (text3 or "")[-200:]))

    # ④ 导出是逐段累积的（用户能看完整转写）
    d = file_read.export_dir(cfg)
    exports = [n for n in os.listdir(d) if n.startswith("v_") and n.endswith(".txt")]
    check("转写全文按段累积在导出文件里", bool(exports), exports)
    if exports:
        body = open(os.path.join(d, exports[0]), encoding="utf-8").read()
        check("累积文件里有三段的内容（00:00 / 00:08 / 00:16 都出现）",
              "00:00~00:08" in body and "00:08~00:16" in body and "00:16~" in body,
              body[-300:])

    # ⑤ 再「继续」一次：已经到末尾
    text4, err4 = file_read.extract_page(cfg=cfg, cursor=cur2)
    check("重复「继续」也如实说（不返空、不重读）",
          (err4 and "末尾" in err4) or (text4 and "末尾" in text4), (err4, (text4 or "")[-120:]))
    return mp4


def t3_frames_rules(mp4):
    print("\n── 3 · 画面抽帧的三条规矩（off / 关闭 / 张数上限） ──")
    if not mp4 or not os.path.isfile(mp4):
        skip("没有样本视频，跳过")
        return
    calls = {"n": 0}
    real_grab = video_read._grab_frame

    def counting(*a, **kw):
        calls["n"] += 1
        return real_grab(*a, **kw)

    video_read._grab_frame = counting
    try:
        base = {"image": {"mode": "off"}, "file": {"max_bytes": 0},
                "video": {"max_seconds": 8, "frame_seconds": 2, "max_frames": 6}}
        text, err = file_read.extract(mp4, base)
        check("image.mode=off → 明说「画面没看」", text and "画面没看" in text, text[:200])
        check("……并且**一张都没抽**（零次抽帧）", calls["n"] == 0, calls)

        calls["n"] = 0
        cfg2 = {"image": {"mode": "ocr"}, "file": {"max_bytes": 0},
                "video": {"max_seconds": 8, "frame_seconds": 0, "max_frames": 6}}
        text, err = file_read.extract(mp4, cfg2)
        check("frame_seconds=0 → 明说「画面没看」且零次抽帧",
              text and "画面没看" in text and calls["n"] == 0, (text[:200], calls))

        calls["n"] = 0
        cfg3 = {"image": {"mode": "ocr"}, "file": {"max_bytes": 0},
                "video": {"max_seconds": 8, "frame_seconds": 1, "max_frames": 2}}
        text, err = file_read.extract(mp4, cfg3)
        check("max_frames=2 → 正好抽 2 张", calls["n"] == 2, calls)
        check("……并且把**实际间隔**写出来（不悄悄少抽）",
              text and "每 4 秒抽 1 张" in text, text[:300])
    finally:
        video_read._grab_frame = real_grab


def t4_clamps_are_loud():
    print("\n── 4 · 配置被夹取时要**说出来**（静默改用户配置是禁止的） ──")
    video_read._warned.clear()
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        v = video_read.max_seconds({"video": {"max_seconds": 99999}})
    finally:
        sys.stdout = old
    check("超上限被夹到 7200 并告警", v == 7200 and "超出" in buf.getvalue(), (v, buf.getvalue()))
    check("默认是 1800（30 分钟一段）", video_read.max_seconds({}) == 1800)


def main():
    print("=" * 60)
    print("视频读取回归自测（临时目录：%s）" % TMP)
    print("=" * 60)
    mp4 = None
    try:
        t1_without_pyav()
        mp4 = t2_real_video()
        t3_frames_rules(mp4)
        t4_clamps_are_loud()
    finally:
        video_read.sweep_tmp(max_age=0)
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 60)
    if _SKIP:
        print(f"（跳过 {len(_SKIP)} 项：{'; '.join(_SKIP)}）")
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
