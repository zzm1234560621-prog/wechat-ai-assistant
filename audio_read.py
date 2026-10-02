"""把**音频文件**变成文字（语音输入的地基）。

范围和边界（先看清，别顺手扩）：
  * ✅ 只处理**以文件形式**存在的音频（`msg/file/<月>/xxx.m4a` 这类，
    也就是 `file_read.files_roots()` 允许的目录）。这些人发/自己发的音频是明文的。
  * ❌ **微信语音条（local_type=34）不在这里**——真机实测拿不到音频字节
    （82 个 `Rec/` 目录全空、全盘无 `.silk/.amr`），见 `docs/voice-msg-feasibility.md`。
  * ❌ 发语音 / 语音通话：hook 做不到，别在这里假装。

三条硬约束（都是不可谈的，理由写在 `docs/voice-input-spec.md`）：

1. **仍然必须有硬上限，超了如实拒绝**（`audio.max_seconds` 默认 **1800** = 30 分钟 +
   `file.max_bytes`）。⚠️ 2026-10-02 起上限从 120 秒提到 1800：音频是**重活**，现在由
   `read_worker` 在**另一条线程**里读（见 `file_read.is_heavy`），不再占着收消息那条线程；
   而且 `video.max_seconds` 也是 1800 —— 视频里抽出来的音轨正好卡在这个上限内，
   两个数字**必须对齐**（改一个就顺手看另一个）。
   上限存在的理由变成了「别让一次转写白占着后台队列太久 / 云端上传体量」。
2. **绝不在聊天里静默下模型**。模型只由用户显式执行 `python audio_read.py --setup` 下载
   （走 `HF_ENDPOINT=https://hf-mirror.com`——本机 `huggingface.co` 不通）。
   推理时**只认本地模型目录**，结构上就不可能偷偷联网。
3. **默认 local → 音频一个字节都不出本机**；配成 cloud 才上传，**上传必打日志**。

失败一律**如实 + 可照做**：不返回空字符串假装成功，不降级到另一个后端。
"""
import json
import os
import sys
import urllib.error
import urllib.request

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 能转写的扩展名（小写比较）。amr 是 3.9.x 时代微信语音的常见后缀，
# 留着是因为老录音文件可能直接以 .amr 躺在 msg/file 里。
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".amr", ".ogg", ".aac", ".flac", ".wma", ".opus")

DEFAULT_MODEL = "small"
DEFAULT_MAX_SECONDS = 1800
HF_MIRROR = "https://hf-mirror.com"
# 云端默认给硅基流动：本机实测可达（api.openai.com 不通），SenseVoice 中文好又便宜。
DEFAULT_CLOUD_BASE = "https://api.siliconflow.cn/v1"
DEFAULT_CLOUD_MODEL = "FunAudioLLM/SenseVoiceSmall"
_CLOUD_TIMEOUT = 60


def is_audio(path):
    """是不是我们认的音频文件（只看扩展名，内容由解码器判断）。"""
    return str(path or "").lower().endswith(AUDIO_EXT)


def section(cfg):
    sec = (cfg or {}).get("audio")
    return dict(sec) if isinstance(sec, dict) else {}


def backend(cfg):
    """`local`（默认）/ `cloud`。非法值当 local 并告警——**不静默按云端来**。"""
    b = str(section(cfg).get("backend") or "local").strip().lower()
    if b not in ("local", "cloud"):
        print(f"[audio] ⚠️ audio.backend 只认 local / cloud，收到 {b!r}，按 local 处理"
              f"（音频不出本机）", file=sys.stderr, flush=True)
        return "local"
    return b


def model_name(cfg):
    return str(section(cfg).get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def model_dir(cfg):
    """本地模型目录。放在 `data/models/` 下（`data/` 已被 .gitignore 忽略）。"""
    d = section(cfg).get("model_dir")
    if d:
        return os.path.abspath(os.path.expanduser(str(d)))
    return os.path.join(PROJECT_DIR, "data", "models", f"faster-whisper-{model_name(cfg)}")


def max_seconds(cfg):
    try:
        n = int(section(cfg).get("max_seconds") or DEFAULT_MAX_SECONDS)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_SECONDS
    return max(1, min(3600, n))          # 上限 1 小时，拦住乱填


def cloud_cfg(cfg):
    c = section(cfg).get("cloud")
    c = dict(c) if isinstance(c, dict) else {}
    return {
        "base_url": str(c.get("base_url") or DEFAULT_CLOUD_BASE).rstrip("/"),
        "api_key": str(c.get("api_key") or ""),
        "model": str(c.get("model") or DEFAULT_CLOUD_MODEL),
    }


def _has(mod):
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def available(cfg):
    """返回 `(能不能用, 说明)`。说明里**必须**写清缺什么、怎么补。

    说明会原样进工具返回、可能被模型复述给用户，所以是给人看的、要能照做。
    """
    if backend(cfg) == "cloud":
        c = cloud_cfg(cfg)
        if not c["api_key"]:
            return False, ("云端转写还没配 key。让用户去 config.yaml 的 "
                           "audio.cloud.api_key 里填一个（或把 audio.backend 改回 local，"
                           "音频就不用上传了）。")
        return True, f"云端转写：{c['base_url']} 的 {c['model']}（音频会上传出去）"

    if not _has("faster_whisper"):
        return False, ("本地转写要装 faster-whisper（还没装）。让用户执行：\n"
                       "  .venv\\Scripts\\python.exe -m pip install faster-whisper\n"
                       "（装完还要下一次模型，见下一条）")
    if not os.path.isdir(model_dir(cfg)):
        return False, ("本地转写模型还没下载（不联网自动下，得用户显式执行）：\n"
                       f"  .venv\\Scripts\\python.exe audio_read.py --setup\n"
                       f"会下到 {model_dir(cfg)}（走 hf-mirror 镜像）。")
    return True, f"本地转写：faster-whisper {model_name(cfg)}（音频不出本机）"


def _duration(path):
    """音频时长（秒）。拿不到返回 None —— 那时**只按体积卡**，并在报错里说清楚。"""
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(path) as c:
            if c.duration is None:
                return None
            return float(c.duration) / float(av.time_base)
    except Exception:
        return None


def _max_bytes(cfg):
    """字节上限复用 `file.max_bytes`（file_read 是它的 owner）。**0 = 不限。**

    ⚠️ `0` 和「没配」必须分开（真机踩到过）：`file.max_bytes: 0` 是**不限大小**
    （2026-10-02 起的默认值），而老写法 `mb or 30MB` 把 0 当"没配"，
    于是上限算成 0 字节 → **所有音频都被拒**（"超过上限 0.0MB"）。
    这里只判「键在不在」：不在才用默认 30MB；在就原样用（哪怕它是 0）。
    """
    sec = ((cfg or {}).get("file") or {})
    v = sec.get("max_bytes")
    if v is None or v == "":
        try:
            import file_read
            return file_read._cfg(cfg)[0]          # 默认值由 file_read 那边定，不另立一份
        except Exception:
            return 30 * 1024 * 1024
    try:
        return int(v)                              # 0 就是不限
    except (TypeError, ValueError):
        return 30 * 1024 * 1024


def _precheck(path, cfg, max_bytes=None):
    """上限预检。返回 (能不能转, 拒绝原因)。**不静默截断音频。**"""
    if not os.path.isfile(path):
        return False, f"没有这个文件：{path}"
    # 调用方显式给了就用它的（视频切出来的临时 wav 就是这么传的）；没给才读配置。
    cap = int(_max_bytes(cfg) if max_bytes is None else max_bytes)
    size = 0
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"读不了文件大小：{e}"
    if cap and size > cap:                          # cap=0 = 不限，跳过这道闸
        return False, (f"这个音频 {size / 1048576:.1f}MB，超过上限 "
                       f"{cap / 1048576:.1f}MB。让用户调大 config.yaml 的 file.max_bytes，"
                       f"或者截短/压缩后再发。**没有截断音频**。")
    dur = _duration(path)
    if dur is not None:
        lim = max_seconds(cfg)
        if dur > lim:
            return False, (f"这个音频 {dur:.0f} 秒，超过 audio.max_seconds（{lim} 秒）。"
                           f"转写是重活、已经丢给后台 worker（不占轮询线程），但"
                           f"一次转太久会白占着队列；所以要么调大 audio.max_seconds，"
                           f"要么让它分段读。**没有截断音频**。")
    return True, ""


# ---------------- 本地（faster-whisper） ----------------

def initial_prompt(cfg=None):
    """可选的 `audio.initial_prompt`（默认空 = 不传）。

    为什么留这个开关、且**默认关**（2026-10-02 实测，样本是一段 44 秒普通话录音）：
      * 不传：**50.2 秒**，内容更贴原话，但 whisper small **输出繁体**；
      * 传「以下是普通话的句子。」：**87.1 秒**（慢 ~1.7 倍），简体了，
        可同一段话的识别质量**肉眼可见地变差**（出现"历史"这类明显误词）。
    慢一倍换一个简繁差别 —— 所以默认**不**开，谁在意繁体谁自己打开。
    """
    return str(section(cfg or {}).get("initial_prompt") or "").strip()


def _local(path, cfg):
    from faster_whisper import WhisperModel
    mdir = model_dir(cfg)
    # 只认本地目录：**推理期结构上不可能联网**（模型不存在时上面 available() 已经拦了）
    model = WhisperModel(mdir, device="cpu", compute_type="int8")
    prompt = initial_prompt(cfg)
    kw = {"language": "zh", "vad_filter": True}
    if prompt:
        kw["initial_prompt"] = prompt
    segments, info = model.transcribe(path, **kw)
    text = "".join(getattr(s, "text", "") or "" for s in segments).strip()
    if not text:
        return "", (f"转写结果为空（识别到语言={getattr(info, 'language', '?')}、"
                    f"时长={getattr(info, 'duration', 0):.1f} 秒）。"
                    f"可能是没有人声、音量太小或纯音乐。**没有编内容**。")
    return text, ""


# ---------------- 云端（OpenAI 兼容 /audio/transcriptions） ----------------

def _multipart(fields, file_field, filename, data):
    """手工拼 multipart/form-data —— 项目里没有 requests，也不想为一个端点加硬依赖。"""
    boundary = "----wechat-ai-assistant-audio-boundary"
    out = []
    for k, v in fields.items():
        out.append(f"--{boundary}\r\n"
                   f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8"))
    out.append(f"--{boundary}\r\n"
               f'Content-Disposition: form-data; name="{file_field}"; '
               f'filename="{filename}"\r\n'
               f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
    out.append(data)
    out.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _cloud(path, cfg):
    c = cloud_cfg(cfg)
    with open(path, "rb") as fh:
        data = fh.read()
    body, ctype = _multipart({"model": c["model"]}, "file",
                             os.path.basename(path), data)
    req = urllib.request.Request(
        f"{c['base_url']}/audio/transcriptions", data=body, method="POST",
        headers={"Authorization": f"Bearer {c['api_key']}",
                 "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=_CLOUD_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return "", (f"云端转写失败：HTTP {e.code} {detail}。"
                    f"（key 不对 / 模型名不对 / 额度用完都会这样）让用户核对 "
                    f"audio.cloud 那三项。**没有编内容**。")
    except Exception as e:
        return "", (f"云端转写连不上：{e}。让用户检查网络，或把 audio.backend "
                    f"改回 local（音频不出本机）。**没有编内容**。")
    text = str((payload or {}).get("text") or "").strip()
    if not text:
        return "", f"云端返回里没有 text 字段（拿到：{str(payload)[:120]}）。**没有编内容**。"
    return text, ""


def tmp_dir():
    d = os.path.join(PROJECT_DIR, "data", "tmp_audio")
    os.makedirs(d, exist_ok=True)
    return d


def sweep_tmp(max_age=86400.0):
    """清理切音频留下的临时文件（**删了什么要打日志**）。"""
    import time
    d = os.path.join(PROJECT_DIR, "data", "tmp_audio")
    try:
        names = os.listdir(d)
    except OSError:
        return []
    dead, now = [], time.time()
    for n in names:
        p = os.path.join(d, n)
        try:
            if now - os.path.getmtime(p) > max_age:
                os.remove(p)
                dead.append(n)
        except OSError:
            continue
    if dead:
        print(f"⚠️ audio_read: 清理了 {len(dead)} 个音频临时文件（>{max_age/3600:.0f} 小时）。",
              flush=True)
    return dead


def slice_to_wav(src, out, start=0.0, secs=None, rate=16000):
    """把 `[start, start+secs)` 的音频切片写成 16kHz 单声道 WAV。返回写进去的帧数。

    给两处用：**长音频分段**（这里）和**视频抽音轨**（`video_read` 调它）——同一个实现，
    免得两边的重采样参数慢慢跑偏。抽不出来（没装 av / 没有音轨）返回 0。
    """
    try:
        import av
    except ImportError:
        return 0
    end = None if secs is None else start + secs
    with av.open(src) as s:
        if not s.streams.audio:
            return 0
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        n = 0
        with av.open(out, "w", format="wav") as dst:
            st = dst.add_stream("pcm_s16le", rate=rate)
            st.layout = "mono"
            for frame in s.decode(audio=0):
                t = float(frame.pts * frame.time_base) if frame.pts is not None else 0.0
                if t + float(frame.samples) / float(frame.sample_rate) <= start:
                    continue
                if end is not None and t >= end:
                    break
                for rf in resampler.resample(frame):
                    n += 1
                    for pkt in st.encode(rf):
                        dst.mux(pkt)
            for rf in resampler.resample(None):
                for pkt in st.encode(rf):
                    dst.mux(pkt)
            for pkt in st.encode():
                dst.mux(pkt)
        return n


def window(path, cfg=None, start=0, max_bytes=None):
    """读音频的一个窗口（长音频**分段续读**）。返回 `(文本, 下一段起点或 None, 说明)`。

    * 时长在 `audio.max_seconds` 以内（或时长读不出）→ 照老路**直接转写原文件**
      （不重编码、不损质量）；
    * 超了 → 切出 `[start, start+max_seconds)` 这一段再转写，**不是拒绝**；
      还有剩就给 `next_start`，由上层拼成 cursor 让用户说「继续」。
    """
    cfg = cfg or {}
    if not os.path.isfile(path):
        return None, None, "音频不在本机"
    dur = _duration(path)
    lim = max_seconds(cfg)
    start = max(0, int(start))
    if dur is None or dur <= lim:
        # ⚠️ **时长读不出时不分段**，直接转写原文件（这也是 P2 之前的行为）：
        #    切段要靠时长算窗口，硬切只会切出个空/坏文件，然后报一个与事实无关的错。
        #    代价是"读不出时长的超长音频"没法分段（`file.max_bytes` 那道闸还在）。
        text, err = transcribe(path, cfg, max_bytes)
        if err:
            return None, None, err
        return text, None, ""
    if start >= max(0, dur - 0.5):
        return None, None, f"这段音频一共 {dur:.0f} 秒，已经读到末尾了。"

    # 超长：切一段（16k 单声道，1800 秒≈57MB，不会把内存/磁盘撑爆）
    out = os.path.join(tmp_dir(), f"{os.path.basename(path)[:40]}.{start}.wav")
    try:
        n = slice_to_wav(path, out, start=start, secs=lim)
    except Exception as e:
        return None, None, (f"切音频失败：{type(e).__name__}: {str(e)[:120]}"
                            f"（切不了就没法分段读）")
    if n == 0:
        return None, None, ("这段音频切不出内容（可能没有音轨，或片段全是空的）。")
    try:
        # 这个 wav 是**我们刚切出来的、有界的**产物（最多 lim 秒），
        # 所以按它自己的体积放行，不再套用户的 file.max_bytes（那会一动就拒）。
        text, err = transcribe(out, cfg, max_bytes=os.path.getsize(out))
    finally:
        try:
            if os.path.isfile(out):
                os.remove(out)
        except OSError:
            pass
    if err:
        return None, None, err
    next_start = None
    if dur is not None and start + lim < dur - 0.5:
        next_start = start + lim
    return text, next_start, ""


def transcribe(path, cfg=None, max_bytes=None):
    """把音频转成文字。返回 `(text, err)`；`err` 非空就是失败（text 必为 ""）。

    同步执行（调用方决定在哪儿跑）：音频是重活，**`read_file` 那条路会把它丢给
    `read_worker`**，所以它通常跑在 worker 线程上，不占收消息那条线程。
    上限仍然是硬性的（见模块 docstring）。
    """
    ok, why = _precheck(path, cfg or {}, max_bytes)
    if not ok:
        return "", why

    be = backend(cfg or {})
    usable, info = available(cfg or {})
    if not usable:
        return "", info

    if be == "cloud":
        # 上传必须留痕——隐私面上"悄悄传出去"是最不能接受的失败方式
        print(f"[audio] ⚠️ 上传音频到云端转写：{os.path.basename(path)} "
              f"→ {cloud_cfg(cfg or {})['base_url']}", file=sys.stderr, flush=True)
        text, err = _cloud(path, cfg or {})
    else:
        text, err = _local(path, cfg or {})
    if err:
        return "", f"转写失败：{err}"
    return text, ""


# ---------------- 用户显式执行的模型下载 ----------------

def download_model(cfg=None, mirror=None):
    """下载 faster-whisper 模型到 `model_dir(cfg)`。

    **只由用户显式执行**（`--setup`）。绝不从聊天路径调用——
    在轮询线程里下 150~500MB 会把消息处理停掉好几分钟。
    """
    if not _has("huggingface_hub"):
        return False, ("要下模型得先装 huggingface_hub：\n"
                       "  .venv\\Scripts\\python.exe -m pip install faster-whisper")
    os.environ.setdefault("HF_ENDPOINT", mirror or HF_MIRROR)
    from huggingface_hub import snapshot_download
    repo = f"Systran/faster-whisper-{model_name(cfg or {})}"
    dest = model_dir(cfg or {})
    os.makedirs(dest, exist_ok=True)
    print(f"[audio] 从 {os.environ['HF_ENDPOINT']} 下载 {repo} → {dest}")
    snapshot_download(repo_id=repo, local_dir=dest)
    return True, f"模型已就位：{dest}"


if __name__ == "__main__":
    import yaml
    try:
        _cfg = yaml.safe_load(open(os.path.join(PROJECT_DIR, "config.yaml"),
                                   encoding="utf-8")) or {}
    except Exception:
        _cfg = {}

    if "--setup" in sys.argv:
        print("先装依赖（不随主程序安装）：")
        print("  .venv\\Scripts\\python.exe -m pip install faster-whisper")
        ok, msg = download_model(_cfg)
        print(("✅ " if ok else "❌ ") + msg)
        sys.exit(0 if ok else 1)

    if "--transcribe" in sys.argv:
        i = sys.argv.index("--transcribe")
        target = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        if not target:
            print("用法：python audio_read.py --transcribe <音频路径>")
            sys.exit(2)
        usable, info = available(_cfg)
        print("可用性：", usable, info)
        text, err = transcribe(target, _cfg)
        print("错误：" + err if err else "转写：\n" + text)
        sys.exit(1 if err else 0)

    print("用法：")
    print("  python audio_read.py --setup                 # 下本地模型（显式、不会被聊天触发）")
    print("  python audio_read.py --transcribe <音频路径>  # 手工转一条试试")
    print(f"\n当前：backend={backend(_cfg)} model={model_name(_cfg)} "
          f"max_seconds={max_seconds(_cfg)}")
    print("可用性：", available(_cfg))
