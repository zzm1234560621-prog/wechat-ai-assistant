"""插件契约：工具注册表 + 生命周期事件 + 待确认 kind 注册。

**规格（权威）**：`docs/plugin-contract-spec.md`。本文件只实现契约。

## 这是什么

让「加一个新功能」从**改 4 处**（`agent_tools.TOOLS` + `t_*` 处理器 + 两份 config 的
`system_prompt`）、跨 2 个文件 2 份配置，变成**往 `plugins/` 放一个文件**。

## 为什么值得存在（不是推测）

2026-10-02 `send_asset` 的 8 行指导和整个 `assets:` 段**只加进了本机 `config.yaml`**，
`config.example.yaml` 里一个字都没有 —— 后果不是报错，而是**开发机上好用、
发布包里静默失效**（模型从 `TOOLS` 看得到工具，却拿不到「什么时候该调它」的判据）。
`selftest_tool_registry.py` 就是为堵那个洞写的。

本契约把「名字 + 参数 schema + 处理器 + **模型指导文本**」收进**同一个文件**，
让那一类失效对插件工具**结构上不可能**再发生。

## 唯一真源

`REGISTRY` 是工具**声明与派发**的唯一真源。`agent_tools.TOOLS` 降级为
**内置工具的声明输入**（注册表在 `agent_tools` 导入时读它），不再是运行期真源。
**不许**再出现「直接读 `TOOLS` 派发」的第二条路径 —— 那就是第二个所有者。

## 依赖方向（**不许反过来**）

本模块**谁也不 import**（纯注册表）。`agent_tools` / `files` **import 它**，
并在各自模块底部自注册。注册表去 import 工具层/文件层会**立刻**变成循环依赖。
"""
__all__ = [
    "PluginError", "Registry", "REGISTRY",
    "CONFIRM_MODES", "EXEC_MODES",
]


class PluginError(Exception):
    """契约违规。**加载时必须响亮地失败**，绝不许静默跳过或静默覆盖。

    为什么是异常而不是返回错误：插件出问题要么是语法写错、要么是名字撞了、
    要么是声明了做不到的东西。三种都必须在**加载那一刻**让用户看见，
    而不是等哪天模型调到它才发现（「静默失效」是本项目最大的坑）。
    """


# 确认策略。`auto` = 沿用注册方自己的确认策略；`always` = 一律先登记待确认。
CONFIRM_MODES = ("auto", "always")

# 执行模式。`inline` = 就地跑在收消息那条线程上。
#
# ⚠️ `worker`（丢给后台线程）**本轮未实现**：还没有任何连接器落地，
# 不为没有消费者的东西先写执行路径（YAGNI）。但**字段必须留着**，
# 因为它是给未来那个人的**安全信号**：没有它，连接器作者会自然而然地在
# 轮询线程上写网络 I/O，直接把收微信卡死。现在声明 `worker` 会**加载即失败**，
# 而不是被静默当成 `inline` 跑（那等于一边阻塞微信一边声称自己没阻塞）。
EXEC_MODES = ("inline", "worker")


class Registry:
    """工具注册表。一个进程一个实例（模块级的 `REGISTRY`）。"""

    def __init__(self):
        self._tools = {}        # name -> 规范化后的 spec
        self._order = []        # 注册顺序（**必须保住**：模型看到的清单顺序要稳定）

    # ------------------------------------------------------------ 注册

    def register_tool(self, spec, source="plugin"):
        """注册一个工具。`source` 是 `"builtin"` 或插件名。

        契约违规一律抛 `PluginError`（见该类注释）。特别是**重名**：
        后者静默覆盖前者，等于其中一个永远调不到、而且没人会发现。
        """
        if not isinstance(spec, dict):
            raise PluginError(
                f"[{source}] 工具定义必须是 dict，给的是 {type(spec).__name__}")

        name = str(spec.get("name") or "").strip()
        if not name:
            raise PluginError(f"[{source}] 工具缺 name")

        if name in self._tools:
            prev = self._tools[name]["source"]
            raise PluginError(
                f"[{source}] 工具名「{name}」和 {prev} 撞了 —— "
                f"重名会让其中一个永远调不到，所以加载即失败，不做静默覆盖")

        desc = str(spec.get("description") or "").strip()
        if not desc:
            raise PluginError(f"[{source}] 工具「{name}」缺 description（模型要靠它判断何时调用）")

        params = spec.get("parameters")
        if not isinstance(params, dict) or not params:
            raise PluginError(
                f"[{source}] 工具「{name}」缺 parameters（JSON Schema，形状与 "
                f"agent_tools.TOOLS 里那条完全同形）")

        handler = spec.get("handler")
        method = str(spec.get("method") or "").strip()
        if handler is not None and method:
            raise PluginError(
                f"[{source}] 工具「{name}」同时给了 handler 和 method —— 二选一")
        if handler is None and not method:
            raise PluginError(
                f"[{source}] 工具「{name}」既没给 handler（插件）也没给 method（内置）")
        if handler is not None and not callable(handler):
            raise PluginError(f"[{source}] 工具「{name}」的 handler 不是可调用的")

        # 「模型指导文本」随定义走。
        #
        # 内置工具**可以留空**：它们的指导在两份 config 的 system_prompt 里，
        # 那是既有设计（而且用户可以自己改措辞），由 selftest_tool_registry.py 守着。
        # 插件工具**必填**：第三方插件的名字不可能预先写在 config.example.yaml 里，
        # 指导不随定义走就没地方去了 —— 那正是 send_asset 那次失效的形状。
        guidance = str(spec.get("guidance") or "").strip()
        if not guidance and source != "builtin":
            raise PluginError(
                f"[{source}] 工具「{name}」缺 guidance —— 插件必须自带模型用法指导，"
                f"否则模型看得到工具、不知道怎么用（就是 send_asset 那次的失效形状）")

        confirm = str(spec.get("confirm") or "auto").strip() or "auto"
        if confirm not in CONFIRM_MODES:
            raise PluginError(
                f"[{source}] 工具「{name}」的 confirm={confirm!r} 不认识，"
                f"只认 {CONFIRM_MODES}")

        mode = str(spec.get("mode") or "inline").strip() or "inline"
        if mode not in EXEC_MODES:
            raise PluginError(
                f"[{source}] 工具「{name}」的 mode={mode!r} 不认识，只认 {EXEC_MODES}")
        if mode == "worker":
            raise PluginError(
                f"[{source}] 工具「{name}」声明了 mode=\"worker\"，但 worker 执行模式"
                f"**本轮未实现**（没有连接器落地，不先写没人用的执行路径）。"
                f"绝不许静默按 inline 跑 —— 那等于一边阻塞微信一边声称没阻塞。"
                f"等第一个连接器要落地时再照 read_worker 的样板实现它。")

        self._order.append(name)
        self._tools[name] = {
            "name": name,
            "description": desc,
            "parameters": params,
            "handler": handler,
            "method": method or None,
            "guidance": guidance,
            "confirm": confirm,
            "mode": mode,
            "source": source,
        }
        return self._tools[name]

    # ------------------------------------------------------------ 查询

    def get(self, name):
        """按名字取规范化后的 spec（没有则 None）。"""
        return self._tools.get(str(name or ""))

    def has(self, name):
        return str(name or "") in self._tools

    def names(self):
        """按注册顺序返回全部工具名。"""
        return list(self._order)

    def tools(self):
        """给模型看的工具清单。

        形状与 `agent_tools.TOOLS` 里那条**逐条同形**（`name`/`description`/
        `parameters`）—— 这不是巧合：那个形状恰好就是 MCP 的 tool 形状，
        照抄它以后接 MCP 就是零翻译。
        """
        return [{"name": self._tools[n]["name"],
                 "description": self._tools[n]["description"],
                 "parameters": self._tools[n]["parameters"]}
                for n in self._order]

    def builtin_names(self):
        """内置工具的名字（按注册顺序）。给自测做「零行为变化」比对用。"""
        return [n for n in self._order if self._tools[n]["source"] == "builtin"]

    # ------------------------------------------------------------ 派发

    def resolve(self, name, box):
        """把一个工具名解析成**可调用对象**，解析不到返回 None。

        **只有这一条派发路径**：插件工具 → `spec.handler(args, ctx)`；
        内置工具 → `getattr(box, spec.method)(args)`。
        不留「查不到就 `getattr(box, 't_'+name)`」的兜底 —— 留了就等于
        两套派发、第二个所有者（规格第七节第 8 条）。
        """
        spec = self.get(name)
        if spec is None:
            return None
        if spec["handler"] is not None:
            fn = spec["handler"]
            return lambda args: fn(args, box.ctx())
        return getattr(box, spec["method"], None)


# 进程级的唯一注册表。`agent_tools` / `files` 在模块底部往它注册。
REGISTRY = Registry()


def load_builtin_tools(tools, register_source="builtin"):
    """把一份 `TOOLS` 形状的清单灌进 `REGISTRY`。

    单独抽成函数是为了让 `agent_tools` 的调用点只有一行、也让自测能拿
    临时清单反复演练（重名 / 缺字段 / `worker` 那些分支）。

    ⚠️ **内置工具存的是方法名**（`method`）而不是绑定好的函数：
    处理器要用到 `self.client` / `self.cfg` / `self.contacts`，
    只能在 `ToolBox` 实例上解析。
    """
    out = []
    for t in (tools or []):
        name = str((t or {}).get("name") or "").strip()
        out.append(REGISTRY.register_tool({
            "name": name,
            "description": (t or {}).get("description"),
            "parameters": (t or {}).get("parameters"),
            "method": "t_" + name if name else "",
        }, source=register_source))
    return out
