# 开机自启 = 计划任务（2026-10-07 定案，**取代** 2026-10-06 的「只用 Run 键」）

> 本文是 `autostart.py` 的设计与决策记录，也是 `docs/admin-elevation-notes.md` 第四节的
> **订正件**（那一节写着「用户 2026-10-06 明确选了 RunAs，计划任务留作以后要改时的第一选项」——
> 2026-10-07 用户明确改选了计划任务，那一节已同步改写）。
>
> 关联：`CLAUDE.md`（`autostart.py` 条目）、`README.zh-CN.md` / `README.md` /
> `docs/README-full.md`（「开机自启」那一段）、`selftest_autostart.py`。

## 一、事故：自启开了，却从 22:12 静默失联到第二天早上

真机时间线（2026-10-06 → 10-07）：

| 时刻 | 事实 | 证据 |
|---|---|---|
| 10-06 21:59 | 用户登录（最后一次登录） | `explorer.exe` 启动时间 21:59:53 |
| 10-06 22:03 | HKCU Run 拉起 pythonw（cwd=`C:\WINDOWS\System32`）→ 自己提权开新窗口 | `bot.log`：`工作目录已从 C:\WINDOWS\System32 切到项目目录` + `已在新窗口里以管理员身份启动` |
| 10-06 22:12 | 连不上 hook（当时微信没开，`WinError 10061`）→ 等约 4.5 分钟后 `sys.exit(1)` | `bot.log` 最后一行 `[bot] 连不上 aixed 服务` |
| 10-07 07:48 | 微信自己换了进程（不是登录） | `Weixin` 主进程 PID 11992，30001 由它监听 |
| 10-07 09:34 | 人工 `botctl start` 才恢复 | 端口 39001 此前一直空着；`botctl.py status` = 「没有在跑」 |

**根因不是「自启没配」**，而是三条叠在一起：

1. **Run 键是"一次性发射"，不是守护**：只在登录那一刻响一次；进程之后死掉，没有任何东西再拉它。
2. **它只等约 5 分钟就放弃**（`bot.py` 里 `_GATE_HARD_FAIL_LIMIT = 30`）：微信晚开一会儿，它就永久下线。
3. **开机那一刻没人点 UAC**：Run 键拉起来的**一定是普通权限**，而 `bot.py` 当时调
   `admin.ensure_elevated(capture=True)` 时**没传 `assume`**（注释却写着传了）→ 它会去弹 UAC；
   没人点就 `exit(2)`，而 pythonw 无窗口 ⇒ **一点提示都没有**。22:03 那次能起来，只是因为你
   正好在旁边点了「是」。

第二条和第三条在**任何一台新电脑上都会重演**——所以这不是本机配置问题，是包里那条自启路径的问题。

## 二、决策：自启改成计划任务，`autostart.py` 是唯一所有者

| 维度 | 计划任务 |
|---|---|
| 触发器 | `AtLogOn`（该用户）+ `Once` 起始 + **每 5 分钟重复、不写 `RepetitionDuration`**（= 无限重复） |
| 权限 | `RunLevel=Highest` → **静默管理员**，开机不再需要点 UAC（语音条那条硬约束直接满足） |
| 自愈 | `MultipleInstances=IgnoreNew`：**任务实例就是 bot 进程本身**，所以"每 5 分钟触发"在 bot 活着时被忽略、bot 死了才会重新拉起 ⇒ 天然的看门狗 |
| 时长 | `ExecutionTimeLimit = PT0S`（无限）；`-StartWhenAvailable`（错过触发点也补）；允许电池 |
| 动作 | `.venv\Scripts\pythonw.exe bot.py`，工作目录 = 项目目录 |

> ⚠️ **坑（本次真机踩到）**：`-RepetitionDuration ([TimeSpan]::MaxValue)` 会序列化成
> `P99999999DT23H59M59S`，Task Scheduler 直接拒收：`The task XML contains a value which is
> incorrectly formatted or out of range`（HRESULT `0x80041318`）。**省略 `Duration` 才是"无限重复"**。

`bot.py` 的 39001 单实例锁是第二道保险：重复触发、`botctl start` 与任务同时抢，都只会有一个活着。

**取代关系（退役项）**：老的 `HKCU\...\Run\WeChatAIAssistant` 值**退役**——
- `autostart.py on` 注册任务后顺手删掉它；`off` 也删（幂等）；
- `status` 若发现它还残留，**明确报警**（说明这台机器是老的、且两条路会打架）；
- 代码里仍**只读地**认识这个名字，用于清理，不再写它。
理由：两个 owner（Run 键 + 计划任务）在登录时会同时拉进程，而 Run 键那条还会弹 UAC、没人点就 `exit(2)`。
`selftest_autostart.py` 钉住"`on` 之后 Run 值必须消失"。

## 三、`bot.py` 两处配套改动

1. **启动闸门不再放弃**（退役 `_GATE_HARD_FAIL_LIMIT` 的"放弃"语义）：
   `connect_aixed()` 在连不上 hook 时**一直等**（10→30→60 秒退避，日志仍 1 分钟一行、
   本地通知仍约 10 分钟一次），不再 `return None` → `sys.exit(1)`。
   旧注释说「连不上 hook = 真起不来」，10-07 的事故证明这句是错的：微信可能几小时后才起来。
   `connect_aixed(base_url, give_up_after=None)` 保留一个**显式上界参数**（秒），
   只有自测会传；产品路径故意不传。
2. **`assume` 由"有没有控制台"决定**（修掉注释与代码不一致）：
   `assume = not _has_interactive_console()`。没控制台（pythonw / 计划任务 / 老 Run 键）⇒
   没人能点 UAC ⇒ `assume=True`：**只告警、继续以普通权限跑**（文本仍能回，语音条读不到），
   绝不 `exit(2)` 静默消失。有控制台（`启动助手.bat`、终端里 `python bot.py`）⇒ 照旧弹一次 UAC。
   计划任务那条路本来就是管理员，`is_admin()` 先返回，不受影响。

## 四、验收（真机证据）

**2026-10-07 当天已跑到的（都是本机实测，不是推断）**

| 项 | 结果 |
|---|---|
| 计划任务的形状 | `Get-ScheduledTask`：`State=Running`、`RunLevel=Highest`、`MultipleInstances=IgnoreNew`、`ExecutionTimeLimit=PT0S`；触发器具 `PT5M` 重复（**没有** `RepetitionDuration`） |
| **自愈** | 09:44:05 `Stop-ScheduledTask` → **不做任何人工干预** → 09:49:05 触发 → **09:49:09** 新 PID 占住 39001（差 4 秒） |
| 重复触发不产生第二个实例 | 任务在跑时再 `Start-ScheduledTask`：39001 的 owner PID 不变、`bot.log` 无新会话（`lastResult=0x800710E0` 正是它被 `IgnoreNew` 拦下的记录，已翻成人话） |
| 老 Run 值被清掉 | `autostart.py on` 之后 `HKCU\...\Run\WeChatAIAssistant` 不存在 |
| `status` 只读、不提权 | 一次 UAC 都没弹，输出任务状态 + 重复间隔 + 上次/下次触发 + 助手在不在跑 |
| 提权失败不降级 | 真机撞到一次：UAC 弹窗 120 秒没人点 → 如实报「提权超时」并 `exit 2`，**没有降级成普通权限**（话术原来甩锅给 PowerShell，已改成"UAC 没人点"） |
| **`off` → `on` 端到端（新代码路径）** | 11:38 真机跑通：`off` 后任务真被删、`status` 如实说"未开启"、助手按设计**没被停**；`on` 后任务重建（`Running/Highest/IgnoreNew/PT0S/PT5M`）、老 Run 值不存在、**旧实例被停掉、由任务实例接管**（pid 4068 → 23844，进程树是 `svchost → 外层 → 真解释器`） |
| 回归 | `selftest_autostart.py`（65 项）+ `selftest_all.py` **36 份全绿** |

**真机跑出来、顺手修掉的一处"报得比知道的准"**：`on` 那条路里，非提权那份父进程早先会
报「助手在跑（pid N）」——那个 N 是**接管之前**的旧实例（真机就是 4068，而接管后是 23844）。
现在父进程**只等任务出现、如实转述状态**，不再报 pid（接管发生在另一个提权窗口里，
父进程本来就看不清）。回归：`selftest_autostart.t10` 明确断言"输出里不许出现 pid"。

## 五、回归

| 套件 | 钉住什么 |
|---|---|
| `selftest_autostart.py`（新） | 任务定义/命令串形状、`PT5M` 重复且**不写 Duration**、RunLevel/IgnoreNew/PT0S、`on` 必须清理旧 Run 值、`off` 幂等、status 解析（假 JSON 注入，不真连计划任务） |
| `selftest_bot_loop.py` | 闸门契约改成"连不上也**不放弃**"（原 ⑥ 用例是"到上限返回 None"，已按新契约改写）+ `assume` 判据两条 |
| `selftest_install.py` / `selftest_portable.py` | 菜单提权链没被动、新文件无本机绝对路径、`selftest_all.py` 自动收录新套件 |
| `selftest_all.py` | 全量 |
