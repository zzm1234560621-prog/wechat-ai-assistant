"""看图片：系统 OCR（免费离线）、视觉模型（描述成文字）、或**原图直接进对话**。

**为什么不是接 hook 的 Decode_Pic 去解 .dat**：那条路走不通——
`.dat` 的前 1024 字节需要一把没拿到的 AES 固定密钥（见
docs/wechat4-dat-image-notes.md）。这里用的是**已经能拿到字节**的那些图：
微信缓存的明文缩略图、当文件发来的原图、自己发出去的图（RWTemp 明文）、
Office/PDF 内嵌图、视频抽帧。

四种模式（config.yaml 的 `image.mode`）见 `handoff()`；要点：

  off     不解读，只说"有这张图"
  ocr     系统 OCR 认**图里的字**：免费、离线、无限额。**默认值**。
          看不懂风景照/表情包（那种图 OCR 返回空）。
  vision  调视觉模型**描述画面**（OpenAI 兼容接口，按次收费），描述当文字用。
  inline  **原图直接进这一轮发给模型的消息**（要求模型支持视觉）。
          比 vision 强在"模型自己看图"，代价是图片 token 更贵。

**免费优先**：`image.ocr_first`（默认开）让 vision/inline 也**先跑一遍 OCR**——
抽出足够多的字就直接用 OCR 结果，**一次视觉模型都不调**（省钱的第一道闸）。
"""
import base64
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_OCR_PS1 = os.path.join(_HERE, "tools", "ocr.ps1")
_RESIZE_PS1 = os.path.join(_HERE, "tools", "resize.ps1")

DEFAULT_LIMIT_BYTES = 5 * 1024 * 1024      # 聊天缩略图那条路
DEFAULT_SEND_BYTES = 8 * 1024 * 1024       # 任何图**送模型**的上限
DEFAULT_DOWNSCALE = 1024                   # 送模型前长边压到多少（0 = 不压）
DEFAULT_OCR_MIN_CHARS = 20                 # OCR 出这么多字就算"这是有字的图"
_CACHE_MAX = 200                           # 描述缓存最多留几条

_MODES = ("off", "ocr", "vision", "inline")
_warned = set()


def _warn_once(key, msg):
    """同一个配置问题只嚷一次（mode_of 在热路径上，每次图都喊会刷屏）。"""
    if key in _warned:
        return
    _warned.add(key)
    print(f"⚠️ image_read: {msg}", flush=True)


def _image_cfg(cfg):
    return (cfg or {}).get("image") or {}


def _int_opt(value, default, allow_zero=False):
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 0:
        return default
    if n == 0:
        return 0 if allow_zero else default
    return n


def mode_of(cfg):
    """四种模式之一。**不认识的值一律按 off 处理**（fail-safe：宁可不解读，
    也不因为写错一个词就把图发去某个地方）。"""
    raw = str(_image_cfg(cfg).get("mode", "ocr") or "off").strip().lower()
    if raw in _MODES:
        return raw
    _warn_once("mode:" + raw,
               f"image.mode={raw!r} 不认识（只有 {'/'.join(_MODES)}），按 off 处理。")
    return "off"


def ocr_first(cfg):
    """vision/inline 也要先跑一遍 OCR（默认开）——省钱的第一道闸。"""
    v = _image_cfg(cfg).get("ocr_first")
    return True if v is None else bool(v)


def ocr_min_chars(cfg):
    return _int_opt(_image_cfg(cfg).get("ocr_min_chars"), DEFAULT_OCR_MIN_CHARS)


def send_cap(cfg):
    """送模型那张图的体积上限（0 = 不限）。"""
    return _int_opt(_image_cfg(cfg).get("send_max_bytes"), DEFAULT_SEND_BYTES,
                    allow_zero=True)


def downscale_edge(cfg):
    """送模型前把长边压到多少像素（0 = 不压）。"""
    return _int_opt(_image_cfg(cfg).get("downscale"), DEFAULT_DOWNSCALE, allow_zero=True)


def cache_path(cfg):
    p = str(_image_cfg(cfg).get("cache_file") or "").strip() or os.path.join("data", "vision_cache.json")
    return p if os.path.isabs(p) else os.path.join(_HERE, p)


def tmp_dir():
    return os.path.join(_HERE, "data", "tmp_img")


def _too_big(path, cfg, limit=None):
    try:
        n = os.path.getsize(path)
    except OSError:
        return True
    if limit is None:
        limit = int(_image_cfg(cfg).get("max_bytes", DEFAULT_LIMIT_BYTES))
    if not limit:               # 0 = 不限（`file.max_bytes: 0` 那条路会传 0 下来）
        return False
    return n > limit


def ocr(path, timeout=90):
    """用 Windows 自带 OCR 识别图里的文字。返回 (是否成功, 文本或错误)。"""
    if not os.path.isfile(_OCR_PS1):
        return False, "找不到 tools/ocr.ps1"
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", _OCR_PS1, "-Path", path],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "OCR 超时"
    except OSError as e:
        return False, f"起不了 PowerShell：{e}"

    out = (p.stdout or b"").decode("utf-8", "ignore")
    if p.returncode != 0:
        return False, out.strip()[:200] or f"OCR 退出码 {p.returncode}"
    if "----" not in out:
        return False, out.strip()[:200]
    text = out.split("----", 1)[1].strip()
    return True, text


def vision(path, cfg, timeout=120):
    """调视觉模型描述图片。返回 (是否成功, 文本或错误)。"""
    v = _image_cfg(cfg).get("vision") or {}
    key = v.get("api_key") or os.getenv("VISION_API_KEY")
    base = (v.get("base_url") or "").rstrip("/")
    model = v.get("model")
    if not (key and base and model):
        return False, "没配视觉模型（config.yaml 的 image.vision: base_url/model/api_key）"

    try:
        data = base64.b64encode(open(path, "rb").read()).decode()
    except OSError as e:
        return False, f"读图失败：{e}"

    ext = "png" if open(path, "rb").read(8).startswith(b"\x89PNG") else "jpeg"
    prompt = v.get("prompt") or "用一两句中文描述这张图的内容。"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{data}"}},
        ]}],
        "max_tokens": 300,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            detail = ""
        return False, f"视觉模型返回 HTTP {e.code}：{detail}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"连不上视觉模型：{e}"
    try:
        return True, (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return False, f"返回格式看不懂：{str(body)[:200]}"


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_load(cfg):
    try:
        with open(cache_path(cfg), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        _warn_once("cache-read", f"描述缓存读不出来（当空处理）：{e}")
        return {}


def _cache_get(path, cfg, model):
    try:
        row = _cache_load(cfg).get(f"{_md5_file(path)}|{model}")
    except Exception:
        return None
    return (row or {}).get("text") or None


def _cache_put(path, cfg, model, text):
    """把视觉模型的描述按「内容 md5 + 模型」缓存 —— 同一张图看第二次不再花钱。"""
    try:
        d = _cache_load(cfg)
        d[f"{_md5_file(path)}|{model}"] = {"text": text, "at": time.time()}
        if len(d) > _CACHE_MAX:                      # 满了丢最老的
            for k in sorted(d, key=lambda k: (d[k] or {}).get("at", 0))[:len(d) - _CACHE_MAX]:
                d.pop(k, None)
        p = cache_path(cfg)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception as e:
        _warn_once("cache-write", f"描述缓存写不进去（不影响这次解读）：{e}")


def resize(path, max_edge, out=None):
    """用系统自带的 System.Drawing 缩图。返回 `(是否成功, 新路径或错误)`。

    **为什么自己写**：缩图这件事 Windows 就有（和 OCR 同一条 PowerShell 路子），
    不必为了它给所有人装 Pillow。缩不动就返回失败，由调用方如实说。
    """
    if not os.path.isfile(_RESIZE_PS1):
        return False, "找不到 tools/resize.ps1"
    if out is None:
        try:
            os.makedirs(tmp_dir(), exist_ok=True)
        except OSError as e:
            return False, f"建临时目录失败：{e}"
        out = os.path.join(tmp_dir(), f"{_md5_file(path)}.{int(max_edge)}.jpg")
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", _RESIZE_PS1, "-Path", path, "-MaxEdge", str(int(max_edge)),
             "-Out", out],
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "缩图超时"
    except OSError as e:
        return False, f"起不了 PowerShell：{e}"
    text = (p.stdout or b"").decode("utf-8", "ignore").strip()
    if p.returncode != 0 or not text.startswith("OK:"):
        return False, (text or f"缩图退出码 {p.returncode}")[:200]
    return True, out


def sweep_tmp(max_age=2 * 86400.0):
    """清理缩图留下的临时文件（**删了什么要打日志**：静默丢弃不允许）。"""
    d = tmp_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    dead = []
    now = time.time()
    for n in names:
        p = os.path.join(d, n)
        try:
            if now - os.path.getmtime(p) > max_age:
                os.remove(p)
                dead.append(n)
        except OSError:
            continue
    if dead:
        print(f"⚠️ image_read: 清理了 {len(dead)} 个缩图临时文件（>{max_age/86400:.0f} 天）。",
              flush=True)
    return dead


def handoff(path, cfg, max_bytes=None, collect=None):
    """把一张图交给「读图」这条链，返回一个 dict：

        {"mode": off|ocr|vision|inline,
         "kind": "text" | "image" | "none",
         "text": <已经有文字了（OCR 结果 / 视觉描述）>,
         "path": <kind=image 时要放进消息里的图片路径>,
         "why":  <没读出来的原因（如实说的那句话）>}

    规则（对应 docs/file-input-spec.md 第八节）：
      * **免费优先**：`ocr_first` 开着时，vision/inline 也先跑 OCR；抽出
        `ocr_min_chars` 个以上的字就直接用 OCR 结果，**一次视觉模型都不调**。
      * `vision`：调视觉模型描述，结果**按内容 md5 缓存**（同一张图第二次不花钱）。
      * `inline`：把**原图路径**交回给调用方（`collect(path, note)`），由上层塞进
        这一轮的消息；需要时先按 `downscale` 缩图，超 `send_max_bytes` 就如实拒绝。
      * **任何失败都带 `why`**，绝不用空文本假装成功。
    """
    m = mode_of(cfg)
    if m == "off":
        return {"mode": m, "kind": "none", "text": "", "path": None,
                "why": "图片解读已关闭（config.yaml 的 image.mode=off）"}
    if not path or not os.path.isfile(path):
        return {"mode": m, "kind": "none", "text": "", "path": None, "why": "图不存在"}
    cap = (int(_image_cfg(cfg).get("max_bytes", DEFAULT_LIMIT_BYTES))
           if max_bytes is None else int(max_bytes))
    if _too_big(path, cfg, limit=cap):
        return {"mode": m, "kind": "none", "text": "", "path": None,
                "why": (f"图太大了（上限 {cap / 1048576:.1f}MB）" if cap else "图太大了")}

    # ① 免费优先：先 OCR
    ocr_text, ocr_why = "", ""
    if m == "ocr" or ocr_first(cfg):
        ok, got = ocr(path)
        if ok:
            ocr_text = got.strip()
            if m == "ocr" or len(ocr_text) >= ocr_min_chars(cfg):
                if not ocr_text:
                    return {"mode": m, "kind": "none", "text": "", "path": None,
                            "why": "图里没识别到文字（系统 OCR 只认图里的字）"}
                return {"mode": m, "kind": "text", "text": ocr_text, "path": None, "why": ""}
        else:
            ocr_why = f"系统 OCR 没成功：{got}"
    if m == "ocr":
        return {"mode": m, "kind": "none", "text": "", "path": None,
                "why": ocr_why or "图里没识别到文字（系统 OCR 只认图里的字）"}

    if m == "vision":
        v = _image_cfg(cfg).get("vision") or {}
        model = str(v.get("model") or "")
        cached = _cache_get(path, cfg, model)
        if cached:
            return {"mode": m, "kind": "text", "text": cached, "path": None,
                    "why": "（这条是缓存里的描述，没重复调用视觉模型）"}
        ok, got = vision(path, cfg)
        if not ok:
            return {"mode": m, "kind": "none", "text": "", "path": None,
                    "why": (got + (f"；另外 {ocr_why}" if ocr_why else ""))}
        got = got.strip()
        if not got:
            return {"mode": m, "kind": "none", "text": "", "path": None,
                    "why": "视觉模型没返回内容"}
        _cache_put(path, cfg, model, got)
        return {"mode": m, "kind": "text", "text": got, "path": None, "why": ""}

    # ② inline：把原图交回去（必要时先缩图）
    send = path
    note = ""
    edge = downscale_edge(cfg)
    if edge:
        ok, out_or_err = resize(path, edge)
        if ok:
            send = out_or_err
            try:
                if os.path.getsize(send) < os.path.getsize(path):
                    note = (f"（原图 {os.path.getsize(path)/1024:.0f}KB 已缩到长边 {edge}："
                            f"{os.path.getsize(send)/1024:.0f}KB）")
                else:
                    send = path          # 缩了反而更大 → 用原图
            except OSError:
                pass
        else:
            note = f"（缩图没成功，用的原图：{out_or_err}）"
    sc = send_cap(cfg)
    size = os.path.getsize(send)
    if sc and size > sc:
        return {"mode": m, "kind": "none", "text": ocr_text, "path": None,
                "why": (f"这张图 {size/1048576:.1f}MB，超过 image.send_max_bytes="
                        f"{sc/1048576:.1f}MB；把上限调大或开 image.downscale 就能发")}
    attached = True
    if collect is not None:
        # 收图的人可以拒（比如超过 image.max_per_round）——**如实回传**，
        # 不然工具层会跟模型说"原图已交给模型看"，而其实没给（那就成了撒谎）。
        attached = bool(collect(send, note))
    return {"mode": m, "kind": "image", "text": ocr_text, "path": send,
            "attached": attached, "why": (ocr_why + " " + note).strip()}


def describe(path, cfg, max_bytes=None):
    """按配置把图**变成文字**。返回 (是否成功, 文本)。

    注意：`mode=inline` 时**没有文字可给** —— 原图是交给对话模型的，不是在这儿转文字。
    这条路径没有"收图的人"时如实说清楚，绝不用空文本假装成功。
    """
    r = handoff(path, cfg, max_bytes=max_bytes)
    if r["kind"] == "text":
        return True, r["text"]
    if r["kind"] == "image":
        if r.get("text"):
            return True, r["text"]
        return False, ("image.mode=inline：这张原本要交给支持视觉的对话模型看，"
                       "在这条只想要文字的路径上没人接（把 image.mode 改成 ocr 或 vision）")
    return False, (r.get("why") or "这张图没解读出内容")
