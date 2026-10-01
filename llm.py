"""调用大模型的轻量封装。

支持两种后端，用 config.yaml 的 provider 选：

  provider: "anthropic"  -> Claude 官方 API（走 anthropic SDK）
  provider: "openai"     -> OpenAI 兼容的 /chat/completions 接口
                            DeepSeek、Ollama、各类中转都走这个

API Key 从 config / 环境变量 ANTHROPIC_API_KEY（或 OPENAI_API_KEY）读取，显式传入优先。
具体用哪家由 provider + base_url + model 三者决定，默认模型会跟着 provider 走。
"""
import json
import os
import urllib.error
import urllib.request

DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"


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

        api_key = api_key or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "缺少 API Key。可在微信里发 /api <key> 设置，"
                "或设环境变量 ANTHROPIC_API_KEY / OPENAI_API_KEY。"
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
    __slots__ = ("text", "tool_calls")

    def __init__(self, text="", tool_calls=None):
        self.text = text or ""
        self.tool_calls = list(tool_calls or [])

    def __repr__(self):
        return f"ChatResult(text={self.text[:40]!r}, tool_calls={[c.name for c in self.tool_calls]})"



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
        if self.temperature is not None:
            try:
                resp = self.client.messages.create(
                    **kwargs, extra_body={"temperature": self.temperature}
                )
                return _anthropic_text(resp)
            except TypeError:
                pass
            except Exception as e:
                if "temperature" not in str(e).lower():
                    raise
        resp = self.client.messages.create(**kwargs)
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
        if self.temperature is not None:
            try:
                resp = self.client.messages.create(
                    **kwargs, extra_body={"temperature": self.temperature})
            except TypeError:
                resp = self.client.messages.create(**kwargs)
            except Exception as e:
                if "temperature" not in str(e).lower():
                    raise
                resp = self.client.messages.create(**kwargs)
        else:
            resp = self.client.messages.create(**kwargs)

        text, calls = "", []
        for b in resp.content:
            bt = getattr(b, "type", "")
            if bt == "text":
                text += b.text
            elif bt == "tool_use":
                calls.append(ToolCall(getattr(b, "id", ""), getattr(b, "name", ""),
                                      getattr(b, "input", None)))
        return ChatResult(text, calls)


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
            msg = body["choices"][0]["message"]
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
        return ChatResult(text, calls)

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
