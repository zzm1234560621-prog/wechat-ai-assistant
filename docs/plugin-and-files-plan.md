# 两个接口 · 实施计划

> 日期：2026-10-04
> 依据（已批准的设计文档，本计划的**权威来源**）：
> - `docs/plugin-contract-spec.md` — 程序内接口（插件契约 + 生命周期事件 + 待确认 kind 注册 + 连接器契约）
> - `docs/computer-files-spec.md` — 程序外接口（`computer_files` + 路径模型 + 触发者 + 确认模型 + 扩 `send_file`）
>
> 本计划**不重新论证设计**。设计里的每个取舍都已在上面两份文档里定稿并经用户批准；
> 这里只回答「谁、按什么顺序、改哪些文件、拿什么证」。

**Aegis Visibility**：这份工作新增两个 owner（`plugins.py` / `files.py`）、一条公共契约、
以及一次**权限边界扩张**（模型首次获得免确认写盘能力），三样都过了设计门；
计划的价值在于把「改动顺序」钉死——尤其 `plugins.py` 必须先于 `files.py` 落地，
否则 `files.py` 会绕过契约直接接线，那正是本项目最忌讳的第二个所有者。

---

## 一、Requirement Ready Check

| 项 | 内容 |
|---|---|
| 需求来源 | 用户原话（2026-10-04）+ 两份已批准 spec |
| 目标与非目标 | 见 `plugin-contract-spec.md` 第一节、`computer-files-spec.md` 第一节 |
| 用户/场景 | ① 写代码的人（插件）；② 用户自己（微信控制电脑） |
| 验收标准 | 见本文「五、验证」+ 两份 spec 各自的「回归用例」节 |
| 未决阻塞问题 | **无**（4 轮澄清 + 2 次追加拍板，全部已闭环） |
| 判定 | `ready` |

**用户已拍板的边界（不得在实施中改动）**：

1. 路径：可配，**默认全盘**；系统目录默认挡住。
2. 触发者：**只认我的对话 + 指定对话**。
3. 确认：可配，**默认读写免确认、删除要确认**；`overwrite` 追加一道闸；`delete` **永远强制确认**。
4. 软件连接（MCP/IDE）：**只钉契约，不写实现**。
5. 任意磁盘路径发文件：**入范围**，但**一律进确认队列，绝不走 `auto_send_whitelist` 直发**。

## 二、Change Necessity

**为什么不能只改文档/配置**：现有机制无法表达「插件自带模型指导文本」这件事——
`system_prompt` 是两份静态 YAML 字符串，第三方插件名不可能预先写在里面。
必须新增一个运行期可注册的 owner。

**最小代码边界**：两个新模块（`plugins.py` 纯注册表、`files.py` 文件内核）
+ 三个既有文件的接线点（`agent_tools.py` / `bot.py` / 两份 config）+ 三份自测。

**为什么 `files.py` 要写成契约的消费者而不是直接接线**：如果它绕过契约，
契约就成了一件没人用的摆设，而「以后接 MCP」正是要靠它被证明过才敢信任。

## 三、Ripple Signal Triage

信号**命中**（shared/core、公共契约、持久化、权限）：

| 面 | 规范所有者 | 下游消费者 / 受影响方 |
|---|---|---|
| 工具声明与派发 | `plugins.REGISTRY`（新） | `bot.run_agent`、`ToolBox.run`、`llm.chat_with_tools`（**形状不变**） |
| 待确认队列（持久化到 `state.json`） | `agent_tools.set_pending` / `_action_key` / `send_pending` | `bot.restore_pending`、`describe_pending`、`selftest_policy` / `selftest_bot_loop` / `selftest_assets` |
| 发文件准入 | `t_send_file` + `file_read.pick` | `send_pending` 的 `allowed_dirs` 二次校验、`describe_pending` 的 file 分支 |
| 权限 | `files.path_ok`（新） | `t_send_file`、`computer_files` |

**契约/真源风险**：唯一真源从「`agent_tools.TOOLS`」扩成「注册表（内置 + 插件）」。
**不许**保留第二条注册路径；`TOOLS` 降级为**内置工具的声明输入**，不是运行期真源。

**回退/兼容**：不新增任何兼容分支或 fallback。`plugins.enabled: false` + 空目录 = 行为逐字不变。

## 四、Baseline / 权威引用

- `CLAUDE.md` — 架构、hook 铁律、确认队列规矩、改代码的约定（**权威**）
- `agent_tools.py:550` `TOOLS` / `agent_tools.py:1701` `set_pending` / `:1609` `_action_key` /
  `:1879` `describe_pending` / `:2121` `send_pending` / `:2953` `ToolBox` / `:4445` `t_send_file`
- `bot.py:1152` `restore_pending` / `:1203` `run_agent` / `:2469` `main` / `:2888` 主循环
- `file_read.py` `extract()` / `pick()` — 读文件与按名定位的唯一入口
- `executor.py` + `shell.auto_ok` — 精确匹配免确认名单的先例
- `agent_tools.py:1467` `allowed_image_dirs` — 「边界可以宽但必须打告警」的先例
- `selftest_tool_registry.py` — 全量一致性闸门（本计划要扩它）
- `envsetup.py:43` — `requirements_specs()` 跳过 `#` 行（可选依赖只能写注释）

**TDD Route**：mode `off`；decision `skipped`（无 RED/GREEN 仪式）；
authority = 用户未要求 strict TDD、项目也未规定 test-first。
**测试姿态**：项目约定是**每个任务落地即带回归用例并跑通**（不是先写测试），
所以每个任务都列了「新增/扩展的自测 + 跑法」，这不是 TDD 的 RED 步骤。

## 五、验证

**跑法**（本机 venv，从仓库根目录）：
```
.venv\Scripts\python.exe <selftest>.py
```

| 层 | 手段 |
|---|---|
| 单元/回归 | 新增 `selftest_plugins.py`、`selftest_files.py`；扩展 `selftest_tool_registry.py` |
| 既有用例不回归 | `selftest_policy.py`、`selftest_bot_loop.py`、`selftest_assets.py`、`selftest_web.py`、`selftest_install.py` |
| 打包合规 | `selftest_portable.py`（无本机绝对路径）、`selftest_install.py` |
| 零行为变化 | 注册表联合视图 == 今天 `agent_tools.TOOLS`（逐条、含顺序） |
| 真机 | `verify_real.py` + 人工看回收站（见 T11） |
| 全量 | `selftest_all.py` 全绿 |

**每个任务结束时必须做到**：本任务列出的自测**全绿** + 受影响的既有自测**全绿**。
不许「先合并、回头补测」。**最后一个任务做完，`selftest_all.py` 要整体全绿。**

**停止条件（drift stop）**：任何一步发现必须（a）加 fallback/adapter 兼容分支、
（b）保留两条注册路径、或（c）放宽已拍板的权限边界 —— **停下回设计**，不许就地折中。

---

## 六、任务

> 顺序即依赖顺序。T1/T2 是地基，T5 是最易出静默失效的一步，T6–T9 依赖 T2+T5。

### T1 · 前置：给 CLAUDE.md 腾出指令预算

- **为什么是第一个**：`CLAUDE.md` 现 65156 / 65536 字节，**只剩 380**。T10 要把两个新模块写进权威文档，
  现在写不进去；留到最后做就会「写不下 → 少写」或「写了 → 尾部被静默截断」（`fixes-2026-10.md:305,360` 记着已发生两次）。
- **文件**：`CLAUDE.md`、**新** `docs/executor-notes.md`
- **最小改动**：把「⚠️ 本地执行（run_command / executor.py）」一节按既有惯例整体搬进
  **新建的** `docs/executor-notes.md`，`CLAUDE.md` 里留一段带指针的要点。
  **一个字都不许删**，只搬家。
  ⚠️ **不要**搬进 `docs/executor-review.md` —— 那是 2026-10-01 的**对抗式复核报告**，
  钉在当时的文件哈希上（`agent_tools.py` 记的是 1452 行，现在 5068 行），
  是**历史记录而不是活文档**；把活规矩塞进审计报告会把两者都污染。
  命名照既有惯例（`auto-reply-notes.md` / `assets-notes.md` / `broadcast-group-notes.md`
  都是同一套「从 CLAUDE.md 搬出来的活笔记」）。
- **兼容**：纯文档，无代码影响。
- **验证**：`CLAUDE.md` 字节数降到 60000 以下；搬家后的原文在 `docs/executor-review.md` 里完整存在；
  确认 `CLAUDE.md` 尾部（「参考资料」节）**仍在**（证明没被截断）。

### T2 · `plugins.py`：注册表 + 内置适配 + 派发改造

- **文件**：新 `plugins.py`；改 `agent_tools.py`（`TOOLS` 适配注册 + `ToolBox.run` 查表）、`bot.py`（`run_agent` 取 `REGISTRY.tools()`）
- **最小改动**：
  - `Registry` + `register_tool(spec)`，字段 = spec 2.1 表（`name`/`description`/`parameters`/`handler`/`guidance`/`confirm`/`mode`）
  - 内置工具以 `method="t_<name>"` 形式注册，`source="builtin"`
  - `ToolBox.run`：插件 → `spec.handler(args, ctx)`；内置 → `getattr(self, spec.method)(args)`。**不加 getattr 兜底**
  - `mode` 字段**只做校验不做实现**：声明 `"worker"` 的插件**加载即失败并明说「本轮未实现」**
    ——**绝不静默按 `inline` 跑**（那等于一边阻塞微信一边声称没阻塞）
  - `plugins.py` **不 import** `agent_tools` / `files`（避免循环依赖）
- **兼容边界**：`llm.chat_with_tools` 收到的仍是同形状的 list；`ToolBox` 的既有构造签名不变
- **验证**：新增 `selftest_plugins.py`
  - **零行为变化**：`plugins.enabled: false` + 空目录 → `REGISTRY.tools()` 与 `agent_tools.TOOLS` 逐条相等（含顺序）
  - 重名 → 加载失败并**点名**是谁撞了谁
  - 声明 `mode="worker"` → **加载失败并明说「本轮未实现」**（反向证明没被静默当成 `inline`）
  - 跑 `selftest_bot_loop.py`、`selftest_policy.py` 必须全绿（派发改造不回归）

### T3 · 插件目录加载 + 失败隔离 + 开关

- **文件**：`plugins.py`、`bot.py`（`main()` 里进轮询循环**之前**扫目录）
- **最小改动**：扫 `<repo>/plugins/*.py`、跳过 `_` 前缀、`plugins.enabled` / `plugins.disabled`、
  **启动时逐条打印加载了哪些插件**；加载失败（语法错/缺依赖/契约不合规）只告警跳过
- **验证**：扩展 `selftest_plugins.py`
  - 语法错的插件 → 告警、跳过、其余插件照常、**启动成功**
  - `_example.py` 不加载
  - `plugins.disabled: [名字]` 生效
  - 可选依赖只写 `requirements.txt` 注释行 → `selftest_install.py` 不因插件新增依赖

### T4 · 生命周期事件总线 + 五条硬规矩

- **文件**：`plugins.py`（`register_event` + 派发器）、`bot.py`（插事件触发点）
- **最小改动**：6 个事件（spec 第三节表）；`ctx` 形状同 spec 2.1；
  异常隔离、耗时测量（`plugins.slow_ms` 默认 500）、连续超时（`plugins.disable_after` 默认 5）自动停用并明说
- **硬约束**：全部在**收消息那条线程**上同步调用；**只有 `before_reply` 能改行为**，且返回值仍过 `bot.send` 既有校验
- **验证**：扩展 `selftest_plugins.py`
  - `on_message` 抛异常 → 消息循环继续，异常被记
  - `before_reply` 返回 `None` / `int` → 回复原文**不变**
  - 连续超时达阈值 → 该插件被停用且**明说原因**

### T5 · 待确认 kind 注册 + `extra` + `key_fields`（**最易静默失效，单独一步**）

- **文件**：`agent_tools.py`（`set_pending` 加 `extra=None`；`_action_key` 并入 `extra` + `key_fields`；
  `describe_pending` / `send_pending` 改为走注册表分派）、`bot.py`（`restore_pending` 透传 `extra`）、`plugins.py`（`register_pending_kind`）
- **最小改动**：
  - `set_pending(..., extra=None)`，整包存；**现有 9 个具名参数一个都不动**
  - `restore_pending` 透传 `extra`
  - `_action_key` 并入 `_canon(item.get("extra"))` 与注册表声明的 `key_fields`
  - `register_pending_kind(kind, describe_fn, apply_fn, key_fields)`；**不给 `key_fields` 加载即失败**
- **⚠️ 两个已证实的坑**（spec 4.1 / 4.2）：漏做 `extra` 透传 → 重启后待确认项**静默退化成别的操作**；
  漏做 `_action_key` → 两条不同动作**判成同一条**、用户照菜单确认**做掉另一件事**
- **验证**：扩展 `selftest_plugins.py` + `selftest_bot_loop.py`
  - `extra` 经 `set_pending` → `restore_pending` 后**字段一个不少**
  - 两条**不同**动作不判重；两条**逐字相同**的仍判重
  - `key_fields` 缺失 → 加载失败
  - `selftest_policy.py` / `selftest_assets.py` 的 `describe_pending` 既有断言全绿

### T6 · `files.py`：路径模型 + 读类动作

- **文件**：新 `files.py`（用 T2 的契约注册成内置消费者）
- **最小改动**：
  - `path_ok(path, cfg)`：`roots` 空 = 全盘；`deny` 默认挡系统四目录；两侧 `realpath` 归一化（挡 `..` 与符号链接）
  - 读类 `list` / `find` / `info` / `read`；**`read` 复用 `file_read.extract()`**（不重写抽取内核）
  - `list`/`find` 有上限并**明说截断**；`read` 对目录如实拒绝并指路用 `list`
  - 指导文本里带上解析后的 `roots`
- **验证**：新增 `selftest_files.py`
  - roots 空放行 / 落 deny 被拒 / `..` 绕过被拒 / 符号链接绕过被拒 / roots 非空时根外被拒
  - `read` 走的是 `file_read.extract()`（**用桩证明同一条内核**）
  - 截断明说；`read` 目录如实拒绝

### T7 · `files.py`：写类动作 + 删除进回收站

- **文件**：`files.py`
- **最小改动**：
  - 写类 `write` / `append` / `mkdir` / `copy` / `move` / `rename`
  - `write` **默认只新建**；`overwrite=true` 且目标已存在 → 进确认队列
  - `delete` 走 `ctypes` + `SHFileOperationW`（`FO_DELETE | FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT`），
    **零依赖、不起子进程**；`FOF_ALLOWUNDO` 不许删
  - **`delete` 永远强制确认**：即使从 `files.confirm` 里删掉也拦住并告警
- **验证**：扩展 `selftest_files.py`
  - `write` 命中已存在且无 `overwrite` → 如实拒绝；给了 `overwrite` → 进确认队列（不直接写）
  - `delete` 强制确认（配置里删掉也拦，且告警）
  - 回收站：自建临时目录里删 → 返回 0、含 `FOF_ALLOWUNDO`、原路径消失（**只碰自建临时目录**）
  - `computer_files` **不能执行程序**（反向证明没有 exec 参数）

### T8 · 触发者闸门 + 两份 config

- **文件**：`files.py`、`bot.py`（把 `from_self` 事实传进 `run_agent`/`ToolBox`）、
  `config.yaml`、`config.example.yaml`
- **最小改动**：
  - `files.who`（空 = 只控制会话里我自己发的）、`files.enabled`
  - 闸门设在**工具层**，判据由 bot 主循环传进来（**事实留在有事实的那条线程**）
  - 不在名单里 → **如实拒绝并说清**，绝不静默降级
  - 两份 config：本机按用户口径（全盘 / 控制会话+指定 / `[delete, overwrite]`）；
    **示例**用保守默认（桌面/文档/下载、只控制会话、全部写类要确认）
  - 两份 config **只加 `files:` 段**。⚠️ **`computer_files` 的模型指导不抄进
    system_prompt** —— 它用契约的 `guidance` 随工具定义走（`bot.system_now()` 拼进去）。
    抄两份正是「同一个指导存两处、漏一处就静默失效」那个老 bug 的形状
  - 边界放宽时启动打告警
- **兼容**：`selftest_tool_registry.py` 第 4 项查的是**段对齐**不是值相等，两份用不同默认值合规
- **验证**：扩展 `selftest_files.py` + 跑 `selftest_tool_registry.py`
  - 非 `files.who` 会话被拒且**说清原因**
  - 两份 config 段对齐；`computer_files` 在示例 system_prompt 里出现

### T9 · 扩 `send_file`：任意路径 + 强制确认 + 菜单分级

- **文件**：`agent_tools.py`（`t_send_file` / `describe_pending` / `send_pending`）
- **最小改动**：
  - 定位口径放宽为 `msg/file/`（`file_read.pick`，**原样不动**）∪ `files.path_ok` 允许的绝对路径
  - **任意磁盘路径来源一律进确认队列，跳过 `auto_send_whitelist` 直发那条分支**
  - `send_pending` 对 `kind=="file"` 的 `allowed_dirs` 二次校验**同时认 `files.path_ok`**
  - `describe_pending` 的 file 分支**按来源分级**：`msg/file` → 只显示文件名（原样）；
    磁盘路径 → **显示原样路径**，长则截断**必须标注**
- **⚠️ 安全边界（用户已批准）**：不这么做就等于「模型能把盘上任意文件免确认发给白名单里的人」
- **验证**：扩展 `selftest_files.py` / `selftest_policy.py`
  - 收件人**在白名单里也进确认队列**（反向证明直发分支没被放宽）
  - `msg/file/` 老行为**逐字不变**（`selftest_policy` 既有断言全绿）
  - 菜单分级：`msg/file` 只给文件名；磁盘来源给原样路径 + 截断标注

### T10 · 文档与自测收尾

- **文件**：`CLAUDE.md`（架构 + 改代码时的约定，**此时 T1 已腾出空间**）、`README.md`、
  `selftest_tool_registry.py`、`selftest_all.py`、`plugins/_example.py`
- **最小改动**：
  - `CLAUDE.md`：加 `plugins.py` / `files.py` 两段（含「不回退项」要点）、指针指向两份 spec
  - `README.md`：用户怎么用 `computer_files`、怎么放插件、怎么开关；
    **外加一节「以后怎么接 MCP / IDE」** —— 本轮不写实现，但要把接入路径写给人看
    （在 `on_start` 里连 server、拉 `tools/list`、逐条注册；配置放 `plugins.<名字>:`；
    **必须先实现 `worker` 模式**，否则网络 I/O 会卡死轮询）
  - `selftest_tool_registry.py` **扩成联合视图**：既有 4 项继续只针对内置；新增第 5 项针对插件
    （`guidance` 非空 + 不与内置重名）
  - `plugins/_example.py`：带注释的模板（`_` 前缀 → 不加载）
  - `selftest_all.py` 接上两个新自测
- **验证**：`CLAUDE.md` 字节数仍 < 65536 且**尾部完整**；`selftest_all.py` 全绿

### T11 · 真机项

- **文件**：`verify_real.py`（增一条只读检查）
- **最小改动**：文档化「删除进回收站」的**人工确认**步骤
- **为什么必须人工**：「真的进了回收站」自测证不了（要枚举回收站得走 Shell COM，代价过大）。
  自动测只证「旗标对 + 原路径消失」。**不许把没验证的说成验证过了。**
- **验证**：真机删一个测试文件，人去回收站看一眼它在不在；`verify_real.py` 通过

### T12（**可选加固，用户未表态**）· `plugins/` 对 `run_command` 只读

- **背景**：助手手里有 `run_command`，理论上能被引导去写一个插件文件（要用户回确认、且写完要重启）。
  这是既有能力的副作用，不是本设计的目的。
- **若做**：`executor` 侧对 `plugins/` 目录加一条写保护，如实拒绝并说明。
- **状态**：**等用户表态**。不做不影响 T1–T11 的任何验收。

---

## 七、修复轨 / 退役轨

**退役**：本计划**不删除任何内部路径**。

**保留但角色变更（必须显式，否则以后有人会直接读它）**：

| 保留物 | 新角色 | 判据 |
|---|---|---|
| `agent_tools.TOOLS` | **内置工具的声明输入**，不再是运行期真源 | 运行期一律读 `REGISTRY.tools()`；谁直接读 `TOOLS` 派发就是第二个所有者 |
| `ToolBox` 的 `t_*` 方法 | 内置工具的处理器实现 | 由注册表用 `method` 名解析；**不保留 getattr 兜底** |
| `file_read.pick`（`msg/file/`） | 发文件老路径，**原样保留** | `selftest_policy` 既有断言全绿 |
| `t_call` 孤儿处理器 | 不变（用户已拍板保留代码、删描述） | `selftest_tool_registry.py` 里那条带原因的例外不动 |

**退役触发条件**：无。本计划不引入需要退役的旧路径。

## 八、风险

| 风险 | 缓解 |
|---|---|
| **`extra` / `key_fields` 漏做 → 静默退化/判重错** | T5 单独成步 + 4 条回归用例；这是全局最高风险项 |
| 派发改造引入回归 | T2 的「零行为变化」判据 + 跑既有 `selftest_bot_loop` / `selftest_policy` |
| `CLAUDE.md` 又顶破预算 | T1 前置腾空间 + T10 落地后复测字节数与尾部 |
| 插件把 hook 搞崩 | 契约明写「插件在轮询线程上、自己的线程绝不碰 hook」；慢插件自动停用 |
| 新增依赖撞 `requirements_specs()` | 契约禁止核心新增正式行；T3 用 `selftest_install.py` 挡 |
| 权限边界被顺手放宽 | T8/T9 各带反向用例（白名单也进确认队列 / deny 不可代码放宽） |
| 两份 config 默认值写反 | T8 显式列两份的值；`selftest_tool_registry` 查段对齐 |

## 九、执行路线

- **route**：`inline`（不走 subagent-driven）。
- **理由**：T2→T9 是**强顺序依赖**，且 T2/T5/T8/T9 全部写同一批文件
  （`agent_tools.py` / `bot.py` / 两份 config），写作用域重叠，并行协调成本高于收益。
- **User confirmation required**：`no` —— 设计已批准、权限边界已拍板、无破坏性动作；
  两处破坏性/外发行为（`delete`、扩 `send_file`）均已在 spec 里定好闸门并按用户口径实现。
- 唯一**待用户表态**项：T12（可选加固），不阻塞其余任务。

## 十、ADR 信号（留到收尾落档，不在此刻写架构记忆）

1. 新增 owner：`plugins.py`、`files.py`
2. 新增公共契约：插件契约（`register_tool` / `register_event` / `register_pending_kind`）
3. **权限边界扩张**：模型首次获得免确认写盘能力；`send_file` 可达文件集扩张但**不连带放宽直发**
4. 真源迁移：`agent_tools.TOOLS` → `plugins.REGISTRY`（`TOOLS` 降为声明输入）
5. 静默失效结构性消除：`restore_pending` 字段列表 / `_action_key` 判重元组 → `extra` + `key_fields`
6. 回收站实现选型：`ctypes` + `SHFileOperationW`（否掉 `send2trash` / `winshell` / PowerShell）
