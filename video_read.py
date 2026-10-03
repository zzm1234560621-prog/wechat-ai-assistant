"""视频 → 文字 + 画面（P2）。规格：`docs/file-input-spec.md` 第九节。

一个视频里的信息有两条：**声音**（说话内容）和**画面**（幻灯片、字幕、演示）。
所以这里的做法是：

1. **音轨 → 文字**：把窗口内的音轨抽成 16kHz 单声道 WAV，交给 `audio_read.transcribe()`
   （本机 faster-whisper 默认不出本机；配了 cloud 才上传，且**上传必打日志**）。
2. **画面 → 图片通道**：按 `video.frame_seconds` 在窗口内**均匀**抽帧、存成 JPEG，
   交给 `image_read.handoff()`（所以 OCR/视觉/inline 四种模式全自动适用）。
3. **长视频分段**：`video.max_seconds`（默认 1800 = 30 分钟）是一次的量；超了**不是拒绝**，
   而是给一个 `cursor=<id>:<秒>`，用户说「继续」就从那个秒数接着读（**不重不漏**）。

三条铁律：
  * **零外部依赖**：只用 PyAV（wheel 自带 ffmpeg）。抽帧用 PyAV 自带的 mjpeg 编码器，
    **不需要 Pillow / numpy / ffmpeg.exe**。
  * **拿不到就说拿不到**：没有音轨、时长读不出、字幕轨不转、抽帧失败 —— 每一条都**如实**写在结果里。
  * **不给画面编内容**：`image.mode=off` 时画面那张**一张都不抽**，并明说"画面没看"。
"""
import os
import shutil
import time

import tempdir

_HERE = os.path.dirname(os.path.abspath(__file__))

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv",
             ".flv", ".mpg", ".mpeg", ".3gp", ".ts")

# 抽出来的音轨统一成 whisper 喜欢的规格（也省磁盘：30 分钟≈57MB，而不是 158MB）
_WAV_RATE = 16000


def is_video(path):
    return str(path or "").lower().endswith(VIDEO_EXT)


def section(cfg):
    sec = ((cfg or {}).get("video") or {})
    return dict(sec) if isinstance(sec, dict) else {}


_warned = set()


def _warn_once(key, msg):
    if key in _warned:
        return
    _warned.add(key)
    print(f"⚠️ video_read: {msg}", flush=True)


def _int_opt(value, default, lo, hi, key=""):
    """取一个整数配置。**夹取时要说出来**（静默改用户的配置是禁止的）。"""
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        _warn_once(f"{key}:bad", f"video.{key}={value!r} 不是整数，按默认 {default} 用。")
        return default
    if n < lo or n > hi:
        fixed = max(lo, min(n, hi))
        _warn_once(f"{key}:{n}", f"video.{key}={n} 超出 [{lo}, {hi}]，按 {fixed} 用。")
        return fixed
    return n


def max_seconds(cfg=None):
    """一次最多读多少秒（默认 1800）。超了给 cursor，**不是拒绝**。"""
    return _int_opt(section(cfg).get("max_seconds"), 1800, 1, 7200, "max_seconds")


def frame_seconds(cfg=None):
    """画面抽帧的最小间隔（秒）。0 = 不抽画面。"""
    return _int_opt(section(cfg).get("frame_seconds"), 30, 0, 3600, "frame_seconds")


def max_frames(cfg=None):
    """一次最多抽几张画面（默认 20）。"""
    return _int_opt(section(cfg).get("max_frames"), 20, 1, 60, "max_frames")


def available(cfg=None):
    """PyAV 装了没。返回 `(能不能读, 说明)`。"""
    try:
        import av
    except ImportError:
        return False, ("读视频需要 PyAV（它自带 ffmpeg，不需要另装 ffmpeg）："
                       "`.venv\\\\Scripts\\\\python.exe -m pip install av`")
    return True, f"PyAV {av.__version__}"


def tmp_dir():
    """抽帧/抽轨的临时目录。统一走 `tempdir.get()` —— 可用 `PROJ_TMP` 改道（见 `tempdir.py`）。"""
    return tempdir.get("tmp_video")


def sweep_tmp(max_age=86400.0):
    """清理抽帧/抽轨留下的临时文件（**删了什么要打日志**，由 `tempdir.sweep` 统一实现）。"""
    return tempdir.sweep("tmp_video", max_age, "video_read")


def info(path):
    """视频基本信息。读不出来返回 None（**不猜**）。"""
    ok, _why = available()
    if not ok:
        return None
    import av
    try:
        with av.open(path) as c:
            dur = None
            if c.duration is not None:
                dur = float(c.duration) / float(av.time_base)
            v = c.streams.video[0] if c.streams.video else None
            a = c.streams.audio[0] if c.streams.audio else None
            if dur is None and v is not None and v.duration is not None:
                dur = float(v.duration * v.time_base)
            return {
                "duration": dur,
                "has_audio": a is not None,
                "audio_codec": a.codec_context.name if a else None,
                "video_codec": v.codec_context.name if v else None,
                "width": v.codec_context.width if v else None,
                "height": v.codec_context.height if v else None,
                "fps": (float(v.average_rate) if v and v.average_rate else None),
            }
    except Exception as e:
        print(f"⚠️ video_read: 读不出视频信息（{type(e).__name__}: {str(e)[:120]}）", flush=True)
        return None


def _grab_audio(path, out, start, secs):
    """把视频里的音轨切出来 —— **实现只有一份**，在 `audio_read.slice_to_wav`。

    为什么共用：16k/单声道的重采样参数要是两处各写一套，早晚会跑偏；
    而且长音频分段和视频抽音轨本来就是同一件事。
    """
    import audio_read
    return audio_read.slice_to_wav(path, out, start=start, secs=secs)


def _grab_frame(path, out, at):
    """在 `at` 秒附近抽一帧、编成 JPEG。返回**实际那一帧的时刻**（拿不到返回 None）。

    `seek` 只能落到关键帧，所以抽到的可能是稍早一点的那一帧——**标签按实际时刻写**，
    绝不把"我要 90 秒"当成"这就是 90 秒的画面"。
    """
    import av
    try:
        with av.open(path) as c:
            if not c.streams.video:
                return None
            c.seek(int(max(0.0, at) / float(av.time_base)), any_frame=False, backward=True)
            frame = None
            for f in c.decode(video=0):
                frame = f
                if f.time is not None and f.time >= at - 0.001:
                    break
            if frame is None:
                return None
            stamp = float(frame.time) if frame.time is not None else float(at)
            frame = frame.reformat(format="yuvj420p")
            with av.open(out, "w", format="image2") as o:
                st = o.add_stream("mjpeg", rate=1)
                st.width, st.height, st.pix_fmt = frame.width, frame.height, "yuvj420p"
                for pkt in st.encode(frame):
                    o.mux(pkt)
                for pkt in st.encode():
                    o.mux(pkt)
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return stamp
    except Exception as e:
        print(f"⚠️ video_read: 抽帧失败（{type(e).__name__}: {str(e)[:120]}）", flush=True)
    return None


def _hms(sec):
    sec = int(max(0, sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def read_window(path, cfg=None, start=0, on_image=None):
    """读视频的一个窗口。返回 `(文本, 下一段起点或 None, 说明)`。

    * `start`：从第几秒开始（普通读取给 0；「继续」由 cursor 带过来）；
    * `on_image`：`image.mode=inline` 时的收图回调（交给上层塞进这一轮的消息）。
    """
    cfg = cfg or {}
    ok, why = available(cfg)
    if not ok:
        return None, None, why
    if not os.path.isfile(path):
        return None, None, "视频不在本机"

    meta = info(path)
    if meta is None:
        return None, None, "这个文件 PyAV 打不开（可能损坏、或其实不是视频）。"

    import image_read
    import audio_read

    dur = meta["duration"]
    start = max(0, int(start))
    if dur is not None and start >= max(0, dur - 0.5):
        return None, None, f"这个视频一共 {_hms(dur)}，已经读到末尾了。"
    limit = max_seconds(cfg)
    end = min(dur, start + limit) if dur is not None else (start + limit)

    head = [f"—— 视频（{_hms(start)}~{_hms(end) if dur is not None else '结尾未知'}",
            f"；总长 {_hms(dur) if dur is not None else '读不出（按容器没写时长）'}"]
    if meta["width"]:
        head.append(f"；{meta['width']}x{meta['height']}"
                    + (f"@{meta['fps']:.0f}fps" if meta["fps"] else ""))
    if meta["video_codec"]:
        head.append(f"；画面 {meta['video_codec']}")
    head.append("）")
    parts = ["".join(head) + "\n"]

    # ① 音轨 → 文字
    if meta["has_audio"]:
        wav = os.path.join(tmp_dir(), f"{os.path.basename(path)[:40]}.{start}.wav")
        try:
            n = _grab_audio(path, wav, start, max(1.0, end - start))
            if n == 0:
                parts.append("（音轨在窗口内没解出音频帧——可能是空的/纯静音）")
            else:
                text, err = audio_read.transcribe(wav, cfg, max_bytes=os.path.getsize(wav))
                if err:
                    parts.append(f"【说话内容】转写没成功：{err}")
                elif text.strip():
                    parts.append(f"【说话内容（{_hms(start)}~{_hms(end)}）】\n{text.strip()}")
                else:
                    parts.append("【说话内容】这段里**没有识别到人声**（不是失败，是真的没说话声）。")
        except Exception as e:
            parts.append(f"【说话内容】抽音轨失败：{type(e).__name__}: {str(e)[:120]}（**没有文字可给**）")
        finally:
            try:
                if os.path.isfile(wav):
                    os.remove(wav)          # 音轨只在转写期间需要
            except OSError:
                pass
    else:
        parts.append("（这个视频**没有音轨**，所以没有可转写的说话内容）")

    # ② 画面 → 图片通道
    if not frame_seconds(cfg):
        parts.append("（config.yaml 里 video.frame_seconds=0，**画面没看**）")
    elif image_read.mode_of(cfg) == "off":
        parts.append("（image.mode=off，**画面没看**；要看画面就把 image.mode 改成 ocr/vision）")
    else:
        span = max(1.0, end - start)
        step = max(float(frame_seconds(cfg)), span / max_frames(cfg))
        times = []
        t = start
        while t < end and len(times) < max_frames(cfg):
            times.append(t)
            t += step
        parts.append(f"【画面】（每 {step:.0f} 秒抽 1 张，共 {len(times)} 张）")
        got = 0
        for i, at in enumerate(times):
            jpg = os.path.join(tmp_dir(), f"{os.path.basename(path)[:30]}.{start}.{i}.jpg")
            stamp = _grab_frame(path, jpg, at)
            if stamp is None:
                parts.append(f"  [约 {_hms(at)}] 这一帧没抽出来")
                continue
            got += 1
            r = image_read.handoff(jpg, cfg, collect=on_image)
            if r["kind"] == "text":
                body = r["text"].strip() or "（画面里没认出文字）"
                parts.append(f"  [{_hms(stamp)}] {body}")
            elif r["kind"] == "image":
                if r.get("attached") is False:
                    parts.append(f"  [{_hms(stamp)}] 原图**没能**交给模型看"
                                 f"（超过每轮上限）")
                else:
                    extra = f"（图里的字：{r['text'][:120]}）" if r.get("text") else ""
                    parts.append(f"  [{_hms(stamp)}] 原图已交给模型看{extra}")
            else:
                why = r.get("why") or "未知原因"
                if "没识别到文字" in why:
                    # 纯画面（风景/演示背景）："没文字"是**正常结果**，不是读失败
                    parts.append(f"  [{_hms(stamp)}] 画面里没有文字"
                                 f"（系统 OCR 只认图里的字；要看画面内容就用 image.mode=vision）")
                else:
                    parts.append(f"  [{_hms(stamp)}] 这张画面没读出来：{why}")
        if got == 0:
            parts.append("  ⚠️ **一张画面都没抽出来**（编码不支持 seek 之类）——别当成画面看过了。")

    next_start = None
    if dur is None:
        # 时长读不出：只能说"这段读完了"，**不假装知道后面还有没有**
        parts.append(f"（容器没写时长，所以**没法判断后面还有没有**；这段是从第 {start} 秒起 {limit} 秒）")
    elif end < dur - 0.5:
        next_start = int(end)
        parts.append(f"（这个视频一共 {_hms(dur)}，上面只读到 {_hms(end)}。）")
    return "\n".join(parts), next_start, ""
