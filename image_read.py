"""看图片内容：系统 OCR（免费离线）或视觉模型（可选）。

**为什么不是接 hook 的 Decode_Pic 去解 .dat**：那条路走不通——
`.dat` 的前 1024 字节需要一把没拿到的 AES 固定密钥（见
docs/wechat4-dat-image-notes.md）。这里用的是微信自己缓存的**已解码缩略图**，
路径由 live_history.v4_images() / image_cache.py 给出。

两种模式（config.yaml 的 `image.mode`）：

  off     不解读，只报"有这张图"
  ocr     用 Windows 自带的 Windows.Media.Ocr 识别**图里的文字**。
          不花钱、不联网。适合截图、聊天记录截图、带字的图；
          **看不懂风景照/表情包的内容**（那种图 OCR 返回空）。
  vision  调一个视觉模型描述画面。要走 OpenAI 兼容接口，
          需要自己配 base_url/model/api_key，每次调用都产生费用。

注意：**缩略图很小**（长边通常 200~300 像素），OCR 的识别率会明显低于原图。
"""
import base64
import json
import os
import subprocess
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_OCR_PS1 = os.path.join(_HERE, "tools", "ocr.ps1")

DEFAULT_LIMIT_BYTES = 5 * 1024 * 1024


def _image_cfg(cfg):
    return (cfg or {}).get("image") or {}


def mode_of(cfg):
    return str(_image_cfg(cfg).get("mode", "ocr") or "off").lower()


def _too_big(path, cfg, limit=None):
    try:
        n = os.path.getsize(path)
    except OSError:
        return True
    limit = limit or int(_image_cfg(cfg).get("max_bytes", DEFAULT_LIMIT_BYTES))
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


def describe(path, cfg):
    """按配置解读一张图。返回 (是否成功, 文本)。"""
    if not path or not os.path.isfile(path):
        return False, "图不存在"
    if _too_big(path, cfg):
        return False, "图太大了，跳过"

    m = mode_of(cfg)
    if m == "off":
        return False, "图片解读已关闭（config.yaml 的 image.mode 设成 off）"
    if m == "vision":
        return vision(path, cfg)
    return ocr(path)
