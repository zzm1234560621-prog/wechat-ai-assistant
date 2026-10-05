# 微信 AI 助手

把你的**个人微信**变成一个能读会写的 AI 助手：它实时读本地聊天记录来回答问题，也能代你给别人发消息、自动回复。
全程在微信原生窗口里说话，不用装第二个客户端。

## ⚠️ 先读这三条

1. **封号风险**：本项目向微信进程注入 hook DLL 来收发消息，**违反微信用户协议**。个人低频自用一般没事，官方严打时可能封号——建议用小号测试，风险自担。
2. **版本锁死**：hook 是按**特定微信版本**编译的，微信一更新就失效。装好后务必关掉微信自动更新（安装脚本会帮你关）。
3. **合规**：只处理**你自己账号、你合法拥有**的数据。未经授权抓取他人聊天记录是违法的。

## 它能做什么

- **问历史**：「我和张三聊了什么」「最近聊了什么」——实时查本地库，不用先导出
- **代你回复**：指定某个人或某个群，AI 结合上下文替你回；可开审核，草稿先发给你确认
- **主动发消息**：群发（可按分组/标签）、定时任务与到点提醒、关键词监听
- **读文件读图**：Word / Excel / PPT / PDF、压缩包递归、邮件、SQLite、图片、语音条转文字、视频音轨
  （语音转文字要装一次**可选组件**，见下面「可选组件」）
- **联网搜索**：问本机资料之外的事，回答带来源（自建 SearXNG，免费无 API key；后端**随包携带**，装一次可选组件即可，默认关）
- **碰你电脑上的文件**：列 / 搜 / 读 / 写 / 复制 / 移动 / 删到回收站（删除强制确认）
- **撤回原文回显**：对方撤回的内容，助手把原文回显给你
- **运行看护**：日志轮转、掉登录告警、token 用量统计、只读状态页

## 实现方法

主线就一句话：**用 [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) 把微信变成一个本地 HTTP 服务，剩下的都是普通程序。**

```
微信 PC 4.1.10.27 ──[注入 version.dll]──> 本地 HTTP 服务 127.0.0.1:30001
                                             ▲ 读：POST /QueryDB/execute  （直接发 SQL）
                                             │ 写：POST /SendTextMsg、/SendImgMsg
                                             ▼
   bot.py 每 5 秒轮询数据库拿新消息 ──> 是命令就执行，否则调大模型（带工具循环）──> 回复
```

- **收消息靠轮询**：这套 hook **没有推送接口**，只能被查询，所以 bot 每 `poll_interval`（默认 **5 秒**）查一次新消息——秒级，不是毫秒级。
- **发消息就是一次 HTTP POST**：`/SendTextMsg` 发文本，`/SendImgMsg` 发图片（**发普通文件也走它**，名字里的 Img 是上游历史遗留）。
- **查询统一走 `live_history.py`**：它同时适配微信 3.9.x / 4.1.x 两套库结构，别在别处裸调 hook。
- **hook 不支持并发**：查询和发送全部串行（查询还有预算闸），这是"慢一点但稳"的原因，也是微信不被搞崩的前提。
- **模型通道**支持 Anthropic 官方 / OpenAI 兼容两种协议，`/provider` 一键切服务商（DeepSeek、Claude、通义、Kimi、智谱、OpenAI、本地 Ollama）。

> 这个 hook 一共只暴露 8 个端点：`/SendTextMsg`、`/SendImgMsg`、`/ForwardXMLMsg`、`/Decode_Pic`、
> `/GetSelfProfile`、`/QueryDB/execute`、`/QueryDB/GetAllDBName`、`/QueryDB/status`——
> 没有"收消息"接口，这就是必须轮询的原因。

## 部署说明

**环境**：Windows 10/11 64 位 · 微信 PC **4.1.10.27** · **64 位 Python 3.11**（3.8~3.12 可用）

```powershell
git clone https://github.com/zzm1234560621-prog/wechat-ai-assistant.git
cd wechat-ai-assistant
```

### 一键部署：双击 `一键部署.bat`，一路回车

**就这一个动作**（它等于 `助手.bat` → `[9]`，只是省掉按菜单那一下）。它按真实顺序自己做完六件事：

```
双击 一键部署.bat  →  一路回车
                   │
                   ├─ ⓪ 查微信版本（没装 / 不是 4.1.10.27 就装包里自带的那份）
                   ├─ ① 装 hook 进微信（会弹 UAC，点「是」）
                   ├─ ② 装 Python 依赖（自动建虚拟环境，要联网，第一次几分钟）
                   ├─ ③ 可选组件（语音转文字 / 网上搜索；**会问你**，可跳过）
                   ├─ ④ 启动助手
                   └─ ⑤ 就地配模型（选服务商 + 填 API Key，不用去微信里打字）
```

> **③「可选组件」是什么**：语音转文字和网上搜索这两样，依赖**不随主程序装**
> （语音要下几百 MB 本地模型，搜索要一份自己专用的虚拟环境），所以单独问你一次。
> **跳过完全不影响**聊天、发消息、读文件、定时；以后想装/想关：双击 **`可选组件.bat`**。
> 详见下面「可选组件」一节。

装完之后日常就双击 **`助手.bat`**（菜单）：`[3]` 启动 / `[4]` 停 / `[5]` 看状态 / `[6]` 看日志。

- **唯一的前提**：这台电脑要有 **64 位 Python**（`一键部署.bat` 找不到会直接告诉你装哪条命令：
  `winget install -e --id Python.Python.3.11`，装完再双击一次；装了 32 位的它也会拦下来）。
- **微信版本必须是 4.1.10.27，这一步别跳过**：hook 是按**这一个版本**的函数偏移编译的，
  换个版本它注不进去——而且**不报错**：DLL 会被微信正常加载、安装脚本还写着「已放置，成功」，
  但 30001 永远没人监听，你看到的只是 bot 一直「连不上 30001」。
  `⓪` 会自动查：不对就装包里自带的那份 `installers\wechat-4.1.10.27\WeChatWin_4.1.10.27.exe`
  （静默装、不用手点；但会结束微信进程，**装完要重新扫码登录**）。
  手工装也行：双击那个 exe，弹「你已安装新版本的微信，安装更早的版本？」时点**「继续安装」**。
- 装完**重启微信**，浏览器打 `http://127.0.0.1:30001/QueryDB/status`，返回 JSON 就说明 hook 通了
  （`IsLogin: 1` = 已登录）。然后去微信「文件传输助手」发一句话就能用。

### 可选组件（语音转文字 / 网上搜索 / 文件格式包 / 本地语义检索）

这些**代码在包里**，但依赖和模型**不随包**（语音要下几百 MB 本地模型；搜索要一份自己专用的
虚拟环境；语义检索会拖进 torch）——所以要显式装一次。跳过完全不影响聊天、发消息、读文件、定时。

| 组件 | 装什么 | 要下多大 | 装完怎么开 |
|---|---|---|---|
| 语音转文字 | `faster-whisper` + `pilk` | 本地模型（大小看 `audio.model`，默认 `small` 约 **464MB**，走 hf-mirror 镜像） | `config.yaml` 的 `audio` 段；`backend: local` 时**音频一个字节不出本机** |
| 网上搜索 | 包**自带 SearXNG 源码**，在它的目录里建一份专用 venv | 依赖十几 MB | `search.enabled: true`；`search.autostart` 默认开（助手启动时把它带起来） |
| 文件格式增强包 | `av`（视频）、`extract-msg`（.msg 邮件）、`py7zr`/`rarfile`（压缩包）、`xlrd`/`olefile`（老 Office）、`Pillow`（PDF 内嵌图） | 几十 MB，装完**立刻**能用 | 不用开开关——多会读哪一种，缺的时候它会说 |
| 本地语义检索 | `sentence-transformers`（会拖进 **torch**，本表最重） | 依赖几百 MB + 本地模型 | 还要下模型、**建索引（建之前必须停一下助手）**；`semantic.enabled: true` |

入口：`一键部署.bat` 的第 `③` 步，或双击 **`可选组件.bat`**（也能看状态、切换
「以后还要不要自动装」）。⚠️ **本地语义检索是唯一「默认不装」的一项**（回车=跳过，要手打 `y`）。

三点值得知道：

- **模型和 venv 绝不跨机器拷**：模型几百 MB、torch 更大；venv 里记的是绝对路径，拷过去必坏
  （和助手自己的 `.venv` 同一条规矩）。所以包里只带源码，到你自己机器上现建现下。
- **`.rar` 光装 `rarfile` 还不够**：它只是个壳，真正解压要外部程序（unrar / 7-Zip / bsdtar）。
  状态屏会**分开**说这两件事，不会把「装了 rarfile」报成「.rar 能读了」。
- 「以后还要不要自动装」记在 **`settings.json` 的 `optional`**。关掉只是**不再自动装**，
  已经装好的不动。写 `settings.json` 而不是 `config.yaml`，是因为程序从不回写带注释的 `config.yaml`。

### 分步做（一键失败、或想自己控制每一步）

<details>
<summary>点开：手动装 hook / 装依赖 / 起 bot / 配模型</summary>

**① 装 hook（这一步要管理员）** —— 以管理员身份打开 PowerShell，再跑：（一键配置那条路不用自己提权，它会弹 UAC）

```powershell
Set-Location installers\wechat-4.1.10.27

# 放 version.dll + 用 ACL 挡住微信自动更新（日志 hook-install-log.txt）
powershell -NoProfile -ExecutionPolicy Bypass -File .\do_hook_install.ps1

# 微信不是 4.1.10.27 时才需要：静默安装仓库自带的那份（日志 install-log.txt）
powershell -NoProfile -ExecutionPolicy Bypass -File .\do_install.ps1
```

脚本自动探测微信安装目录和登录用户，换电脑、换盘都不用改。装完**重启微信**，
打 `http://127.0.0.1:30001/QueryDB/status` 能返回 JSON 就成了（连不上：DLL 没放对 / 微信版本不对 /
被安全软件拦了）。想摘掉 hook：`do_remove_hook.ps1`，或把微信目录里的 `version.dll` 改名后重启微信。

**② 装 Python 依赖**

```powershell
# 双击 install.bat：建虚拟环境 → 按 requirements.txt 装依赖 → 生成 启动助手.bat
# 或者手动：
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**③ 启动**：双击 `启动助手.bat`（窗口关了就停）；后台常驻用 `助手.bat` → `[3]`，
开机自启用 `助手.bat` → `[8]` → `[6]`。

**④ 配模型**：`助手.bat` → `[8]` → `[1]`，或双击 `配置模型.bat`；也可以在微信「文件传输助手」里发：

```
/provider         列出可选服务商
/provider 1       选第 1 个（DeepSeek），自动配好协议 + 接口 + 模型
/api sk-你的key    设置密钥，并当场测一次连通性
```

配完直接在微信里发消息提问即可。全部命令发 `/help` 看。

</details>

### 给别的电脑装

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_package.ps1
```

产出 `dist\wechat-ai-assistant-<日期>.zip`（约 238MB）。对方解压 → **双击 `一键部署.bat`**（一个文件走完全程）
→ 或者按包里的 `从这里开始.txt` 走。私人数据、真实 API key、`.venv` 都不会进包。

## 把别的软件接进微信（比如 vibe coding 的工具）

两条路：**接进本助手**（让它多一个工具或事件）走 ①，这是**本项目自己提供的接口**；
**接微信本身**（任何语言、任何进程）走 ②，那是 hook 提供的本地 HTTP，本项目也建在它上面。

### ① 本助手的接口：插件契约（`plugins/`，2026-10-04 落地）

**往 `plugins/` 放一个 `.py`、重启助手，就多了一个功能**，不用改项目里任何代码：

```python
# plugins/我的插件.py   文件名不要以 _ 开头；入口必须叫 setup(reg)
def setup(reg):
    def notify(args, ctx):            # ctx 只读：chat / cfg / from_self …
        return f"已记下待通知：{(args or {}).get('text', '')}（会话 {ctx.get('chat')}）"

    reg.register_tool({                # ① 给模型加一个工具
        "name": "notify_build",
        "description": "示例工具：记下一条构建结果（演示用）。",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string", "description": "内容"}},
                       "required": ["text"]},
        "handler": notify,
        "guidance": "用户说「构建完通知我」时调用 notify_build。",   # 必填：模型指导随定义走
        # "confirm": "always",         # 发消息 / 删文件这类不可逆动作就打开它
    })

    reg.register_event("on_message", lambda ctx: print("来了条消息", ctx.get("chat")))  # ② 事件
    # ③ reg.register_pending_kind(...) 还能自己加一类「等你回确认」的动作
```

- 六个事件：`on_start` / `on_message` / `before_reply` / `after_reply` / `on_tool` / `on_tick`；
  **只有 `before_reply` 能改行为**（改回复文本），其余只观察——路由只能有一个所有者。
- 工具和事件都跑在**收消息那条线程**上，所以必须快（超 `plugins.slow_ms`（默认 500ms）告警，
  连续 `plugins.disable_after`（默认 5）次**自动停用并说明原因**），而且**绝不许碰 hook**；
  插件工具的不可逆动作走**同一条确认闸**，没有例外通道。
- 加载失败只告警跳过、**绝不拦住 bot 启动**；重名工具在加载期失败；注册到一半**整份回滚**。
  `plugins.enabled: false` 关全部，`plugins.disabled: [名字]` 关单个；改完要**重启**才生效。
- 模板 `plugins/_example.py`，权威契约 [docs/plugin-contract-spec.md](docs/plugin-contract-spec.md)。

⚠️ **网络型连接器（MCP server、IDE 桥）这一版只钉了契约、没写实现**：它们需要的
`mode="worker"`（丢给后台线程）**声明即在加载期失败**——宁可起不来，也不许它悄悄按就地模式跑、
一边卡着收微信一边声称没卡。所以现在要把网络软件接进来，先用下面的 ②。

### ② 微信本身的接口：hook 的本地 HTTP（任何语言都能用）

**这套接口不是本项目专用的。** hook 在微信进程里起的是一个**本地 HTTP 服务**，任何会发 HTTP
的程序都能用它**读写微信**：Node / Python / Go 脚本、IDE 插件、命令行 AI 编码工具……
只要它跑在**同一台电脑**上（服务在 `127.0.0.1:30001`，别暴露到局域网或公网）。

两个方向都能用：

- **往外推**：编码工具跑完任务 / 需要你确认 / 报错了，`POST /SendTextMsg` 发到你微信，
  手机上就能看到进度（Cursor、Claude Code、Codex CLI 之类挂一个通知脚本即可）。
- **往里收**：用 `POST /QueryDB/execute` 轮询微信里的新消息，把你在微信里发的
  「继续修那个 bug」交给本地编码 agent 执行——微信就成了这些工具的遥控器。

```powershell
# 发一条微信消息（收件人写 wxid；filehelper = 文件传输助手）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:30001/SendTextMsg `
  -ContentType 'application/json' `
  -Body '{"wxidorgid":"filehelper","msg":"构建完成 ✅"}'
```

```python
# 轮询微信里的新消息：先 GET /QueryDB/GetAllDBName 看有哪些库，
# 消息主源是 message_fts.db，分片虚表 message_fts_v4_0 ~ v4_3，正文在 acontent（游标用 rowid）
import requests
API = "http://127.0.0.1:30001"

def q(db, sql):
    return requests.post(f"{API}/QueryDB/execute",
                         json={"optDbName": db, "SQL": sql}).json()

print(q("message_fts.db", "SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'"))
print(q("message_fts.db",
        "SELECT rowid, acontent, session_id, create_time FROM message_fts_v4_0 "
        "WHERE rowid > 100000 ORDER BY rowid ASC LIMIT 20"))
```

接的时候记住三条实测出来的规矩：

- **没有推送，只能轮询**：查询间隔别低于 5 秒，查太勤会把微信拖死（这个 hook 的查询是内存扫描 + SQLite）。
- **不要并发**：hook 不支持并发，两个进程同时查或同时发会让微信崩。本项目用单实例锁
  （回环端口 39001）兜底；你接的软件请串行调用。
- **发送是不可逆的**：本项目里所有发送都先走「待确认」再发；你自己的脚本接上去时，
  请别绕过这道闸（微信消息发出去就收不回来了）。

### ③ 顺带：同一天落地的另一条接口 —— 让助手反过来操作你电脑上的文件

微信里说「读一下那份报告」「建个目录」「把那个文件删了」，助手会真的去动磁盘上的文件
（列 / 搜 / 读 / 写 / 复制 / 移动 / **删到回收站**）。**删除和覆盖每次都要你回「确认」**，
系统目录（Windows / Program Files / ProgramData）默认挡住，默认只认你自己发的消息
（放开要改 `files.who`，那等于让对方能改你硬盘上的文件）。规格
[docs/computer-files-spec.md](docs/computer-files-spec.md)。

## 更多文档

- `CLAUDE.md` —— 架构、hook 铁律与踩坑的权威说明（改代码前先看）
- `docs/` —— 设计规格与实测记录（hook、文件、语音、搜索、插件契约等）
- `docs/README-full.md` —— 旧版详细 README：全部命令、配置项、常见问题排查

## 参考来源

- [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) —— 本项目**主线**用的 hook（微信 4.x），
  注入 `version.dll` 后提供本地 HTTP 接口；编译好的 DLL 与源码快照都在 `installers/` 里
- [WeChatFerry](https://github.com/lich0821/WeChatFerry) —— 保留的**另一条后端**（仅微信 3.9.x）
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump) —— 3.9.x 时代的历史记录导出工具（4.x 未采用）
