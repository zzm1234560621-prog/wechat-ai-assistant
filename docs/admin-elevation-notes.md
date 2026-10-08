# 助手必须以管理员身份运行（2026-10-06 定案）

> 本文是 `README.md`「部署说明」那段警告、`CLAUDE.md` 里 `admin.py` 那一条的细节延伸。
> **一句话**：用户 2026-10-06 拍板「永远让助手跑在管理员上，之后部署在其他电脑上也要
> 永远跑在管理员上」。落地方式是**启动时自己提权（`-Verb RunAs`，每次弹一次 UAC）**，
> 用户明确选的那一种（另一条路是计划任务 `RunLevel=Highest`，静默但改动大，没选）。

## 一、为什么非要管理员（不是偏好，是硬边界）

语音条（`local_type=34`）的音频**不在磁盘上**，只能从微信进程内存里读（明文 SILK）。
而 Windows **不允许低完整性级别的进程读高完整性级别进程的内存**。

2026-10-06 真机取证（同一台机器、同一时刻）：

| 探针 | `OpenProcess(QUERY_INFORMATION\|VM_READ)` | token 完整性级别 |
|---|---|---|
| 普通权限 python | ❌ 5 个 `Weixin.exe` 全部 `DENIED err=5` | —（`OpenProcessToken` 也是 err=5） |
| **提权** python | ✅ 5 个全部 `OK` | 自身 High、**Weixin High** |
| 对照 explorer | — | Medium |

用户当时的微信是**提权打开的**（High），助手是 Medium → 语音条永远读不出来，
而报错文案当时还写着「微信没在跑？或者权限不够」，把人往错方向引
（文案已在 `voice_mem.ProcessProbe` 里修，见 `docs/voice-reliability-2026-10-03.md` 第六节）。

**要注意的方向是"助手要够高"，不是"微信要降下来"**：助手提权后，微信无论是
普通开还是提权开都能读（提权进程可以读低完整性进程）。

## 二、落地：`admin.py` + 四个入口

新增模块 **`admin.py`**（只依赖标准库，`console.py` 顶层可以直接 import 它）：

* `is_admin()` —— **唯一判据**是 `IsUserAnAdmin()`。绝不拿"在不在 Administrators 组里"
  当判据（管理员账户在 UAC 下跑的程序默认也是非提权的）。
* `ensure_elevated()` —— 已经是管理员就直接过；不是就重新拉起自己（弹一次 UAC）。
* `build_relaunch_command()` —— 纯函数，命令串只在这里写一次。
* `claim_launch_token()` —— 一次性令牌，用来证明"提权已经发生过"（见下）。

四个「起 bot」的入口都过这道闸（**漏一个就是下次的静默失效**）：

| 入口 | 位置 | 说明 |
|---|---|---|
| `助手.bat` → `console.py` | `main()` 开头 | 整个菜单以管理员跑；失败就如实说并退出，不偷偷降级 |
| `启动助手.bat` → `bot.py` | `main()` 开头（**在单实例锁之前**） | 正常提权、弹一次 UAC |
| 开机自启（**计划任务** → `bot.py`） | `autostart.py` | `RunLevel=Highest`：**静默管理员、不弹 UAC**（2026-10-07 改，见第四节） |
| 菜单/命令行的启停 | `botctl.start()` | 提权在**真正拉起进程之前**，且把调用方指定的 `pythonw` 传下去 |

两处门面也要分清（`ensure_elevated` 的第二项是**调用方要不要立刻返回**的信号；
第三项 `launched` 就是它）：

* **交互式脚本**（`console.py` / `bot.py` / `启动助手.bat` 那条路）：`capture=True`，
  子进程继承本窗口，用户能在同一个窗口里看到它做什么。代价是拿不到它的 stderr，
  所以 UAC 被拒只能靠 PowerShell 自己弹的错误 —— 返回真只代表"**拉起来了**"。
* **命令式脚本**（`botctl.py start/stop/restart/search-*`）：`wait=True`，
  父进程**等子进程结束并沿用它的退出码**，结果直接打在这个窗口里。
  ⚠️ `stop` 也必须提权：助手是管理员跑的，普通权限的 `taskkill` **杀不掉高完整性进程**。
  只读命令（`status` / `health` / `log` / `follow` / `search-status`）**不提权**——
  看一眼状态不该弹 UAC。

顺带两处：`console.act_foreground()`（前台启动那条路）和 `console.auto()`（一键流程）
也走同一个门。

## 三、两个必须守住的坑

### ① 无限提权循环

提权后若仍判"不是管理员"→ 再提权 → 无限套娃。而且**不能靠环境变量**：
`Start-Process -Verb RunAs` **不继承**父进程环境变量（PowerShell 5.1 的 `Start-Process`
也没有 `-Environment`，7.4+ 才有）。

所以用**一次性令牌文件**：父进程提权前写 `%TEMP%\wxa_elevate_<pid>.tok`，
并把路径通过 `$env:WXA_ELEVATE_TOKEN='…'; Start-Process … -Verb RunAs` **写进提权命令行
内部**；子进程起来时 `claim_launch_token()` 命中即删、返回 True —— "我知道我是被提权拉起来的"。

**为什么不加命令行开关**（`--elevated` 之类）：每个入口的参数解析都不一样
（`bot.py` 有 `--once/--probe`、`autostart.py` 有 `on/off/status`、`console.py` 没有），
塞一个进去要逐处改解析，**任何一处漏改就是无限循环**。令牌不动任何脚本的命令行契约。

真机验证过（不提权、只验 argv 与令牌传递）：含空格的中文参数原样到达子进程，
令牌 `claim_launch_token()` 返回 True，删掉后第二次返回 False。

### ② UAC 被拒后**绝不静默降级**

用户点「否」→ `rc=1223 (ERROR_CANCELLED)`。这时必须**如实失败并退出**，
不许"那我按普通权限跑吧" —— 那正是本项目最忌讳的失效形态（看起来一切正常，功能悄悄废掉）。

### ③ 消费方必须分清 `ensure_elevated()` 返回 True 的两种含义

| 情形 | True 的含义 | 调用方该做什么 |
|---|---|---|
| 本来就是管理员 / 令牌命中 | 就是这一份进程 | 继续跑 |
| 刚重新拉起了一个提权进程 | "已经拉起来了"，**这一份不是它** | **立刻 return** |

分辨方法：拿 True 之后再调一次 `admin.is_admin()`。`botctl.start()` 就是这么写的
（那里错一次就是**两个助手同时轮询 hook**，实测会把微信搞崩）。

## 四、开机自启：2026-10-07 改成**计划任务**（本节已取代 2026-10-06 的 Run 键方案）

**旧方案（已退役）**：HKCU Run → 普通权限拉起 `bot.py`，`assume=True` 只告警不弹窗。
它有三条叠在一起的毛病，2026-10-06 夜里真机后果是「自启开着、却静默失联一整晚」：

1. Run 键只在登录响一次，**不是守护**（进程死了没人再拉）；
2. 它连不上 hook 就等约 5 分钟后 `sys.exit(1)`（微信晚开一会儿就永久下线）；
3. **开机那一刻没人点 UAC** → 提权失败就 `exit(2)`，pythonw 无窗口 ⇒ 一点提示都没有。

**现在**：`autostart.py on` 注册计划任务（`AtLogOn` + 每 5 分钟重复、`RunLevel=Highest`、
`MultipleInstances=IgnoreNew`），**静默拿到管理员**，而且挂了 5 分钟内自己回来。
菜单入口一字未改（`助手.bat → [8] → [6]`），但它现在需要一次 UAC（注册 Highest 任务）。

**`assume=True` 的用途随之收窄**：它现在的意思是「**这份进程没有控制台、没人能点 UAC**」，
判据是 `bot._has_interactive_console()`（`GetConsoleWindow()`）。计划任务那条路本来就是管理员，
`is_admin()` 先返回，走不到这一档；它兜的是"老机器上还留着 Run 值"或"有人用 pythonw 手起"。
**绝不**再写死 `assume=False` —— 那正是 2026-10-06 夜里 `exit(2)` 静默消失的成因。

完整决策、时间线与真机验证见 **`docs/autostart-task-notes.md`**。

## 五、自测与验证

* `selftest_admin.py`（24 项，**不弹 UAC**）：命令串形状、令牌一次性、
  五个分支（已管理员 / 提权一次 / 令牌命中不再提权 / UAC 被拒不降级 / 自启不弹窗）。
* `selftest_botctl.t5_start_guards`：提权被拒 → 启动失败且**一个进程都没拉起**；
  刚拉起提权进程 → 本进程不再拉进程；提权时确实把 **pythonw + bot.py** 传下去了。
* 端到端（真弹 UAC、人工点一下）：双击 `助手.bat` → 菜单里 `[3] 启动` →
  `bot.log` 里应出现 `[bot] 权限：已经是管理员`，且 `_audit\check_voice_ready.py`
  打印 ✅（这个脚本要和助手同权限跑）。

## 六、顺带的代价（用户已知晓，别自行加码）

助手以管理员运行时，它通过微信收到的「跑命令」（`run_command`）**也是管理员权限**。
用户 2026-10-06 的口径是：**只改提权，不动 `run_command` 的规矩** ——
`shell.auto_ok` 免确认名单继续按「整条精确相等」匹配，**不许**借这次机会扩大它。

## 附：同一批加的「hook 版本自检」（2026-10-06）

**症状**（真机）：用户换了**两遍**新包，助手仍卡在
「hook 已加载，但数据库打不开（微信没登录？请在微信里扫码登录）」，
而微信登录得好好的、库还在被写（诊断实测：2 秒前刚写过 `message_fts.db`）。

**根因**：「包里那份 `version.dll`」和「微信目录里已经装上的那份」是**两个文件** ——
解压新包**不会**替换已装的那份。已装那份是旧构建，它的闸门判据要求核心库
「**连续一直在写**」，安静时刻永远不成立 ⇒ `IsLogin` 恒 0。

**补上的机制**（`hook_check.py`，启动时各跑一次）：

| 时机 | 证据 | 能发现什么 |
|---|---|---|
| 启动、连微信之前 | 微信目录那份的 SHA256 vs 包里那份 | **装的是旧 hook**（该重装）—— 这是那时唯一能拿到的证据 |
| 连上 hook 之后 | `/QueryDB/status` 里有没有 `LoginGateInfo` | **文件换了但微信没重启**（旧进程还在答话） |

不一致就打印带**具体命令**的一句话（`助手.bat → [8] → [7] → [1] 装 hook`，
并提醒**装完必须重启微信**）。判据只写在 `hook_check.py` 一处；安装目录的查找规则与
`installers/.../_common.ps1` 的 `Find-Weixin` **必须一致**（两处各写一份就会分叉）。

**诊断工具随包走**：`hook_doctor.py`（只读）在**包根**和 `tools\` 各一份，
一次说清「微信里的 hook 是哪版 / 运行中的是哪版 / 闸门为什么没开 / 库有没有在被写」。
回归：`selftest_hook_check.py`（29 项）+ `selftest_portable.py` §7b
（doctor 存在、放包根、必须复用 `hook_check` 而不是再写一份判据）。
