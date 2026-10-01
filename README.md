# 个人微信 AI 助手

在**微信原生窗口**里跟一个 AI 助手对话，它能读取你的历史聊天记录来回答，也能代你给别人发消息、自动回复。

## ⚠️ 先读这段（重要）

1. **封号风险**：本工具向微信进程注入 hook DLL 来收发消息，**违反微信用户协议**。个人低频自用一般没事，但官方严打时可能封号。建议用小号测试，风险自担。
2. **版本锁死**：hook 是按**特定微信版本**编译的，**微信一更新就失效**。装好后务必关闭微信自动更新（下面的安装步骤里有）。
3. **合规**：只处理**你自己账号、你合法拥有**的数据。未经授权抓取他人聊天记录是违法的，别碰。

## 技术路线（先看这张图，别照着旧文档走）

本项目的**主线**是：

```
微信 4.1.10.27 ──[自编译 aixed hook：version.dll 注入]──> 本地 HTTP 服务 :30001
                                                              │ QueryDB/execute
                                                              ▼
                                            bot.py 轮询本地数据库拿新消息
```

- hook 放在微信安装目录，启动微信时自动加载，在微信进程里起一个本地 HTTP 服务（默认 `30001`）。
- bot 靠**轮询数据库**收消息（间隔 `poll_interval`，默认 **5 秒**），所以是秒级而不是毫秒级。
- 所有查询都经过 `live_history.py`（它同时适配 3.9.x 和 4.1.x 两套库结构）。

### 版本对应表

| 微信 PC 版本 | 后端 | 状态 |
|---|---|---|
| **4.1.10.27** | **aixed hook（`version.dll`）+ `backend: aixed`** | ✅ **主线，推荐** |
| 3.9.12.51 | wcferry **39.5.2.0**（`backend: wcferry`） | 保留的另一条后端（见下） |
| 3.9.12.17 | wcferry **39.4.5.0**（`backend: wcferry`） | 保留的另一条后端（见下） |

> ⚠️ 上面两条 wcferry 版本号写的是**四段**（PyPI 上的真实形式）。文档里常看到的
> 三位短号（`39.5.2` / `39.4.4`）**在 PyPI 上不存在**，`pip install wcferry==39.4.4`
> 会直接 No matching distribution——那是 WeChatFerry 的 **GitHub release** 号，不是
> wheel 号，两者只在部分版本上重合。对应关系与依据写在 `wechat_version.WX_TO_WCFER`
> 的注释里；**别凭猜把短号补成 `.0`**。
| 其它 4.x | — | ❌ hook 偏移不同，会崩。只能装回 4.1.10.27 |

> **「另一条后端」是什么意思**：`config.yaml` 的 `backend` 有两档。`aixed`（4.x 主线，轮询数据库）和 `wcferry`（3.9.x，事件回调，毫秒级）。
> 下面凡是标了「**仅 3.9.x**」的步骤都**只适用于 wcferry 后端**——4.x 主线上不要做，尤其**不要为了用本项目去把微信降级到 3.9.x**。

## 环境要求

- Windows 10/11 64 位
- Python **3.11**（推荐；3.8 ~ 3.12 都能跑）
  > 3.13+ 上 wcferry 依赖的 `pynng` 通常没有预编译轮子，装不上。主线（4.x + hook）不需要 wcferry，但装个 3.11 最省事：
  > `winget install -e --id Python.Python.3.11`
- 微信 PC 版 **4.1.10.27**（安装包就在 `installers/wechat-4.1.10.27/WeChatWin_4.1.10.27.exe`）

## 安装步骤（4.x 主线）

分两段：**先部署 hook（让微信能被读）**，**再装 Python 环境（让 bot 能跑）**。

### 第一段：把 hook 装进微信

这一步必须**以管理员身份**做（要往 `C:\Program Files\Tencent\Weixin` 写文件、动微信进程）。

1. **确认微信版本是 4.1.10.27**
   微信里「设置 → 关于微信」看一眼。不是这个版本就先装 `installers/wechat-4.1.10.27/WeChatWin_4.1.10.27.exe`：
   右键 `PowerShell` → 以管理员身份运行 →
   ```powershell
   Set-Location "你的项目目录\installers\wechat-4.1.10.27"
   powershell -NoProfile -ExecutionPolicy Bypass -File .\do_install.ps1   # 静默安装 4.1.10.27，日志落 install-log.txt
   ```
2. **放置 hook 并关掉微信自动更新**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\do_hook_install.ps1
   ```
   它做两件事：把 `version.dll` 复制进微信安装目录；用 ACL 拒绝写入微信的 update 目录（挡住自动更新把版本顶掉）。
   结果写在 `hook-install-log.txt`。**注意：这个脚本里写死了本机的微信目录和用户名，换机器要先改脚本开头的 `$WX` / `$UPD` / `$USER` 三行。**
3. **重启微信并确认端口通了**
   重启后（不必先登录）浏览器或 Postman 打 `http://127.0.0.1:30001/QueryDB/status`，能返回 JSON 就说明 hook 已加载。
   `IsLogin` 是 `1` 才算真的登录成功（停在登录界面时是 `0`）。
   连不上就是 hook 没加载：DLL 没放对 / 微信版本不是 4.1.10.27 / 被安全软件拦了。

`installers/wechat-4.1.10.27/` 下这一组脚本各管一件事（**都以管理员身份运行**）：

| 脚本 | 干什么 |
|---|---|
| `do_install.ps1` | 静默安装微信 4.1.10.27 → `install-log.txt` |
| `do_hook_install.ps1` | 放 `version.dll` + 禁用微信自动更新 → `hook-install-log.txt` |
| `do_replace_dll.ps1` | 换新版 hook DLL（先备份旧的）→ `replace-dll-log.txt` |
| `do_remove_hook.ps1` | **摘掉 hook**（结束微信、拿走 `version.dll`）→ `remove-hook-log.txt` |
| `do_restore_hook.ps1` | 把 hook 装回去并重启微信 → `restore-hook-log.txt` |
| `do_restore_restart.ps1` | 装回 hook + 重启微信（另一版流程）→ `restore-restart-log.txt` |
| `do_deploy_patched.ps1` | 部署自己编译的补丁版 DLL（内含 `g_IsLogin` 修补）→ `deploy-patched-log.txt` |
| `do_deploy_loginready.ps1` | 部署「登录就绪探测」版 DLL（从 `src-4.1.10.27\...\x64\Release\version.dll` 取）→ `deploy-loginready-log.txt` |

> 想临时关掉 hook：把微信目录里的 `version.dll` 改名 `version.dll.disabled` 再重启微信即可（等价于 `do_remove_hook.ps1`）。
> 要自己改 hook 源码重编译：源码在 `installers/wechat-4.1.10.27/src-4.1.10.27/WeChat-Hook-4.1.10.27/`。

### 第二段：装 Python 环境、起 bot

**双击 `install.bat`**（4.x 主线**别走** `助手.bat` 菜单的 `[7] 自动`，理由见下面）。

脚本会：检测微信版本 → 建虚拟环境 → 按 `requirements.txt` 装依赖 → 生成 `启动助手.bat`。
4.x 上它会**跳过 wcferry**（那条后端才需要它），其余依赖照装，并在最后如实告诉你「未装 wcferry」——这不是失败。

装完**双击 `启动助手.bat`** 起 bot（助手 = bot）。

> **⚠️ 4.x 主线上哪些入口不要用**
> - **`降级.bat` / 菜单 `[1] 降级微信 4.x -> 3.9.x`**：那是 **wcferry 后端**的准备动作。**4.x 主线上不要降级**——降了就装不了 hook，只会退回到旧方案。
> - **`助手.bat` 菜单 `[7] 自动`**：检测到的是 4.x 时，它现在会**直接把话说明白**（4.x 走 aixed hook 主线，不需要 wcferry、也**不要降级**）然后退出，不再劝你降级。但一键流程本身仍是给 wcferry 那套排的，4.x 用户**建议直接跑 `install.bat`**。
> - **`启动助手.bat` 的自检包清单**：从 `requirements.txt` 派生，并**按 `config.yaml` 的 `backend` 过滤**——4.x 主线（`backend: aixed`）下清单里**不含 `wcferry`**，所以「没装 wcferry 也能正常起 bot」。（以前这里写死了 wcferry，会导致全新 4.x 机器反复重装却永远起不来。）
> - **`配置模型.bat`**：这个是通用的，4.x 上照常可用（见下面配 API 那步）。
> - **`python export_history.py` / PyWxDump**：那是「取数据库密钥 → 解密 → 全量导出 JSONL」的老路子，流程是围绕 **3.9.x** 写的（4.x 下取密钥要么不行、要么会失败），**没在本机 4.1.10.27 上验证过**。4.x 主线**不需要**它——bot 直接实时读库，别去折腾密钥。

如果想手动来（等价于 `install.bat` 做的事）：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 使用流程

装完之后**全程在微信里操作**，不用再碰任何文件。

### 第一步：跑起来

双击 **`启动助手.bat`**。助手会**实时**查微信本地数据库里的历史，不需要先导出。

### 第二步：在「文件传输助手」里配 API

依次发这三条：

```
/provider              列出可选服务商
/provider 1            选第 1 个（DeepSeek），自动配好协议+接口+模型
/api sk-你的key         设置密钥，并自动测一次连通性
```

`/provider` 会列出：

```
[1] DeepSeek      [2] Claude 官方   [3] 通义千问    [4] Kimi
[5] 智谱 GLM      [6] OpenAI        [7] 本地 Ollama
```

选完再发 `/api <key>`，它会**当场告诉你通不通**：

```
API Key 已设置（sk-a****1234）。
当前：openai | deepseek-chat
✅ 连通性测试通过（模型回了「成功」）
现在可以直接发消息提问了。
```

配置写进 `settings.json`，重启后依然生效。

> 也可以用 `配置模型.bat` 走命令行向导（同样是选编号），配完会把结果发到文件传输助手。
> 效果一样，看你喜欢在哪配。

### 第三步：提问

直接发消息就行：

- 「我和张三聊了什么」
- 「最近聊了什么」
- 「关于 SAT 都聊了啥」

它会查历史 + 调模型 + 回你。

### 其他命令

| 命令 | 作用 |
|---|---|
| `/provider` | 列出可选服务商 |
| `/provider <编号>` | 选服务商（自动配协议/接口/模型） |
| `/api <key>` | 设置密钥并测连通性（`/api clear` 清除） |
| `/baseurl <url>` | 单独改接口地址 |
| `/model <id>` | 单独改模型 |
| `/temp 0.7` | 设 temperature |
| `/addchat <wxid>` | 添加要响应的聊天 |
| `/delchat <wxid>` | 移除聊天 |
| `/status` | 查看当前配置 **+ 运行健康**：运行时长、最近一次轮询、登录态、分片查询失败、发送失败累计 |
| `/用量 [天数]` | 看 token 用量和估算费用（默认最近 **7 天**；`/用量 30` 看 30 天，`/用量 0` 看总账） |
| `/定时 ...` | 定时任务（到点自动给对方发消息 / 到点让助手答一个问题）。见下 |
| `/盯着 ...` | 盯着某人（他发消息只通知你，不回他）。见下 |
| `/help` | 帮助 |

### 定时任务（`/定时`）

精度只到 `poll_interval`（默认 **5 秒**）——定时任务和轮询跑在同一条线程上（hook 不支持并发，这是故意的）。

| 命令 | 作用 |
|---|---|
| `/定时` | 看列表 |
| `/定时 加 <时间> <对象> <内容>` | 到点给对方发固定文本。例：`/定时 加 明天9:00 张三 记得带伞` |
| `/定时 加提问 <时间> <问题>` | 到点把问题交给助手答一遍，答案发回本会话。例：`/定时 加提问 每天8:00 谁还没回我` |
| `/定时 加通话 <时间> <对象>` | 打电话——**这条路径还没打通，到点只会如实报错**，不会假装打了 |
| `/定时 删\|开\|关 <编号\|all>` | 删 / 恢复 / 暂停 |

时间写法：`9:00`=每天、`明天9:00`=只一次、`每周一 9:00`=每周、`每30分钟`=每隔一段、`9点半`=每天9:30；相对现在的只一次：`10分钟后` / `半小时后` / `2小时后` / `3天后`。也可以直接跟助手说「10分钟后提醒我给李同学发你好」。

也可以直接说人话「明天9点提醒我给张三发个消息说带伞」，助手会自己调用工具。

### 盯着某人（`/盯着`）

名单里的人一给你发消息，就把内容转到控制会话告诉你——**不回复对方**。

| 命令 | 作用 |
|---|---|
| `/盯着` | 看名单 |
| `/盯着 加 <昵称\|wxid\|roomid>` | 加进来 |
| `/盯着 删 <昵称\|wxid>` | 移出去 |
| `/盯着 开` / `/盯着 关` | 总开关 |

和 `/auto`（代你回对方）**互斥**：同一个会话两边都开会互相拦下来。

### 自动回复（让 AI 代替你本人回某个人）

指定若干会话（好友单聊或群），对方发来消息时 AI 结合上下文代替你回复；不想让它回了就关掉，你自己手动回。

**不用记命令，直接说人话就行**——助手会自己调用工具改配置：

> 「以后张三的消息你帮我回一下」
> 「群里的消息也帮我回」
> 「别自动回李四了」
> 「发之前先给我看一眼」
> 「关掉自动回复」

也可以发命令，都发在文件传输助手里：

| 命令 | 作用 |
|---|---|
| `/auto` | 看开关、审核状态和名单 |
| `/auto on` / `/auto off` | 总开关（关掉就你自己回） |
| `/auto add <昵称\|wxid\|roomid> [self\|assistant]` | 加入名单，默认 `self` |
| `/auto del <昵称\|wxid>` | 移出名单 |
| `/auto mode <谁> self\|assistant` | 改人设 |
| `/auto review on\|off [谁]` | 开审核：草稿先发给你，你回「确认」才真发出去 |
| `/auto ctx <1~30>` | 每次带多少条历史当上下文 |

- **人设**：`self` = 假装你本人（口语化，对方看不出是 AI）；`assistant` = 明说是助手。
  人设全文在 `config.yaml` 的 `auto_reply.persona_self` / `persona_assistant`，可以随便改。
- **群聊**：不用 @，由 AI 自己判断该不该接话——不该回就静默。**群只能用 roomid 添加**（形如 `xxxx@chatroom`）。
- **审核**：开启后草稿发到文件传输助手，只有回「确认」两个字才发出去（回「ok」不算，防止随口一句把草稿发出去）；回「不发」取消。
- 自动回复的会话不能同时是控制会话（`target_chats`），两者重叠会被拒绝。

### 待确认的动作（发消息 / 跑命令 / 审核草稿）

**发消息、跑本机命令都是不可逆动作，默认一律先让你确认。** 助手会说清楚要做什么、发给谁、内容原文是什么，你回：

- **「确认」** —— 执行/发送。
- **「不发」**（或「取消」「算了」）—— 全部作废。
- **同时有多条在等确认时**，会先回一个**编号菜单**（`1) … 2) …`），你回 **「确认 <编号>」** 指明是哪一条；只说「确认」它会再问一次，不会替你猜。
  - 如果选中的那条是**本地命令**或**自动回复草稿**，还要再回一次**裸「确认」**——严格词闸门只认整句的「确认 / 确定 / 确认发送」，`确认 2` 这种带编号的**不算**。这是**故意**的（fail-closed）：宁可多问一次，也不让一句带编号的话直接在本机跑命令。
- 本地执行只认 **「确认 / 确定 / 确认发送」**（随口一句 `ok` / `y` / `发送` **不算**）；待确认项有有效期（`agent.confirm_ttl`，默认 300 秒），过期就作废。

## 目录结构

```
wechat-ai-assistant/
├── 助手.bat           # 控制台菜单（旧流程；4.x 主线建议直接用 install.bat / 启动助手.bat）
├── install.bat        # 一键安装（自动挑合适的 Python，4.x 上会跳过 wcferry）
├── 启动助手.bat        # 启动 bot（安装时自动生成，venv 失效会自愈重装）
├── 配置模型.bat        # 模型配置向导（setup_llm.py）
├── 降级.bat           # 微信 4.x -> 3.9.x（**仅 wcferry 后端需要；4.x 主线不要用**）
├── envsetup.py        # 路径与 venv 健康检查（所有脚本共用的地基）
├── installer.py       # 安装逻辑（检测版本 -> 建 venv -> 装依赖 -> 生成启动脚本）
├── console.py         # 控制台菜单
├── wechat_version.py  # 识别微信版本并匹配 wcferry
├── downgrade.py       # 降级微信（仅 wcferry 后端）
├── bypass_update.py   # 绕过微信强制更新
├── autostart.py       # 开机自启开关
├── bot.py             # 主程序：收消息 -> 命令/问答 -> 实时查历史 -> 调大模型回复
├── live_history.py    # 实时查库核心（双版本 schema：v3 3.9.x / v4 4.1.x）
├── aixed_api.py       # aixed hook 的本地 HTTP 客户端（HTTP :30001）
├── image_cache.py     # 微信 4.x「已解码图片」的明文缩略图缓存查找
├── auto_reply.py      # 自动回复：代你回指定会话（生成、清洗、静默判定、/auto 命令）
├── watch.py           # 盯着某人：他发消息就通知你，不回他（/盯着）
├── scheduler.py       # 定时任务：到点发文本 / 到点让助手答题（/定时）
├── agent_tools.py     # 给大模型的工具层（19 个工具）+ 待确认机制 + 查询预算
├── executor.py        # 本地执行：跑一条命令行命令（同步、带超时/输出上限/工作目录）
├── image_read.py      # 读图片里的字（系统 OCR；可切视觉模型）
├── file_read.py       # 读收到的文件（pdf/docx/xlsx/pptx/纯文本）
├── llm.py             # 大模型封装（Anthropic 官方 / OpenAI 兼容两种协议）
├── providers.py       # 服务商预设表（/provider 用）
├── setup_llm.py       # 模型配置向导（配置模型.bat 的入口）
├── settings.py        # 运行期配置（微信里命令改，存 settings.json）
├── health.py          # 健康看护：日志轮转 + 运行快照 + 掉登录告警 + 桌面通知
├── usage.py           # token / 费用统计（/用量；落盘 data/usage.jsonl）
├── redact.py          # 送云端前脱敏（手机号/身份证/银行卡/邮箱/IP）
├── status_page.py     # 只读本地状态页（默认关；只绑回环、绝不查库）
├── history.py         # 静态历史检索（兜底）
├── export_history.py  # PyWxDump 解密 + 导出 JSONL（**3.9.x 时代的可选功能，4.x 未验证**）
├── config.yaml        # 运行期配置（含真实 wxid / 目录，**已被 .gitignore 忽略**）
├── config.example.yaml# 示例配置（推到 GitHub 的那份，已脱敏）
├── settings.json      # 运行时配置（自动生成，命令改的都在这里）
├── requirements.txt   # 依赖清单的唯一真源（installer 按它装）
├── docs/              # 设计/踩坑文档（executor-review、wechat4-dat-image-notes 等）
├── tools/ocr.ps1      # 系统 OCR 脚本（image_read.py 调用）
├── data/              # 运行期落盘（已忽略）：history.jsonl / state.json / status.json / usage.jsonl
├── test_images/       # 自测用图片（已忽略）
├── selftest_aixed.py           # hook/HTTP 层回归基线（改 live_history.py 后必跑）
├── selftest_live_history.py    # live_history 兜底路径 / appmsg 渲染 / LIKE 转义
├── selftest_policy.py          # 待确认队列 / 发图白名单 / 查询预算
├── selftest_sched_auto.py      # scheduler / auto_reply
├── selftest_io_llm.py          # file_read / llm / settings
├── selftest_redact_usage.py    # redact / usage
├── selftest_health.py          # health / status_page
├── selftest_bot_loop.py        # bot 主循环侧改动（确认词、落盘、脱敏接线）
├── selftest_install.py         # 安装/环境链路（依赖清单、提权、版本探测）
├── selftest_executor_chain.py  # 本地执行确认闸门链路
├── executor_selftest.py        # executor 的独立自测
├── _probe_enc.py / _probe_xlsx.py / _probe_zip.py   # 临时探针脚本
└── installers/        # 微信安装包 + hook 源码 + 部署脚本
```

## 自测

这些自测**全都不联网、不碰 hook（不占 30001）、不需要真微信**，改完代码先跑它们：

```powershell
.venv\Scripts\python.exe selftest_aixed.py            # 改 live_history.py 后必跑
.venv\Scripts\python.exe selftest_live_history.py
.venv\Scripts\python.exe selftest_policy.py
.venv\Scripts\python.exe selftest_sched_auto.py
.venv\Scripts\python.exe selftest_io_llm.py
.venv\Scripts\python.exe selftest_redact_usage.py
.venv\Scripts\python.exe selftest_health.py
.venv\Scripts\python.exe selftest_bot_loop.py
.venv\Scripts\python.exe selftest_install.py
.venv\Scripts\python.exe selftest_executor_chain.py
.venv\Scripts\python.exe executor_selftest.py
```

## 运行健康与只看不动的状态页

- **日志会轮转**：`bot.log` 超过 5MB 就变成 `bot.log.1`、`bot.log.2`、`bot.log.3`（最多留 3 份，再老的删掉）。轮转发生在 bot 打开日志**之前**。
- **日志在哪**：项目根的 `bot.log`。后台无窗口跑时，它是唯一的信息来源。
- **掉登录会告警**：微信有时会自己重启到登录界面，表现和「查不出消息」几乎一样，但**只能人工扫码恢复**。bot 每 `health.login_check_interval`（默认 300 秒）探一次登录态，掉了就弹一次 Windows 本地通知（同类告警 `health.alert_cooldown`，默认 1 小时一次，防刷屏）。
- **只读状态页**（默认关闭）：把 `config.yaml` 的 `status.enabled` 改成 `true` 再起 bot，就能在浏览器打开 `http://127.0.0.1:39002/` 看运行快照（`/status.json` 是机器可读的那份）。它**只渲染内存快照、绝不查库**，而且**只允许绑回环地址**——填局域网 IP 会被直接拒绝启动。
- **重启会续上**：bot 把轮询游标落在 `data/state.json`，重启后从上次位置继续（窗口由 `state.resume_window` 控制，默认 30 分钟）。停机期间积压的旧消息**只通知、不自动回复**（几小时前的话现在代你回，比漏掉更糟）。
- **发送失败不会重试**：发消息是不可逆动作，超时/HTTP 500 时无法确认对方到底收没收到，所以 bot **只如实告警、故意不自动重试**（重试可能发两条）。累计失败次数会出现在 `/status` 里。
- **主循环有外层守护**：未预期的异常不再让进程退出（打栈 + 等 5 秒继续跑）。后台无窗口时进程一退就没人收消息了。
- **隐私脱敏（默认关）**：`config.yaml` 的 `privacy.redact` 改成 `true` 后，**送云端的那一份文本**里的手机号/身份证/银行卡/邮箱/IP 会被打码（本地聊天记录一个字都不动），命中处会打日志。

## 常见问题

- **装好了但收不到消息**：按顺序查三件事——① `http://127.0.0.1:30001/QueryDB/status` 能不能通、`IsLogin` 是不是 1；② `bot.log` 里有没有轮询心跳（`[bot] 轮询心跳 #N，游标=X`），游标不动就是查库那条坏了；③ `/status` 里登录态和分片查询失败数。**注意 hook 每轮查询都会校验一遍数据库句柄，查得越勤越容易把它拖死**，所以 `poll_interval` 默认就是 **5 秒**，别改成 1、2 秒（实测 2 秒间隔会把微信卡到 CPU 999 秒）。
- **`poll_interval` 该填多少**：默认 **5 秒**（代码里的兜底值和 `config.yaml` 一致）。调小只会增加 hook 压力，不会让消息更快——它本来就是轮询。
- **`shell.timeout` 能调多大**：代码里的真实上限是 **600 秒**（`executor.MAX_TIMEOUT`，写 `99999` 也只给 600）。但**别调大**：本地执行是同步阻塞在收消息那条主循环线程上的，超时期间轮询和定时任务全停着，调大就是让 bot 卡更久。默认 60 秒够用。
- **读 PDF 需要 `pypdf`**：`read_file` / `find_files` 解析 PDF 用的是 `pypdf`，它已经在 `requirements.txt` 里，**现在的安装脚本会自动装上**。如果是老版本装的环境（或手动装的依赖）报「抽不出文字 / 没有 pypdf」，补一句：
  ```powershell
  .venv\Scripts\python.exe -m pip install pypdf
  ```
  （docx/xlsx/pptx 不用额外库——都是 zip+xml，标准库就能扒；扫描件 PDF 没有文字层，谁都抽不出来，助手会如实说读不了。）
- **移动/拷贝了文件夹之后跑不起来**：venv 里记的是绝对路径，挪了位置就失效。双击 `启动助手.bat`，它会自动检测到并重跑安装；也可以直接双击 `install.bat`。
- **安装时报 `pynng` 装不上 / 找不到 wheel**：你的 Python 太新（3.13+），那只是 wcferry 的依赖。**4.x 主线不需要 wcferry**，安装脚本会跳过它并把其余依赖装完；想彻底干净就换 3.11 重跑。
- **`import wcferry` 失败 / 连接失败**：那是 **wcferry 后端（3.9.x）**的问题——微信版本和 wcferry 必须严格对应（见上面的版本表）。主线用户不用管它。
- **`/用量` 显示「还没有记录」**：账本**已经接到模型调用链上**（`llm.py` 每次拿到用量就记一笔），所以这只说明**还没成功调用过模型**（没配 key / 一直失败），或者调用返回里没有用量字段。价目表只收了 DeepSeek 两条，别的模型只报 token、不报钱——这是有意为之，不编价格。
- **想换成本地模型（不花钱）**：`/provider 7` 选 Ollama，或自己改 `base_url` / `model` 指到本地 OpenAI 兼容服务。
- **搜索不精准**：当前是关键词匹配，想要语义搜索可以加 embedding（向量检索），需要的话再提。

## 参考来源

- [WeChatFerry](https://github.com/lich0821/WeChatFerry)（**另一条后端**：微信 3.9.x）
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump)（3.9.x 时代的可选导出工具）
- [WeChatFerry 文档](https://wechatferry.readthedocs.io/zh/latest/)
- aixed hook 文档：`showdoc.com.cn`（密码 `1234`；各版本索引在 hook 源码的 `README.md` 里，即 `installers/wechat-4.1.10.27/src-4.1.10.27/WeChat-Hook-4.1.10.27/README.md`）
