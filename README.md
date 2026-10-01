# 个人微信 AI 助手

在**微信原生窗口**里跟一个 AI 助手对话，它能读取你的历史聊天记录来回答。

## ⚠️ 先读这段（重要）

1. **封号风险**：本工具通过 [WeChatFerry](https://github.com/lich0821/WeChatFerry)（`wcferry`）注入微信进程来收发消息，**违反微信用户协议**。个人低频自用一般没事，但官方严打时可能封号。建议用小号测试，风险自担。
2. **版本锁死**：`wcferry` 只支持特定微信 PC 版本（见下），**微信一更新就失效**。装好后务必关闭微信自动更新。
3. **合规**：只处理**你自己账号、你合法拥有**的数据。未经授权抓取他人聊天记录是违法的，别碰。

## 版本对应关系

| 微信 PC 版本 | wcferry 版本 |
|---|---|
| 3.9.12.51 | 39.5.2 |
| 3.9.12.17 | 39.4.4 |

> 不支持微信 4.x。微信版本可在「设置 → 关于微信」里看。

## 环境要求

- Windows 10/11 64 位
- Python **3.8 ~ 3.12**（推荐 **3.11**）
  > 3.13+ 上 wcferry 依赖的 `pynng` 通常没有预编译轮子，装不上。没装的话：
  > `winget install -e --id Python.Python.3.11`

## 安装步骤

**双击 `install.bat` 就行**（或者双击 `助手.bat` 进菜单选 `[7] 自动`）。

安装脚本会自动跑完：检测微信版本 → 匹配 wcferry → 建虚拟环境 → 装依赖 → 生成启动脚本。全程不用手动改任何文件。

> **项目文件夹放哪都行**——任意盘符、任意路径。脚本内部全部用相对路径（`%~dp0` / `__file__`）定位，
> 不依赖固定目录，换台电脑直接把文件夹拷过去重新跑一次 `install.bat` 即可。
>
> venv 记录的是绝对路径，所以**移动文件夹后 venv 会失效**。不用慌：双击 `启动助手.bat` 会自己检测到并重新安装。

如果想手动来（等价于上面脚本做的事）：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install wcferry==39.5.2 anthropic "setuptools<81" PyYAML
```

（`wcferry` 的版本必须匹配你的微信版本，见上面的对应表；`pip install -r requirements.txt` 也可以，但要先手动改版本号。）


## 使用流程

装完之后**全程在微信里操作**，不用再碰任何文件。

### 第一步：跑起来

双击 **`启动助手.bat`**。

助手会**实时**查微信本地数据库里的历史，不需要先导出。

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
API Key 已设置（sk-f****c674）。
当前：openai | deepseek-chat
✅ 连通性测试通过（模型回了「成功」）
现在可以直接发消息提问了。
```

配置写进 `settings.json`，重启后依然生效。

> 也可以用 `配置模型.bat` 走命令行向导（同样是选编号），配完会把结果发到文件传输助手。
> 效果一样，看你喜欢在哪配。

### 第三步：提问

直接发消息就行：

- 「我和李同学聊了什么」
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
| `/status` | 查看当前配置 |
| `/help` | 帮助 |

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

### （可选）导出完整历史

想全量导出、或接语义搜索/训练，再用这套：

```powershell
python export_history.py
```

它会依次：取密钥 → 解密数据库 → 导出为 `data/history.jsonl`。
也可用 PyWxDump 网页界面：`python -m pywxdump ui`（浏览器打开 http://127.0.0.1:5000/）。

## 目录结构

```
wechat-ai-assistant/
├── 助手.bat           # 总入口：双击进控制台菜单（降级/安装/启动/自启）
├── install.bat        # 一键安装（自动挑合适的 Python）
├── 启动助手.bat        # 启动 bot（安装时自动生成，venv 失效会自愈重装）
├── 降级.bat           # 微信 4.x -> 3.9.x（需管理员权限）
├── envsetup.py        # 路径与 venv 健康检查（所有脚本共用的地基）
├── installer.py       # 安装逻辑（检测版本 -> 建 venv -> 装依赖 -> 生成启动脚本）
├── console.py         # 控制台菜单
├── wechat_version.py  # 识别微信版本并匹配 wcferry
├── downgrade.py       # 降级微信
├── bypass_update.py   # 绕过微信强制更新
├── autostart.py       # 开机自启开关
├── bot.py             # 主程序（收消息 -> 命令/问答 -> 实时查历史 -> 调大模型回复）
├── live_history.py    # 实时查库（query_sql 直接读 MicroMsg.db / MSG*.db）
├── auto_reply.py      # 自动回复：代你回指定会话（生成、清洗、静默判定、/auto 命令）
├── agent_tools.py     # 给大模型的工具层（查联系人/翻历史/发消息，含待确认机制）
├── aixed_api.py       # aixed hook 的本地 HTTP 客户端（微信 4.x 后端）
├── settings.py        # 运行期配置（微信里命令改，存 settings.json）
├── llm.py             # 大模型封装（Anthropic 官方 / OpenAI 兼容两种协议）
├── history.py         # 静态历史检索（兜底）
├── export_history.py  # PyWxDump 解密 + 导出 JSONL（可选）
├── config.yaml        # 默认配置
├── settings.json      # 运行时配置（自动生成，命令改的都在这里）
└── requirements.txt   # 依赖
```

## 常见问题

- **移动/拷贝了文件夹之后跑不起来**：venv 里记的是绝对路径，挪了位置就失效。双击 `启动助手.bat`，它会自动检测到并重跑安装；也可以直接双击 `install.bat`。
- **安装时报 `pynng` 装不上 / 找不到 wheel**：你的 Python 太新（3.13+）。换 3.11 重跑 `install.bat`，脚本会自动优先挑 3.11。
- **`import wcferry` 失败 / 连接失败**：微信版本和 wcferry 不匹配，退回对应版本或换微信版本。
- **收不到消息**：确认 `target_chats` 里填对了 wxid；微信是否登录；`enable_receiving_msg` 是否返回 True。
- **实时查库报错 / 提示退回静态模式**：你的 wcferry 版本可能没有 `query_sql` 或方法名不同。跑一下看它有哪些接口：
  ```powershell
  python -c "from wcferry import Wcf; w=Wcf(); print([m for m in dir(w) if 'db' in m.lower() or 'sql' in m.lower() or 'table' in m.lower()])"
  ```
  把输出贴给我，我帮你对齐方法名。
- **想换成本地模型（不花钱）**：可接 Ollama，把 `llm.py` 里的客户端换成 OpenAI 兼容接口即可，需要再告诉我帮你改。
- **搜索不精准**：当前是关键词匹配，想要语义搜索可以加 embedding（向量检索），需要的话我帮你加。

## 参考来源

- [WeChatFerry](https://github.com/lich0821/WeChatFerry)
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump)
- [WeChatFerry 文档](https://wechatferry.readthedocs.io/zh/latest/)
