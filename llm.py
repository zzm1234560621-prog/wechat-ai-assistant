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


def _anthropic_messages(messages):
    """中立格式 -> Anthropic 的 content block 格式。"""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content", "")})
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

        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            raise RuntimeError(f"{self.base_url} 返回 HTTP {e.code}：{detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"连不上 {self.base_url}：{e}") from e

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

    def _post(self, payload):
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            raise RuntimeError(f"{self.base_url} 返回 HTTP {e.code}：{detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"连不上 {self.base_url}：{e}") from e


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
