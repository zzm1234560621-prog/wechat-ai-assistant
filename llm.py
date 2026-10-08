"""调用大模型的轻量封装。

支持两种后端，用 config.yaml 的 provider 选：

  provider: "anthropic"  -> Claude 官方 API（走 anthropic SDK）
  provider: "openai"     -> OpenAI 兼容的 /chat/completions 接口
                            DeepSeek、Ollama、各类中转都走这个

API Key 从 config / 环境变量读取，显式传入优先；**环境变量按 provider 各用各的**
（anthropic → ANTHROPIC_API_KEY，openai → OPENAI_API_KEY，绝不互相回退，见 _KEY_ENV）。
具体用哪家由 provider + base_url + model 三者决定，默认模型会跟着 provider 走。
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"

# 每个协议只认自己那把 key 对应的环境变量。
# **绝不能互相回退**：provider=openai 时拿本机的 ANTHROPIC_API_KEY 去请求
# OpenAI/DeepSeek，服务端只会回一个莫名的 401；而 Claude 桌面应用恰恰会给子进程
# 注入 ANTHROPIC_* 环境变量（见 CLAUDE.md「编译期踩过的坑」），用户根本查不到原因。
_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _key_from_env(provider):
    """按协议挑环境变量；不认识的自定义协议仍按 openai 兼容那套取。"""
    name = _KEY_ENV.get(provider, "OPENAI_API_KEY")
    return os.getenv(name)


def _temperature_unsupported(e):
    """这个异常是不是「服务端不认 temperature 参数」？

    **判据必须严**：只有两种情形才算——
      * `TypeError`：SDK 版本把 temperature 从 create() 签名里去掉了（本地参数就错了）；
      * HTTP 400 且报错正文里**明确出现** temperature（服务端说这个参数不合法）。
    原来的写法是「错误文本里含 temperature 就降级重发」，于是任何一条恰好带了
    这个词的失败（400 无效字段、网关报错）都会把请求**再发一遍**——重复计费，
    而且把真实错误吞掉了。其它异常一律往上抛。
    """
    if isinstance(e, TypeError):
        return True
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    return status == 400 and "temperature" in str(e).lower()


class ChatLLM:
    """统一入口：chat(system, messages) -> str"""

    def __init__(self, model=None, api_key=None, base_url=None,
                 max_tokens=2000, temperature=0.7, provider="anthropic"):
        self.provider = (provider or "anthropic").lower()
        # 默认模型必须跟 provider 匹配：拿 claude-* 去请求 DeepSeek 会直接被拒。
        model = model or ("claude-sonnet-5" if self.provider == "anthropic"
                          else "deepseek-chat")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

        api_key = api_key or _key_from_env(self.provider)
        if not api_key:
            raise RuntimeError(
                f"缺少 API Key（当前协议 provider={self.provider}）。"
                f"可在微信里发 /api <key> 设置，"
                f"或设环境变量 {_KEY_ENV.get(self.provider, 'ANTHROPIC_API_KEY')}。"
                f"（两种协议的 key 不通用，环境变量也不通用，别互相借。）"
            )
        # HTTP 头只能放 latin-1，key 里混进中文/全角字符会在发请求时才报
        # 「'latin-1' codec can't encode ...」这种看不懂的错，这里提前拦掉。
        if not str(api_key).isascii():
            raise RuntimeError(
                "API Key 里有非 ASCII 字符（多半是复制时混进了中文或全角符号），请重新复制。"
            )

        if self.provider == "openai":
            self._impl = _OpenAICompat(model, api_key, base_url, max_tokens, temperature)
        else:
            self._impl = _AnthropicImpl(model, api_key, base_url, max_tokens, temperature)

    def chat(self, system: str, messages: list) -> str:
        """messages: [{"role": "user", "content": "..."}, ...]"""
        return self._impl.chat(system, messages)

    def chat_with_tools(self, system: str, messages: list, tools: list) -> "ChatResult":
        """带工具调用的对话。返回 ChatResult(text, tool_calls)。

        messages 用统一的中立格式，两边协议各自转换：
          {"role":"user",      "content": str}
          {"role":"assistant", "content": str, "tool_calls":[{"id","name","arguments"}]}
          {"role":"tool",      "tool_call_id": str, "name": str, "content": str}

        tools 也是中立格式：[{"name","description","parameters"(JSON Schema)}]
        """
        if not tools:
            return ChatResult(self._impl.chat(system, messages))
        return self._impl.chat_with_tools(system, messages, tools)


class ToolCall:
    __slots__ = ("id", "name", "arguments")

    def __init__(self, id="", name="", arguments=None):
        self.id = id
        self.name = name
        self.arguments = arguments if isinstance(arguments, dict) else {}


class ChatResult:
    """一次带工具调用的答复结果。

    `truncated` = 「这次答复被 max_tokens 截断了」，**只暴露事实，不在这里拼提示**：
    要不要提醒用户、文案怎么写由上层（bot / auto_reply）决定。
    半句话被当成完整答复、还被 auto_reply 再截一刀发出去，是用户能看见的错。
    """

    __slots__ = ("text", "tool_calls", "truncated")

    def __init__(self, text="", tool_calls=None, truncated=False):
        # truncated 带默认值、排在最后：ChatResult(text, calls) 这种位置调用继续可用
        self.text = text or ""
        self.tool_calls = list(tool_calls or [])
        self.truncated = bool(truncated)

    def __repr__(self):
        return (f"ChatResult(text={self.text[:40]!r}, "
                f"tool_calls={[c.name for c in self.tool_calls]}, "
                f"truncated={self.truncated})")


def _record_usage(provider, model, pt, ct, kind="chat"):
    """把这次调用的 token 用量记到本地账本（微信里的 `/用量` 就是读它）。

    两条硬要求：
      * **绝不许因为记账失败影响一次正常调用** —— `usage.record` 自己不会抛，
        这里是第二道保险；
      * 只记 provider/model/token 数，**不记请求内容、不记密钥**（落盘格式由 usage.py 管）。
    """
    try:
        import usage
        usage.record(provider, model, pt, ct, kind=kind)
    except Exception as e:                                  # pragma: no cover
        print(f"[llm] ⚠️ 用量没记上（不影响本次调用）：{e}", flush=True)


def _rec_anthropic(model, resp, kind):
    try:
        import usage
        pt, ct = usage.extract_anthropic_usage(resp)
    except Exception:                                       # pragma: no cover
        return
    _record_usage("anthropic", model, pt, ct, kind)


def _rec_openai(model, body, kind):
    try:
        import usage
        pt, ct = usage.extract_openai_usage(body)
    except Exception:                                       # pragma: no cover
        return
    _record_usage("openai", model, pt, ct, kind)



# ============================================================
#  Anthropic 官方
# ============================================================

class _AnthropicImpl:
    def __init__(self, model, api_key, base_url, max_tokens, temperature):
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        # 必须显式给官方端点，不能传 None：
        # SDK 在 base_url=None 时会回退去读环境变量 ANTHROPIC_BASE_URL，
        # 而某些环境（例如 Claude 桌面应用给子进程注入的）会把它指向本地网关，
        # 结果请求被发去别处、报 401，极难排查。
        self.client = anthropic.Anthropic(
            api_key=api_key,
            base_url=base_url or DEFAULT_ANTHROPIC_URL,
        )

    def chat(self, system, messages):
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
        )
        # 新版 anthropic SDK（实测 1.8.0）已把 temperature 从 create() 签名中去掉，
        # 只能通过 extra_body 透传；服务端不支持时降级为不带它重试一次。
        # 判据见 _temperature_unsupported —— 只有「确实是这个参数不被支持」才重试，
        # 别的一律抛（否则会重复计费，还把真实错误吞掉）。
        if self.temperature is not None:
            try:
                resp = self.client.messages.create(
                    **kwargs, extra_body={"temperature": self.temperature}
                )
                _rec_anthropic(self.model, resp, "chat")
                return _anthropic_text(resp)
            except Exception as e:
                if not _temperature_unsupported(e):
                    raise
        resp = self.client.messages.create(**kwargs)
        _rec_anthropic(self.model, resp, "chat")
        return _anthropic_text(resp)

    def chat_with_tools(self, system, messages, tools):
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=_anthropic_messages(messages),
            tools=[{"name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}}}
                   for t in tools],
        )
        resp = None
        if self.temperature is not None:
            try:
                resp = self.client.messages.create(
                    **kwargs, extra_body={"temperature": self.temperature})
            except Exception as e:
                # 与 chat() 分支保持对称：只有「temperature 不被支持」才降级重发一次，
                # 其余异常直接抛给上层。
                if not _temperature_unsupported(e):
                    raise
        if resp is None:
            resp = self.client.messages.create(**kwargs)

        text, calls = "", []
        for b in resp.content:
            bt = getattr(b, "type", "")
            if bt == "text":
                text += b.text
            elif bt == "tool_use":
                calls.append(ToolCall(getattr(b, "id", ""), getattr(b, "name", ""),
                                      getattr(b, "input", None)))
        return ChatResult(text, calls, truncated=_is_truncated(resp))


def _anthropic_blocks(blocks):
    """中立 content 数组 → Anthropic 的 content block。

    **图片那块的形状两边不一样**：中立/OpenAI 是
    `{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,…"}}`，
    而 Anthropic 要 `{"type":"image","source":{"type":"base64","media_type":…,"data":…}}`。
    直接透传会被 API 拒；所以这一层必须翻译。
    """
    out = []
    for b in blocks or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            out.append({"type": "text", "text": str(b.get("text") or "")})
        elif b.get("type") == "image_url":
            url = str(((b.get("image_url") or {}).get("url")) or "")
            if url.startswith("data:") and ";base64," in url:
                head, data = url.split(";base64,", 1)
                media = (head[len("data:"):] or "image/jpeg").strip() or "image/jpeg"
                out.append({"type": "image", "source": {
                    "type": "base64", "media_type": media, "data": data}})
    return out


def _anthropic_messages(messages):
    """中立格式 -> Anthropic 的 content block 格式。"""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                # 带图片的那条（image.mode=inline 才会出现）
                out.append({"role": "user", "content": _anthropic_blocks(content)})
            else:
                out.append({"role": "user", "content": content})
        elif role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"],
                               "name": tc["name"], "input": tc.get("arguments") or {}})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            }]})
    return out


def _anthropic_text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _is_truncated(resp) -> bool:
    """这个响应是不是被 max_tokens 截断了（anthropic 侧）。

    `stop_reason == "max_tokens"` 就是「没说完就被上限砍了」。
    取不到这个字段时按「没截断」处理——不能因为 SDK 换了字段名就谎报截断。
    """
    return getattr(resp, "stop_reason", None) == "max_tokens"


# ============================================================
#  连接失败：分类 + **只在「请求没发出去」时**有界重试
# ============================================================
# 2026-10-07 真机（校园网 Wi-Fi 掉线，38 分钟里 5 次提问全废）：
#   12:51:57 Wi-Fi 断开 → 12:52:47 重连 → 12:54:45/50 仍拿不到 DHCP 地址；
#   这 3 分钟里到 api.deepseek.com:443 的 TCP 连接被**立即拒绝**（WinError 10061），
#   而用户收到的是「出错了，看终端日志。」——他是无窗口的后台进程，根本没有终端。
# 所以这里做三件事（每件都有反例钉着，别删）：
#   ① 只在**请求根本没发出去**的失败上重试：域名没解析出来（`gaierror`）、
#      对端直接拒连（`ConnectionRefusedError`）——这两类没到服务端、**不会重复计费**；
#   ② **绝不对读超时 / 连接重置重试**：无法确认模型是否已经生成并计费（项目既定立场：
#      宁可如实说失败，也不冒"重复扣费/重复动作"的险）；
#   ③ 放弃前探一下**别的公网目标**，把「本机整体没网」和「只有这个接口不通」分开说
#      —— 不然又把排查方向指错（和当初那句「看终端日志」一个毛病）。
_LLM_ATTEMPTS = 3
_LLM_RETRY_SLEEP = (2.0, 5.0)
# 「睡醒/掉线后等本机网络回来」的总预算（秒）。2026-10-07 真机：这台笔记本走
# Modern Standby，一天进出几十次，每次醒来 Wi-Fi 都要重新关联 + 续租，那 **30~90 秒**
# 里到模型接口的连接被立即拒绝 —— 用户看到的正是"睡醒后发消息没回复"。
# 7 秒的短重试救不了，所以这里再等一会儿（每 5 秒探一次本机网络）。
# ⚠️ 这段等待发生在 **HTTP 层、请求发出去之前**：既不会重复计费，也不会把上层
# 已经发生的副作用（待确认项、工具调用）重跑一遍。配 0 = 不等（老行为）。
_NET_RECOVER_WAIT_SEC = 90.0
_NET_RECOVER_STEP_SEC = 5.0
# 探测目标用**国内公共 DNS**（TCP:53）：校园网/家宽都通，纯 TCP、不发 HTTP、不花钱。
# 两个都连不上才算「本机整体没网」——单看一个目标，可能只是它自己关了那个端口。
_PROBE_TARGETS = (("223.5.5.5", 53), ("114.114.114.114", 53))
_PROBE_TIMEOUT = 1.5


class LLMUnreachable(RuntimeError):
    """模型接口**连接阶段**失败（请求没发出去）。`kind` 让上层直接说人话，不必嗅探文本。

    `kind`：`net_down`（探针说本机整体没网）/ `refused`（被拒连）/ `dns`（解析失败）/
    `host_down`（其它连接失败，含读超时、TLS 之类）。
    `waited`：为了等网络回来实际等了多久（秒；0 = 没等）。
    """

    def __init__(self, message, kind="host_down", target="", detail="", waited=0.0):
        super().__init__(message)
        self.kind = kind
        self.target = target
        self.detail = detail
        self.waited = float(waited or 0.0)


def _retryable(reason):
    """这个连接错误**是不是发生在请求发出去之前**（只有这种才允许重试）。

    * `socket.gaierror` —— 域名都没解析出来，一个字节都没发；
    * `ConnectionRefusedError` —— 对端直接拒了，也没发。
    其余（`TimeoutError` / `ConnectionResetError` / TLS 错误）**一律不重试**：
    读超时可能是模型已经生成、正在计费；连接重置可能已经把请求送出去过。
    """
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


def network_down(timeout=_PROBE_TIMEOUT):
    """本机对外是不是**整体**不通。纯 TCP 探测，不发 HTTP、不花钱、不碰 hook。

    只有「连接阶段失败」的收尾才调它（探针自己也要建 TCP，别浪费在读超时上）。
    返回 True = 两个独立目标都连不上（那就别把锅甩给模型接口）。
    """
    for host, port in _PROBE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return False          # 有一个通 → 本机网络是好的
        except OSError:
            continue
    return True


def _wait_network_back(seconds=_NET_RECOVER_WAIT_SEC, step=_NET_RECOVER_STEP_SEC):
    """睡醒/掉线后等本机网络回来：每 `step` 秒探一次，最多 `seconds` 秒。

    返回 `(网络是否回来, 实际等了多久秒)`。

    ⚠️ 这段等待**必须**发生在请求发出去之前（`_request` 里就是），这样它既不会重复计费，
    也不会把上层已经发生的副作用重跑一遍。等待期间打印真实进度（排查时一眼能看出来）。
    """
    try:
        total = max(0.0, float(seconds))
        every = max(1.0, float(step))
    except (TypeError, ValueError):
        return False, 0.0
    waited = 0.0
    while waited + every <= total + 1e-6:
        time.sleep(every)
        waited += every
        if not network_down():
            print(f"[llm] ✅ 网络回来了（等了 {waited:.0f} 秒），立刻重试这次请求", flush=True)
            return True, waited
        print(f"[llm] ⏳ 本机还是没网，继续等（已等 {waited:.0f}/{total:.0f} 秒）", flush=True)
    return False, waited


# ============================================================
#  OpenAI 兼容（DeepSeek / Ollama / 中转）
# ============================================================

class _OpenAICompat:
    def __init__(self, model, api_key, base_url, max_tokens, temperature):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_OPENAI_URL).rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature

    def chat(self, system, messages):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        payload = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        body = self._request("/chat/completions", payload)
        # 记账放在解析之前：body 已经拿到了，且**绝不许**让记账失败被下面那个
        # except 当成「返回格式看不懂」误报。
        _rec_openai(self.model, body, "chat")
        try:
            return body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"返回格式看不懂：{str(body)[:200]}")

    def chat_with_tools(self, system, messages, tools):
        msgs = [{"role": "system", "content": system}] if system else []
        msgs.extend(_openai_messages(messages))

        payload = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": self.max_tokens,
            "stream": False,
            "tools": [{"type": "function", "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            }} for t in tools],
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        body = self._post(payload)
        try:
            choice = body["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"返回格式看不懂：{str(body)[:200]}")

        text = msg.get("content") or ""
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(tc.get("id", ""), fn.get("name", ""), args))
        # finish_reason == "length" 即「撞到 max_tokens 上限」。只暴露事实，不拼文案。
        _rec_openai(self.model, body, "tools")
        return ChatResult(text, calls, truncated=(choice.get("finish_reason") == "length"))

    def _request(self, path, payload):
        """**唯一的 HTTP 出口**（`chat` 与 `chat_with_tools` 都走它，别各写一份请求）。

        行为契约（见文件里那一节的长注释）：
          * 连接阶段失败（域名解析不了 / 被拒连）→ 有界重试 `_LLM_ATTEMPTS` 次，间隔 `_LLM_RETRY_SLEEP`；
          * 读超时 / 连接重置 / TLS 错误 → **不重试**，直接如实抛；
          * HTTP 4xx/5xx → **不重试**，原样带 detail 抛（语义明确，重试也白搭）；
          * 最终失败一律抛 `LLMUnreachable`（带 `kind`，让上层说人话）。
        """
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        attempt = 0
        waited_net = False
        while True:
            attempt += 1
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.loads(r.read().decode("utf-8", "ignore"))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "ignore")[:300]
                except Exception:
                    pass
                # HTTP 层错误语义明确：**不重试**（429/401 各有各的处理，别在这里糊）
                raise RuntimeError(f"{self.base_url} 返回 HTTP {e.code}：{detail}") from e
            except (urllib.error.URLError, OSError) as e:
                reason = getattr(e, "reason", e)
                if _retryable(reason) and attempt < _LLM_ATTEMPTS:
                    delay = _LLM_RETRY_SLEEP[min(attempt - 1, len(_LLM_RETRY_SLEEP) - 1)]
                    print(f"[llm] ⚠️ 连接失败（{type(reason).__name__}，请求没发出去），"
                          f"{delay:.0f} 秒后重试（第 {attempt + 1}/{_LLM_ATTEMPTS} 次）", flush=True)
                    time.sleep(delay)
                    continue
                kind = ("dns" if isinstance(reason, socket.gaierror)
                        else "refused" if isinstance(reason, ConnectionRefusedError)
                        else "host_down")
                # 只有"请求没发出去"的失败才值得探本机网络（探针自己也要建 TCP）
                if kind in ("refused", "dns") and network_down():
                    kind = "net_down"
                waited = 0.0
                # 「本机整体没网」（睡醒那几十秒就是这种）→ 等网络回来再试一次。
                # 这段等待在请求发出去之前，所以**不会重复计费、也不会重跑上层副作用**。
                if kind == "net_down" and not waited_net and _NET_RECOVER_WAIT_SEC > 0:
                    waited_net = True
                    back, waited = _wait_network_back()
                    if back:
                        continue
                _err = str(e)[:200]
                print(f"[llm] ❌ 连不上 {self.base_url}（kind={kind}，等了 {waited:.0f} 秒）：{_err}",
                      flush=True)
                raise LLMUnreachable(f"连不上 {self.base_url}：{e}", kind=kind,
                                     target=self.base_url, detail=_err, waited=waited) from e

    def _post(self, payload):
        """`chat_with_tools` 的 HTTP 出口（**自测就是桩这一层**，别再往里加逻辑）。"""
        return self._request("/chat/completions", payload)


def _openai_messages(messages):
    """中立格式 -> OpenAI 的 messages 格式。"""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            item = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                item["tool_calls"] = [{
                    "id": tc["id"], "type": "function",
                    "function": {"name": tc["name"],
                                 "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)},
                } for tc in m["tool_calls"]]
            out.append(item)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""),
                        "content": m.get("content", "")})
    return out
