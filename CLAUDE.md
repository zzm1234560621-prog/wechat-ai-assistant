# CLAUDE.md

个人微信 AI 助手：在微信原生窗口里跟 AI 对话，它能实时读本地聊天记录来回答，也能代你给别人发消息、自动回复。

技术路线**不是** wcferry 3.9.x，而是：微信 **4.1.10.27** + 自编译的 **aixed hook**（`version.dll` 注入微信进程，起本地 HTTP 服务，默认 **30001**），bot 靠**轮询数据库**收消息。wcferry/3.9.x 是**保留的另一条后端**（`backend: wcferry`），不是主线。

> README.md 的技术路线与安装段已经按这条主线重写过（用户按它走能装出能用的东西）；wcferry 的旧步骤在 README 里已明确标成「仅 3.9.x」。本文件仍是架构与踩坑的权威。

## 常用命令

```bash
# 跑自测（本地假服务，不需要真微信、不碰 hook）——改 live_history.py 后必跑
.venv/Scripts/python.exe selftest_aixed.py

# 其余自测（同样不联网、不碰 30001、不需要真微信）
.venv/Scripts/python.exe selftest_live_history.py   # live_history 兜底路径
.venv/Scripts/python.exe selftest_policy.py         # 待确认队列 / 发图白名单 / 查询预算
.venv/Scripts/python.exe selftest_sched_auto.py     # scheduler / auto_reply
.venv/Scripts/python.exe selftest_io_llm.py         # file_read / llm / settings
.venv/Scripts/python.exe selftest_redact_usage.py   # redact / usage
.venv/Scripts/python.exe selftest_health.py         # health / status_page
.venv/Scripts/python.exe selftest_bot_loop.py       # bot 主循环侧改动
.venv/Scripts/python.exe selftest_audio.py          # 语音输入（音频转文字）
.venv/Scripts/python.exe selftest_install.py        # 安装/环境链路
.venv/Scripts/python.exe selftest_executor_chain.py # 本地执行确认闸门
.venv/Scripts/python.exe executor_selftest.py       # executor 独立自测

# 起 bot（正常入口是双击 启动助手.bat；命令行仅用于调试）
.venv/Scripts/python.exe bot.py

# 真机自检（**必须先停 bot**；只读，脚本自己会拒绝「bot 在跑」的情况）
# 查：hook/登录态、库结构、游标、联系人、发图白名单、落盘状态与账本
.venv/Scripts/python.exe verify_real.py

# 语音输入（音频 → 文字）。模型**只由用户显式执行才下**，绝不从聊天路径触发：
.venv/Scripts/python.exe audio_read.py --setup          # 下本地模型（走 hf-mirror）
.venv/Scripts/python.exe audio_read.py --transcribe x.m4a

# 看 bot 日志（后台无窗口运行时唯一的信息来源；会自动轮转，见下）
tail -f bot.log
```

## 架构

```
微信进程 ──[version.dll hook]──> HTTP :30001 (aixed_api.AixedClient)
                                      │ query_sql(db, sql)
                                      ▼
bot.py 主循环 ── 轮询 live_history.new_messages() ──> 收到消息
   │                    ↑ 每轮轮询的空档还跑一次 scheduler.run_due()（发定时消息）
   │                    └ 顺手给 health.note_poll() 记一笔（纯内存，不查库）
   │
   ├─ / 开头        -> handle_command()        （改配置）＋ /用量、/status 的运行健康
   ├─ 「确认」/「不发」-> agent_tools.pop_pending() （执行待确认发送；多条时先回编号菜单）
   └─ 其他          -> build_user_prompt()（可选 redact）-> run_agent()（带工具循环）-> 回复
```

外围（都挂在同一条线程/纯内存，**绝不自己查库、绝不起线程碰 hook**）：

```
health.rotate_log()  ← setup_logging() 打开 bot.log **之前**（轮转 .1/.2/.3）
health.Health        ← 记账 + 掉登录告警 + 落 data/status.json
status_page.start()  ← 只读状态页（默认关，只绑回环），渲染 Health.snapshot()
usage / redact       ← /用量 读 data/usage.jsonl；redact 只作用于送云端的那一份文本
```

- `live_history.py` — 查库核心，**双版本 schema 适配**（v3 = wcferry/3.9.x，v4 = aixed/4.1.x）。所有查询都经过它，别在别处裸调 `client.query_sql`。
- `agent_tools.py` — 给大模型的工具层（**20 个工具**：find_contact / send_text / read_history / search_history / auto_reply / schedule / watch / find_images / read_image / find_files / read_file / recent_messages / search_in_chat / pending_replies / group_members / send_image / send_images / forward_message / send_asset / run_command）+ 待确认机制 + 查询预算。联系人解析统一走模块级的 `resolve_contacts` / `resolve_one`（`/定时` 命令复用同一套，重名规则才不会两处不一致）。
- `assets.py` — **素材暂存区**：用户在控制会话里发一次图/表情，之后说「发给谁」就能再发。见下面「素材暂存」。
- `auto_reply.py` — 代用户本人回指定会话。
  - **审核是「每个会话一份」，全局那份只是默认值**（`review_on(rec, cfg)`：`rec["review"]` 优先，`None` 才继承全局）。
    所以「只让某个人免确认」是 `/auto review off 张三`，不该动全局。
  - **模型那条路必须显式说明范围**：`agent_tools.t_auto_reply` 里 `review` **不带 `who` 直接拦住**，
    要改全局得写 `who=全局`；`on/off` 是**全局总开关、不认 `who`**（以前传了被静默丢掉）。
    真机踩过（2026-10-01）：用户说「给李同学加上自动回复，不用我同意内容」，模型调
    `review`+`review=false` 没带 who → **所有人**的审核都被关了（它回复里补了一句
    「注意：审核是全局开关」，但用户仍然被搞混）。根因**不是模型撒谎**，而是
    **工具说明只教了 `action=review, review=false` 这种写法、压根没提 who**，
    加上工具层允许漏参数静默改全局——**静默扩大影响面**才是要堵的那一头。
    人手打 `/auto review on`（不带对象）改全局仍然照旧：人的明确意图，模型漏参数不算。
    回归用例：`selftest_sched_auto.t9_review_scope_is_explicit`。
- `watch.py` — 盯着某个会话：他发消息就**通知我**、不回他。和 `auto_reply` 互补且互斥（同一会话同时开会既通知又回复），加的时候互相拦。
- `executor.py` — **本地执行**：subprocess 跑一条命令行命令（同步、带超时/输出上限/工作目录）。
  **它只管"怎么跑"，不管"该不该跑"**——要不要跑由上层把关，见下面「本地执行」。
  自测：`.venv/Scripts/python.exe executor.py`（另有两份：`executor_selftest.py` 纯逻辑、`selftest_executor_chain.py` 确认闸门链路）。
- `scheduler.py` — 定时任务（到点自动给对方发文本或打电话）。任务存在 `settings.json` 的 `schedule` 段，命令 `/定时` 维护；**必须跑在收消息那条线程上**，见下面「改代码时的约定」。
  - `action` 有三种：`text` 发固定内容 / `ask` 到点把 `text` 当提问跑一遍 agent、答案回控制会话（「每天早8点给我整理谁还没回我」就是这么做的）/ `call` 打电话（还没打通，只报错）。
  - **时间写法**（`scheduler.parse_when`）认：`9:00`=每天、`明天9:00`/`10-02 9:00`=只一次、
    `每周一 9:00`、`每30分钟`、`9点半`，以及**相对一次性** `10分钟后` / `半小时后` /
    `2小时后` / `3天后`（换算成 `date`+`at` 的绝对时刻，**向上取整到分钟**——宁可晚十几秒，
    也绝不比用户说的更早触发；这样 `next_ts` 仍是墙上时钟，重启不漂）。
    ⚠️ 相对这一支以前**没有**：用户说「10分钟后」，`parse_when` 会掉到最后的 `_hhmm()` 兜底，
    报「时间「10分钟后」没看懂」，然后助手让用户改说具体时刻——真机踩过（2026-10-01），
    别再删。**改 `parse_when` 要顺带看 `_REL_RE` 别把「每N分钟」（重复规则）抢走**。
    回归：`selftest_sched_auto.t10_relative_time`。
- `file_read.py` — 读**别人发来的文件**（pdf/docx/xlsx/pptx/文本）。微信把收到的文件明文放在 `<数据目录>/<账号>/msg/file/<年-月>/`，不用解密；文件名从消息的 appmsg XML 里拿。**只允许读那个目录**，按文件名匹配，不接任意路径。PDF 走 `pypdf`（在 `requirements.txt` 里）。
- `image_read.py` / `file_read.py` 都是「把本地文件变成文字喂给模型」，区别是图片要 OCR、文件要解析。
- `audio_read.py` — **语音输入**：把**音频文件**（`.m4a/.mp3/.wav/.amr`…）转成文字，
  由 `file_read.extract()` 按扩展名分派过来（**没有新工具，还是 `read_file`**）。规格：`docs/voice-input-spec.md`。
  - **范围**：只处理**当文件发来**的音频（明文在 `<账号>/msg/file/<月>/`）。
    ❌ **微信语音条**（那个小喇叭，`local_type=34`）**不在这里** —— 真机实测拿不到音频字节
    （82 个 `Rec/` 目录全空、全盘无 `.silk/.amr`），可行性见 `docs/voice-msg-feasibility.md`。
    ❌ 发语音 / 语音通话：hook 做不到。
  - **三条硬约束**（改之前先读规格）：① 转写跑在**收消息那条线程**上 → `audio.max_seconds`（默认 120）
    + `file.max_bytes` 是**硬上限，超了如实拒绝、绝不静默截断音频**；
    ② **绝不在聊天里静默下模型** —— 推理只认本地目录（结构上不可能联网），下载只由
    `--setup` 触发（走 `HF_ENDPOINT=https://hf-mirror.com`，本机 huggingface.co 不通）；
    ③ 默认 `local` → **音频一个字节都不出本机**；配 `cloud` 才上传，**上传必打日志**。
  - **`_looks_garbled` 那条「短于 20 字当可疑」对音频不适用**（它是为「字节解码错了」设计的）：
    一句 3 秒的「好的」只有两个字，按那条会被拒。所以 `extract()` 里音频分支**跳过**这个判据。
  - **`faster-whisper` 绝不能写成 `requirements.txt` 的正式需求行**（真踩过）：
    `envsetup.requirements_specs()` 读**所有非注释行**，「可选段」只是文件里的约定；
    写成正式行它就会进 `required_import_names()` → 启动助手.bat 自检要求它 →
    没装的人「装完还是起不来」死循环（H1 那类），installer 还会去装这个重包。
    **可选依赖一律写成注释**（pywxdump 一直是这么写的）。回归：`selftest_audio.py`。
- `image_cache.py` — 找微信 4.x 的**明文缩略图缓存**（`<账号>/cache/<月>/Message/<md5>/Thumb/`）。`send_image` 的默认白名单就是这里的 `image_cache_dirs()`（即 `<账号>/cache`），**不再是整个 `xwechat_files`**。
  - **「自己发出去的图没有明文缩略图」这条只对了一部分**（2026-10-01 的观察，
    现在实测已经反例）：`cache\<月>\Message\<md5(会话)>\Thumb\<local_id>_<create_time>_thumb.jpg`
    里确实有**自己发出去**的图——`md5("filehelper")` 那个目录下就有
    `265_1789304494_thumb.jpg`（同目录 `Bubble\` 里还有配对的加密 `_b.dat`）。
    所以**别拿「自己发的图一定没缩略图」当判据**：有就发/能读，没有才如实说看不了。
    覆盖到哪一步取决于微信渲染与缓存清理，**不是保证**；而且仍然是缩略图不是原图。
    `read_image` 对没有缓存的图**读不了内容**，只能在消息里如实说「看不了」。
    这是微信的存储事实，不是本项目的 bug；**别顺手去解密 `.dat`**（那是另一件事，
    见 `docs/wechat4-dat-image-notes.md`）。
  - 渲染图片消息时**带上 `local_id`**（`live_history` 里做），模型据此能直接 `read_image(contact, local_id)`；不带的话它得先 `find_images` 再 `read_image`，白多一次查库。
- `llm.py` — anthropic / openai 两种协议，工具调用格式互转。
- `providers.py` — 服务商预设表（`/provider` 与 `setup_llm.py` 共用同一份，别各写一份）。
- `setup_llm.py` — 命令行模型配置向导（`配置模型.bat`）。
- `health.py` — 健康看护：日志轮转 + 运行快照 + 掉登录告警 + Windows 本地通知。**规范见下面「运行看护」**。
- `usage.py` — token/费用统计，落盘 `data/usage.jsonl`，`/用量` 读它。
- `redact.py` — 送云端前的脱敏（手机号/身份证/银行卡/邮箱/IP），**默认关闭**，规范见下面「运行看护」。
- `status_page.py` — 只读本地状态页（默认关）。**规范见下面「运行看护」**。
- 入口有三条，都会起 `bot.py`：`助手.bat` 菜单、`启动助手.bat`、开机自启注册表。

## ⚠️ 本地执行（run_command / executor.py）—— 微信就是远程执行入口

**微信消息 = 一条能跑本机命令的远程入口。** 所以这条链的规矩只有一条，且不许放松：
**模型只能"提出"命令，必须先原样发给用户、用户回「确认」才真的跑。**
不许出现「模型说跑就跑」，也不许模型没调工具就自己编「我已经提交了」。

链路（2026-10-01 真机实测走通的顺序）：

```
用户：帮我执行 dir /b
  → 模型调 run_command 工具
  → agent_tools.ToolBox.t_run_command **只 set_pending(kind="shell", cmd=原文)**，一个字都不执行
  → bot 回用户：尚未执行 + **命令原文**
  → 用户回「确认」（只认 确认/确定/确认发送；ok / y / 发送 / 发吧 **不算**）
  → bot.py 确认分支 executor.run_command(cmd, cfg=cfg) → executor.format_result() 发回结果
```

- **存的和跑的是同一个字符串**：`item["cmd"]` 就是模型给的原文，确认消息里显示的也是它。
  用户审的是真命令——这是防提示词注入的关键，别在中间做转述/截断/拼接。
- `executor.py` 只负责执行（超时、输出上限、工作目录、编码回退），**不管该不该跑**。
  `subprocess` 同步阻塞、**故意不开线程**：hook 不支持并发，跑命令期间轮询会停。
  所以 `shell.timeout` 默认只有 60 秒，**别调大**。代码里的硬上限是 **600 秒**
  （`executor.MAX_TIMEOUT`，`[1, 600]` 夹取，写 99999 也只给 600）——上限存在只是为了
  拦住非法配置，不代表 600 秒是推荐值：超时期间轮询、定时任务、看护记账全停着。
- Windows 上命令是 `cmd.exe /d /s /c "<原文>"`（整条命令拼成字符串、**再整体包一层引号**）。
  两个坑都踩过、别改回去：① 用 list 形式 `["cmd.exe","/d","/s","/c",cmd]` 会走 Python 的
  list2cmdline 转义把引号变成 `\"`，**带引号的路径全废**；② 少包那层引号时，
  **以带引号的可执行路径开头的命令**（如 `"C:\Program Files\x.exe" a`）会 rc=1。
- 输出编码：utf-8 → gbk 回退。注意 utf-8 与 GBK 有 1920 个「两边都能解、结果不同」的
  2 字节序列（如 GBK「目录」被 utf-8 解成 'Ŀ¼'），`_decode_printable_trap` 只兜其中一类，
  **改判/存疑都要在结果里带 ⚠️ 提示**——不许静默把乱码当正常输出。
- 结果文本按**微信消息体量**裁（`executor.WECHAT_MAX_CHARS`，默认 1500 字，实测微信扛得住 6000 字，
  这个上限是我们主动设的体量闸），裁了必须明说"只发了前 N 字"。
- `shell.auto_ok` 是**用户自己在 config.yaml 里**写死的免确认名单，匹配是**整条精确相等**
  （绝不用前缀/子串/通配符——那等于给模型留了绕过确认的注入面）。默认空 = 每条都确认。
- **没跑就是没跑**：工具返回、bot 兜底、system_prompt 三处都要保证模型不能说"已经跑了"。
  真机踩过：模型不调工具就自己演了一段「我来提交，等你确认」——为此 bot 侧加了
  **确定性兜底**（回答里提到命令但本轮没有登记过 shell，就固定追加一句真话），别删。

## ⚠️ hook 使用铁律

**这个 hook 前后把微信搞崩过 6 次**（转储里能数出 6 份，见下面的对照）。崩溃的直接诱因是**两个 bot（或两路查询）同时在轮询**——已加了单实例锁（`bot.py:acquire_single_instance`，回环端口 39001），但这只是兜底，真正的死因是下面三条：

**崩溃取证怎么做**：微信自己的转储在
`%APPDATA%\Tencent\xwechat\crashinfo\reports\Weixin_*.dmp`（不是 WER 那份）。
`%TEMP%\dump_parse.py <dmp>` 能直接解出异常码 / 出错地址 / 归属模块偏移（纯 struct，不要 windbg）。

| 转储 | 出错位置 | 类型 |
|---|---|---|
| 9a6d8521 / 236cbbac(00:08) / 0a06ee65 | Weixin.dll **+0x32BB4xx ~ +0x32BB80x** | 写 NULL |
| 0d35e9b6 (09:28) | Weixin.dll +0xE23753 | 写 NULL |
| 488215b7 (13:17) | Weixin.dll +0x505AFBD | **读 NULL** |
| c4ce3551 (09-30 22:51) | ntdll.dll | **0xC0000374 堆损坏** |

**注意：转储的模块表里看不到这个 hook**（六份都没有）。钩子会把自己从 PEB 模块链里摘掉
（见 `installers/.../src*/` 的 `inline_weixin_dll_load.cpp` 和 `docs/hook-anti-tamper-notes.md`），
所以**别用「模块在不在」判断钩子有没有涉案**——要看 30001 端口是不是还被那个 PID 占着。

1. **绝不裸调 `GetAllDBName`。** 每调一次都在 700MB 进程里做一次全内存扫描（`getDatabaseInfo()` 先 `m_dbs.clear()` 再 `searchDatabases()`）。唯一允许的调用点是 `live_history.force_rescan()`（自带限流，只为拿「句柄表被重建」这个副作用）。想判断某个库在不在，探 `sqlite_master`。
2. **绝不做不带选择性过滤的排序查询。** 典型反例 `WHERE local_type=1 ORDER BY create_time DESC`（先匹配全部消息再排序），实测 0.3 秒起、劣化时到 6 秒。

守好这两条，其余查询都很快（实测 0.001~0.41 秒）。**`aixed_api.query_sql` 里有慢查询告警**（>1 秒打 `⚠️ 慢查询`）。跑起来后盯这个，一旦出现立刻停手。

**另一个判据**：`SELECT 1 FROM xxx LIMIT 1` 这种空探测如果超过 1 秒，说明卡的是**微信进程本身**（不是 SQL），必须立刻停手。

**`live_history.py` 是唯一应该读微信库的地方。** 新增查询请加在那里并复用它的缓存（`_cached` / `_cached_positive`），别自己拼 SQL。

3. **别在 bot 轮询的同时手工发查询。** 2026-10-01 13:17 那次崩溃（微信 `Weixin.dll+0x505AFBD` 读 NULL）就是这么来的：
   bot 每 5 秒轮询 4 个 fts 分片，我又从外部连着发了十几次 `/QueryDB/execute`（fts `MATCH`、
   再加一条没带选择条件的 `WHERE local_type IN (...)` 全表扫描），**两路查询同时压在 hook 上**
   —— 和「两个 bot 同时轮询」是同一个死法。现场日志：
   ```
   ⚠️ 慢查询 3.50s  db=message_fts.db   ← bot 自己的轮询被挤慢
   /QueryDB/execute 返回 HTTP 500       ← hook 内部出错
   连不上 30001（WinError 10061）        ← 微信进程没了
   ⚠️ 慢查询 1.05s SELECT 1 ... LIMIT 1 ← 空探测都 1 秒 = 卡的是微信本身
   ```
   **要手工查库就先停 bot**，查完再起。

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
- **⚠️ 图片消息不在 fts 里（2026-10-01 实测）。** 四个分片的
  `SELECT COUNT(*) FROM <分片> WHERE local_type = 3` **全是 0** —— 微信的 fts 只索引
  有文本内容的行。后果：**只靠 fts 游标轮询，永远看不见别人发来的图片**
  （表现就是「我把图发过去了，它一点反应没有」）。而另一条路
  `_v4_new_messages_session` 靠 `SessionTable.summary`，图片的 summary 是**空串**，
  被 `if not content: continue` 跳过 —— **两条通路都瞎**。
  这就是 `live_history._v4_pickup_nontext` 存在的原因：拿 `SessionTable` 的
  `last_timestamp` + `summary = ''` 当「最后一条不是文本」的信号，只对这类会话
  回查一次消息表（水位线 `cursors["__nonttext__"]` 防重复；**稳态下 0 行 → 零额外查询**）。
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

## 素材暂存（assets.py）——发一次图/表情，之后说「发给谁」就能再发

用户要的能力：在控制会话（文件传输助手）里发一张图或一个表情，之后只说「发给张三」
就发出去，想发几次发几次。

```
控制会话来了图片/表情/视频
  → bot.py `stash_control_media()`：`live_history.media_kind()` 认出是哪一类
  → `live_history.latest_media()` 拿到那条的 local_id（一次 PK 索引查询）
  → `live_history.message_xml()` 取**原始 XML** → `assets.stash()` 落盘 data/assets.json
  → 回一句回执（「已暂存这张图。说『发给谁』我就发」）
用户说「发给张三」
  → 模型调 `send_asset`：名单内 `send_xml_repeated()` 直接转发 /
    名单外 `set_pending(..., label=...)` 等用户回「确认」
```

- **存的是消息 XML，不是图片副本**（定下来的方案，别改回去）：用户**自己发出去**的图
  在磁盘上只有 AES 加密的 `.dat`（密钥没拿到，见 `docs/wechat4-dat-image-notes.md`），
  明文顶多是**缩略图**、表情包基本没有；转发原始 XML 不需要解密，原图/动图都保留。
- **hook 成功也无条件回 `ret:0`**（`aixed_api` 的说明），所以「转发其实没成」本地
  发现不了。回给用户的只能是「已发出」，**不许**说成「对方收到了」——`assets.py` 顶部、
  `send_asset` 的注释和 config 的 `assets` 段都写了这一条。**真机验收要肉眼确认一次。**
- 只收**用户自己发出去的**（`msg.from_self()`）：控制会话是"和自己说话"，别人发进来的图
  不该被悄悄收进暂存区——那会让「发给谁」把对方刚发来的东西又发出去。
- 只收**能转发的三类**（`live_history.media_kind()`：图片/视频/表情，含 appmsg 编码）。
  语音/文件/位置/名片/链接**一律不收**：转发不了，收进来就是骗用户（工具回一句「已发」
  而对方什么都收不到）。**想往 `media_kind` 里加类型，先确认 hook 真能转发那一类。**
- **补存（`ToolBox._sync_latest_asset`）不能删。** 图和「发给谁」这句话可能落在
  **同一个轮询间隔**（`poll_interval` 默认 5 秒）里，那时 `SessionTable.summary` 已经是
  文字、非空，`_v4_pickup_nontext` 会**整条会话都不回查** —— 那张图永远不会被暂存。
  所以 `send_asset` 在真的要发素材时回查一次「控制会话里最新的媒体」并补存
  （普通消息上**一次都不查**）。**取不到那张更新图的原文时必须一张都不发**，
  绝不退回发暂存区里旧的那张——发错东西不可逆。回归：`selftest_assets.test_sync_latest`。
- **重复暂存不叠两条**（同会话同 local_id 挪到最新）；满了顶掉最老的，并且**要把
  「顶掉了几条」说出来**——静默丢弃不允许。
- 容量 `assets.max_items`（默认 5）在 **`assets.cap_of(cfg)` 里夹在 1~20 并告警**（唯一一处
  钳制逻辑，bot 和工具都走它），别让它变成「配置写 9999 就真存 9999 条」。
- `assets.load()` 遇到坏文件/形状不对**只告警、当空**（和 `data/state.json` 同一姿势）；
  没有 xml 的条目读出来就丢——那种条目存下去也转发不了。`assets.stash()` 缺 xml/local_id
  **直接拒绝存**（抛 ValueError），别让它进暂存区。
- 暂存区**故意不并进 `data/state.json`**：那份是「轮询游标 + 待确认队列」的单一真源，
  素材库是另一码事，混进去会让那条规矩变糊。
- `send_pending()` 对 `xml` 认 `count`（素材那条路要连发）；`forward_message` **没有**
  count 参数、仍然只发一次——别顺手给它加（转发别人的消息连发更容易发错对象）。
- 3.9.x（wcferry）后端没有 `message_xml`，这条功能只有 4.x 有；媒体在 v3 那条路上
  根本进不了主循环（`bot.py` 开头 `msg.type != 1` 就 continue）。
- 回归：`selftest_assets.py`（媒体类型判定 / latest_media / 落盘与容量 / 按序号取 /
  名单内直发与名单外待确认 / 连发钳制 / 中途失败的「已发出 N」）。

## 运行看护（health / status_page / usage / redact）

静默失效之所以是最大的坑，是因为**「没反应」和「一切正常」在用户眼里一模一样**。
这四个模块就是给「没反应」装上仪表盘。它们有一条共同的铁律，和 hook 铁律同源：

**`health` 和 `status_page` 绝不查微信库、绝不自己起线程去碰 hook。**
它们只处理**已经拿到的事实**：bot 在收消息那条线程上把事实喂进来
（`health.Health.note_poll` / `note_sent` / `note_send_failure` / `note_hook_error` / `note_login`），
`status_page` 只渲染 `Health.snapshot()` 返回的那个 dict。
任何「让它顺便去查一下库」的想法都会绕开「hook 不支持并发」这条铁律——**别加**。

- **日志轮转发生在 `setup_logging()` 打开文件之前。**
  `bot.setup_logging()` 先调 `health.rotate_log(LOG_PATH)`，再 `open(..., "a")`。
  顺序不能反：`_Tee` 一旦持有文件句柄（Windows 上就是被占用），`os.replace` 挪不动它。
  `rotate_log` 因此**绝不允许长期持有句柄**（每个文件都是 with 打开、立刻关闭），
  阈值 5MB、留 3 份（`bot.log` → `bot.log.1` → `.2` → `.3`，更老的删掉）；
  轮转失败（被占用/权限不足）只告警，**绝不许让 bot 起不来**。
- **`status_page` 只许绑回环地址。** `start(host=...)` 会拿 `LOOPBACK_HOSTS`
  校验（`127.0.0.1` / `localhost` / `::1` / `127.0.0.2`），不是回环就拒绝启动——
  这个页面里有 wxid、群名、错误文本，暴露到局域网等于把隐私和攻击面一起送出去。
  它同样**只读**：GET `/`、`/status.json`、`/healthz`，其余方法一律 405，渲染前 `html.escape`。
  端口占用只告警并返回 None，默认关闭（`status.enabled`），端口 39002
  （**别和单实例锁 39001、hook 30001 撞**）。
- **掉登录必须主动探、主动告警。** 微信会自己重启回登录界面（见「静默失效」的 debug 顺序第 0 步），
  所以链路上每 30 轮心跳调一次 `due_login_check()` / `note_login()`，掉了就弹本地通知
  （`health.notify`，best-effort，10 秒超时，**绝不弹阻塞对话框**）。
  同类告警按 `health.alert_cooldown` 冷却，防刷屏。
- **`health.notify` 只用系统自带 PowerShell + `NotifyIcon` 弹气泡**，不用 `msg.exe` / `MessageBox`
  （那会把 bot 卡死）。**是否真机可见尚未确认**，见 `docs/fixes-2026-10.md`。
- **发送失败只告警、绝不自动重试。** 发消息不可逆，超时/HTTP 500 时无法确认对方到底收没收到，
  重试就可能发两条。`bot.send()` 统一兜住异常、`note_send_failure()` 记一笔、如实回给用户。
- **`redact` 只改「送出去的那一份文本」。** `bot.build_user_prompt()` 末尾按 `privacy.redact`
  决定要不要打码，**本地原文一个字都不动**；默认关闭，且严格按 `is True` 判定
  （写 `"true"` 字符串 / `1` / 整段是标量，一律当关）。命中数要打日志——不许悄悄改内容还装作没发生。
  宁可漏几个，也不许把版本号、年份、金额打成马赛克（见 `redact.patterns()` 的反例清单）。
- **`usage` 落盘 `data/usage.jsonl`**（`data/` 已被 .gitignore 忽略），`/用量 [天数]` 读它。
  **已接线**：`llm.py` 四个返回点各调一次 `_rec_openai` / `_rec_anthropic`
  （anthropic 与 openai 两协议 × `chat` / `chat_with_tools`），只记
  `ts/provider/model/prompt_tokens/completion_tokens/kind`，
  **不记请求内容、不记密钥**；记账失败只告警、绝不影响本次调用
  （`usage.record` 自己不抛，`llm._record_usage` 是第二道保险）。
  只记**成功拿到 usage 的调用**，所以它是「本地估算」而不是账单；
  价目表只有 `deepseek-chat` / `deepseek-reasoner` 两条，其余模型 `price_of` 返回 None、
  `/用量` 会明说「没有价目表，只报 token 不算钱」——**别为了好看给它编一个价格**。

## 改代码时的约定

- **动 `live_history.py` 要同时照顾两套 schema**（v3 / v4），并把用例加进 `selftest_aixed.py`（用假服务，不碰真 hook）。
- **新增 agent 工具要改两处**：`agent_tools.TOOLS` 和 `config.yaml` 的 `system_prompt`。只加前者，模型根本不知道有这工具。
- **工具返回的文本要顺手告诉模型「该怎么办」。** 查库失败时别只回一句「失败：…」——模型会原地重试，而每次重试都是一次真实的 hook 调用。统一用 `agent_tools._db_fail()`。
- **往对话记忆里只放原始提问和最终答复**（`bot.dialog_*`），**绝不能放检索到的历史**——那段每轮都重算，记下来等于每轮重发整块历史，token 直接爆。
- **发消息是不可逆动作**，默认不许乱发：名单外的一律走「待确认」（`agent_tools`）。别绕过这个机制。文本/图片/转发的分派在 `agent_tools.send_pending()`。
- **`send_image` 的路径必须过 `_image_path_ok()` 白名单**。path 是**模型填的**，不校验就等于让它从你硬盘上挑任意文件发出去。默认白名单是 `image_cache.allowed_image_dirs()` 推出来的**微信图片缓存根**（`<账号>/cache`），**不是整个 `xwechat_files`**（那是 `data_root()`，里面有配置、`db_storage`、收到的文件）；推不出来才退回 `data_root()` 并告警。用户在 `agent.send_image_dirs` 里配的目录是**加在默认之上**（并集），**不是换一份名单**——以前实现是「配了就顶掉默认」，真机自检里撞出来过：用户为了自测加了个 `test_images`，就**静默地**再也发不出聊天里的图了。改并集时**必须打一条告警**说明「两处都能发」（边界可以宽，但用户得知道宽在哪）。要加目录让**用户**改 `agent.send_image_dirs`，不要自己改配置绕。`send_images`（按目录群发）走同一个 `_in_allowed_dirs`，别另开一套。
- **这个 hook 只能发文本和图片**（`SendTextMsg` / `SendImgMsg` / `ForwardXMLMsg`，转发也只认图片/视频/动图）。**发不了普通文件**（pdf/Word/Excel 一律不行），转发别人的文件也不行。用户提这类需求时要**如实说做不到**，别含糊、更别假装发了。想加只能改 hook 的 C++ 重编译。
- **重名不许静默取第一个。** 解析联系人统一走 `ToolBox._one()`，重名时回一句让模型去问用户——静默取第一个会读错人、发错人。
- **渲染「谁说的」一律用显示名。** 预取路径用 `bot._msg_speaker()`，工具路径用 `agent_tools.speaker_of()` / `format_history_lines()`。**绝不要把 talker（wxid / roomid）原样塞进给模型的文本**——模型会照抄一串 id 给你。这是 2026-10-01「看不到真正的名字」的根因。
- **hook 不支持并发**。工具串行执行，查询有预算（`agent.max_queries`）；连发消息是同步的、故意不开线程。任何"并发加速"的想法都会让微信崩。
- **定时任务同样不许开后台线程。** `scheduler.py` 靠 `bot._Ticker` 挂在**收消息那条线程**的轮询空档里跑（`iter_aixed_messages` / `iter_wcferry_messages` 各调一次）。代价是精度只有 `poll_interval`（默认 5 秒），换来「定时发消息」和「轮询」永不并发。往 `run_due` 里加新动作时别起线程。
- **不支持的功能要如实报错，不许静默降级。** 典型：定时任务里 `action: call`（语音通话）现在打不出去，`run_due` 就明确报错并通知用户，**绝不偷偷改成发文本**——那是在骗用户。加新功能时保持这条。
- **发送失败只告警、不自动重试**（`bot.send()` 里兜住，见上面「运行看护」）。别为了「更可靠」加重试：发消息不可逆，重试可能让对方收到两条。
- **待确认项是多条时先回编号菜单。** 用户回「确认 <编号>」指明哪一条，只说「确认」会再问一次、**绝不替他猜**（`bot.pending_index_of`）。命令/发送/审核草稿三类队列混在一起时，显示的编号和实际执行的那条必须是同一条（回归用例在 `selftest_policy.py` / `selftest_bot_loop.py`）。
- **落盘状态只有一份真源：`data/state.json`。** 轮询游标和待确认队列都写它，**原子写**（同目录临时文件 + `os.replace`）；文件坏了/读不出来**只告警、不许拦住启动**（也就退回「从最新开始收」）。
  「重启补齐」的语义：落盘游标距今在 `state.resume_window`（默认 1800 秒）内就续上；续上来的、比 `state.stale_after`（默认 120 秒）还旧、**且早于本进程启动**的消息 = 停机期间的旧消息，**只通知、不自动回复**（`watch` 命中仍通知），命令和提问也不补。判据按**消息年龄**走，不按「第几轮」，所以积压多少条都不会误判。
- **加新模块时先看它有没有「绝不查库 / 绝不自己起线程」的要求。** `health` / `status_page` 有（见「运行看护」）；`usage`（只读 `data/usage.jsonl`）、`redact`（纯函数、只改送出去的那份）也**不许**顺手去碰 hook。
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
- 源码快照两份，**xLog hook 偏移不同，不要混用**：项目内 `installers/wechat-4.1.10.27/src-4.1.10.27/`，以及作者发布包里解出来的那一份（放哪儿由你自己决定，**别把绝对路径写进文档/配置**）。
- 图片加密：`docs/wechat4-dat-image-notes.md`；hook 反篡改：`docs/hook-anti-tamper-notes.md`。
- **做不了的事**：hook 没有任何通话接口，`VoipEngine.dll` 那条路要自逆向 `Weixin.dll`，且 README 说明 main 分支已移除协议直发能力。别去文档里找通话接口。
