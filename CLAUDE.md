# CLAUDE.md

个人微信 AI 助手：在微信原生窗口里跟 AI 对话，它能实时读本地聊天记录来回答，也能代你给别人发消息、自动回复。

技术路线**不是** README 里写的 wcferry 3.9.x，而是：微信降级到 **4.1.10.27** + 自编译的 **aixed hook**（`version.dll` 注入微信进程，起本地 HTTP 服务，默认 **30001**），bot 靠**轮询数据库**收消息。README.md 的前半部分仍是 wcferry 时代的文档，已过时，以本文件为准。

## 常用命令

```bash
# 跑自测（本地假服务，不需要真微信、不碰 hook）——改 live_history.py 后必跑
.venv/Scripts/python.exe selftest_aixed.py

# 起 bot（正常入口是双击 启动助手.bat；命令行仅用于调试）
.venv/Scripts/python.exe bot.py

# 看 bot 日志（后台无窗口运行时唯一的信息来源）
tail -f bot.log
```

## 架构

```
微信进程 ──[version.dll hook]──> HTTP :30001 (aixed_api.AixedClient)
                                      │ query_sql(db, sql)
                                      ▼
bot.py 主循环 ── 轮询 live_history.new_messages() ──> 收到消息
   │                    ↑ 每轮轮询的空档还跑一次 scheduler.run_due()（发定时消息）
   │
   ├─ / 开头        -> handle_command()        （改配置）
   ├─ 「确认」/「不发」-> agent_tools.pop_pending() （执行待确认发送）
   └─ 其他          -> build_user_prompt() -> run_agent()（带工具循环）-> 回复
```

- `live_history.py` — 查库核心，**双版本 schema 适配**（v3 = wcferry/3.9.x，v4 = aixed/4.1.x）。所有查询都经过它，别在别处裸调 `client.query_sql`。
- `agent_tools.py` — 给大模型的工具层（8 个工具）+ 待确认机制 + 查询预算。联系人解析统一走模块级的 `resolve_contacts` / `resolve_one`（`/定时` 命令复用同一套，重名规则才不会两处不一致）。
- `auto_reply.py` — 代用户本人回指定会话。
- `watch.py` — 盯着某个会话：他发消息就**通知我**、不回他。和 `auto_reply` 互补且互斥（同一会话同时开会既通知又回复），加的时候互相拦。
- `scheduler.py` — 定时任务（到点自动给对方发文本或打电话）。任务存在 `settings.json` 的 `schedule` 段，命令 `/定时` 维护；**必须跑在收消息那条线程上**，见下面「改代码时的约定」。
  - `action` 有三种：`text` 发固定内容 / `ask` 到点把 `text` 当提问跑一遍 agent、答案回控制会话（「每天早8点给我整理谁还没回我」就是这么做的）/ `call` 打电话（还没打通，只报错）。
- `llm.py` — anthropic / openai 两种协议，工具调用格式互转。
- 入口有三条，都会起 `bot.py`：`助手.bat` 菜单、`启动助手.bat`、开机自启注册表。

## ⚠️ hook 使用铁律

**这个 hook 前后把微信搞崩过 4 次**（最后一次 2026-10-01 00:08，`0xC0000005` 读 NULL，出错指令在 `Weixin.dll+0x32BB80D`）。崩溃的直接诱因是**两个 bot 同时在轮询**——已加了单实例锁（`bot.py:acquire_single_instance`，回环端口 39001），但这只是兜底，真正的死因是下面两条：

1. **绝不裸调 `GetAllDBName`。** 每调一次都在 700MB 进程里做一次全内存扫描（`getDatabaseInfo()` 先 `m_dbs.clear()` 再 `searchDatabases()`）。唯一允许的调用点是 `live_history.force_rescan()`（自带限流，只为拿「句柄表被重建」这个副作用）。想判断某个库在不在，探 `sqlite_master`。
2. **绝不做不带选择性过滤的排序查询。** 典型反例 `WHERE local_type=1 ORDER BY create_time DESC`（先匹配全部消息再排序），实测 0.3 秒起、劣化时到 6 秒。

守好这两条，其余查询都很快（实测 0.001~0.41 秒）。**`aixed_api.query_sql` 里有慢查询告警**（>1 秒打 `⚠️ 慢查询`）。跑起来后盯这个，一旦出现立刻停手。

**另一个判据**：`SELECT 1 FROM xxx LIMIT 1` 这种空探测如果超过 1 秒，说明卡的是**微信进程本身**（不是 SQL），必须立刻停手。

**`live_history.py` 是唯一应该读微信库的地方。** 新增查询请加在那里并复用它的缓存（`_cached` / `_cached_positive`），别自己拼 SQL。

## 微信 4.x 库结构（和 3.9.x 完全不同）

| | v3（3.9.x） | v4（4.1.x） |
|---|---|---|
| 库名 | `MicroMsg.db` / `MSG0.db` | `contact.db` / `message_0.db` |
| 分表 | 无 | **按会话分表** `Msg_<md5(会话名)>` |
| 列名 | `StrContent` / `CreateTime` / `IsSender` | `message_content` / `create_time` / `real_sender_id` |
| 判「谁发的」 | `IsSender` 字段 | 比对 `Name2Id.rowid`，需 `config.yaml` 的 `self_wxid` |

关键技巧：

- **主数据源是 FTS 库 `message_fts.db`**，不是 `message_0.db`（后者实测**常常解析不出句柄**，别依赖它）。
- 轮询用 **fts 的 `rowid` 当游标**（`WHERE rowid > N ORDER BY rowid` 是纯索引范围扫描，每分片 0.005 秒）。**别用 create_time**（要排全表）。
- 查某会话历史：`WHERE session_id = N ORDER BY create_time DESC LIMIT k` —— 过滤先缩小行集，所以快（0.41 秒）。
- 关键词检索走 `acontent MATCH '...'`（唯一有索引的路径；`message_fts.db` 带 fts5 + 微信自研中文分词器）。
- **fts 库的 `Name2Id` 和 `message_N.db` 的不是同一套 id**，`session_id` / `sender_id` 必须用 fts 库自己的解。
- 最近消息直接读 `session.db` 的 `SessionTable.summary`（一次查询 0.012 秒）；逐个会话去 FTS 捞要 4.4 秒。
- `all_contacts` 的 limit 别设小（用户有 10875 个联系人，曾写死 5000 导致按人名查历史时灵时不灵）。
- **`contact.db` 的表**（2026-10-01 实探）：`contact`、`chatroom_member`、`chat_room`、`chat_room_info_detail`、`stranger`、`biz_info`、`contact_label`、`name2id`、`encrypt_name2id` 等。
  - **群也在 `contact` 表里**（`username` 形如 `xxx@chatroom`，群名在 `nick_name`，`remark` 通常为空）。所以 `all_contacts()` 本来就覆盖群，按 roomid 找群名能直接命中。
  - **群成员别用 `chatroom_member`**：那张表只有 `(room_id, member_id)` 两个整数外键，还要再解一层 `name2id`。用 **`chat_room.ext_buffer`** —— 它是 protobuf，直接带 wxid + 群昵称（`live_history.decode_room_members` 已实现：字段 1=wxid、2=群昵称、3=角色(群主=9)、4=邀请人）。
- **`session.db` 的 `SessionTable` 比想象中富**：除 `summary` 外还有 `unread_count`（微信自己统计的未读，**别自己猜「最后一条不是我发的」**）、`last_msg_sender`、**`last_sender_display_name`**（微信算好的发言人显示名，群里就是群昵称——比拿 wxid 查联系人表准）。几百行的小表，一次查询。

## 「静默失效」是最大的坑

**fts 句柄掉了之后查询不报错、只返回 0 行**（连 `sqlite_master` 都列不出表）。表现是：**无报错、无日志、游标不动，看起来就是「bot 没反应」**。

- 恢复手段：`live_history.force_rescan(client)`（重建句柄表，自带 45s 限流，实测 1.8 秒修好）。
- 已加自愈：`_v4_fts_tables` 探测为空会自动 `force_rescan` 再重试一次，间隔由 `agent.fts_rescan_interval` 控制（默认 300s）。
- 另有 `_v4_new_messages_session` 只用 `session.db` 的 `SessionTable.summary` 兜底，fts 和 `Msg_` 表同时掉线也能收到消息。
- **debug 顺序**：
  0. **先分诊「是不是掉登录了」**：跑 `is_login()` / `self_profile()`。
     微信会**自己重启到登录界面**（换 PID、内存掉到 ~148MB、30001 仍在监听但 `IsLogin: 0`）——
     表现和 fts 静默失效几乎一样，但**恢复只能人工扫码**，`force_rescan` 没用还白花一次全内存扫描。
     判据：掉登录 → `is_login()` 为 False、`self_profile()` 全空；fts 失效 → 两者都正常、只是查不出行。
  1. 再看 `bot.log` 的轮询心跳（`[bot] 轮询心跳 #N，游标=X`）。游标不动就是 fts 那条。
  2. 才手工 `force_rescan`。
- **不需要重启 bot**——每轮空结果都会重查 `_v4_fts_tables`，修好后 5 秒内自动接上。
- 回归用例：`selftest_aixed.py` 的 `_V4StaleFtsStub`。

## 改代码时的约定

- **动 `live_history.py` 要同时照顾两套 schema**（v3 / v4），并把用例加进 `selftest_aixed.py`（用假服务，不碰真 hook）。
- **新增 agent 工具要改两处**：`agent_tools.TOOLS` 和 `config.yaml` 的 `system_prompt`。只加前者，模型根本不知道有这工具。
- **工具返回的文本要顺手告诉模型「该怎么办」。** 查库失败时别只回一句「失败：…」——模型会原地重试，而每次重试都是一次真实的 hook 调用。统一用 `agent_tools._db_fail()`。
- **往对话记忆里只放原始提问和最终答复**（`bot.dialog_*`），**绝不能放检索到的历史**——那段每轮都重算，记下来等于每轮重发整块历史，token 直接爆。
- **发消息是不可逆动作**，默认不许乱发：名单外的一律走「待确认」（`agent_tools`）。别绕过这个机制。文本/图片/转发的分派在 `agent_tools.send_pending()`。
- **`send_image` 的路径必须过 `_image_path_ok()` 白名单**。path 是**模型填的**，不校验就等于让它从你硬盘上挑任意文件发出去。默认只放行微信图片缓存目录，要加目录让**用户**改 `agent.send_image_dirs`，不要自己改配置绕。
- **重名不许静默取第一个。** 解析联系人统一走 `ToolBox._one()`，重名时回一句让模型去问用户——静默取第一个会读错人、发错人。
- **渲染「谁说的」一律用显示名。** 预取路径用 `bot._msg_speaker()`，工具路径用 `agent_tools.speaker_of()` / `format_history_lines()`。**绝不要把 talker（wxid / roomid）原样塞进给模型的文本**——模型会照抄一串 id 给你。这是 2026-10-01「看不到真正的名字」的根因。
- **hook 不支持并发**。工具串行执行，查询有预算（`agent.max_queries`）；连发消息是同步的、故意不开线程。任何"并发加速"的想法都会让微信崩。
- **定时任务同样不许开后台线程。** `scheduler.py` 靠 `bot._Ticker` 挂在**收消息那条线程**的轮询空档里跑（`iter_aixed_messages` / `iter_wcferry_messages` 各调一次）。代价是精度只有 `poll_interval`（默认 5 秒），换来「定时发消息」和「轮询」永不并发。往 `run_due` 里加新动作时别起线程。
- **不支持的功能要如实报错，不许静默降级。** 典型：定时任务里 `action: call`（语音通话）现在打不出去，`run_due` 就明确报错并通知用户，**绝不偷偷改成发文本**——那是在骗用户。加新功能时保持这条。
- **不要随手重启微信**：每次重启都会掉登录态，要重新扫码。
- **摘除 hook**：把微信目录的 `version.dll` 改名 `version.dll.disabled` 重启微信即可（脚本 `installers/wechat-4.1.10.27/do_remove_hook.ps1`，装回 `do_restore_hook.ps1`）。
- **改 hook 源码**（`installers/wechat-4.1.10.27/src-4.1.10.27/WeChat-Hook-4.1.10.27`）后重编译：
  ```bash
  "C:/Program Files/Microsoft Visual Studio/18/Community/MSBuild/Current/Bin/MSBuild.exe" \
    -p:Configuration=Release -p:Platform=x64
  ```
  MSBuild 开关要用 `-` 不能用 `/`（Git Bash 会把 `/m` 转成 `M:/`）。本机用的是 VS **18**。新 DLL 部署前**先验证发消息等现有功能正常**——编译产物和线上那个不是同一个二进制。

## 编译期踩过的坑

- `anthropic` SDK 的 `base_url=None` 会回退读环境变量 `ANTHROPIC_BASE_URL`，而 Claude 桌面应用会给子进程注入 `http://127.0.0.1:15721/claude-desktop`，导致 401。**必须显式写官方端点**（`llm.py` 已修）。
- 新版 anthropic SDK（1.8.0）把 `temperature` 从 `create()` 签名移除了，只能 `extra_body` 透传。
- `/api` 的 key 要容错：用户手打一遍 `sk-` 再粘贴会变成 `sk-sk-...`。

## 参考资料

- aixed hook 文档：`showdoc.com.cn`，**密码统一 1234**（各版本索引在 hook 源码的 README.md 里）。
- TG 交流群：`t.me/WeChat_Hook`（作者的导出快照已失效，别引用）。
- 源码快照两份，**xLog hook 偏移不同，不要混用**：项目内 `installers/wechat-4.1.10.27/src-4.1.10.27/`、`C:\Users\zzm12\Downloads\WeChat-Hook-411027_.zip`。
- 图片加密：`docs/wechat4-dat-image-notes.md`；hook 反篡改：`docs/hook-anti-tamper-notes.md`。
- **做不了的事**：hook 没有任何通话接口，`VoipEngine.dll` 那条路要自逆向 `Weixin.dll`，且 README 说明 main 分支已移除协议直发能力。别去文档里找通话接口。
