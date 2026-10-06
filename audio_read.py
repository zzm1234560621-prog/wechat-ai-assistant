"""把**音频文件**变成文字（语音输入的地基）。

范围和边界（先看清，别顺手扩）：
  * ✅ 只处理**以文件形式**存在的音频（`msg/file/<月>/xxx.m4a` 这类，
    也就是 `file_read.files_roots()` 允许的目录）。这些人发/自己发的音频是明文的。
  * ⚠️ **微信语音条（local_type=34）不从这个入口进来，但已经是能读的**：
    由 `bot.read_voice_message()` → `voice_mem.py` 从**微信进程内存**里拿明文 SILK
    → 解码后调本模块转写（见 `docs/voice-msg-feasibility.md` 的 2026-10-03 结论）。
    本模块只负责"音频字节已经在手上"之后的转写，不负责把语音条捞出来。
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
import re
import sys
import urllib.error
import urllib.request

import tempdir

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 能转写的扩展名（小写比较）。amr 是 3.9.x 时代微信语音的常见后缀，
# 留着是因为老录音文件可能直接以 .amr 躺在 msg/file 里。
AUDIO_EXT = (".m4a", ".mp3", ".wav", ".amr", ".ogg", ".aac", ".flac", ".wma", ".opus")

DEFAULT_MODEL = "small"
DEFAULT_MAX_SECONDS = 1800
HF_MIRROR = "https://hf-mirror.com"
# 转写语言：**默认 auto（自己判）**。2026-10-03 真机踩到：以前两处 kwargs 写死
# `language="zh"`，用户说英文 `superboynick`，whisper 被迫用中文词汇表硬凑，转出
# 「你好,你好,我跟俗文貴你最近聊了什麼…」—— 一段**通顺但完全捏造**的中文，还被当成
# 用户的原话送进 agent 去执行。这类「静默给错内容」比读不出来严重得多。
# 留 `zh` 是给「只说自己母语」的人省掉语言探测的：设了就按它走。
# ⚠️ 白名单校验而非直接下传：whisper 对非法 language 会**抛异常**，把「一个拼错的
# 配置值」变成「每次转写都失败」；未知值退回 auto 并告警（不静默）。
KNOWN_LANGS = (
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl",
    "ca", "nl", "ar", "sv", "it", "id", "hi", "fi", "vi", "he", "uk",
    "el", "ms", "cs", "ro", "da", "hu", "ta", "no", "th", "ur", "hr",
    "bg", "lt", "la", "mi", "ml", "cy", "sk", "te", "fa", "lv", "bn",
    "sr", "az", "sl", "kn", "et", "mk", "br", "eu", "is", "hy", "ne",
    "mn", "bs", "kk", "sq", "sw", "gl", "mr", "pa", "si", "km", "sn",
    "yo", "so", "af", "oc", "ka", "be", "tg", "sd", "gu", "am", "yi",
    "lo", "uz", "fo", "ht", "ps", "tk", "nn", "mt", "sa", "lb", "my",
    "bo", "tl", "mg", "as", "tt", "haw", "ln", "ha", "ba", "jw", "su",
    "yue",
)
DEFAULT_LANGUAGE = "auto"
# 允许转写哪些语言（`audio.languages`）。默认中英文：探测出别的语言就**如实拒绝**，
# 不用允许的语言去"凑"——凑出来的正是「通顺但捏造」那种假内容（见 allowed_languages）。
DEFAULT_LANGUAGES = ["zh", "en"]
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
    """本地模型目录。放在 `data/models/` 下（`data/` 已被 .gitignore 忽略）。

    ⚠️ 配置里的相对路径**按项目目录**解析，不用 `os.path.abspath`（那是按进程 CWD）
    —— 2026-10-05 真机：bot 被计划任务/提权方式起时 CWD = C:\\WINDOWS\\System32，
    于是 `./data/...` 这类值会落到系统目录去（建目录被拒 / 找不到模型），
    而且**不报错**。同口径的还有 `semantic._abs`、`image_read.cache_path`。
    """
    d = section(cfg).get("model_dir")
    if d:
        d = os.path.expanduser(str(d))
        return d if os.path.isabs(d) else os.path.join(PROJECT_DIR, d)
    return os.path.join(PROJECT_DIR, "data", "models", f"faster-whisper-{model_name(cfg)}")


def max_seconds(cfg):
    try:
        n = int(section(cfg).get("max_seconds") or DEFAULT_MAX_SECONDS)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_SECONDS
    return max(1, min(3600, n))          # 上限 1 小时，拦住乱填


def language(cfg):
    """转写语言：`auto`（默认，自己判）/ 具体两字母代码（`zh`、`en`…）。

    返回 **faster-whisper 该收到的值**：`auto` → `None`（不传 = 让它探测），
    否则返回归一化后的代码。

    为什么必须有这个（2026-10-03 真机）：以前两处 kwargs 写死 `language="zh"`，
    英文 `superboynick` 被中文强行音译成「俗文貴」，还顺带编了一整句
    「你好,你好,我跟…聊了什麼」——**通顺、但完全是捏造的**，然后被当作
    用户的原话进 `run_agent` 去执行。用户只会说中文时它是"能用"的，
    一旦说英文/中英混说，它就**静默地给错误内容**。

    非法值**退回 auto 并告警**，不静默下传：whisper 遇到不认识的 language 会抛，
    那等于把「配置里一个手滑的拼写」变成「每次转写都失败」。
    """
    v = str(section(cfg or {}).get("language") or DEFAULT_LANGUAGE).strip().lower()
    if v in ("", "auto", "none", "detect"):
        return None
    if v in KNOWN_LANGS:
        return v
    print(f"[audio] ⚠️ audio.language 不认识 {v!r}（要 auto 或两字母代码，如 zh/en），"
          f"这次按 auto 处理（让它自己探测）", file=sys.stderr, flush=True)
    return None


def allowed_languages(cfg):
    """`audio.languages`：**允许转写哪些语言**。返回归一化后的 list；空 = 不限制。

    为什么需要（2026-10-03 真机第二次）：`audio.language: auto` 之后，用户说的一句
    短外语被 whisper 判成了**法语**，转出 `Super poignée comme elle a l'air d'un
    chemin.` —— **语法通顺、语义不通**。也就是说「不写死语言」只解决了"被迫说中文"，
    没解决"**选错语言照样捏造**"。而捏造的内容会当作原话进 `run_agent`。

    所以这里的语义是**限制**：探测出的语言不在集合内就**如实拒绝**，
    绝不用集合内的语言去"凑"那段音频（凑出来的正是通顺的假话）。
    默认 `["zh", "en"]`（用户要的中英文）；配 `[]` / `auto` 可关掉限制。

    非法项**逐个告警并丢掉**；全非法则退回默认 —— 不静默接受看不懂的配置。
    """
    raw = section(cfg or {}).get("languages")
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in ("", "auto", "all")):
        return list(DEFAULT_LANGUAGES)
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[,\s]+", raw) if x]
    if not isinstance(raw, (list, tuple)):
        print(f"[audio] ⚠️ audio.languages 要是列表（如 [zh, en]），收到 {type(raw).__name__}，"
              f"退回默认 {DEFAULT_LANGUAGES}", file=sys.stderr, flush=True)
        return list(DEFAULT_LANGUAGES)
    if len(raw) == 0:                     # 显式空列表 = 关掉限制
        return []
    out, bad = [], []
    for x in raw:
        s = str(x or "").strip().lower()
        if not s or s in ("auto", "all"):
            continue
        if s in KNOWN_LANGS:
            if s not in out:
                out.append(s)
        else:
            bad.append(str(x))
    if bad:
        print(f"[audio] ⚠️ audio.languages 里这些不认识、已忽略：{bad}", file=sys.stderr, flush=True)
    return out or list(DEFAULT_LANGUAGES)


def detect_language(path, cfg):
    """探测音频语言。返回 `(代码, 错误)`；探测不了返回 `(None, 原因)`。

    为什么用 `detect_language` 而不是 `transcribe(language=None)`：后者会直接
    **用探测到的语言把整段转写完**，等我们看清它猜成法语时，捏造的中文/法文已经成型了。
    先探、再决定"要不要转"，才能做到「不在允许集合里就**一个字都不给**」。

    任何失败（老版本没这个接口 / 模型报错）都退回 `None`，由调用方按"探测不了"处理
    ——**绝不因此拒绝**，那会把一个可选功能变成硬故障。
    """
    mdir = model_dir(cfg)
    model = _get_model(mdir)
    det = getattr(model, "detect_language", None)
    if det is None:
        return None, "这个 faster-whisper 版本没有语言探测接口，跳过语言限制"
    try:
        lang, prob, all_probs = det(path)
    except Exception as e:
        return None, f"语言探测失败：{type(e).__name__}: {str(e)[:80]}"
    code = str(lang or "").strip().lower()
    if not code:
        return None, "语言探测没给出结果"
    try:
        p = float(prob)
    except (TypeError, ValueError):
        p = 0.0
    return code, f"探测={code}（把握 {p:.2f}）"


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


def av_conflict():
    """PyAV 与 faster-whisper 不兼容时，回一句**能照做**的话；否则 `None`。

    为什么要有它（2026-10-06 另一台电脑真机）：
    `pip install faster-whisper` 会顺手装上**最新的 PyAV**，而 faster-whisper 内部是
    `av.open(input_file, mode="r", metadata_errors="ignore")` —— **PyAV 19 把这个参数删了**
    （18.1.0 还接受）。于是**每一条转写都抛 `TypeError`**：语音条和音频文件**一起**读不出来，
    用户看到的只有「解析失败 / 没读出来」，没人知道该做什么。
    真机对照：那台 `av 19.0.1` → 全读不出来；本机 `av 18.1.0` → 同一条语音转出「你好 你好」。

    **判据是"问函数本身"，不是猜版本号**：拿一个空流去调
    `av.open(..., metadata_errors="ignore")` ——
      * 抛 `TypeError` 且提到这个参数名 ⇒ 不支持；
      * 抛别的异常（空数据不是合法容器）⇒ 参数被接受了 ⇒ 支持。
    （本机 av 18 实测：文档里有 `metadata_errors`，空流抛的是 InvalidDataError。）
    """
    try:
        import av
    except ImportError:
        return None            # 没装 av：那是依赖清单的事，别在这儿冒充
    try:
        import io as _io
        av.open(_io.BytesIO(b""), metadata_errors="ignore")
    except TypeError as e:
        if "metadata_errors" in str(e):
            return ("PyAV（`av`）版本太新，和 faster-whisper 不兼容：faster-whisper 内部调 "
                    "`av.open(..., metadata_errors=…)`，而这个参数在 **PyAV 19** 里被删掉了 "
                    "⇒ **每一条转写都会失败**（语音条和音频文件都读不出来）。\n"
                    "修法就一条命令：\n"
                    "  .venv\\Scripts\\python.exe -m pip install \"av<19\"\n"
                    "（可选组件那条安装线已经改成直接装 `av<19`；把语音组件重装一遍也一样。）")
        return None
    except Exception:
        return None            # 空流不合法之类的错 = 参数被接受了
    return None


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
    conflict = av_conflict()
    if conflict:
        return False, conflict
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


_MODEL_CACHE = {}
_MODEL_LOCK = None


def _get_model(mdir):
    """进程内复用 `WhisperModel`。**为什么要缓存**：`WhisperModel(...)` 每次构造都要
    读模型文件、建实例 —— 实测 1.9 秒（冷的时候飘到 14 秒），而一段 1.2 秒音频的
    转写本身只要 ~5 秒。一条语音重载一次模型等于白花 1/4 的时间；语音条那条路
    （`voice_mem`）本来就更贵，不能再把这个成本乘上去。

    加锁是因为 worker 线程和主线程都可能调它（一条语音＝一次调用）。

    ⚠️ 缓存里连**类对象**一起存：`WhisperModel` 换了（自测会把
    `faster_whisper` 整个换成假模块）就必须重建，否则会拿上一次的假模型继续用
    —— 自测真抓到过（「转出空文本算失败」那条用例拿到的是**上一个**假模型，
    于是断言全错）。判据用 `is`：实现类变了，缓存就是陈的。
    """
    global _MODEL_LOCK
    import threading
    if _MODEL_LOCK is None:
        _MODEL_LOCK = threading.Lock()
    with _MODEL_LOCK:
        from faster_whisper import WhisperModel
        cached = _MODEL_CACHE.get(mdir)
        if cached is not None and cached[0] is WhisperModel:
            return cached[1]
        m = WhisperModel(mdir, device="cpu", compute_type="int8")
        _MODEL_CACHE[mdir] = (WhisperModel, m)
        return m


def _local(path, cfg):
    mdir = model_dir(cfg)
    # 只认本地目录：**推理期结构上不可能联网**（模型不存在时上面 available() 已经拦了）
    model = _get_model(mdir)
    prompt = initial_prompt(cfg)
    kw = {"vad_filter": True}
    # ⚠️ 不传 language = 让 whisper 自己探测（`auto`）。**别再写死 "zh"**：
    #    那会让英文语音被中文词汇表硬凑成一段捏造的中文（见 language() 的注释）。
    lang = language(cfg)
    allow = allowed_languages(cfg)
    if lang:
        kw["language"] = lang
    elif allow:
        # 没写死语言时**先探一次再决定转不转**：探测出的语言不在允许集合里就如实拒绝。
        # 为什么不能「探到什么就用什么转」：那正是 `Super poignée comme elle a l'air
        # d'un chemin.` 那次 —— 猜成法语之后照样捏一句通顺的假话，还进了 `run_agent`。
        det, why = detect_language(path, cfg)
        if det and det not in allow:
            return "", (f"这段音频听着像 **{det}**，而 `audio.languages` 只允许 "
                        f"{'/'.join(allow)}，所以**没有转写**。（{why}）**没有编内容**。"
                        f"想把 {det} 也放开，就把它加进 `audio.languages`；"
                        f"确认只说自己要的那两种语言就保持现状。")
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
    fields = {"model": c["model"]}
    # 云端同样按配置传语言：`auto` 就**不传**（OpenAI 兼容端点是"不传就自己探测"）
    lang = language(cfg)
    if lang:
        fields["language"] = lang
    body, ctype = _multipart(fields, "file",
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
    """切音频的临时目录。**别在这里再硬编码路径**：统一走 `tempdir.get()`，
    这样受限环境/别的部署形态可以用 `PROJ_TMP` 环境变量改道（见 `tempdir.py`）。"""
    return tempdir.get("tmp_audio")


def sweep_tmp(max_age=86400.0):
    """清理切音频留下的临时文件（**删了什么要打日志**，由 `tempdir.sweep` 统一实现）。"""
    return tempdir.sweep("tmp_audio", max_age, "audio_read")


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


def transcribe_scored(path, cfg=None):
    """像 `transcribe()`，但**多回一个置信度分数**（本地才有；云端返回 None）。

    为什么要它（2026-10-03 真机取证）：语音条那条路要在一堆「时长一模一样」的
    候选里挑一条，而**唯一能把对的挑出来的信号就是置信度** ——

        local_id=615（用户说「我最近聊了什么」，1600 毫秒）两条候选：
          1400ms SILK 2730 字节（**更接近**加密字节数 2804）  no_speech 0.17  logprob -0.97  -> 「要饿了吗呢」✗
          1400ms SILK 2547 字节                              no_speech 0.06  logprob -0.76  -> 「我最近得好了什么」✓

    时长分不出来、SILK 字节数**还会把错的排前面**；没有分数就只能瞎挑 ——
    而"瞎挑"在这里等于**拿别人的话去执行**，比读不出来严重得多。

    分数 = 平均 `avg_logprob` − 最大 `no_speech_prob`，**越高越好**。
    这是**经验值**，不是校准过的概率；所以调用方必须允许"分不出来就拒绝"。

    返回 `(文本, 分数, 错误)`；失败时文本 ""、分数 None、错误是人话。
    """
    cfg = cfg or {}
    if backend(cfg) == "cloud":
        text, err = transcribe(path, cfg)
        return (text, None, "") if not err else ("", None, err)
    ok, why = _precheck(path, cfg, None)
    if not ok:
        return "", None, why
    usable, info = available(cfg)
    if not usable:
        return "", None, info
    try:
        model = _get_model(model_dir(cfg))
        kw = {"vad_filter": True}
        # 同 `_local`：语言不写死，默认让 whisper 自己探测（见 language() 注释）；
        # 没写死时再按 `audio.languages` **限制**——探测出别的语言就如实拒绝（不给文本）。
        lang = language(cfg)
        allow = allowed_languages(cfg)
        if lang:
            kw["language"] = lang
        elif allow:
            det, why = detect_language(path, cfg)
            if det and det not in allow:
                return "", None, (f"这段语音听着像 **{det}**，而 `audio.languages` 只允许 "
                                  f"{'/'.join(allow)}，所以**没有转写**。（{why}）"
                                  f"**没有编内容**。")
        prompt = initial_prompt(cfg)
        if prompt:
            kw["initial_prompt"] = prompt
        segments, meta = model.transcribe(path, **kw)
        segs = list(segments)
    except Exception as e:
        return "", None, f"{type(e).__name__}: {str(e)[:150]}"
    text = "".join(getattr(s, "text", "") or "" for s in segs).strip()
    if not text:
        return "", None, (f"转写结果为空（识别到语言={getattr(meta, 'language', '?')}、"
                          f"时长={getattr(meta, 'duration', 0):.1f} 秒）。"
                          f"可能是没有人声、音量太小或纯音乐。**没有编内容**。")
    lps = [getattr(s, "avg_logprob", None) for s in segs]
    lps = [x for x in lps if isinstance(x, (int, float))]
    nsps = [getattr(s, "no_speech_prob", None) for s in segs]
    nsps = [x for x in nsps if isinstance(x, (int, float))]
    score = None
    if lps:
        score = (sum(lps) / len(lps)) - (max(nsps) if nsps else 0.0)
    return text, score, ""


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

    if "--status" in sys.argv:
        # 给控制台/一键部署用的一行探针：**可用性只有 available() 这一份判据**
        # （依赖在不在 + 模型下没下），别在菜单里另抄一套。
        _ok, _why = available(_cfg)
        print(("✅ " if _ok else "❌ ") + _why)
        sys.exit(0 if _ok else 1)

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
    print("  python audio_read.py --status                # 一行：现在能不能转写（控制台在调）")
    print("  python audio_read.py --setup                 # 下本地模型（显式、不会被聊天触发）")
    print("  python audio_read.py --transcribe <音频路径>  # 手工转一条试试")
    print(f"\n当前：backend={backend(_cfg)} model={model_name(_cfg)} "
          f"max_seconds={max_seconds(_cfg)}")
    print("可用性：", available(_cfg))
