"""图片交付四模式的回归自测（`image_read.handoff` 那一套）。

规格：`docs/file-input-spec.md` 第八节。**不联网**：视觉模型那一路用**本地假 HTTP 服务**，
OCR 那一路用桩；缩图走真的系统 System.Drawing（不联网、不装依赖）。

钉住的都是"钱"和"撒谎"这两类问题：
  * **免费优先**：OCR 抽出足够多的字时，**一次视觉模型都不调**；
  * **按内容 md5 缓存**：同一张图看第二次不再调用视觉模型；
  * `inline`：把**原图路径**交回给调用方（而不是在这儿转成文字）；超 `send_max_bytes` 如实拒绝；
  * `downscale`：真能缩（缩完更小才用缩后那张）；
  * 不认识的 mode 一律按 `off`（fail-safe），并且**告警**；
  * 任何失败都带得出一句人话（不许空文本假装成功）。

用法：`.venv/Scripts/python.exe selftest_image_handoff.py`
"""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import image_read  # noqa: E402

TMP = tempfile.mkdtemp(prefix="selftest_img_handoff_")
_ok = True
_CALLS = []          # 假视觉模型收到的请求数


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


# ---------------- 一个假的「视觉模型」HTTP 服务 ----------------
class _VisionHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        _CALLS.append(json.loads(body.decode("utf-8")))
        out = json.dumps({"choices": [{"message": {"content": "假视觉模型：图里有一群人"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out.encode())))
        self.end_headers()
        self.wfile.write(out.encode())

    def log_message(self, *a):
        pass


def _start_vision_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _VisionHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _cfg(**image_kw):
    d = {"mode": "ocr", "cache_file": os.path.join(TMP, "vision_cache.json")}
    d.update(image_kw)
    return {"image": d}


def _png(path, size=200, color="Navy"):
    """造一张真 PNG（系统 System.Drawing 要能打开它，所以不能是假字节）。

    ⚠️ `color`/`size` 必须**每个用例都不一样**：视觉描述是按**内容 md5** 缓存的，
    两张一模一样的图会命中同一个缓存条目 —— 用例之间就会互相污染（踩过）。
    """
    import subprocess
    ps = (f'Add-Type -AssemblyName System.Drawing; '
          f'$b=New-Object System.Drawing.Bitmap({size},{size}); '
          f'$g=[System.Drawing.Graphics]::FromImage($b); '
          f'$g.FillRectangle([System.Drawing.Brushes]::{color},0,0,{size},{size}); '
          f'$g.Dispose(); $b.Save("{path}",[System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()')
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=60)
    return path


def t1_ocr_is_free_first():
    print("\n── 1 · 免费优先：OCR 有字就不调视觉模型 ──")
    img = _png(os.path.join(TMP, "a.png"), color="Navy")
    real_ocr, real_vision = image_read.ocr, image_read.vision
    calls = {"ocr": 0, "vision": 0}
    try:
        def fake_ocr(path, timeout=90):
            calls["ocr"] += 1
            return True, "这是一张有字的截图，字足够多足够长可以直接用。"
        def fake_vision(path, cfg, timeout=120):
            calls["vision"] += 1
            return True, "视觉描述"
        image_read.ocr, image_read.vision = fake_ocr, fake_vision

        r = image_read.handoff(img, _cfg(mode="vision", ocr_first=True,
                                        cache_file=os.path.join(TMP, "t1a.json")))
        check("vision 模式下 OCR 抽到足够多的字 → 用 OCR 结果",
              r["kind"] == "text" and r["text"].startswith("这是一张有字的截图"), r)
        check("……并且**一次视觉模型都没调**", calls["vision"] == 0, calls)

        calls.update(ocr=0, vision=0)
        def fake_ocr_short(path, timeout=90):
            calls["ocr"] += 1
            return True, "好的"
        image_read.ocr = fake_ocr_short
        r = image_read.handoff(img, _cfg(mode="vision", ocr_first=True, ocr_min_chars=20,
                                        cache_file=os.path.join(TMP, "t1b.json")))
        check("OCR 只认出两个字（不够）→ 才去调视觉模型",
              r["kind"] == "text" and r["text"] == "视觉描述" and calls["vision"] == 1, r)

        calls.update(ocr=0, vision=0)
        r = image_read.handoff(img, _cfg(mode="vision", ocr_first=False,
                                        cache_file=os.path.join(TMP, "t1c.json")))
        check("ocr_first=false 时直接走视觉模型（不问 OCR）",
              calls["vision"] == 1 and calls["ocr"] == 0, calls)
    finally:
        image_read.ocr, image_read.vision = real_ocr, real_vision


def t2_vision_cache():
    print("\n── 2 · 视觉描述按内容 md5 缓存（同一张图不重复花钱） ──")
    img = _png(os.path.join(TMP, "b.png"), color="DarkRed")
    cfg = _cfg(mode="vision", ocr_first=False,
               cache_file=os.path.join(TMP, "t2.json"))
    real_ocr, real_vision = image_read.ocr, image_read.vision
    calls = {"vision": 0}
    try:
        def fake_ocr(path, timeout=90):
            return True, ""
        def fake_vision(path, cfg_, timeout=120):
            calls["vision"] += 1
            return True, f"描述第 {calls['vision']} 次"
        image_read.ocr, image_read.vision = fake_ocr, fake_vision

        r1 = image_read.handoff(img, cfg)
        r2 = image_read.handoff(img, cfg)
        check("第一次调用视觉模型、第二次命中缓存",
              calls["vision"] == 1 and r2["text"] == r1["text"] == "描述第 1 次", (calls, r1, r2))
        check("命中缓存时明确说了「这是缓存里的描述」", "缓存" in (r2.get("why") or ""), r2)

        img2 = _png(os.path.join(TMP, "c.png"), size=64, color="DarkGreen")  # 内容不同 → 不该命中
        r3 = image_read.handoff(img2, cfg)
        check("换一张图仍然要调模型（md5 不同）", calls["vision"] == 2, calls)
    finally:
        image_read.ocr, image_read.vision = real_ocr, real_vision


def t3_vision_over_http():
    print("\n── 3 · 真 HTTP：视觉模型那一路通（对本地假服务） ──")
    srv, url = _start_vision_server()
    try:
        img = _png(os.path.join(TMP, "d.png"), color="Purple")
        cfg = _cfg(mode="vision", ocr_first=False,
                   vision={"base_url": url, "model": "fake-vl", "api_key": "sk-test"},
                   cache_file=os.path.join(TMP, "vision_cache2.json"))
        _CALLS.clear()
        real_ocr = image_read.ocr
        image_read.ocr = lambda path, timeout=90: (True, "")
        try:
            r = image_read.handoff(img, cfg)
        finally:
            image_read.ocr = real_ocr
        check("走通了本地假视觉服务", r["kind"] == "text" and "假视觉模型" in r["text"], r)
        check("请求体里带的是 image_url + base64 data URL",
              bool(_CALLS) and _CALLS[0]["messages"][0]["content"][1]["type"] == "image_url"
              and _CALLS[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/"),
              _CALLS[:1])
    finally:
        srv.shutdown()


def t4_inline_hands_back_image():
    print("\n── 4 · inline：把**原图**交回给调用方，而不是转成文字 ──")
    img = _png(os.path.join(TMP, "e.png"), size=800, color="Orange")
    got = []
    real_ocr = image_read.ocr
    image_read.ocr = lambda path, timeout=90: (True, "")        # 纯画面：OCR 没字
    try:
        r = image_read.handoff(img, _cfg(mode="inline", downscale=128),
                              collect=lambda p, note="": got.append((p, note)))
    finally:
        image_read.ocr = real_ocr
    check("kind=image（原图交给上层）", r["kind"] == "image" and r["path"], r)
    check("collect 收到了路径", len(got) == 1 and got[0][0] == r["path"], got)
    check("downscale 生效：交给模型的是缩过的那张（更小）",
          os.path.getsize(r["path"]) < os.path.getsize(img),
          (os.path.getsize(img), os.path.getsize(r["path"]) if r["path"] else 0))
    check("缩图这件事在结果里有说明", "缩" in (r.get("why") or "") or "缩" in (got[0][1] or ""),
          (r.get("why"), got))

    # 不给 collect 时：kind=image 但没有收图的人 → describe() 要如实说
    real_ocr = image_read.ocr
    image_read.ocr = lambda path, timeout=90: (True, "")
    try:
        ok, text = image_read.describe(img, _cfg(mode="inline"))
    finally:
        image_read.ocr = real_ocr
    check("inline 走 describe()（没人收图）→ 如实说清楚，不假装有文字",
          ok is False and "inline" in text, text)


def t5_send_cap_and_bad_mode():
    print("\n── 5 · 送模型的上限 + 不认识的 mode 按 off（fail-safe） ──")
    img = _png(os.path.join(TMP, "f.png"), size=1024, color="Teal")
    got = []
    real_ocr = image_read.ocr
    image_read.ocr = lambda path, timeout=90: (True, "")
    try:
        r = image_read.handoff(img, _cfg(mode="inline", send_max_bytes=1024, downscale=0),
                              collect=lambda p, note="": got.append(p))
    finally:
        image_read.ocr = real_ocr
    check("超过 send_max_bytes → **如实拒绝**，不硬发", r["kind"] == "none" and "send_max_bytes" in r["why"], r)
    check("也没往 collect 里塞东西", not got, got)

    r = image_read.handoff(img, _cfg(mode="lol-not-a-mode"))
    check("不认识的 mode → 按 off 处理（fail-safe）", r["kind"] == "none" and
          "图片解读已关闭" in (r["why"] or ""), r)
    check("mode_of 对坏值返回 off", image_read.mode_of(_cfg(mode="xyz")) == "off")


def t6_resize_real():
    print("\n── 6 · 缩图是真跑系统 System.Drawing（不装 Pillow） ──")
    img = _png(os.path.join(TMP, "g.png"), size=600, color="Goldenrod")
    ok, out = image_read.resize(img, 150)
    check("缩图成功", ok and os.path.isfile(out), (ok, out))
    if ok:
        try:
            import struct
            with open(out, "rb") as f:
                head = f.read(4)
            check("产物是真 JPEG（FF D8 FF）", head.startswith(b"\xff\xd8\xff"), head)
        except Exception as e:
            check("产物可读", False, e)
        check("缩完确实更小", os.path.getsize(out) < os.path.getsize(img),
              (os.path.getsize(img), os.path.getsize(out)))
    ok2, err2 = image_read.resize(img, 150, out=os.path.join(TMP, "no_such_dir", "x.jpg"))
    check("写不进去时如实失败（不抛异常）", ok2 is False and err2, (ok2, err2))


def main():
    print("=" * 60)
    print("图片四模式回归自测（临时目录：%s）" % TMP)
    print("=" * 60)
    try:
        t1_ocr_is_free_first()
        t2_vision_cache()
        t3_vision_over_http()
        t4_inline_hands_back_image()
        t5_send_cap_and_bad_mode()
        t6_resize_real()
    finally:
        image_read.sweep_tmp(max_age=0)          # 顺手把缩图临时文件清掉（会打日志）
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 60)
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
