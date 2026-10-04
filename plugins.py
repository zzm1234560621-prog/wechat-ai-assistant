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
import os

__all__ = [
    "PluginError", "Registry", "REGISTRY", "ScopedRegistrar",
    "CONFIRM_MODES", "EXEC_MODES",
    "plugins_dir", "load_dir", "enabled", "disabled_names",
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

    def rollback_source(self, source):
        """把某个 `source` 注册的东西全部撤掉，返回撤掉的工具名。

        **给加载器做「半加载」回滚用**：插件在 `setup` 里注册了工具 A、
        再在 B 上抛错，如果留着 A，那这个插件就是**半加载**状态 ——
        它的工具模型看得到，但它的其它东西（事件、待确认 kind）没生效。
        这种状态最难查（「怎么有个工具，但又不好使」），所以宁可不加载。

        工具注册表是**唯一真源**，回滚也只能回滚自己注册的那一份 ——
        绝不能顺手把别人的也清掉，所以按 `source` 精确匹配。
        """
        gone = [n for n in self._order if self._tools[n]["source"] == source]
        for n in gone:
            self._order.remove(n)
            self._tools.pop(n, None)
        return gone

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


# ────────────────────────────────────────────────────────────────────────
# 插件加载（规格 2.3）
# ────────────────────────────────────────────────────────────────────────

PLUGIN_ENTRY = "setup"
PLUGINS_DIRNAME = "plugins"


class ScopedRegistrar:
    """交给插件用的注册视图：**把 source 钉成插件名**。

    为什么不让插件自己填：插件名是**加载器**从文件名知道的，让插件自己写就会写错
    或撞名；而 `source` 是「回滚半加载插件」的唯一依据（见 `rollback_source`）。
    插件拿到的这个对象只有注册方法，没有任何回滚/查询能力。
    """

    def __init__(self, registry, source):
        self._reg = registry
        self.source = source

    def register_tool(self, spec, **kw):
        kw.pop("source", None)          # 插件说了不算：source 由加载器定
        return self._reg.register_tool(spec, source=self.source)


def plugins_dir(base=None):
    """插件目录：`<repo>/plugins`。"""
    base = base or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, PLUGINS_DIRNAME)


def _switch_on(cfg, log):
    """`plugins.enabled` 是不是开着。

    判据：**只有显式的 `false` 才算关**；缺省 = 开；其它值（`"false"` 字符串、
    `0`、整个段写成标量）告警并**当开**。

    为什么和 `redact` 那条（严格 `is True`，写 `"true"` 一律当关）**反着来**：
    那边判错的安全方向是「关」；这边一旦判错成「关」，用户放进 `plugins/` 的插件会
    **静默不加载** —— 那正是本项目最忌讳的失效。宁可多加载，也要让它**说出来**。
    """
    p = (cfg or {}).get("plugins")
    if p is None:
        return True
    if not isinstance(p, dict):
        log(f"[plugins] ⚠️ config 的 plugins 段不是表（是 {type(p).__name__}），"
            f"已按默认值当**开**处理。")
        return True
    v = p.get("enabled", True)
    if v is False:
        return False
    if v is not True:
        log(f"[plugins] ⚠️ plugins.enabled={v!r} 不是布尔值，已当**开**处理"
            f"（写成字符串 \"false\" 关不掉插件）。要关就写 enabled: false")
    return True


def disabled_names(cfg):
    """`plugins.disabled: [名字]` —— 单个关掉。

    非字符串项直接跳过（YAML 写歪了只能变成「没关掉」，不能变成「关了什么」）。
    """
    p = (cfg or {}).get("plugins")
    if not isinstance(p, dict):
        return []
    raw = p.get("disabled") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if isinstance(x, str) and x.strip()]


def _import_file(path, mod_name):
    """按文件路径把插件导入成模块。

    模块名带 `_wechat_plugin_` 前缀，避免和真实模块撞名。

    **不把插件目录加进 `sys.path`**：那会让插件里的文件名有机会遮蔽标准库
    ——一个叫 `json.py` 的插件能悄悄换掉全进程的 json。插件是**单文件**；
    要复用代码就用 `_` 前缀的辅助文件并自己 importlib 加载。
    """
    import importlib.util
    key = f"_wechat_plugin_{mod_name}"
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"这个文件没法当模块加载：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_dir(directory=None, cfg=None, log=print, registry=None):
    """扫插件目录并加载。**永不抛异常**，永远返回一份报告 dict。

    报告形状：`{"dir", "loaded": [名], "skipped": [(名, 原因)], "failed": [(名, 原因)]}`

    规矩（规格 2.3，每条都有原因）：

    * 只认 `*.py`，**跳过 `_` 开头**（`_example.py` 是模板，不加载）；
    * **不用 `entry_points`**：这个包是 zip + `.bat` 发的，没有 `pip install`
      这一步，entry_points 永远不会被触发（写了等于静默失效）；
    * **加载失败只告警并跳过，绝不拦住 bot 启动** —— 同 `health` / `status_page` /
      坏掉的 `state.json` 那条规矩：一个写坏的插件不该让整台助手起不来；
    * 失败的插件**整份回滚**（见 `Registry.rollback_source`），不留半加载状态；
    * **逐条打印**加载结果 —— 往目录里丢个文件就能让代码跑起来，这件事不能是暗的。
    """
    reg = registry if registry is not None else REGISTRY
    rep = {"dir": "", "loaded": [], "skipped": [], "failed": []}

    if not _switch_on(cfg, log):
        rep["skipped"].append(("(全部)", "plugins.enabled 是显式的 false"))
        log("[plugins] 插件已按配置关闭（plugins.enabled: false）。")
        return rep

    d = directory or plugins_dir()
    rep["dir"] = d
    if not os.path.isdir(d):
        return rep                 # 目录不存在 = 一份插件都没有，这是正常状态

    off = set(disabled_names(cfg))
    try:
        entries = sorted(os.listdir(d))
    except OSError as e:
        log(f"[plugins] ⚠️ 插件目录读不出来（{e}），跳过。")
        rep["failed"].append(("(目录)", f"{type(e).__name__}: {e}"))
        return rep

    for fn in entries:
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        name = fn[:-3]
        if name in off:
            rep["skipped"].append((name, "在 plugins.disabled 名单里"))
            continue
        try:
            mod = _import_file(os.path.join(d, fn), name)
            entry = getattr(mod, PLUGIN_ENTRY, None)
            if not callable(entry):
                raise PluginError(
                    f"插件必须定义 {PLUGIN_ENTRY}(reg) 作为入口（这个文件里没有）")
            entry(ScopedRegistrar(reg, name))
        except Exception as e:
            gone = reg.rollback_source(name)      # 半加载最难查，整份撤掉
            why = f"{type(e).__name__}: {e}"
            if gone:
                why += f"（已回滚它注册的 {len(gone)} 个工具：{'、'.join(gone)}）"
            rep["failed"].append((name, why))
            log(f"[plugins] ❌ 插件「{name}」加载失败，已跳过并回滚：{why}")
        else:
            rep["loaded"].append(name)

    for name, why in rep["skipped"]:
        log(f"[plugins] ⏭  跳过「{name}」：{why}")
    if rep["loaded"]:
        log(f"[plugins] 已加载 {len(rep['loaded'])} 个插件：" + "、".join(rep["loaded"]))
    elif not rep["failed"] and not rep["skipped"]:
        log(f"[plugins] 插件目录是空的（{d}），没有插件。")
    return rep

