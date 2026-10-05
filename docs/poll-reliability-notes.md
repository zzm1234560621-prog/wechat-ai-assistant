# 轮询的可靠性：长时间挂着，一说话就马上回复

记录时间 2026-10-05（真机事故当天修完）。这份文件是几个常量的**说明书**：

| 东西 | 在哪 | 值 |
|---|---|---|
| 一轮轮询的总时限 | `live_history.POLL_BUDGET_SEC` | 20 秒 |
| 轮询里单条查询的超时 | `live_history.QUERY_TIMEOUT` | 8 秒 |
| `message_N.db` 那条补捞路的重扫间隔 | `live_history.MSGDBS_RESCAN_INTERVAL` | 600 秒 |
| 分片熔断的阈值 / 冷却 | `live_history.SHARD_FAIL_LIMIT` / `SHARD_COOLDOWN_SEC` | 3 次 / 30 秒 |
| 一轮里**每个 fts 分片**最多取多少行 | `live_history.POLL_ROWS_PER_SHARD` | 20（原 200） |
| 连续有消息时两轮之间的最小间隔 | `bot._min_round_interval(cfg)`（config `poll_min_interval`） | 1 秒（0 = 关） |
| appmsg 原文回查的熔断 | 共用 `message_0.db` 那把（`shard_blocked`），取不到时贴 `live_history.APPMSG_NO_XML_NOTE` | 30 秒 / 一句如实标注 |
| 按次超时的能力开关 | `aixed_api.AixedClient.supports_call_timeout` | `True` |
| 登录态探测的三态 | `bot._probe_login` / `health.Health.note_login` | True / False / **None** |

## 事故现场（2026-10-05 00:11，开发机）

用户在微信里发了一句「你好」，**没有任何反应**。取证：

- `message_fts_v4_0` 里 **有**那条消息（`rowid=200466`），说明它进了库；
- bot 的游标卡在它**前面两行**（`200464`），一直没动；
- `data/status.json` 的 `last_poll_at` 冻在 **00:11:25**，`bot.log` 从 **00:11:27** 起
  再没写过一行（心跳本该每 2.5 分钟一行）；
- 但 bot 进程**活着**（39001 拿着、`data/state.json` 每 10 秒还在写）；
- 卡住前一刻：四个分片同时 `WinError 10061 连接被拒`；
- 同一时刻直接打 hook：`/QueryDB/status` **200**、查 fts **能出数据**、微信进程在跑、
  30001 在听 —— 也就是说**微信和 hook 都是好的**，是 bot 自己那一轮出不来了。

根因不是「掉登录」（当时 `[health]` 就是这么写的，方向全错），而是：

**一轮轮询要发 6~7 个查询，每个按客户端默认的 15 秒超时算，最坏一轮 100 秒；
而心跳是「每 30 轮一行」→ 最坏 45 分钟才出一行日志。** hook 只要卡几分钟（它有这毛病：
所有连接都回 10061 或不回应），bot 就进入这种「进程活着、但几分钟不推进」的状态，
用户看到的就是「发消息它不理我」。

## 四条改动

### 1 · 一轮一个总时限（`begin_poll` / `poll_budget_left` / `PollBudgetOut`）

`live_history.new_messages()` 一进来就 `begin_poll()`，之后**每个查询**都受它约束：
`_query()` 先看还剩多少时间，超了直接抛 `PollBudgetOut`（调用方按「本轮没查到」处理）。
顺带把单查询超时压到 `QUERY_TIMEOUT = 8` 秒。

- **消息不会丢**：失败时游标只前进到确实取到的那一行，下一轮还会再试。
- **有痕**：一轮被截断会打一行 `[live] ⚠️ 本轮轮询超过总时限（20s）被截断…`（30 秒一行，防刷屏）。

### 2 · 每轮先用最便宜的请求探活（`new_messages` 开头那个 `db_status()`）

hook 卡住时，一轮那 6~7 个查询会全部撞超时——**那是在往一个已经卡死的服务上继续加压**，
实测会把它的 listen backlog 顶满，之后连 TCP 都直接 10061（这就是「连接被拒」的来源）。

所以每轮先打一个 `/QueryDB/status`（**不碰任何数据库句柄**、实测 0.04 秒）：

- 不通 → 整轮跳过，这一轮只发这 **1** 个请求，并记一笔 `_POLL_ERRORS["hook"]`（心跳里看得见）；
- 通了 → 照常跑完整轮。

代价是每轮多一个廉价请求；换来的是：**hook 一恢复，同一轮就能把新消息捞上来（≤5 秒）**，
不用等下一轮，也不用等超时把一轮耗完。自测里的假客户端没有 `db_status`，会跳过这一关
（行为与以前一致，所以那一堆老用例不受影响）。

### 3 · `message_N.db` 不许每 45 秒触发一次全内存扫描

`message_N.db`（补捞图片/文件用的分片）实测**经常就是解析不出句柄**（CLAUDE.md 记着这条），
于是每轮 `_probe_heal` 探不通 → 每 45 秒触发一次 `force_rescan` → `GetAllDBName` =
**700MB 进程里的全内存扫描**。那是给微信上负担，也是把一轮轮询拖慢的另一个源头。

现在这条路用 `min_interval=MSGDBS_RESCAN_INTERVAL`（10 分钟）；fts / contact 那些权威路
保持默认 45 秒。`force_rescan` 那次调用本身也给 `QUERY_TIMEOUT`——我们要的只是服务端
「句柄表被重建」这个副作用，等它把结果吐完没有意义。

### 4 · 附带：`health` 不再把「连不上」说成「掉登录」

`bot._probe_login()` 现在返回**三态**：`True` 在线 / `False` 明确掉登录 / `None` 探针本身失败。
`health.note_login(None, ...)` 会告警「**探不到登录态（连不上 hook）**」并明说
「**这不等于掉登录**」，且不计入 `login_lost_count`（新增 `login_probe_failed_count`）。
旧实现在这种情况下写的是「微信似乎回到登录界面了（IsLogin=0）…请打开微信重新登录」——
真机就是这么把排查带偏的：微信好好的，用户被叫去扫码。

### 5 · 分片熔断：探活通、查询全 500 时别再每 5 秒重发（2026-10-05 第二次崩溃后加的）

第 2 条那道闸管的是「**整个连不上**」（探活失败 ⇒ 整轮只发 1 个请求）。但真机第二次
崩溃是**另一种形态**：

```
[aixed] ⚠️ 慢查询 4.49s  db=message_fts.db …
[aixed] ⚠️ 慢查询 5.39s  db=contact.db   SELECT 1 FROM contact LIMIT 1   ← 空探测 5.39 秒
[live]  ⚠️ 轮询 message_fts_v4_0/1/2/3 连续失败：/QueryDB/execute 返回 HTTP 500
[live]  ⚠️ 轮询 hook 连续失败：连不上 30001（WinError 10054 远程主机强迫关闭了一个现有的连接）
```

探活是通的、**具体查询在 500**。这时第 2 条闸放行整轮，于是 bot 每 5 秒照发
4 个 fts 分片 + `message_0.db` —— 一路砸在一个已经出错的 hook 上。

现在同一条分片连错 `SHARD_FAIL_LIMIT`（3）次就**熔断** `SHARD_COOLDOWN_SEC`（30）秒：
这期间不发这个查询，到点自动**半开**（放一次过去），一次成功立刻解除（用
`_note_poll_ok()`，恢复不等冷却）。熔断提示 5 分钟最多一行。

**为什么安全（只会晚、不会丢）**：失败就是失败 —— `message_fts_v4_*` 的 rowid 游标、
`__nonttext_seq__` / `__nonttext_pending__` 水位线都**不前进**，冷却结束从原地接着查。
熔断只在「已经查不动」时触发，正常时**零代价**。

⚠️ 只挂**允许延迟**的两条路：fts 分片查询、`message_0.db` 的非文本补捞。
**绝不**挂到 `session.db` 兜底 / 按会话表那两层 —— 那是最后一道防线，静音它们
等于「静默收不到消息」，正是本项目最不能有的形态。

### 6 · 追赶限速：有消息也不许满速扫库（2026-10-05 真机，第二次崩溃当天）

生产日志现场（助手重启后正在追赶积压）：

```
[bot] 轮询心跳 #300，游标={'message_fts_v4_0': 148003, '…v4_2': 129421, '…v4_3': 111359}
[bot] 轮询心跳 #330，游标={'message_fts_v4_0': 154003, '…v4_2': 135421, '…v4_3': 117359}
[aixed] ⚠️ 慢查询 1.59s  db=message_0.db  sql=SELECT message_content FROM Msg_ab2ad322… WHERE …
[live]  ⚠️ 轮询 message_0.db 连续失败：查库 message_0.db 失败：get database handle … failed
```

- 心跳之间（30 轮）**每个分片各 +6000 = 每轮满页 200 行**；实测约 **2 秒/轮**
  （`poll_count` 310 → 341 / 65 秒），折算 ≈ **300 行/秒**。原因是主循环
  「有消息就不 sleep」—— 稳态没问题，追赶积压时就是连轴转。
- 而且 `_v4_new_messages` 对每条 appmsg（链接/文件/引用）都会**再回查一次
  `message_N.db`** 拿原始 XML（`_fetch_message_xml`）。句柄坏掉时那就是
  1.59 秒的慢查询 + HTTP 500 —— **持续把失败查询压在一个已经出错的 hook 上**。

两道闸：

| # | 改动 | 关键数字 / 所有者 |
|---|---|---|
| 1 | 每分片每轮行数 **200 → 20**。`limit` 的唯一所有者是 `live_history`；`aixed_api.poll_messages` 的 `limit` 默认改成 `None`（不再自己写死 200 —— 那是第二个所有者，改了不生效） | `live_history.POLL_ROWS_PER_SHARD = 20` |
| 2 | 主循环「有消息也不许连轴转」：一轮至少占满 `poll_min_interval` 秒；**没消息时照旧睡 `poll_interval`** | `bot._min_round_interval` / `bot._round_sleep`、config `poll_min_interval: 1` |

⚠️ 代价要认：**追赶会变慢**（同样的积压，每轮行数少一个数量级 ⇒ 追赶时间多一个数量级）。
换来的是不再以 300 行/秒压 hook。**消息不丢**：游标与水位线照旧只前进、失败不推进。

### 7 · appmsg 原文回查接同一把熔断 + 取不到就如实说（2026-10-05 真机）

`_v4_new_messages` / `_v4_fts_rows` 对每条 appmsg（链接/引用/文件）都要回查一次
`message_N.db` 拿原始 XML（`_fetch_message_xml`）。而 `_v4_msg_dbs` **只在探到分片时才缓存**
（探不到 = 空结果不缓存）⇒ 句柄坏掉时**每条 appmsg 最多再打 8 次探测查询**。真机现场：

```
[aixed] ⚠️ 慢查询 1.59s  db=message_0.db  sql=SELECT message_content FROM Msg_ab2ad322… WHERE …
[live]  ⚠️ 轮询 message_0.db 连续失败：查库 message_0.db 失败：get database handle … failed
```

两处改动：

1. `_fetch_message_xml` 走 `message_0.db` 那把熔断：熔断中**一个查询都不发**；
   失败计入熔断计数（`_v4_msg_dbs` 探不到分片这件事以前是静默的，现在也算失败）；
   一旦查得动就 `_note_poll_ok` **立刻清账**（不留「库坏了」的假告警——这也是
   `healthy: false` 一直挂着的那条）。
2. 取不到时**如实标注**：新增唯一所有者 `_appmsg_text()`，两条调用点共用，
   返回 `摘要 + APPMSG_NO_XML_NOTE`。绝不容许静默降级 —— 「模型只看到用户打的那几个字」
   正是「引用消息答非所问」的老根因（见 `_v4_new_messages` 里 appmsg 那段的注释）。

### 8 · 死游标对齐：游标 > 头部 = 这条分片永远读不到新行（2026-10-05，用户拍板只做这一半）

`message_fts_v4_*` 的 **rowid 空间在分片被重建之后会从头开始**（`force_rescan` / 微信自己重建索引）。
旧游标比新表头部还大 ⇒ `WHERE rowid > 游标` **永远 0 行**：这条分片此后一条新消息都收不到，
而且**不报错、不留痕** —— 正是本项目最怕的「静默失效」（用户看到的只是「它不理我」）。

`live_history.align_stale_cursors(client, cursors)`：只在 `游标 > 头部` 时把该分片对齐到头部，
返回 `(新游标, {分片: (旧, 新头部)})`。**启动时**由 `bot.iter_aixed_messages` 调一次，
有修就打印并给控制会话发一条如实通知。

**对齐不会丢任何东西**：比新头部更大的 rowid 本来就不存在，那条查询一行都取不到 ——
这不是「跳过历史」，是把一条**死游标**救活（`selftest_live_history._t_align_stale_cursor`
里钉着「对齐前连新消息都收不到、对齐后立刻收得到」）。

⚠️ **故意没做的另一半**：游标**落后**头部很多就跳到头部。那会真少一批旧通知，属于语义变更，
必须用户明确点头；当前兜底是「每轮 20 行 + 两轮至少隔 1 秒」，追赶只是慢，会自己停。

⚠️ 查不到头部的分片**一律不动**（失败要保守，绝不猜）。

### 8b · 「落后太多就跳到头部」（2026-10-05，用户明确要：**它丢通知**，换回「不落后、不卡」）

同一天用户又拍板做了另一半。`live_history.skip_far_behind(client, cursors, max_gap)`：
某个分片的 `头部 - 游标 > max_gap` 就**直接跳到头部**，返回 `{分片: (旧, 新头, 跳过行数)}`。

为什么值得丢通知：真机见过 `v4_2` 从 `3400` 一路追到 `135421` ≈ **13 万行**，按每轮 20 行
（见 §6）要追**几小时**，而这期间**新消息全排在那批历史后面** —— 用户看到的是「发消息半天不回」。
跳过去之后新消息立刻就能回。

三道保险（缺一不可）：

1. **阈值可调、可关**：config `poll_max_catchup`（默认 `5000` 行/分片 = `CURSOR_MAX_GAP`；**0 = 关**）；
   配置读不出来时回**默认阈值**，**不许**静默变成 0（那等于悄悄把闸关了）。
2. **每次触发都如实说**：bot 打印一行 + 往控制会话发一条（「约 N 条旧消息不会再逐条通知，
   想看就翻 XX 会话」），并告诉用户怎么改回慢慢追。
3. **只在启动时判一次**（和 §8 的死游标对齐同一处，顺序：先对齐死的，再判落后的）。
   运行中落后是限速下的正常追赶 —— 那时跳会把**刚发生**的消息也丢掉，比启动时危险得多。

### 9 · 会话名拿不到 ≠ 消息不重要（2026-10-05 真机：一条「你好」静默消失）

现场：用户在控制会话发「你好」，助手**读过它**（`message_fts_v4_0` **rowid 3284**，游标
`3283 → 3285`）却既没回、也**一行日志都没有**。倒推出来：那次启动正撞在微信**重建索引**
的窗口里，`_v4_fts_session_map`（rowid → 会话名，**缓存 10 分钟**）是重建中途建的 →
`filehelper` 被认成 `session_297` → bot 主循环按「不是我该管的会话」`continue` 丢掉，
而**那条分支不打日志**。

| # | 改动 | 关键点 |
|---|---|---|
| 1 | `live_history._v4_fts_session_map(client, refresh=True)`：**查不到会话名时先刷新一次映射**再判（一轮最多刷新一次） | 不拿 10 分钟的旧缓存把一个会话判死；刷新拿到名字 → 消息照常处理 |
| 2 | **留痕**：刷新后仍查不到 → `live_history.note_talker_miss()`（60 秒限流）打一行；bot 侧 `_note_offtarget_skip(sender)` 对 `session_<N>` 这种**故障形状**再打一行 | 正常群消息照旧安静（那是有意丢的）；**故障形状**一定留痕，能一眼区分「真的没消息」与「消息被丢了」 |

⚠️ 判据：`session_<N>` 是**故障形状**，不是正常会话名 —— 它意味着「这条消息拿不到会话名」。

## 回归

- `selftest_aixed.py`：新增一段「轮询的总时限 / 按次超时 / 重扫限流」——
  超时抛 `PollBudgetOut`、真客户端拿到按次超时、假客户端照常、重扫被限流、
  `message_N.db` 间隔 ≥ 10×默认、**hook 连不上时整轮跳过且游标原样返回**。
- `selftest_aixed.py`：新增一段「分片熔断」——熔断前照查（有对照）、连错到阈值被熔断、
  熔断期间**一个查询都不发**、**游标原样不动**、冷却到点自动半开、一次成功立刻解除。
- `selftest_health.py`：新增「探针失败 ≠ 掉登录」（文案、计数、`login_ok is None`、`_healthy()`）。
  另加「相对 `status_file` 按项目目录解析（不跟 CWD 跑）」与「每轮都失败的告警被节流 +
  窗过后如实补报折叠次数」。
- `selftest_bot_loop.py`：`t_probe_login` 改成断言三态；新增 `t_chdir_project_root`
  （CWD 归位 / 返回原目录 / 已在项目目录时不重复切）；新增 `t_round_pacing`
  （没消息照旧睡 `poll_interval`、有消息补到 `min_round`、本来就够长就不额外睡、
  `poll_min_interval=0`＝关、配置读不出来时回默认 1 而不是静默变成 0）。
- `selftest_aixed.py`：新增「每轮取多少行」——`POLL_ROWS_PER_SHARD ≤ 50`、
  **fts 分片 SQL 里真的用了它**（不是一个常量摆着没人用）、
  `aixed_api.poll_messages` 的 `limit` 默认不再是写死的 200（第二个所有者）。
- `selftest_live_history.py`：新增 `_t_appmsg_breaker` —— 取到原文就用原文（不贴标注）、
  **熔断中一个查询都不发**、熔断中也**如实标注**「原始内容没取到」、回查失败计入熔断、
  冷却到点查得动就立刻解除并清账。
- `selftest_live_history.py`：新增 `_t_align_stale_cursor` —— 只有 `游标 > 头部` 才对
  （落后的**一个字都不动**）、相等不误报、对齐后落在**当时**的头部、
  **对齐前连新消息都收不到 / 对齐后立刻收得到**、头部查不到时保守不动。
- `selftest_live_history.py`：新增 `_t_talker_miss` —— 查不到会话名时**先刷新一次映射**、
  刷新拿到名字后 `talker` 就是 `filehelper`、刷新后仍查不到才退回编号并**留一行痕**、
  留痕有限流。
- `selftest_bot_loop.py`：新增 `t_offtarget_note` —— 普通群消息（有名字的会话）**不留痕**、
  只有 `session_<N>` 这种故障形状才留痕且说清「这不是没消息」、留痕限流。
  另加 `t_max_catchup` —— 缺省用默认阈值、配置说了算、**0 = 关**、读不出来回默认（不静默关闸）。
- `selftest_live_history.py`：新增 `_t_skip_far_behind` —— 落后在阈值内**一个字都不动**、
  超阈值才跳到头部、**如实报出跳过行数**、没超的分片不动、`max_gap=0` 关闸、
  垃圾阈值回默认、头部查不到保守不动。
- ⚠️ 在本机这种**受限环境**跑自测要设 `PROJ_TMP` 改道（项目里 `data/` 可能对当前
  会话不可写，症状是 `read_file` 报 `Errno 13` 而看不出原因）：
  `PROJ_TMP=%TEMP%\projtmp .venv/Scripts/python.exe selftest_bot_loop.py`。
- 全量：`selftest_all.py` **30/30 通过**。

## 调试时怎么用这几个数

- 用户说「发了消息没反应」→ 先看 `data/status.json` 的 `last_poll_at`：
  **它冻住 = 轮询停了**（对照 `bot.log` 的心跳）。再看有没有
  `本轮轮询超过总时限…被截断`：有的话就是 hook 慢，不是消息丢了。
- `_POLL_ERRORS` 里出现 `hook` 这个键 = 整个连不上（微信没跑 / hook 卡住 / 端口不对），
  和 `message_fts_v4_*`（句柄表要重扫）不是一回事，处置也不同。
- 别再把这些闸调回「不限时」：`begin_poll(0)` 只许给自测和手工查库用。
