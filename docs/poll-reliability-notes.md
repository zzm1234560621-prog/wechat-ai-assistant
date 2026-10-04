# 轮询的可靠性：长时间挂着，一说话就马上回复

记录时间 2026-10-05（真机事故当天修完）。这份文件是几个常量的**说明书**：

| 东西 | 在哪 | 值 |
|---|---|---|
| 一轮轮询的总时限 | `live_history.POLL_BUDGET_SEC` | 20 秒 |
| 轮询里单条查询的超时 | `live_history.QUERY_TIMEOUT` | 8 秒 |
| `message_N.db` 那条补捞路的重扫间隔 | `live_history.MSGDBS_RESCAN_INTERVAL` | 600 秒 |
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

## 回归

- `selftest_aixed.py`：新增一段「轮询的总时限 / 按次超时 / 重扫限流」——
  超时抛 `PollBudgetOut`、真客户端拿到按次超时、假客户端照常、重扫被限流、
  `message_N.db` 间隔 ≥ 10×默认、**hook 连不上时整轮跳过且游标原样返回**。
- `selftest_health.py`：新增「探针失败 ≠ 掉登录」（文案、计数、`login_ok is None`、`_healthy()`）。
- `selftest_bot_loop.py`：`t_probe_login` 改成断言三态。
- 全量：`selftest_all.py` **30/30 通过**。

## 调试时怎么用这几个数

- 用户说「发了消息没反应」→ 先看 `data/status.json` 的 `last_poll_at`：
  **它冻住 = 轮询停了**（对照 `bot.log` 的心跳）。再看有没有
  `本轮轮询超过总时限…被截断`：有的话就是 hook 慢，不是消息丢了。
- `_POLL_ERRORS` 里出现 `hook` 这个键 = 整个连不上（微信没跑 / hook 卡住 / 端口不对），
  和 `message_fts_v4_*`（句柄表要重扫）不是一回事，处置也不同。
- 别再把这些闸调回「不限时」：`begin_poll(0)` 只许给自测和手工查库用。
