# 插件契约（程序内扩展点）规格

> 状态：设计已与用户确认（2026-10-04，方案 A）。
> 本文是**程序内接口**的权威。**程序外**的文件能力见 `docs/computer-files-spec.md`
> —— 那是本契约的**第一个消费者**，它落地同时就是这份契约的验证。
> 用户口径：MCP / 连接编程软件等**本轮只钉契约、不写实现**。

## 一、目标与非目标

**目标**：把「加一个新功能」从改 **4 处**（`agent_tools.TOOLS` + `t_*` 处理器 + 两份 config 的
`system_prompt`）、跨 2 个文件 2 份配置，变成**往 `plugins/` 放一个文件**；
并给外部软件接入（MCP server、IDE 桥）留一层稳定契约。

**根因（不是推测）**：2026-10-02 `send_asset` 的 8 行指导只加进了本机 `config.yaml`，
`config.example.yaml` 里一个字都没有，连 `assets:` 段都没加 —— 开发机上好用，
**发布包里静默失效**。`selftest_tool_registry.py` 就是为堵它写的（该文件头部记着全过程）。
本契约把「名字 + 参数 schema + 处理器 + 模型指导文本」**收进同一个文件**，
让那一类失效对插件工具**结构上不可能**再发生。

**非目标（明确不做，别顺手扩）**：

- ❌ **不迁移内置 28 个工具**到新写法。`agent_tools.py` 有 5068 行，大重构的风险远大于收益；
  内置工具通过**适配层**注册进同一张表，对模型与用户行为零变化。
- ❌ **不写任何 MCP / IDE 实现**。
- ❌ **不让插件改消息路由**。唯一能改行为的事件是 `before_reply`，且只改文本。
- ❌ **不让插件绕过确认闸**。插件工具走**同一条** `set_pending` 队列，没有例外通道。
- ❌ 不做插件市场 / 热重载 / 版本管理。**重启才生效**，和本项目的其他配置一致。

## 二、注册表：唯一真源

新模块 `plugins.py` 提供 `Registry`，三个注册面：

### 2.1 `register_tool(spec)`

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 工具名。**不得与内置或其他插件重名**——重名在**加载时**失败并明说是谁和谁撞了 |
| `description` | ✅ | 给模型看的说明。同 `TOOLS` 那条的口径 |
| `parameters` | ✅ | JSON Schema。**与 `agent_tools.TOOLS` 里那条完全同形**，不发明新格式 |
| `handler` | ✅ | `handler(args, ctx) -> str`。返回文本直接进工具结果 |
| `guidance` | ✅ | **给模型的用法指导**，随定义一起走（这是根治那个老 bug 的那一条） |
| `confirm` | ❌ | `"auto"`（默认，沿用注册方的确认策略）/ `"always"`（必须走确认闸） |
| `mode` | ❌ | `"inline"`（默认，就地跑在轮询线程上）/ `"worker"`（交给后台线程，见第五节）。⚠️ **`worker` 本轮未实现**：声明它就**加载失败并明说**，**绝不静默按 `inline` 跑**（那等于一边阻塞微信一边声称自己没阻塞） |

**为什么 `parameters` 不发明新格式**：现有 `TOOLS` 的 `{name, description, parameters(JSON Schema)}`
**恰好就是 MCP 的 tool 形状**。照抄它，以后接 MCP 就是零翻译；另发明一套等于先把以后的路堵上。

**`ctx` 是什么**（工具处理器和事件共用同一个形状，只是填多少不同）：

```python
ctx = {
    "chat": "...",        # 这一轮服务于哪个会话（ToolBox 构造时就定下）
    "self_wxid": "...",
    "cfg": {...},         # 生效后的配置
    "from_self": True,    # 触发这一轮的消息是不是我自己发的（bot 主循环给的**事实**）
    "is_group": False,
    "user_query": "...",  # 用户这一轮的原话
}
```

`ctx` **只读**。处理器不许改 `ctx` 来影响别的处理器或主循环 —— 要改行为就 `set_pending`
（走确认闸）或用 `before_reply`。

### 2.2 内置工具怎么进这张表

`agent_tools.py` 在模块加载末尾，把现有 `TOOLS` 逐条 + `ToolBox` 上对应的 `t_*` 方法，
通过适配层灌进 `Registry`，`source="builtin"`。随后：

- 内置工具在注册表里存的是**方法名**（`method="t_send_text"`），不是绑定好的函数
  —— 处理器要用到 `self.client` / `self.cfg` / `self.contacts`，只能在 `ToolBox` 实例上解析。
- `bot.run_agent` 里 `llm.chat_with_tools(system, call_messages, agent_tools.TOOLS)`
  改为 `plugins.REGISTRY.tools()`。
- `ToolBox.run(name, args)` 的 `getattr(self, f"t_{name}")` 改为**查注册表**：
  插件工具 → `spec.handler(args, ctx)`，内置工具 → `getattr(self, spec.method)(args)`。
  **只有这一条派发路径，不留「查不到就 getattr」的兜底** —— 留了就等于两套派发、
  第二个所有者（见第七节第 8 条）。

**注册时机（说清楚，别靠猜）**：

- `agent_tools` / `files` 在**模块导入时自注册**（模块底部调 `REGISTRY.register_tool(...)`）。
  这是有意的：注册表本身**不 import** `agent_tools` 与 `files`（避免循环依赖），
  谁定义谁注册。
- `plugins/` 目录在 `bot.main()` 里、**进轮询循环之前**扫一次（插件可能在 `on_start` 里
  注册更多工具，比如 MCP 的 `tools/list`）。
- 运行期**不重载**插件（改插件要重启，和本项目其他配置一致）。

**判据**：`plugins.enabled: false` 且 `plugins/` 为空时，注册表联合视图必须与今天的
`agent_tools.TOOLS` **逐条相等**（含顺序）。这是「零行为变化」的可验证定义。

### 2.3 加载规则

**插件长什么样**（一个文件就是全部）：

```python
# plugins/hello.py
def setup(reg):
    reg.register_tool({...})                       # 工具（见 2.1）
    reg.register_event("after_reply", my_fn)       # 生命周期事件（见第三节）
    reg.register_pending_kind(...)                 # 待确认种类（见第四节）
```

- 入口**必须**叫 `setup(reg)`；没有它就**加载失败并说清**（不是静默跳过）。
- `reg` 是一个**限定视图**（`ScopedRegistrar`）：只能注册，**不能查询或回滚**，
  而且 `source` 由加载器按**文件名**钉死 —— 插件自己填的不算数（回滚要靠它精确匹配）。
- 插件是**单文件**。**不把插件目录加进 `sys.path`**：那会让插件里的文件名有机会
  遮蔽标准库（一个叫 `json.py` 的插件能悄悄换掉全进程的 json）。
  要复用代码就用 `_` 前缀的辅助文件、自己 `importlib` 加载。
- **注册到一半失败 → 整份回滚**（`Registry.rollback_source`）。
  半加载的插件（工具进去了、事件没挂上）是最难查的一种状态：
  模型看得到工具、调起来却不像预期。宁可不加载。

- 目录：`<repo>/plugins/*.py`；**跳过 `_` 开头**（`_example.py` 当模板，不加载）。
- **不用 `entry_points`**：这个包是 zip + `.bat` 发的，没有 `pip install` 这一步，
  entry_points 永远不会被触发（写了等于静默失效）。
- **加载失败（语法错 / 缺依赖 / 契约不合规）→ 只告警并跳过该插件，绝不拦住 bot 启动。**
  同 `health` / `status_page` / 坏掉的 `state.json` 那条规矩。
- **启动时逐条打印加载了哪些插件**。往 `plugins/` 丢一个文件就能让代码跑起来，
  这件事用户必须看得见，不能是暗的。
- 插件的可选依赖**由插件自己 try/except**。
  ⚠️ **核心绝不许因此新增 `requirements.txt` 正式行**：`envsetup.requirements_specs()` 读**所有非注释行**
  （已核实：`envsetup.py:43` 跳过 `#` 开头的行），写成正式行 → `启动助手.bat` 自检要求它 →
  没装的人「装完还是起不来」死循环。`faster-whisper` 那次已经踩过，可选依赖一律写注释。
- 整块开关：`plugins.enabled`（默认 `true`；包内 `plugins/` 为空 = 与今天无差别）。

## 三、生命周期事件

**全部在收消息那条线程上同步调用。** 事件清单：

| 事件 | 时机 | 能否改行为 |
|---|---|---|
| `on_start(cfg)` | 启动后、进主循环前 | 否 |
| `on_message(ctx)` | 收到一条消息、**尚未路由** | 否（**只观察**） |
| `before_reply(text, ctx) -> str \| None` | **模型答复**发出前 | **能**——只改文本 |
| `after_reply(text, ctx)` | **一条消息真的发出去之后**（凡出站都算） | 否 |
| `on_tool(name, args, result, ctx)` | 每次工具调用后 | 否（审计用） |
| `on_tick(n)` | 轮询空档（`n` = 第几 tick，从 1 起） | 否 |

**这两个事件的覆盖范围**故意**不对称**，各自都是为了不出事：

- `before_reply` 只挂**模型答复**（主循环那处 + 定时「提问」那处）。
  **绝不挂在 `bot.send()` 上**：确认菜单和群发预览是 bot **原样直发**的
  ——用户就是照着它回「确认」的，插件改写它等于把确认闸做废。
- `after_reply` 挂在 `bot.send()` 里，**凡真的发出去的都算**（含菜单与预览）。
  观察类事件没有「改坏原样直发」的风险，而一个记录出站消息的插件本来就该看到全部。

**只有 `before_reply` 能改行为，这是有意的**：路由（这条消息回不回、回给谁、算不算命令）
只能有一个所有者。`on_message` 允许改 query 就等于开了第二个所有者，
出了 bug 谁也说不清是哪一层的 —— 本项目对「静默扩大影响面」的忌讳同源。
（`on_message` 的触发点在**路由之前**，所以插件看到的是「这条消息到了」，
不是「这条消息被采纳了」。）

### 3.1 五条硬规矩

1. **事件抛异常绝不打断消息循环。** 捕获、打日志、计次，主流程照常。
2. **插件跑在轮询线程上** —— 插件自己的线程**绝不许碰 hook**（同 `read_worker` 那条铁律）。
3. **`before_reply` 的返回值仍要过 `bot.send` 的既有校验**。返回 `None` 或非字符串 = 不改
   （fail-safe：写错了只等于不生效，绝不等于把回复吞掉）。
4. **单事件必须快。** Python **中断不了同步调用**，所以「超时杀掉」做不到，不许假装做得到。
   诚实的做法：运行时**测每次耗时**，超 `plugins.slow_ms`（默认 500）告警；
   连续 `plugins.disable_after`（默认 5）次超时 → **自动停用该插件并明说是哪个、为什么**。
5. **插件是配置项，必须能一键关**（用户对可选用可选功能的一贯口径：每一项都要能关）。
   `plugins.enabled: false` 关全部；`plugins.disabled: [名字]` 关单个。

## 四、待确认 kind 注册（插件与核心共用同一条闸）

`register_pending_kind(kind, describe_fn, apply_fn, key_fields=None)` 让插件/核心模块
注册自己的待确认种类：

- `describe_fn(item) -> str` —— 给编号菜单用的一行人类描述。
  ⚠️ **必须原样展示要执行的内容**（同 `shell` 显示命令原文那条规矩）：
  中间任何转述/改写都等于把确认闸做废。
- `apply_fn(item, ctx) -> (真正执行了几条, 错误)` —— 用户回「确认」后执行。
  这一步已经是**用户确认过的真动作**，所以抛异常时核心会如实返回
  `(0, "执行出错：…")`，**绝不假装成功**。
  `ctx` 是 `{"cfg": ..., "client": ...}`（执行阶段拿不到会话上下文，也不需要）。
- `key_fields=[...]` —— **该 kind 的判重字段**，由注册表统一并进 `_action_key`。
  **不给的 kind 在加载时就失败**（见 4.2：不给就等于两条不同的动作被判成同一条）。
- `describe_fn` 抛异常时核心**不编文案**：编号菜单里会显示
  「（一条 X 动作，但它的描述器出错了 —— 先不要确认）」，并打告警。
  用户是照着菜单回「确认」的，编一句听着像那么回事的话比报错危险得多。

**新 kind 的字段一律走 `extra`**（`set_pending(..., extra={...})`），
**不再加具名参数** —— 理由见 4.1，那是这个契约里唯一一条「加错会静默出事」的接线。

### 4.1 ⚠️ 这里有一个已证实的坑，必须结构性堵掉

`bot.restore_pending()`（`bot.py:1152`）是**逐字段白名单**传参给 `set_pending` 的：

```python
agent_tools.set_pending(chat, it.get("to_wxid") or "", it.get("to_name") or "",
                        it.get("text") or "", kind=..., count=...,
                        image=..., xml=..., cmd=..., timeout=...,
                        label=..., items=..., spec=..., file=..., ttl=ttl)
```

每加一个新字段就要记得改**两处**（`set_pending` 签名 + `restore_pending` 调用）。
漏了的后果不是报错，而是**重启后那条待确认项静默退化成别的操作** ——
`label`（素材那条认不出是哪一条）、`items`/`spec`（群发批次变成「没有收件人」，
真机上是「发出去了但没人收到」）、`file`（退化成一发段文字）**都踩过这个死法**。

**解法**：给 `set_pending` 加一个 `extra=None`（dict），整包存、整包还原，
`restore_pending` 一律透传。**新 kind 的字段一律走 `extra`**，不再加具名参数。
现有的 9 个具名参数**一个都不动**（不动 = 不回归）。

### 4.2 ⚠️ 第二个已证实的坑：判重键也是固定字段元组

`agent_tools._action_key(item)`（`agent_tools.py:1609`）返回的是一个**写死的元组**：

```python
(kind, to_wxid, count, paths(file), paths(image), xml, cmd, canon(items), canon(spec), text)
```

**它不含 `extra`,也不含 `path`/`src`/`dst`/`action`。** 后果很具体：
「删掉 A」和「删掉 B」两条 `fileop`，`kind` 相同、`to_wxid` 空、`text` 空、其余字段全空
→ **判重键完全一样** → 第二条被当成「和上面那条一模一样」而**不登记**，
用户看到的是一句「已经有一条相同的了」。他照着菜单回「确认」，
**删掉的其实是 A，不是他说的 B** —— 而删除是不可逆的。

**解法**：`_action_key` 必须把新 kind 的动作身份算进去 —— 加 `_canon(item.get("extra"))`。
这是**强制项**，不是优化：`_action_key` 的既有注释写的判据是
「同一个动作 = 收件人 + kind + 真正要发的那份东西全一样」，
对文件操作来说「真正要做的那个动作」就藏在 `extra` 里。

**给插件作者的契约**：`register_pending_kind` 要**一并声明该 kind 的判重字段**
（`key_fields=[...]`），由注册表统一并进 `_action_key`。
新 kind 不许再靠人手去改那个元组 —— 那正是 `label`/`items`/`file` 漏掉的同一个死法。

## 五、连接器（MCP / IDE）接入契约 —— 本轮不实现

**结论：连接器就是插件，不新增 `connectors:` 抽象。**
MCP 客户端 / IDE 桥恰好就是「注册工具 + 读自己的配置 + 做 I/O」的插件，
再立一层就是**两个扩展机制**，正是本项目最忌的重复所有者。

契约承诺（一个 MCP 客户端需要的能力都在这三样里）：

1. **启动时动态注册工具** —— `tools/list` 的结果逐条 `register_tool()`。契约不要求工具是静态写死的。
2. **读自己的配置段** —— `plugins.<插件名>:`（如 `plugins.mcp.servers`）。
3. **做网络 / 进程 I/O** —— 插件是任意 Python。

**一条硬要求：外部调用必须自带超时。** MCP 是网络/进程 I/O，跑在轮询线程上会卡住收微信。
契约明确两种执行模式，插件在注册时声明：

| 模式 | 适用 | 本轮状态 |
|---|---|---|
| `inline` | 快（目标 < 500ms） | **已实现**，就地跑在轮询线程上 |
| `worker` | 慢 / 不可预期 | **未实现**（没有任何连接器落地，先不写没人用的执行路径）。声明它的插件**加载即失败并明说原因** |

**为什么 `worker` 只留字段不留实现**：YAGNI —— 本轮没有消费者。
**但字段必须留着**，因为它是给未来那个人的**安全信号**：没有它，连接器作者会自然而然地
在轮询线程上写网络 I/O，直接把收微信卡死。留着 + **响亮地拒绝**，
比「静默按 inline 跑」诚实得多（本项目「不支持的功能要如实报错」那条规矩）。
**实现 `worker` 的时机 = 第一个连接器落地时**，那时照 `read_worker` 的样板做。

`read_worker.py` 就是那条路子的样板（三条铁律：只碰磁盘和模型 HTTP、绝不查库绝不碰 hook、
结果只进队列由主线程发）。**外部连接器一律 `worker`**，这是默认期望，不是可选项。

**以后怎么接**：写一个 `plugins/mcp.py`（或任何名字），在 `on_start` 里连 MCP server、
拉 `tools/list`、逐条 `register_tool(..., mode="worker")`，配置放 `plugins.mcp:`。
本文到此为止 —— **不写实现**是用户明确的口径。

## 六、回归用例（`selftest_plugins.py`）

| 用例 | 断言 |
|---|---|
| 零行为变化 | `plugins.enabled: false` + 空目录 → 注册表工具列表与 `agent_tools.TOOLS` 逐条相等 |
| `_` 前缀 | `_example.py` 不加载；非 `_` 开头的加载 |
| 重名 | 插件工具与内置同名 → 加载失败并**点名**是哪个插件撞了哪个工具 |
| 坏插件不拦启动 | 故意写语法错的插件 → 告警、跳过、其余插件照常、启动成功 |
| 事件隔离 | `on_message` 抛异常 → 消息循环继续，异常被记 |
| `before_reply` fail-safe | 返回 `None` / 返回 `int` → 回复原文不变 |
| 慢插件自动停用 | 连续超 `slow_ms` 达 `disable_after` 次 → 该插件被停用且**明说原因** |
| 插件走确认闸 | `confirm="always"` 的插件工具 → 只 `set_pending`，**一个字都不执行** |
| guidance 必须非空 | 缺 `guidance` 的插件工具 → 加载失败（不是静默无指导） |
| guidance **真的送出去** | 注册了自带指导的工具 → `inject_guidance(原文)` 里出现该工具名与指导；**没有自带指导时逐字不变**（零行为变化）；`bot.system_now()` 是唯一注入点、且拼在**时间之前** |
| 判重键并入 | 声明了 `key_fields` 的 kind：两条**不同**动作不判重、两条**逐字相同**的仍判重 |
| `key_fields` 必填 | `register_pending_kind` 不给 `key_fields` → 加载失败（不许静默套用旧元组） |
| `worker` 模式 | 声明 `mode="worker"` 的插件 → **加载失败并明说「本轮未实现」**；反向证明它**没有**被静默当成 `inline` 跑 |

`selftest_tool_registry.py` **扩成联合视图**：现有 4 项检查（TOOLS↔处理器双向、schema 完整、
示例 config 的 system_prompt 教到、两份 config 段对齐）继续只针对**内置**工具，
新增第 5 项针对**插件**工具：`guidance` 非空 + 名字不与内置重名。
（示例 config 的 system_prompt 不可能预先知道第三方插件名，所以那条检查**按来源分开**，
这不是放松 —— 插件的指导由插件自带，比写在示例 config 里更靠近定义。）

## 七、不回退项（改这块之前先读）

1. 插件加载失败**绝不拦住启动**。
2. 事件异常**绝不打断消息循环**。
3. 插件线程**绝不碰 hook**。
4. 单事件必须快；慢了**自动停用并明说**，不许假装能超时中断。
5. 重名工具**加载即失败**，不许后者静默覆盖前者。
6. 插件工具**绝不许绕过确认闸**。
7. 核心**不许因插件新增 `requirements.txt` 正式行**。
8. 注册表是**唯一真源**：不许出现「`TOOLS` 一份、插件表另一份」的两套注册路径。

## 八、⚠️ 前置项：CLAUDE.md 的指令预算已经见底

实测：`CLAUDE.md` 现在是 **65156 字节 / 预算 65536**，**只剩 380 字节**。
`docs/fixes-2026-10.md:305,360` 记着它**两次顶破被静默截断**（被截掉的正好是给 agent 的指令）。

所以要把 `plugins.py` / `files.py` 写进 CLAUDE.md 的「架构」与「改代码时的约定」，
**必须先腾出空间**（把某一节按既有惯例搬到 `docs/`，例如把「本地执行」的细节搬到
**新建的** `docs/executor-notes.md`）。⚠️ **不要**搬进 `docs/executor-review.md`：
那是 2026-10-01 的对抗式复核报告、钉在当时的文件哈希上，是**历史记录不是活文档**。
**这是前置项，不是收尾项** —— 顺序反了就是「新文档写不进去」，或者**写了、把尾部截掉**，
而那正是本项目最怕的那种静默失效。

## 九、ADR 信号

- 新增 owner：`plugins.py`、`docs/plugin-contract-spec.md`
- 新增公共契约：插件契约（`register_tool` / `register_event` / `register_pending_kind`）
- 依赖方向：`plugins.py` **谁也不 import**（纯注册表）；`agent_tools` 与 `files` **import 它**并在
  模块底部自注册。反向依赖（注册表去 import 工具层/文件层）**不许加** —— 那会立刻变成循环依赖。
- **安全边界变更（ADR 级）**：模型首次获得**免确认的写盘能力** —— 见
  `docs/computer-files-spec.md` 第十节，那条要单独落档。
