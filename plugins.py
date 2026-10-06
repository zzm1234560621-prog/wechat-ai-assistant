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
import time

__all__ = [
    "PluginError", "Registry", "REGISTRY", "ScopedRegistrar",
    "CONFIRM_MODES", "EXEC_MODES", "EVENTS", "make_ctx",
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

# 生命周期事件的白名单（规格第三节）。
#
# **在这里集中声明**：拼错事件名必须当场失败。静默注册一个**永远不会被触发**的
# 回调，是查起来最费劲的一种失效 —— 插件作者以为挂上了，实际什么都没发生。
#
# 全部在**收消息那条线程**上同步调用。只有 `before_reply` 能改行为（且只改文本）：
# 路由（这条消息回不回、回给谁、算不算命令）只能有一个所有者。
EVENTS = ("on_start", "on_message", "before_reply", "after_reply", "on_tool", "on_tick")


def make_ctx(chat="", self_wxid="", cfg=None, from_self=None, is_group=None,
             user_query="", **extra):
    """构造工具处理器与生命周期事件共用的**只读**上下文（规格 2.1）。

    **ctx 的形状只有这一处定义** —— `ToolBox.ctx()` 也走它，免得两边各写一份、
    慢慢长歪（这个项目为「两处各写一份」已经付过好几次代价）。

    `from_self` 默认 `None` = **「不知道」**，绝不许当成 True：
    权限类判断（文件能力的 `files.who`）靠它决定放不放行，
    把未知当成 True 等于**静默放宽权限**。
    """
    if is_group is None:
        is_group = "@chatroom" in str(chat or "")
    ctx = {
        "chat": str(chat or ""),
        "self_wxid": str(self_wxid or ""),
        "cfg": cfg if cfg is not None else {},
        "from_self": from_self,
        "is_group": bool(is_group),
        "user_query": str(user_query or ""),
    }
    ctx.update(extra)
    return ctx


def event_limits(cfg):
    """事件的时间预算，返回 `(slow_ms, disable_after)`。

    `disable_after = 0` = **不自动停用**（只告警）。
    """
    p = (cfg or {}).get("plugins")
    if not isinstance(p, dict):
        p = {}

    def _int(key, dflt, lo):
        try:
            v = int(p.get(key, dflt))
        except (TypeError, ValueError):
            return dflt
        return max(lo, v)

    return _int("slow_ms", 500, 1), _int("disable_after", 5, 0)



class Registry:
    """工具注册表。一个进程一个实例（模块级的 `REGISTRY`）。"""

    def __init__(self, log=None):
        self._tools = {}        # name -> 规范化后的 spec
        self._order = []        # 注册顺序（**必须保住**：模型看到的清单顺序要稳定）
        # 生命周期事件：{事件名: [(source, fn), ...]}，按注册顺序调用。
        self._events = {e: [] for e in EVENTS}
        # 事件耗时记账：source -> **连续**超时次数（中间有一次快就清零）。
        self._slow = {}
        # 待确认种类：{kind: {describe, apply, key_fields, source}}。
        self._kinds = {}
        # 被自动停用的插件。**不落盘**：只在本次运行生效，改好代码重启即恢复。
        self._disabled = set()
        # 日志出口。加载器会把它换成 bot 的 print。
        self.log = log or print

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

    # ------------------------------------------------------------ 模型指导

    def guidance_text(self):
        """把各工具自带的 `guidance` 拼成一段给模型看的文字。

        **这就是 `guidance` 存在的全部意义** —— 只把它存进注册表、不送出去，
        等于**装作处理了**那个老 bug：2026-10-02 `send_asset` 的 8 行指导只加进了
        本机 `config.yaml`，`config.example.yaml` 一个字都没有，于是开发机上好用、
        发布包里静默失效。存了不送**比不存更坏**：看代码的人会以为这条路是通的。

        内置工具的 `guidance` 是空的（它们的指导在两份 config 的 `system_prompt` 里，
        那是既有设计、用户能自己改措辞）；这段文字只装**自带指导的工具**
        （插件 + 走契约注册的核心模块，如 `files.py`）。
        """
        lines = [f"【{n}】{self._tools[n]['guidance']}"
                 for n in self._order if self._tools[n]["guidance"]]
        if not lines:
            return ""
        return ("\n\n# 工具用法（由工具定义自带，别手改这一节）\n" + "\n".join(lines))

    def inject_guidance(self, system):
        """把 `guidance_text()` 接到系统提示后面。没有自带指导时**逐字不变**。

        调用方（`bot.system_now`）会把**真实时间**再拼到最后 —— 时间必须留在
        末尾（既有回归钉着「拼在系统提示末尾，原文一个字不动」），而且它是最
        容易过期的一条，放最后最不容易被忽略。
        """
        base = system if isinstance(system, str) else ""
        g = self.guidance_text()
        return (base + g) if g else base

    def rollback_source(self, source):
        """把某个 `source` 注册的东西**全部**撤掉（工具 + 事件 + 待确认种类），
        返回被撤掉的工具名。

        **给加载器做「半加载」回滚、也给自动停用用**：插件在 `setup` 里注册了
        工具 A、事件 B、种类 C，然后抛错 —— 只要留一样，这个插件就是**半加载**
        状态（模型看得到工具、但事件没生效，或者反过来）。半加载最难查
        （「怎么有个工具，但又不好使」），所以宁可不加载、一次撤干净。

        只能撤**自己注册的那一份**（按 `source` 精确匹配）—— 绝不能顺手把别人的
        也清掉，那样 bug 会以「另一个插件莫名消失」的形式出现。
        """
        gone = [n for n in self._order if self._tools[n]["source"] == source]
        for n in gone:
            self._order.remove(n)
            self._tools.pop(n, None)
        for ev in list(self._events):
            self._events[ev] = [x for x in self._events[ev] if x[0] != source]
        for k in [k for k, v in self._kinds.items() if v["source"] == source]:
            self._kinds.pop(k, None)
        return gone

    # ------------------------------------------------------------ 事件
    # 五条硬规矩（规格 3.1），每条都有原因：
    #   1. 事件抛异常**绝不打断消息循环** —— 插件炸了不能把收微信带下去；
    #   2. 插件跑在轮询线程上，它自己的线程**绝不许碰 hook**（契约层面约束，
    #      代码拦不住，所以写在文档和注释里，并由「慢就停用」兜一句）；
    #   3. `before_reply` 的返回值仍要过 `bot.send` 的既有校验；返回 `None` 或
    #      非字符串 = **不改**（fail-safe）；
    #   4. 单事件必须快。**Python 中断不了同步调用**，所以「超时杀掉」做不到，
    #      不许假装做得到 —— 实际做法是测耗时、超阈值告警、连续超 N 次**自动停用**；
    #   5. 插件是配置项，必须能一键关（`plugins.enabled` / `plugins.disabled`）。

    def register_event(self, name, fn, source="plugin"):
        """挂一个生命周期事件。事件名不认识就**当场失败**（见 `EVENTS` 的注释）。"""
        ev = str(name or "").strip()
        if ev not in EVENTS:
            raise PluginError(
                f"[{source}] 不认识的事件「{name}」——只认 {list(EVENTS)}。"
                f"拼错事件名必须当场失败：静默注册一个**永远不会被触发**的回调，"
                f"是最难查的一种失效（作者以为挂上了，其实什么都没发生）")
        if not callable(fn):
            raise PluginError(f"[{source}] 事件 {ev} 的回调不是可调用的")
        self._events[ev].append((source, fn))
        return fn

    def emit(self, event, *args, cfg=None):
        """触发一个**观察类**事件，返回 `[(source, 结果), ...]`。

        异常与耗时都在 `_call` 里兜住。调用方**不看返回值也能用**；
        唯一的例外是 `before_reply`，它有专用入口（要串联、要 fail-safe）。
        """
        return [(src, self._call(src, event, fn, *args, cfg=cfg))
                for src, fn in list(self._events.get(event, ()))]

    def before_reply(self, text, ctx=None, cfg=None):
        """跑 `before_reply` 链，返回最终文本。

        fail-safe：回调返回 `None` 或**非字符串**一律当「不改」——
        写坏了只等于不生效，**绝不等于把回复吞掉**。
        多个插件按注册顺序**串联**（后一个看到前一个的结果）。

        ⚠️ 调用点只许是**模型答复**那一处。确认菜单 / 群发预览是
        `bot` **原样直发**的（用户照着它回「确认」），插件改写它等于把确认闸做废。
        """
        out = text
        for src, fn in list(self._events.get("before_reply", ())):
            r = self._call(src, "before_reply", fn, out, ctx, cfg=cfg)
            if isinstance(r, str):
                out = r
        return out

    def _call(self, source, event, fn, *args, cfg=None):
        if source in self._disabled:
            return None
        t0 = time.monotonic()
        try:
            return fn(*args)
        except Exception as e:
            self.log(f"[plugins] ⚠️ 插件「{source}」的 {event} 抛异常，已忽略并继续"
                     f"（插件炸了绝不能把消息循环带下去）：{type(e).__name__}: {e}")
            return None
        finally:
            self._note_ms(source, event, (time.monotonic() - t0) * 1000.0, cfg)

    def _note_ms(self, source, event, ms, cfg):
        slow_ms, limit = event_limits(cfg)
        if ms <= slow_ms:
            self._slow[source] = 0          # 「连续」：中间有一次快就重新计
            return
        n = self._slow.get(source, 0) + 1
        self._slow[source] = n
        self.log(f"[plugins] ⚠️ 插件「{source}」的 {event} 花了 {ms:.0f}ms"
                 f"（阈值 {slow_ms}ms，连续第 {n} 次）—— 事件跑在**收消息那条线程**上，"
                 f"它慢就是在卡收微信。")
        if limit and n >= limit:
            self.auto_disable(source)

    def auto_disable(self, source, why="连续超时"):
        """自动停用某个插件：撤掉它的工具 / 事件 / 待确认种类，并**明说是谁、为什么**。

        **不假装能超时中断**（Python 中断不了同步调用）。停用是唯一能真正
        止血的动作，代价是这个插件的功能没了 —— 所以必须说清楚，
        而且只在本次运行生效（改好重启即恢复）。
        """
        if source in self._disabled:
            return []
        self._disabled.add(source)
        n_ev = sum(1 for lst in self._events.values() for x in lst if x[0] == source)
        n_kind = sum(1 for v in self._kinds.values() if v["source"] == source)
        gone = self.rollback_source(source)
        self.log(f"[plugins] ❌ 插件「{source}」{why}，**已自动停用**："
                 f"撤掉 {len(gone)} 个工具、{n_ev} 个事件、{n_kind} 个待确认种类。"
                 f"停用只在本次运行生效 —— 改好代码重启就恢复。")
        return gone

    def disabled_plugins(self):
        """被自动停用的插件名（给自测与状态页用）。"""
        return sorted(self._disabled)

    # ------------------------------------------------------------ 待确认种类
    #
    # 插件与核心共用**同一条** `set_pending` 队列 —— 插件工具绝不许绕开确认闸，
    # 所以这里注册的不是「另一条队列」，而是「这一类动作怎么描述、怎么执行、
    # 以及它的**身份**由哪几个字段决定」。

    def register_pending_kind(self, kind, describe_fn, apply_fn, key_fields=None,
                              source="plugin"):
        """注册一个待确认种类。

        * `describe_fn(item) -> str` —— 给编号菜单用的一行人类描述。
          ⚠️ **必须原样展示要执行的内容**（同 `shell` 显示命令原文那条规矩）：
          中间任何转述/改写都等于把确认闸做废。
        * `apply_fn(item, ctx) -> (真正执行了几条, 错误)` —— 用户回「确认」后执行。
          `ctx` 是 `{"cfg": ..., "client": ...}`。
        * `key_fields=[...]` —— **该 kind 的判重字段（必填）**，见下。
        """
        k = str(kind or "").strip()
        if not k:
            raise PluginError(f"[{source}] 待确认种类缺 kind 名")
        if k in self._kinds:
            raise PluginError(
                f"[{source}] 待确认种类「{k}」和 {self._kinds[k]['source']} 撞了 —— "
                f"撞了会让其中一类的动作被另一类的描述/执行器处理")
        if not callable(describe_fn):
            raise PluginError(f"[{source}] 待确认种类「{k}」的 describe_fn 不是可调用的")
        if not callable(apply_fn):
            raise PluginError(f"[{source}] 待确认种类「{k}」的 apply_fn 不是可调用的")

        kf = list(key_fields or [])
        if not kf or not all(isinstance(x, str) and x.strip() for x in kf):
            # ⚠️ 这一条是**强制项**，不是优化。看不到后果就不会有人守它：
            #    「删掉 A」和「删掉 B」两条 `fileop`，kind 相同、其余字段全空
            #    → 判重键**完全一样** → 第二条被当成「和上面那条一模一样」而不登记，
            #    用户照菜单回「确认」——**做掉的是另一件事**。删除是不可逆的。
            #    所以不给 key_fields 的种类**加载期就失败**，不许静默套用旧元组。
            raise PluginError(
                f"[{source}] 待确认种类「{k}」必须声明 key_fields（判重字段）。"
                f"不给就等于两条**不同的动作**被判成同一条 —— 用户照菜单回「确认」，"
                f"做掉的是另一件事（见 docs/plugin-contract-spec.md 4.2）")

        self._kinds[k] = {
            "kind": k, "describe": describe_fn, "apply": apply_fn,
            "key_fields": [x.strip() for x in kf], "source": source,
        }
        return self._kinds[k]

    def pending_kind(self, kind):
        """取一个待确认种类的 spec（没注册则 None）。"""
        return self._kinds.get(str(kind or ""))

    def pending_key_fields(self, kind):
        """取一个待确认种类的判重字段（没注册则 None）。"""
        spec = self.pending_kind(kind)
        return list(spec["key_fields"]) if spec else None

    def pending_kinds(self):
        """已注册的待确认种类名（按注册顺序）。"""
        return list(self._kinds)

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


# 插件加载（规格 2.3）

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

    def register_event(self, name, fn):
        """挂生命周期事件（`EVENTS` 白名单）。"""
        return self._reg.register_event(name, fn, source=self.source)

    def register_pending_kind(self, kind, describe_fn, apply_fn, key_fields=None):
        """注册待确认种类（走核心**同一条**确认队列，没有例外通道）。"""
        return self._reg.register_pending_kind(kind, describe_fn, apply_fn,
                                               key_fields=key_fields,
                                               source=self.source)


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
    reg.log = log
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

