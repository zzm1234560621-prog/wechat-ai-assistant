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

    def __init__(self, segments, language="zh", duration=3.0):
        self.segments = segments
        self.language = language
        self.duration = duration
        self.calls = []

    def install(self, module_name="faster_whisper"):
        outer = self

        class _Model:
            def __init__(self, path, device=None, compute_type=None):
                outer.calls.append(("model", path, device, compute_type))

            def transcribe(self, path, language=None, vad_filter=None):
                outer.calls.append(("transcribe", path, language, vad_filter))
                segs = [types.SimpleNamespace(text=t) for t in outer.segments]
                info = types.SimpleNamespace(language=outer.language,
                                             duration=outer.duration)
                return segs, info

        mod = _fake_module(module_name)
        mod.WhisperModel = _Model
        sys.modules[module_name] = mod
        return self          # 返回**假对象**本身（调用方能查 .calls）


class _Env:
    """临时把模块/依赖状态换掉，退出时还原（免得污染别的用例）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.saved[k] = sys.modules.get(k)
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        return self

    def __exit__(self, *a):
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


# ---------------- T1 可用性：缺依赖 / 缺模型 / 缺 key ----------------

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

    cloud = _cfg(tmp, backend="cloud", cloud={"api_key": ""})
    ok, why = audio_read.available(cloud)
    check("cloud 没 key → 不可用", ok is False, why)
    check("……并给出「改回 local 就不用上传」的选择", "改回 local" in why, why)

    cloud2 = _cfg(tmp, backend="cloud", cloud={"api_key": "k", "base_url": "http://x/v1",
                                               "model": "m"})
    ok, why = audio_read.available(cloud2)
    check("cloud 配好了 → 可用，且明说会上传", ok is True and "上传" in why, why)


# ---------------- T2 硬上限 ----------------

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


# ---------------- T3 local：真转写 + 零网络（隐私底线） ----------------

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
    check("传了 device=cpu / compute_type=int8 / 中文",
          any(c[0] == "model" and c[2] == "cpu" and c[3] == "int8" for c in fw.calls)
          and any(c[0] == "transcribe" and c[2] == "zh" for c in fw.calls), fw.calls)

    # 静音/没识别出内容：必须当失败，不许当成功返回空串
    fw2 = _FakeWhisper([]).install()
    text, err = audio_read.transcribe(p, cfg)
    check("转出空文本 → **算失败**，不返回空串假装成功", text == "" and err != "", (text, err))
    check("……并说明没编内容", "没有编内容" in err, err)
    check("……并带上了识别到的语言/时长（便于排查）",
          "zh" in err and "3.0" in err, err)
    _ = fw2


# ---------------- T4 分派：文档老行为不变 ----------------

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

    # 不支持的后缀：文案里要提到音频也能读（否则用户不知道有这功能）
    z = os.path.join(tmp, "x.zip")
    with open(z, "wb") as f:
        f.write(b"PK\x03\x04")
    text, note = file_read.extract(z, cfg)
    check("回归：不支持的后缀仍如实拒绝", text is None and note, (text, note))
    check("……且文案里提到了音频（新能力可被发现）", ".m4a" in (note or ""), note)

    # 音频上限：file_read 自己那道体积闸先生效（证明这条路上 file.max_bytes 管着）
    text, note = file_read.extract(a, dict(cfg, file={"max_bytes": 8}))
    check("read_file 这条路上 file.max_bytes 仍然生效",
          text is None and "文件太大" in (note or ""), (text, note))


# ---------------- T5 cloud：multipart 编码 + 上传留痕 ----------------

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


def main():
    tmp = tempfile.mkdtemp(prefix="selftest_audio_")
    global _OK
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
    finally:
        if _real_fw is None:
            sys.modules.pop("faster_whisper", None)
        else:
            sys.modules["faster_whisper"] = _real_fw
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
