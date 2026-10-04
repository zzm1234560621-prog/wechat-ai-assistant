# 微信 4.x 库结构（和 3.9.x 完全不同）

> **为什么单独一份**：`CLAUDE.md` 有约 64KB **指令预算**，超了**尾部会被静默截掉**
> （2026-10-04 当天就当场发生过一次：加到 65325 字节时注入的指令被截到 65204，
> 尾部那条「打电话」被切在半句话上）。为给它腾出真实余量，2026-10-04 把这一节整体搬来。
> **内容逐字未改**；`CLAUDE.md` 里留了要紧的四条与本文指针。
>
> 相关的两份细节（都在 `docs/`）：`wechat4-dat-image-notes.md`（图 / 非文本补捞的完整演进）、
> `broadcast-group-notes.md`（标签成员藏在 `contact_fts_v5.search_key` 那段）。

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
- **取会话历史的返回行必须带 `local_type`**（`_v4_fts_rows` 2026-10-01 补上；表那条路
  `_v4_history_from_tables` 一直有；v3 那条 SQL 写死 `Type = 1` 所以显式给 1）。
  理由：历史里非文本消息会被渲染成 `[图片]` 这类标签，**content 看上去和真文本一样**，
  下游（`auto_reply._learn_messages` 挑「用户自己发的文本」当语气样本）只能靠这个字段分辨。
  fts 那条路以前把 local_type **算完就丢**，于是那条过滤在**真机上根本不生效**，
  而当时的自测喂的是带 local_type 的假数据 —— **测试是绿的、生产是漏的**。
  用例：`selftest_live_history._t_fts_history_fields`（含跨模块契约：学语气只取我发的文本）、
  `selftest_aixed.py` 里 v3 的 `query_contact_history()` 断言。
- **⚠️ 图片消息不在 fts 里（2026-10-01 实测）。** 四个分片的
  `SELECT COUNT(*) FROM <分片> WHERE local_type = 3` **全是 0** —— 微信的 fts 只索引
  有文本内容的行。后果：**只靠 fts 游标轮询，永远看不见别人发来的图片**
  （表现就是「我把图发过去了，它一点反应没有」）。而另一条路
  `_v4_new_messages_session` 靠 `SessionTable.summary`，图片的 summary 是**空串**，
  被 `if not content: continue` 跳过 —— **两条通路都瞎**。
  这就是 `live_history._v4_pickup_nontext` 存在的原因：**会话只要有动静就回查一次它的
  消息表**，再**按行**挑出非文本（文本/appmsg 丢给 fts，别重复报）；水位线
  `cursors["__nonttext__"]` 防重复，**稳态 0 个会话命中 → 一次消息表都不查**。
  ⚠️ **判据绝不能退回「最后一条不是文本」**（`last_msg_type NOT IN (1,49)`）——
  2026-10-04 真机：**图后面紧跟一句话**时那张图**永久消失**（fts 没有它、summary 也不认它）。
  两道闸（一轮最多扫 `_NONTEXT_MAX_SESSIONS`(8) 个 + `__nonttext_pending__` 下一轮
  不看 `since` 也照样扫）与完整演进见 **`docs/wechat4-dat-image-notes.md`**。
  **改收消息通路时，必须同时想「fts 装不下的类型怎么办」。**
- **自己发出去的图也会回显成一条新消息**（因为上面那条补捞）。文本有
  `bot.remember_sent` / `is_own_reply` 兜着，**图片没有** —— 所以每条发图路径都要调
  `agent_tools.remember_sent_image()`，主循环用 `is_own_image()` 把回显认掉。
  时间窗只有 30 秒，取不到消息时间就**不当成自己的**：宁可漏判（自聊时多答一句），
  也绝不误判（那会把**对方真发来的图静默丢掉**）。回归用例在 `selftest_live_history.py`
  与 `selftest_bot_loop.py`。
- 最近消息直接读 `session.db` 的 `SessionTable.summary`（一次查询 0.012 秒）；逐个会话去 FTS 捞要 4.4 秒。
- `all_contacts` 的 limit 别设小（用户有 10875 个联系人，曾写死 5000 导致按人名查历史时灵时不灵）。
- **`contact.db` 的表**（2026-10-01 实探）：`contact`、`chatroom_member`、`chat_room`、`chat_room_info_detail`、`stranger`、`biz_info`、`contact_label`、`name2id`、`encrypt_name2id` 等。
  - **群也在 `contact` 表里**（`username` 形如 `xxx@chatroom`，群名在 `nick_name`，`remark` 通常为空）。所以 `all_contacts()` 本来就覆盖群，按 roomid 找群名能直接命中。
  - **群成员别用 `chatroom_member`**：那张表只有 `(room_id, member_id)` 两个整数外键，还要再解一层 `name2id`。用 **`chat_room.ext_buffer`** —— 它是 protobuf，直接带 wxid + 群昵称（`live_history.decode_room_members` 已实现：字段 1=wxid、2=群昵称、3=角色(群主=9)、4=邀请人）。
- **`session.db` 的 `SessionTable` 比想象中富**：除 `summary` 外还有 `unread_count`（微信自己统计的未读，**别自己猜「最后一条不是我发的」**）、`last_msg_sender`、**`last_sender_display_name`**（微信算好的发言人显示名，群里就是群昵称——比拿 wxid 查联系人表准）。几百行的小表，一次查询。
