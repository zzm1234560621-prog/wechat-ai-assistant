"""语音输入（audio_read）的回归自测。

**不联网、不下模型、不碰微信、不需要真音频**：假 `faster_whisper` / 假 `av` +
临时目录 + 一个只绑回环的假 HTTP 服务（照 selftest_aixed 的路子）。

要盯住的是这几条（规格见 docs/voice-input-spec.md）：
  * 分派：音频走转写、**文档老行为一个字不变**；
  * 硬上限：字节 / 秒数超了**如实拒绝**，绝不静默截断音频；
  * 缺依赖 / 缺模型 / 缺 key → **照做指引**，不返回空文本假装成功；
  * `backend: local` 时**零网络调用**（这一条是隐私底线）；
  * `backend: cloud` 时上传要打日志、multipart 编码要对。

用法：`.venv/Scripts/python.exe selftest_audio.py`
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import audio_read      # noqa: E402
import file_read       # noqa: E402

_PASS = 0
_OK = True


def check(label, cond, extra=""):
    """记一笔并**汇总**。别只看 print——失败必须让退出码非 0（假绿是这个项目最恨的）。"""
    global _PASS, _OK
    cond = bool(cond)
    _PASS += 1 if cond else 0
    _OK = _OK and cond
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {extra}" if extra and not cond else ""))
    return cond


def sec(t):
    print(f"\n── {t} ──")


def _fake_module(name):
    m = types.ModuleType(name)
    m.__spec__ = __import__("importlib.machinery", fromlist=["x"]).ModuleSpec(name, None)
    return m


class _FakeWhisper:
    """假的 faster_whisper：只回答「转出了什么」和 info。"""

    def __init__(self, segments, language="zh", duration=3.0, prob=0.9, detect=True):
        self.segments = segments
        self.language = language        # 既当 info.language，也当 detect_language 的结果
        self.duration = duration
        self.prob = prob
        self.detect = detect            # False = 模拟"老版本没有探测接口"
        self.calls = []
        self.detect_calls = 0

    def install(self, module_name="faster_whisper"):
        outer = self

        class _Model:
            def __init__(self, path, device=None, compute_type=None):
                outer.calls.append(("model", path, device, compute_type))

            def transcribe(self, path, language=None, vad_filter=None,
                           initial_prompt=None):
                outer.calls.append(("transcribe", path, language, vad_filter))
                segs = [types.SimpleNamespace(text=t) for t in outer.segments]
                info = types.SimpleNamespace(language=outer.language,
                                             duration=outer.duration)
                return segs, info

        if self.detect:
            # `audio.languages` 那条限制用的是 detect_language（先探再决定转不转），
            # 所以桩必须有它；否则限制被当成"探测不了"而静默放行（用例会假绿）。
            def _detect(audio=None, **kw):
                outer.detect_calls += 1
                return outer.language, outer.prob, [(outer.language, outer.prob)]

            _Model.detect_language = staticmethod(_detect)

        mod = _fake_module(module_name)
        mod.WhisperModel = _Model
        sys.modules[module_name] = mod
        return self          # 返回**假对象**本身（调用方能查 .calls）


class _Env:
    """临时把模块/依赖状态换掉，退出时还原（免得污染别的用例）。

    ⚠️ **只动 `sys.modules` 是不够的**（2026-10-02 真踩到）：`audio_read._has()` 用的是
    `importlib.util.find_spec()`，它看**磁盘上装没装**，不看 `sys.modules`。
    以前本机真没装 faster-whisper，"从 sys.modules 里删掉"就等于"没装"；
    用户让下模型、我们真装上之后，这个模拟就失效了——用例会走到"模型没下"那条分支，
    然后抱怨提示里没有 pip install。现在**连带把 `find_spec` 也钉住**，模拟才成立。
    """

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}
        self._real_find_spec = None

    def __enter__(self):
        import importlib.util
        missing = {k for k, v in self.kw.items() if v is None}
        if missing:
            self._real_find_spec = importlib.util.find_spec
            real = self._real_find_spec

            def fake_find_spec(name, *a, **kw):
                return None if name in missing else real(name, *a, **kw)

            importlib.util.find_spec = fake_find_spec
        for k, v in self.kw.items():
            self.saved[k] = sys.modules.get(k)
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        return self

    def __exit__(self, *a):
        if self._real_find_spec is not None:
            import importlib.util
            importlib.util.find_spec = self._real_find_spec
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        return False


def _cfg(tmp, **audio):
    sec_ = {"backend": "local", "model": "small", "max_seconds": 120,
            "model_dir": os.path.join(tmp, "model")}
    sec_.update(audio)
    return {"audio": sec_, "file": {"max_bytes": 31457280}}


def _make_dir(p):
    os.makedirs(p, exist_ok=True)
    return p


def t1_availability(tmp):
    sec("缺依赖 / 缺模型 / 缺 key：都要给能照做的指引，不许静默当成功")
    cfg = _cfg(tmp)

    # ⚠️ 钉住一个真踩过的坑（2026-10-01）：`faster-whisper` **绝不能**写成
    # requirements.txt 的正式需求行。`envsetup.requirements_specs()` 读所有非注释行，
    # 「可选段」只是文件里的约定；写成正式行它就会进 `required_import_names()`
    # → 启动助手.bat 的自检清单要求它 → 没装的人「装完还是起不来」死循环（H1 那类）。
    try:
        import envsetup
        names = envsetup.required_import_names()
        check("可选依赖没混进「必需」清单（否则启动自检会永远失败）",
              "faster_whisper" not in names and "faster-whisper" not in names, names)
    except ImportError:
        check("envsetup 可导入（用来核对依赖清单）", False)

    with _Env(faster_whisper=None):
        ok, why = audio_read.available(cfg)
        check("没装 faster-whisper → 不可用", ok is False, why)
        check("……而且文案里有 pip install 这条命令", "pip install faster-whisper" in why, why)

    _FakeWhisper(["你好"]).install()
    ok, why = audio_read.available(cfg)          # 模型目录还不存在
    check("装了依赖但没下模型 → 不可用", ok is False, why)
    check("……而且文案里有 --setup 这条命令", "--setup" in why, why)
    check("……并说明「不联网自动下」", "不联网自动下" in why, why)

    _make_dir(audio_read.model_dir(cfg))
    ok, why = audio_read.available(cfg)
    check("模型就位 → 可用", ok is True, why)
    check("……并说明音频不出本机", "不出本机" in why, why)

    # ★ PyAV 19 与 faster-whisper 1.2.1 不兼容（2026-10-06 另一台电脑真机，全靠这条抓出来）：
    # 那台 `av 19.0.1` 上**每一条转写都抛** `TypeError: open() got an unexpected keyword
    # argument 'metadata_errors'`（faster-whisper 内部在传它，而 PyAV 19 删了这个参数）
    # ⇒ 语音条和音频文件**一起**读不出来，用户只看到「解析失败」。
    # 这里把 `av.open` 换成一个"像 PyAV 19 那样拒收这个参数"的桩，钉两件事：
    #   ① 判据本身能认出来（`av_conflict()` 不是猜版本号，是拿空流问函数）；
    #   ② 文案里必须带**能照做**的那条命令（否则用户和我们都只能看到 TypeError）。
    import av as _av
    _real_av_open = _av.open
    try:
        def _av19_open(*a, **kw):
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
        _av.open = _av19_open
        msg = audio_read.av_conflict() or ""
        check("★ 像 PyAV 19 那样拒收 metadata_errors → 判成冲突（不是静默 N/A）",
              bool(msg), msg[:80])
        check("★ 冲突文案里有能照做的命令", 'av<19' in msg, msg[:80])
        ok19, why19 = audio_read.available(cfg)
        check("★ available() 也报这条（--status / 聊天里都看得见）",
              ok19 is False and "av<19" in why19, why19[:80])
    finally:
        _av.open = _real_av_open
    check("本机（av 18，参数还在）→ 没有这条冲突",
          audio_read.av_conflict() is None, audio_read.av_conflict())

    cloud = _cfg(tmp, backend="cloud", cloud={"api_key": ""})
    ok, why = audio_read.available(cloud)
    check("cloud 没 key → 不可用", ok is False, why)
    check("……并给出「改回 local 就不用上传」的选择", "改回 local" in why, why)

    cloud2 = _cfg(tmp, backend="cloud", cloud={"api_key": "k", "base_url": "http://x/v1",
                                               "model": "m"})
    ok, why = audio_read.available(cloud2)
    check("cloud 配好了 → 可用，且明说会上传", ok is True and "上传" in why, why)


def t2_caps(tmp):
    sec("硬上限：超字节 / 超秒数 → 如实拒绝，绝不静默截断")
    _FakeWhisper(["早"]).install()
    cfg = _cfg(tmp)
    _make_dir(audio_read.model_dir(cfg))

    big = os.path.join(tmp, "big.m4a")
    with open(big, "wb") as f:
        f.write(b"\0" * 2048)
    text, err = audio_read.transcribe(big, dict(cfg, file={"max_bytes": 1024}))
    check("超 file.max_bytes → 拒绝", text == "" and "超过上限" in err, err)
    check("……说清了没有截断音频", "没有截断音频" in err, err)
    check("……并给出怎么调（file.max_bytes）", "file.max_bytes" in err, err)

    # 时长上限：假 av 报 200 秒，而 max_seconds=120
    long_ = os.path.join(tmp, "long.m4a")
    with open(long_, "wb") as f:
        f.write(b"\0" * 64)
    fake_av = _fake_module("av")
    fake_av.time_base = 1000000

    class _C:
        duration = 200 * 1000000

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_av.open = lambda p: _C()
    with _Env(av=fake_av):
        text, err = audio_read.transcribe(long_, cfg)
    check("超 audio.max_seconds → 拒绝", text == "" and "超过 audio.max_seconds" in err, err)
    check("……说清了不转的原因（占着轮询线程）", "轮询线程" in err, err)
    check("……并给出怎么调（audio.max_seconds）", "audio.max_seconds" in err, err)

    # 时长取不到时**只按体积卡**，不能因此拒绝也不能放行超大文件
    fake_av.open = lambda p: (_ for _ in ()).throw(RuntimeError("no duration"))
    with _Env(av=fake_av):
        ok, why = audio_read._precheck(long_, cfg, max_bytes=10 ** 9)
    check("拿不到时长时只按体积卡（不因读不到时长就拒绝）", ok is True, why)


def t3_local_and_privacy(tmp):
    sec("local 转写：出文本，且**零网络调用**（隐私底线）")
    fw = _FakeWhisper(["大家好", "，我是测试。"]).install()
    cfg = _cfg(tmp)
    _make_dir(audio_read.model_dir(cfg))
    p = os.path.join(tmp, "a.m4a")
    with open(p, "wb") as f:
        f.write(b"\0" * 64)

    import urllib.request
    real_urlopen = urllib.request.urlopen

    def _boom(*a, **k):
        raise AssertionError("local 后端不许发网络请求！")

    urllib.request.urlopen = _boom
    try:
        text, err = audio_read.transcribe(p, cfg)
    finally:
        urllib.request.urlopen = real_urlopen
    check("local 转写出文本", err == "" and text == "大家好，我是测试。", (text, err))
    check("模型是按本地目录构造的（不可能联网拉模型）",
          any(c[0] == "model" and c[1] == audio_read.model_dir(cfg) for c in fw.calls),
          fw.calls)
    check("传了 device=cpu / compute_type=int8",
          any(c[0] == "model" and c[2] == "cpu" and c[3] == "int8" for c in fw.calls),
          fw.calls)
    # ⚠️ 2026-10-03 之前这里断言的是 `language == "zh"`（代码写死中文）——
    #    正是那条写死让英文语音被硬凑成捏造的中文。现在默认 auto = **不传 language**。
    check("默认 auto：**不写死语言**（不传 language，让 whisper 自己探测）",
          any(c[0] == "transcribe" and c[2] is None for c in fw.calls), fw.calls)

    # 静音/没识别出内容：必须当失败，不许当成功返回空串
    fw2 = _FakeWhisper([]).install()
    text, err = audio_read.transcribe(p, cfg)
    check("转出空文本 → **算失败**，不返回空串假装成功", text == "" and err != "", (text, err))
    check("……并说明没编内容", "没有编内容" in err, err)
    check("……并带上了识别到的语言/时长（便于排查）",
          "zh" in err and "3.0" in err, err)
    _ = fw2


def t4_dispatch(tmp):
    sec("分派：音频走转写，文档/不支持的后缀各自照旧")
    check("识别常见音频后缀",
          all(audio_read.is_audio("x" + e) for e in
              (".m4a", ".mp3", ".wav", ".amr", ".ogg", ".aac", ".flac")),
          audio_read.AUDIO_EXT)
    check("不把文档当音频", not any(audio_read.is_audio("x" + e) for e in
                                    (".pdf", ".docx", ".txt", ".mp4", ".jpg")))

    fw = _FakeWhisper(["这是一段录音"]).install()
    cfg = _cfg(tmp)
    _make_dir(audio_read.model_dir(cfg))

    a = os.path.join(tmp, "voice.m4a")
    with open(a, "wb") as f:
        f.write(b"\0" * 64)
    text, note = file_read.extract(a, cfg)
    check("read_file 收到 .m4a → 走转写并返回文本",
          text == "这是一段录音" and note is None, (text, note))
    check("……确实调了转写（不是当成乱码文本读）",
          any(c[0] == "transcribe" for c in fw.calls), fw.calls)

    # ⚠️ 短转写**不能**被 `_looks_garbled`（「短于 20 字当可疑」）拒掉：
    # 一句 3 秒的「好的」只有两个字，按那条判据会被当成乱码——语音这条路上是错的。
    fw_short = _FakeWhisper(["好的"]).install()
    text, note = file_read.extract(a, cfg)
    check("短转写（「好的」2 个字）**照样能读**，不被乱码判据误伤",
          text == "好的" and note is None, (text, note))
    _ = fw_short
    _FakeWhisper(["这是一段录音"]).install()

    # 纯文本仍走老路（用够长的文本：`_looks_garbled` 对 <20 字一律当可疑，见上）
    t = os.path.join(tmp, "note.txt")
    with open(t, "w", encoding="utf-8") as f:
        f.write("这是一份普通文本文档，用来验证老的读取路径没有被改动。")
    text, note = file_read.extract(t, cfg)
    check("回归：.txt 仍按纯文本读", text and text.startswith("这是一份普通文本") and note is None,
          (text, note))

    # 不支持的类型：文案里要提到音频也能读（否则用户不知道有这功能）
    # 用「未知后缀 + 二进制内容」这条路（2026-10-02 起压缩包已被 T7 接掉，
    # 再拿 PK 头当"不支持"的例子就过时了）；二进制内容会被嗅探判成 binary。
    z = os.path.join(tmp, "x.unknownext")
    with open(z, "wb") as f:
        f.write(b"\x00\x01\x02\x03" * 12)
    text, note = file_read.extract(z, cfg)
    check("回归：不支持的类型仍如实拒绝", text is None and note, (text, note))
    check("……且文案里提到了音频（新能力可被发现）", ".m4a" in (note or ""), note)

    # 压缩包是**支持**的（T7）：拿一个坏 zip 验"报的是它自己的问题"，不是"不支持"
    bad = os.path.join(tmp, "坏包.zip")
    with open(bad, "wb") as f:
        f.write(b"PK\x03\x04")
    text, note = file_read.extract(bad, cfg)
    check("坏压缩包：如实说打不开（而不是含糊的「不支持」）",
          text is None and note and "压缩包" in note, (text, note))

    # 音频上限：file_read 自己那道体积闸先生效（证明这条路上 file.max_bytes 管着）
    text, note = file_read.extract(a, dict(cfg, file={"max_bytes": 8}))
    check("read_file 这条路上 file.max_bytes 仍然生效",
          text is None and "文件太大" in (note or ""), (text, note))


class _Handler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        _Handler.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "ctype": self.headers.get("Content-Type"),
            "body": body,
        })
        payload = json.dumps({"text": "云端转出来的文字"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def t5_cloud(tmp):
    sec("cloud 转写：multipart 编码正确 + 上传留痕（不许悄悄传出去）")
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        host, port = srv.server_address
        cfg = _cfg(tmp, backend="cloud",
                   cloud={"api_key": "sk-test", "base_url": f"http://{host}:{port}/v1",
                          "model": "FunAudioLLM/SenseVoiceSmall"})
        p = os.path.join(tmp, "上传测试.m4a")
        payload = b"\x00\x01AUDIOBYTES"
        with open(p, "wb") as f:
            f.write(payload)

        buf = io.StringIO()
        real_stderr = sys.stderr
        sys.stderr = buf
        try:
            text, err = audio_read.transcribe(p, cfg)
        finally:
            sys.stderr = real_stderr
        check("cloud 转写拿到文本", err == "" and text == "云端转出来的文字", (text, err))

        check("确实打了一条「上传」日志（隐私面留痕）", "上传音频到云端转写" in buf.getvalue(),
              buf.getvalue())
        check("日志里带了服务商，便于审计", "/v1" in buf.getvalue(), buf.getvalue())

        sent = _Handler.seen[-1]
        check("打到了 /v1/audio/transcriptions", sent["path"] == "/v1/audio/transcriptions",
              sent["path"])
        check("带 Bearer key", sent["auth"] == "Bearer sk-test", sent["auth"])
        check("Content-Type 是 multipart 且带 boundary",
              (sent["ctype"] or "").startswith("multipart/form-data; boundary="),
              sent["ctype"])
        body = sent["body"]
        check("multipart 里有 model 字段",
              b'name="model"' in body and b"FunAudioLLM/SenseVoiceSmall" in body)
        check("multipart 里有 file 字段 + 文件名（非 ASCII 也不能炸）",
              b'name="file"' in body and "上传测试.m4a".encode("utf-8") in body)
        check("**音频字节原样在里面**（没有编码坏掉）", payload in body)
        check("body 以结束 boundary 收尾", body.rstrip().endswith(b"--"), body[-40:])
    finally:
        srv.shutdown()
        srv.server_close()


def t6_cloud_failure(tmp):
    sec("cloud 失败：如实报错 + 不编内容")
    cfg = _cfg(tmp, backend="cloud",
               cloud={"api_key": "k", "base_url": "http://127.0.0.1:1/v1", "model": "m"})
    p = os.path.join(tmp, "a.m4a")
    with open(p, "wb") as f:
        f.write(b"\0" * 32)
    text, err = audio_read.transcribe(p, cfg)
    check("连不上 → 失败且不返回文本", text == "" and err, (text, err))
    check("……文案里给出「改回 local」这条退路", "local" in err, err)
    check("……并明说没有编内容", "没有编内容" in err, err)


def t7_long_audio_windows(tmp):
    """长音频**分段续读**（2026-10-02 P2）：超上限不再拒绝，而是切段 + 给 cursor。

    这里用桩替掉「转写」那一步（本机没装 faster-whisper），但**切段是真跑 PyAV**：
    验的是分段数学（哪一段、还有没有下一段）、cursor 契约（`<id>:<秒>`）、
    临时切片用完即删，以及短音频**不该被切**（原来那条路一个字都不动）。
    """
    print("\n── T7 长音频分段（切段真跑，转写用桩） ──")
    import array
    import wave

    import audio_read
    import file_read

    wav = os.path.join(tmp, "四十分钟.wav")
    rate, secs = 8000, 40
    buf = array.array("h", [0] * (rate * secs))
    for i in range(0, len(buf), 8):
        buf[i] = 3000
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(buf.tobytes())
    check("样本时长确实是 40 秒", abs(audio_read._duration(wav) - 40) < 1,
          audio_read._duration(wav))

    # ① 切段是真的（这一步不需要 whisper）
    out1 = os.path.join(tmp, "切片1.wav")
    n = audio_read.slice_to_wav(wav, out1, start=0, secs=20)
    with wave.open(out1, "rb") as w:
        d1 = w.getnframes() / w.getframerate()
    out2 = os.path.join(tmp, "切片2.wav")
    audio_read.slice_to_wav(wav, out2, start=20, secs=20)
    with wave.open(out2, "rb") as w:
        d2 = w.getnframes() / w.getframerate()
    check("切出第一段（≈20 秒）", n > 0 and 19 < d1 < 21, d1)
    check("从第 20 秒切第二段（≈20 秒、不重不漏）", 19 < d2 < 21, d2)

    # ② window 的分段数学（转写用桩，免得依赖模型）
    real_tr = audio_read.transcribe
    seen = []

    def fake_tr(path, cfg=None, max_bytes=None, **kw):
        # ⚠️ `**kw` 不能删：`window()` 是按**关键字**调 `transcribe(path, cfg, max_bytes=…)`
        # 的（见 audio_read.window），而这个桩以前只收位置参数 → 桩自己抛
        # `TypeError: got an unexpected keyword argument 'max_bytes'`，被 `window()`
        # 的兜底吞掉后返回 `(None, None, ...)`，于是下面的 `text.startswith` 报
        # `'NoneType' object has no attribute 'startswith'`。**桩的形状必须跟
        # 生产调用的形状一致**，否则整份套件会在这里断掉、后面所有用例都跑不到。
        seen.append(os.path.basename(path))
        return f"第 {len(seen)} 段的转写", ""

    audio_read.transcribe = fake_tr
    try:
        cfg = {"audio": {"max_seconds": 20, "backend": "local"}, "file": {"max_bytes": 0}}
        text, nxt, note = audio_read.window(wav, cfg, start=0)
        check("第一段：转写的是**切出来的临时文件**（不是原文件）",
              text.startswith("第 1 段") and seen and seen[-1] != os.path.basename(wav), seen)
        check("第一段还有下一段（next_start=20）", nxt == 20, nxt)
        text2, nxt2, _ = audio_read.window(wav, cfg, start=nxt)
        check("第二段：读到末尾（next_start=None）", text2.startswith("第 2 段") and nxt2 is None, nxt2)
        check("临时切片用完就删（目录里没有 .wav 残留）",
              not [x for x in os.listdir(audio_read.tmp_dir()) if x.endswith(".wav")],
              os.listdir(audio_read.tmp_dir()))

        # 短音频：走老路，**不切**
        seen.clear()
        short = os.path.join(tmp, "十秒.wav")
        small = array.array("h", [0] * (rate * 10))
        with wave.open(short, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(small.tobytes())
        text3, nxt3, _ = audio_read.window(short, cfg, start=0)
        check("短音频（10 秒 < 上限 20 秒）**不切**、直接转写原文件",
              seen and seen[-1] == os.path.basename(short) and nxt3 is None, seen)
    finally:
        audio_read.transcribe = real_tr

    # ③ 整条链：cursor 契约（file_read._audio → extract_page）
    real_tr2 = audio_read.transcribe
    audio_read.transcribe = lambda path, cfg=None, max_bytes=None: ("整段转写内容", "")
    try:
        cfg = {"audio": {"max_seconds": 20, "backend": "local"}, "file": {"max_bytes": 0}}
        text, err = file_read.extract(wav, cfg)
        check("extract 给出 cursor（形状 id:秒）",
              bool(text) and re.search(r"cursor=([0-9a-f]{16}:\d+)", text) is not None, (err, text))
        cur = re.search(r"cursor=([0-9a-f]{16}:\d+)", text).group(1)
        check("cursor 指向第 20 秒", cur.endswith(":20"), cur)
        more, err2 = file_read.extract_page(cfg=cfg, cursor=cur)
        check("「继续」从第 20 秒接着读（不是从头再来）",
              bool(more) and not err2 and "末尾" in more, (err2, (more or "")[:120]))
        check("……而且不再给 cursor（已经到末尾）", "cursor=" not in (more or ""), more)
        d = file_read.export_dir(cfg)
        check("转写按段累积在导出文件里",
              any(n.startswith("a_") and n.endswith(".txt") for n in os.listdir(d)),
              os.listdir(d))
        over, err3 = file_read.extract_page(cfg=cfg, cursor=cur)
        check("重复「继续」也如实说（不返空、不重读）",
              (err3 and "末尾" in err3) or (over and "末尾" in over), (err3, over))
    finally:
        audio_read.transcribe = real_tr2
        audio_read.sweep_tmp(max_age=0)



def t8_max_bytes_zero(tmp):
    """`file.max_bytes: 0` = **不限大小**，音频这条路也必须认（2026-10-02 真机抓到的 bug）。

    真机现场：`file.max_bytes: 0`（新默认值）时，音频**全部被拒**——报"超过上限 0.0MB"。
    根因是 `_precheck` 里 `int(max_bytes or _max_bytes(cfg))`：`_max_bytes` 把 0 当"没配"
    退回 30MB 的那套写法，在**音频**这条路上算出来的上限是 **0 字节** → 任何非空音频都超。
    和 `file_read._opt_int` 那个 0/没配不分家的坑**是同一个**，只是漏在了音频这一支。

    这里钉两条：0 = 放行；给了具体值就照样拦。
    """
    print("\n── T8 `file.max_bytes: 0` = 不限（音频也要认） ──")
    import audio_read

    wav = os.path.join(tmp, "两秒.wav")
    import array
    import wave
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(array.array("h", [100] * 16000).tobytes())

    check("0 被识别成「不限」而不是「上限 0 字节」",
          audio_read._max_bytes({"file": {"max_bytes": 0}}) == 0,
          audio_read._max_bytes({"file": {"max_bytes": 0}}))
    check("没配时才退回 30MB 默认",
          audio_read._max_bytes({"file": {}}) == 30 * 1024 * 1024,
          audio_read._max_bytes({"file": {}}))

    ok, why = audio_read._precheck(wav, {"file": {"max_bytes": 0}})
    check("★ max_bytes=0 时**放行**（这就是真机那个 bug）", ok, why)
    ok2, why2 = audio_read._precheck(wav, {"file": {"max_bytes": 8}})
    check("给了具体上限照样拦（8 字节 < 这个文件）", not ok2 and "超过上限" in why2, why2)
    ok3, _ = audio_read._precheck(wav, {"file": {"max_bytes": 0}}, max_bytes=8)
    check("调用方显式传的 max_bytes 优先（视频切出来的 wav 就是这么传的）", not ok3)

    # 端到端：0 时真的会去转写（用桩，免得依赖模型在不在）
    import file_read
    real = audio_read.transcribe
    audio_read.transcribe = lambda path, cfg=None, max_bytes=None: ("桩：转写成功", "")
    try:
        text, err = file_read.extract(wav, {"file": {"max_bytes": 0},
                                           "audio": {"backend": "local", "max_seconds": 1800}})
        check("★ 端到端：max_bytes=0 时音频能读到文字（不再被 0 字节上限拒掉）",
              text and "转写成功" in text and not err, (err, text))
    finally:
        audio_read.transcribe = real


def t9_language(tmp):
    """`audio.language`：默认 auto（不传），显式值照传，非法值退回 auto 并告警。

    **这个用例是补 2026-10-03 真机那个缺陷的**：以前 `_local` / `transcribe_scored`
    两处 kwargs 写死 `language="zh"`，说英文 `superboynick` 被中文词汇表硬凑成
    「你好,你好,我跟俗文貴你最近聊了什麼…」——通顺、但完全是捏造的，还被当成
    用户原话送进 `run_agent` 去执行。写死时**所有用例都是绿的**，所以必须有一条
    专门钉住"语言到底传了什么"，否则这个 bug 会再回来。
    """
    sec("转写语言：auto 不写死 / 显式照传 / 非法值退回 auto")

    check("默认（没配）= auto → 返回 None（让 whisper 探测）",
          audio_read.language({}) is None and audio_read.language({"audio": {}}) is None)
    check("显式 auto（含大小写 / 空串）都当自动",
          all(audio_read.language({"audio": {"language": v}}) is None
              for v in ("auto", "AUTO", " auto ", "", None, "detect")))
    check("显式代码照传（含大小写归一）",
          audio_read.language({"audio": {"language": "en"}}) == "en"
          and audio_read.language({"audio": {"language": "ZH"}}) == "zh"
          and audio_read.language({"audio": {"language": "ja"}}) == "ja")

    # 非法值：**必须告警**（不许静默下传 —— whisper 遇到不认识的 language 会抛，
    # 等于把配置里一个手滑的拼写变成「每次转写都失败」）。stderr 在这里不好抓，
    # 所以只断言"退回了 auto"这个可观测结果。
    check("非法值退回 auto（不抛、不把错值下传给 whisper）",
          all(audio_read.language({"audio": {"language": v}}) is None
              for v in ("chinese", "zh-CN", "ei", "123", "z")))

    p = os.path.join(tmp, "lang.m4a")
    with open(p, "wb") as f:
        f.write(b"\0" * 64)
    _make_dir(audio_read.model_dir(_cfg(tmp)))

    # ① 默认 auto 走完 `transcribe()` 全程：language 必须是 None
    fw = _FakeWhisper(["hello there"]).install()
    cfg = _cfg(tmp)
    text, err = audio_read.transcribe(p, cfg)
    tcall = [c for c in fw.calls if c[0] == "transcribe"]
    check("auto：转写成功且 language=None", err == "" and text == "hello there", (text, err))
    check("auto：确实**没传** language（写死 zh 的 bug 钉在这）",
          tcall and tcall[-1][2] is None, fw.calls)

    # ② 配了 en：`transcribe()` 必须把 en 传下去
    fw2 = _FakeWhisper(["hello there"]).install()
    audio_read.transcribe(p, _cfg(tmp, language="en"))
    tcall2 = [c for c in fw2.calls if c[0] == "transcribe"]
    check("配 en：language='en' 传到了 transcribe",
          tcall2 and tcall2[-1][2] == "en", fw2.calls)

    # ③ 语音条那条路（transcribe_scored）走的是**另一份 kwargs**：
    #    两处以前都写死 zh，必须**两条都钉住**（只钉一处会漏）。
    fw3 = _FakeWhisper(["hello there"]).install()
    text3, score, err3 = audio_read.transcribe_scored(p, _cfg(tmp, language="en"))
    tcall3 = [c for c in fw3.calls if c[0] == "transcribe"]
    check("语音条路（transcribe_scored）也按配置传 language",
          err3 == "" and text3 == "hello there" and tcall3 and tcall3[-1][2] == "en",
          (text3, err3, fw3.calls))

    fw4 = _FakeWhisper(["hello there"]).install()
    audio_read.transcribe_scored(p, _cfg(tmp))
    tcall4 = [c for c in fw4.calls if c[0] == "transcribe"]
    check("语音条路默认 auto 同样是 None",
          tcall4 and tcall4[-1][2] is None, fw4.calls)


def t10_languages(tmp):
    """`audio.languages`：**限制只转写允许的语言**（默认中英文）。

    **为什么必须有这条**：`audio.language: auto`（不写死语言）只解决了"被迫说中文"，
    没解决"**选错语言照样捏造**"——2026-10-03 真机：一句短外语被 whisper 判成法语，
    转出 `Super poignée comme elle a l'air d'un chemin.`（语法通顺、语义不通），
    照样被当作用户原话进 `run_agent`。所以探测出的语言不在允许表里时，
    **必须拒绝并且一个字都不给**，绝不用表内的语言去"凑"那段音频。
    """
    sec("语音语言限制：默认中英文，探测到别的语言就如实拒绝")
    check("默认（没配）→ zh/en", audio_read.allowed_languages({}) == ["zh", "en"],
          audio_read.allowed_languages({}))
    check("字符串 'zh, en' 也认",
          audio_read.allowed_languages({"audio": {"languages": "zh, en"}}) == ["zh", "en"])
    check("显式 [] → 关掉限制",
          audio_read.allowed_languages({"audio": {"languages": []}}) == [])
    check("非法项被丢掉、全非法退回默认",
          audio_read.allowed_languages({"audio": {"languages": ["zh", "klingon"]}}) == ["zh"]
          and audio_read.allowed_languages({"audio": {"languages": ["xx"]}}) == ["zh", "en"])

    p = os.path.join(tmp, "lang2.m4a")
    with open(p, "wb") as f:
        f.write(b"\0" * 64)
    _make_dir(audio_read.model_dir(_cfg(tmp)))

    cfg = _cfg(tmp)
    # 探测到不允许的语言 → 拒绝、且**不调用 transcribe**（不猜）
    fw = _FakeWhisper(["Should not be used"], language="fr").install()
    text, err = audio_read.transcribe(p, cfg)
    check("法语 → 拒绝且不给文本", text == "" and "只允许" in err, (text, err))
    check("……并明说没有编内容", "没有编内容" in err, err)
    check("……**没有**调 transcribe（绝不拿允许的语言去凑）",
          not any(c[0] == "transcribe" for c in fw.calls), fw.calls)

    # 探测到允许的语言 → 照常转写
    fw = _FakeWhisper(["superboynick"], language="en").install()
    text, err = audio_read.transcribe(p, cfg)
    check("英文 → 照常转出文本", err == "" and text == "superboynick", (text, err))

    # 显式 language：用户说死了就照办，不探测
    audio_read.transcribe(p, _cfg(tmp, language="en"))
    tcall = [c for c in fw.calls if c[0] == "transcribe"]
    check("显式 language=en 时透传", tcall and tcall[-1][2] == "en", fw.calls)

    # 语音条那条路同样受限
    fw = _FakeWhisper(["Should not be used"], language="fr").install()
    t3, score, e3 = audio_read.transcribe_scored(p, cfg)
    check("语音条路（scored）同样拒绝", t3 == "" and "只允许" in e3 and score is None,
          (t3, score, e3))

    # 老版本没有 detect_language：可选功能不该变成硬故障（用桩自带的 detect=False 模拟）
    fw = _FakeWhisper(["whatever"], detect=False).install()
    text, err = audio_read.transcribe(p, cfg)
    check("没有探测接口 → 照转（不因可选功能失败）", err == "" and text == "whatever",
          (text, err))


def t11_simplify():
    """繁→简（`audio.simplify`，**默认开**）：2026-10-06 用户报「为什么转写出来的是繁体」。

    识别**一个字不动**，只在输出后换字形（zhconv）。三条钉住：
      ① 默认开 → 繁体变简体；`false` → 一字不改（要原始输出的人）；
      ② 真改了要留一行痕（「繁→简：改了 N 个字」——学 redact 的命中数要打日志）；
      ③ **缺 zhconv 不许静默、也不许让转写失败**：原样返回 + 限流留痕 + `--status` 说明。
    """
    print("\n── 繁→简：默认开、改了要留痕、缺库不静默（2026-10-06）──")
    import contextlib

    class _FakeZhconv:
        @staticmethod
        def convert(t, target):
            return str(t).replace("樹", "树").replace("楊", "杨")

    saved = sys.modules.get("zhconv")
    try:
        sys.modules["zhconv"] = _FakeZhconv
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = audio_read.maybe_simplify("白楊樹", {})
        check("默认开：繁体变简体", out == "白杨树", out)
        check("★ 改了就在日志里报**改了几个字**（我们确实动过内容）",
              "繁→简：改了 2 个字" in buf.getvalue(), buf.getvalue()[:80])
        check("显式 `simplify: false` → 一字不改（要原始输出的人）",
              audio_read.maybe_simplify("白楊樹", {"audio": {"simplify": False}}) == "白楊樹")
        check("本来没有繁体 → 原样", audio_read.maybe_simplify("你好呀", {}) == "你好呀")
        check("空串 → 原样（不抛）", audio_read.maybe_simplify("", {}) == "")
    finally:
        if saved is None:
            sys.modules.pop("zhconv", None)
        else:
            sys.modules["zhconv"] = saved

    # 缺库：原样返回 + 留痕（限流）
    # ⚠️ 这里**不能用 `_Env(zhconv=None)`**（本会话踩了两次）：它模拟的是「装没装」
    # （`find_spec` + 从 sys.modules 里摘掉），而 `maybe_simplify` 里是**真 `import`**
    # —— 磁盘上装着就会重新导入，用例当场假红。真 `import` 的"缺"要用
    # **`sys.modules[name] = None`** 这个 Python 哨兵（它让 import 直接抛 ImportError）。
    _saved_zh = sys.modules.get("zhconv", "MISSING")
    try:
        sys.modules["zhconv"] = None
        audio_read._ZHCONV_MISS_AT[0] = 0.0
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            out2 = audio_read.maybe_simplify("白楊樹", {})
        check("★ 缺 zhconv → **原样返回**（绝不因此让整条转写失败）", out2 == "白楊樹", out2)
        check("★ ……而且留痕（不许静默降级）", "zhconv" in buf2.getvalue(), buf2.getvalue()[:90])
    finally:
        if _saved_zh == "MISSING":
            sys.modules.pop("zhconv", None)
        else:
            sys.modules["zhconv"] = _saved_zh
    return True


def main():
    tmp = tempfile.mkdtemp(prefix="selftest_audio_")
    global _OK
    # 把临时根目录改到本测试自己的目录：**用环境变量，不再 monkeypatch 生产函数**。
    # 为什么需要：生产临时目录是 `data/tmp_audio`（由 `file_read` 使用），而在**受限环境**
    # （只允许写工作区顶层的沙箱）里连建文件都做不到 —— `t7`（长音频分段真跑 PyAV 切片）
    # 会当场 PermissionError 崩掉，整份套件后面的用例一条都跑不到。
    # ⚠️ 必须是 `tmp` 下的**子目录**：t7 有一条断言「切片用完就删、目录里没有 .wav 残留」，
    # 而用例自己的素材 wav（四十分钟.wav 等）就写在 `tmp` 根下 —— 指到根上会让那条断言
    # 把"测试自己的素材"当成"没删干净的切片"，**假红**。
    # 这条覆盖能力由 `tempdir.py` 正式提供（`PROJ_TMP`），所以这里不用再替换函数引用。
    _old_proj_tmp = os.environ.get("PROJ_TMP")
    os.environ["PROJ_TMP"] = os.path.join(tmp, "tmp_audio")
    print("=" * 60)
    print("语音输入（audio_read）回归自测（不联网、不下模型、不碰微信）")
    print("=" * 60)
    _real_fw = sys.modules.get("faster_whisper")
    try:
        t1_availability(tmp)
        t2_caps(tmp)
        t3_local_and_privacy(tmp)
        t4_dispatch(tmp)
        t5_cloud(tmp)
        t6_cloud_failure(tmp)
        t11_simplify()
        t7_long_audio_windows(tmp)
        t8_max_bytes_zero(tmp)
        t9_language(tmp)
        t10_languages(tmp)
    finally:
        if _real_fw is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = _real_fw
        # 还原 PROJ_TMP（别把它留给同进程里后面的用例）
        if _old_proj_tmp is None:
            os.environ.pop("PROJ_TMP", None)
        else:
            os.environ["PROJ_TMP"] = _old_proj_tmp
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
