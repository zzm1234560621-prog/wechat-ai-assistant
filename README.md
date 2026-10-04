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
- **联网搜索**：问本机资料之外的事，回答带来源（本机自建 SearXNG，免费无 API key，默认关）
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

**环境**：Windows 10/11 64 位 · 微信 PC **4.1.10.27** · Python **3.11**（3.8~3.12 可用）

```powershell
git clone https://github.com/zzm1234560621-prog/wechat-ai-assistant.git
cd wechat-ai-assistant
```

### 第一步：装 hook（需要管理员权限）

**最省事**：右键 `助手.bat` → 以管理员身份运行 → 按 `[9]`「一键配置」，然后**一路回车**：
装 hook → 装依赖 → 启动 → 配模型，四件事一次走完。

想分步做：

```powershell
Set-Location installers\wechat-4.1.10.27

# 微信版本不是 4.1.10.27 时，先装仓库里自带的那份（静默安装，日志 install-log.txt）
powershell -NoProfile -ExecutionPolicy Bypass -File .\do_install.ps1

# 把 version.dll 放进微信安装目录，并用 ACL 挡住微信自动更新（日志 hook-install-log.txt）
powershell -NoProfile -ExecutionPolicy Bypass -File .\do_hook_install.ps1
```

这组脚本会**自动探测**微信安装目录和登录用户，换电脑、换盘都不用改。
**重启微信**，然后确认 hook 已经加载：

```
GET http://127.0.0.1:30001/QueryDB/status
```

返回 JSON 就是加载成功（`IsLogin: 1` 表示已登录）。连不上就是 hook 没生效：DLL 没放对 /
微信版本不对 / 被安全软件拦了。
想临时摘掉 hook：跑 `do_remove_hook.ps1`，或把微信目录里的 `version.dll` 改名后重启微信。

### 第二步：装 Python 环境

```powershell
# 双击 install.bat：建虚拟环境 → 按 requirements.txt 装依赖 → 生成 启动助手.bat
# 或者手动：
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 第三步：启动

双击 **`启动助手.bat`**（窗口关了就停）；想后台常驻就用 `助手.bat` → `[3] 启动`，
再在 `[8] 更多` 里开开机自启。

### 第四步：配模型（在微信「文件传输助手」里发）

```
/provider         列出可选服务商
/provider 1       选第 1 个（DeepSeek），自动配好协议 + 接口 + 模型
/api sk-你的key    设置密钥，并当场测一次连通性
```

配完直接在微信里发消息提问即可。全部命令发 `/help` 看。

### 给别的电脑装

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_package.ps1
```

产出 `dist\wechat-ai-assistant-<日期>.zip`（约 238MB）。对方解压 → 双击 `install.bat` →
按包里的 `从这里开始.txt` 走。私人数据、真实 API key、`.venv` 都不会进包。

## 把别的软件接进微信（比如 vibe coding 的工具）

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
- **发送是不可逆的**：本项目里所有发送都先走"待确认"再发；你自己的脚本接上去时，
  请别绕过这道闸（微信消息发出去就收不回来了）。

另外，如果你想接的是**本助手**（而不是直接打 hook），也有一条路：往 `plugins/` 放一个 `.py`
就是一个新工具或事件，不用改项目代码，契约见 `docs/plugin-contract-spec.md`。
需要后台线程的网络型连接器（`mode="worker"`）这一版**声明会在加载时失败**——
宁可起不来，也不许它悄悄卡住收微信的那条线程。

## 更多文档

- `CLAUDE.md` —— 架构、hook 铁律与踩坑的权威说明（改代码前先看）
- `docs/` —— 设计规格与实测记录（hook、文件、语音、搜索、插件契约等）
- `docs/README-full.md` —— 旧版详细 README：全部命令、配置项、常见问题排查

## 参考来源

- [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) —— 本项目**主线**用的 hook（微信 4.x），
  注入 `version.dll` 后提供本地 HTTP 接口；编译好的 DLL 与源码快照都在 `installers/` 里
- [WeChatFerry](https://github.com/lich0821/WeChatFerry) —— 保留的**另一条后端**（仅微信 3.9.x）
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump) —— 3.9.x 时代的历史记录导出工具（4.x 未采用）
