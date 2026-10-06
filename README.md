# 微信 AI 助手

**把你的个人微信，变成一个能读会写、还能动手操作你电脑的 AI 助手。**

它实时读本地聊天记录来回答问题，也能代你给别人发消息、自动回复；
全程在微信原生窗口里说话，不用装第二个客户端。

[English](README.en.md) | **简体中文**

---

## ⚠️ 先读这三条

1. **封号风险**：本项目向微信进程注入 hook DLL 来收发消息，**违反微信用户协议**。
   个人低频自用一般没事，官方严打时可能封号——建议用小号测试，**风险自担**。
2. **版本锁死**：hook 是按**特定微信版本**编译的，微信一更新就失效。装好后务必关掉微信自动更新
   （安装脚本会帮你关）。
3. **合规**：只处理**你自己账号、你合法拥有**的数据。未经授权抓取他人聊天记录是违法的。

> 这是一个**个人自用项目**：功能按作者自己的实际需要长出来，不是通用产品。
> 它能装到别的电脑上（有一键部署），但遇到问题请先读「排错」一节和 `docs/`——里面有真机踩出来的记录。

## 它能做什么

- **问历史**：「我和张三聊了什么」「最近聊了什么」——实时查本地库，不用先导出
- **代你回复**：指定某个人或某个群，AI 结合上下文替你回；可开审核，草稿先发给你确认
- **主动发消息**：群发（可按分组/标签/群成员）、定时任务与到点提醒、关键词监听
- **读文件读图**：Word / Excel / PPT / PDF、压缩包递归、邮件、SQLite、图片、语音条转文字、视频音轨
  （语音转文字要装一次**可选组件**，见下面「可选组件」）
- **联网搜索**：问本机资料之外的事，回答带来源（自建 SearXNG，免费无 API key；后端**随包携带**，
  装一次可选组件即可，默认关）
- **碰你电脑上的文件**：列 / 搜 / 读 / 写 / 复制 / 移动 / 删到回收站（删除强制确认）
- **撤回原文回显**：对方撤回的内容，助手把原文回显给你
- **运行看护**：日志轮转、掉登录告警、token 用量统计、只读状态页
- **可扩展**：往 `plugins/` 丢一个 `.py` 就多一个功能（插件契约，见下）

## 下载与安装

**环境**：Windows 10/11 64 位 · 微信 PC **4.1.10.27** · **64 位 Python 3.11**（3.8~3.12 可用）

### 方式 A · 下载成品包（推荐，非开发者走这条）

1. 到 **[Releases](https://github.com/zzm1234560621-prog/wechat-ai-assistant/releases)** 下载
   `wechat-ai-assistant-<日期>.zip`（约 240MB）
2. 解压到任意目录（路径别带中文和空格，省得踩坑）
3. **双击 `一键部署.bat`，一路回车**，大约 15 分钟
4. 日常用 **`助手.bat`**：`[3]` 启动 / `[4]` 停 / `[5]` 看状态 / `[6]` 看日志

这个包里**自带**：微信 4.1.10.27 官方安装程序、编译好的 hook DLL、hook 源码快照、
随包携带的 SearXNG（搜索后端）、全部文档与自测脚本。

包里**没有**（故意的）：你的聊天记录、本机配置、真实 API key、Python 虚拟环境、语音模型
（`.venv` 和模型都由第 ③ 步在你机器上现建现下——跨机器拷贝必坏）。
包里那份 `config.yaml` / `settings.json` 是**示例**，key 是空的。

### 方式 B · 从源码跑（开发者）

```powershell
git clone https://github.com/zzm1234560621-prog/wechat-ai-assistant.git
cd wechat-ai-assistant
```

⚠️ **仓库里不含两个微信安装程序**（`WeChatWin_4.1.10.27.exe` 239MB、
`WeChatSetup-3.9.12.51.exe` 285MB）：它们太大，只跟着 Release 包发。
从源码这条路请自己准备 **微信 4.1.10.27**——版本必须严格一致（原因见下面「部署说明」的
`⓪` 那一步），或者干脆用方式 A 的 zip。

拿到源码后跟方式 A 一样，双击 **`一键部署.bat`**。

## 实现方法

主线就一句话：**用 [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) 把微信变成一个本地 HTTP 服务，剩下的都是普通程序。**

```
微信 PC 4.1.10.27 ──[注入 version.dll]──> 本地 HTTP 服务 127.0.0.1:30001
                                             ▲ 读：POST /QueryDB/execute  （直接发 SQL）
                                             │ 写：POST /SendTextMsg、/SendImgMsg
                                             ▼
   bot.py 每 5 秒轮询数据库拿新消息 ──> 是命令就执行，否则调大模型（带工具循环）──> 回复
```

- **收消息靠轮询**：这套 hook **没有推送接口**，只能被查询，所以 bot 每 `poll_interval`（默认 **5 秒**）
  查一次新消息——秒级，不是毫秒级。
- **发消息就是一次 HTTP POST**：`/SendTextMsg` 发文本，`/SendImgMsg` 发图片
  （**发普通文件也走它**，名字里的 Img 是上游历史遗留）。
- **查询统一走 `live_history.py`**：它同时适配微信 3.9.x / 4.1.x 两套库结构，别在别处裸调 hook。
- **hook 不支持并发**：查询和发送全部串行（查询还有预算闸），这是「慢一点但稳」的原因，
  也是微信不被搞崩的前提。
- **模型通道**支持 Anthropic 官方 / OpenAI 兼容两种协议，`/provider` 一键切服务商
  （DeepSeek、Claude、通义、Kimi、智谱、OpenAI、本地 Ollama）。

> 这个 hook 一共只暴露 8 个端点：`/SendTextMsg`、`/SendImgMsg`、`/ForwardXMLMsg`、`/Decode_Pic`、
> `/GetSelfProfile`、`/QueryDB/execute`、`/QueryDB/GetAllDBName`、`/QueryDB/status`——
> 没有「收消息」接口，这就是必须轮询的原因。

## 部署说明

> ⚠️ **助手必须以管理员身份运行**（2026-10-06 起的硬约束，本机和所有部署的电脑都一样）。
> 原因不是「想提权」，而是**语音条**：语音要读微信进程内存，而 Windows 不允许低权限进程读
> 高权限进程的内存——你要是提权开着微信，普通权限的助手就永远读不出语音。
> 所以 `助手.bat` / `启动助手.bat` / 一键部署的「启动助手」这一步**都会弹一次 UAC**，
> 点「是」即可；整套流程内部只提权这一处（细节与踩过的坑见
> [docs/admin-elevation-notes.md](docs/admin-elevation-notes.md)）。

### 一键部署：双击 `一键部署.bat`，一路回车

**就这一个动作**（它等于 `助手.bat` → `[9]`，只是省掉按菜单那一下）。它按真实顺序自己做完六件事：

```
双击 一键部署.bat  →  一路回车
                   │
                   ├─ ⓪ 查微信版本（没装 / 不是 4.1.10.27 就装包里自带的那份）
                   ├─ ① 装 hook 进微信（会弹 UAC，点「是」）
                   ├─ ② 装 Python 依赖（自动建虚拟环境，要联网，第一次几分钟）
                   ├─ ③ 可选组件（语音转文字 / 网上搜索 / 文件格式包 / 语义检索；**会问你**，可跳过）
                   ├─ ④ 启动助手（助手要管理员权限，**这里会弹 UAC**，点「是」）
                   └─ ⑤ 就地配模型（选服务商 + 填 API Key，不用去微信里打字）
```

> **③「可选组件」是什么**：那几样依赖**不随主程序装**（语音要下几百 MB 本地模型，
> 搜索要一份自己专用的虚拟环境，语义检索会拖进几百 MB 的 torch），所以单独问你一次。
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

### 助手一直刷「hook 已加载，但数据库打不开」怎么办

先记住一件事：**包里那个 `version.dll` 和微信目录里正在用的那个是两个文件。**
解压新包、装依赖、配模型**都不会**替换微信目录里那份 —— 只有「装 hook」那一步才会。
所以出现过这个形状：包是最新的、日志也正常，功能却还是旧的（因为微信里躺的是旧 DLL）。

三条短命令就能定位（不用管理员、助手开着也能跑）：

```powershell
(Get-Item "C:\Program Files\Tencent\Weixin\version.dll").Length
Invoke-RestMethod http://127.0.0.1:30001/QueryDB/status | ConvertTo-Json -Depth 5
Get-Content <包目录>\installers\wechat-4.1.10.27\hook-install-log.txt
```

| 看到 | 结论 | 怎么做 |
|---|---|---|
| 第一条是 **519168**（新版是 **527360**） | 微信里那份**还是旧的 hook** | `助手.bat` → `[8]` → `[7]` → **`[4]` 只替换 version.dll**，然后**重启微信** |
| 第三条报「文件不存在」 | **装 hook 那一步从来没跑过**（日志是它第一件事就写的） | 同上；日志不存在就别怀疑别的 |
| `status` 里**没有** `LoginGateInfo` 字段 | 文件换过了，但**微信没重启**（DLL 只在进程启动时加载） | 完全退出微信（托盘图标也退）→ 重开 → 扫码登录 |
| 有 `LoginGateInfo` 但 `IsLogin: 0` | 新 hook 在跑，只是**还没等到核心库被写** | 等一分钟再跑第二条；一直不动就是微信没真登录 |
| 有 `LoginGateInfo` 且 `IsLogin: 1` | ✅ 通了 | 起助手（`助手.bat` → `[3]`，**UAC 要点「是」**） |

另外两个容易白忙的地方：

- **一键配置会停在「第 0 步：微信版本」**。那时它会明说「后面的步骤一步都没执行」——
  看到这句就是**装 hook 还没做**，不是配置完了。把微信换成 4.1.10.27 再按一次 `[9]`。
- **提权窗口会一闪就关**：装 hook / 替换 DLL 的脚本跑在管理员新窗口里，结果**同时写在
  `installers\wechat-4.1.10.27\hook-install-log.txt`（或 `hook-fix-log.txt`）**里，
  随时可以 `Get-Content` 回看。

更详细的一站式诊断：`助手.bat` → `[8]` → 跑 `hook_doctor.py`（或包根那个同名文件）。

### 可选组件（语音转文字 / 网上搜索 / 文件格式包 / 本地语义检索）

这些**代码在包里**，但依赖和模型**不随包**（语音要下几百 MB 本地模型；搜索要一份自己专用的
虚拟环境；语义检索会拖进 torch）——所以要显式装一次。跳过完全不影响聊天、发消息、读文件、定时。

| 组件 | 装什么 | 要下多大 | 装完怎么开 |
|---|---|---|---|
| 语音转文字 | `faster-whisper` + `pilk` | 本地模型（大小看 `audio.model`，默认 `small` 约 **464MB**，走 hf-mirror 镜像） | 装完直接能用；`config.yaml` 的 `audio` 段可调；`backend: local` 时**音频一个字节不出本机** |
| 网上搜索 | 包**自带 SearXNG 源码**，在它的目录里建一份专用 venv | 依赖十几 MB | **装完自动打开**（把 `search.enabled` 写进 `settings.json`）并把服务起起来；`search.autostart` 默认开（助手启动时也会带起它） |
| 文件格式增强包 | `av`（视频）、`extract-msg`（.msg 邮件）、`py7zr`/`rarfile`（压缩包）、`xlrd`/`olefile`（老 Office）、`Pillow`（PDF 内嵌图） | 几十 MB，装完**立刻**能用 | 不用开开关——多会读哪一种，缺的时候它会说 |
| 本地语义检索 | `sentence-transformers`（会拖进 **torch**，本表最重） | 依赖几百 MB + 本地模型 | 装完自动打开 `semantic.enabled`；**建索引会先问你「停助手 → 建索引 → 起回来」**（拒了就只留命令） |

入口：`一键部署.bat` 的第 `③` 步，或双击 **`可选组件.bat`**（也能看状态、切换
「以后还要不要自动装」）。**四项默认都会装**，**一路回车就齐**；
全程只有两处会停下来问：语义检索的**建索引要停一下助手**，以及你想跳过某一项时按 `n`。

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

### 自己重新打包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_package.ps1
```

产出 `dist\wechat-ai-assistant-<日期>.zip`（约 240MB）。私人数据、真实 API key、`.venv`
和安装日志都不会进包（脚本自带一遍自检，发现就 `exit 1`）。
对方解压 → **双击 `一键部署.bat`**（一个文件走完全程）→ 或者按包里的 `从这里开始.txt` 走。

## 已知限制与「不做的事」

诚实比好看重要，这些是**故意的**：

- **只在 Windows 上**，且只支持**微信 PC 4.1.10.27** 这一个版本（hook 按函数偏移编译）。
- **收消息是轮询**（默认 5 秒），所以不是「毫秒级秒回」；hook **不支持并发**，
  所有查询/发送串行——这是稳的前提，不是性能问题。
- **发语音条 / 打语音电话做不到**（hook 没有这些端点）。定时任务里的 `call` 能力**已下线**，
  到点会**如实报错**，不会偷偷改成发文本。
- **多账号：已取消**，不支持。
- **MCP server / IDE 桥这类网络连接器**：插件契约里**只钉了契约、没写实现**——
  声明即在加载期失败，宁可起不来，也不许它一边卡着收微信一边声称没卡。
- **`read_image` 只能读微信写过的缩略图缓存**；以**文件**形式发来的图是明文原图，更清楚。
  读不出来时它会说「看不了」，**不会编内容**。
- 任何**发消息、删文件、跑命令**都是不可逆动作，全部走「待确认」闸门。
  助手的定位是「帮你干活」，所以**默认只认你自己发来的消息**。

## 把别的软件接进微信（比如 vibe coding 的工具）

**这一段先占位：下一个版本会把 vibe coding 工具接进去** —— 编码工具跑完任务、需要你确认、
或者报错了，直接在微信里告诉你；你也能在微信里让它继续干活。

这一版先不铺开讲怎么接。已经落地的两条底层接口（本助手的插件契约、hook 的本地 HTTP）
写在 `docs/` 里：[docs/plugin-contract-spec.md](docs/plugin-contract-spec.md)；
hook 暴露的 8 个端点见上面「实现方法」。

## 项目结构

```
bot.py                 主循环：轮询 → 命令 / 大模型 → 回复
live_history.py        查微信库的唯一入口（同时适配 3.9.x / 4.1.x 两套 schema）
agent_tools.py         给大模型的工具层 + 待确认机制 + 查询预算
plugins.py / plugins/  插件契约（工具与事件的唯一真源）+ 用户插件目录
llm.py / providers.py  两种模型协议（Anthropic / OpenAI 兼容）+ 服务商预设
file_read.py 等        读文件 / 图片 / 语音 / 视频 / 压缩包 / 邮件 / 数据库
files.py               操作本机文件（列/搜/读/写/复制/移动/删到回收站）
scheduler.py           定时任务（到点发消息 / 提醒我 / 问一句话）
health.py 等           日志轮转、掉登录告警、用量统计、只读状态页
console.py / *.bat     本地控制台（助手.bat 菜单、一键部署.bat、可选组件.bat）
tools/                 打包、诊断、OCR/Office/缩放等脚本
docs/                  设计规格与真机实测记录
searxng/               随包携带的搜索后端（网上搜索用）
installers/            hook DLL 与安装脚本（+ Release 包里那份微信安装程序）
```

## 更多文档

- [CLAUDE.md](CLAUDE.md) —— 架构、hook 铁律与踩坑的权威说明（改代码前先看）
- [docs/](docs/) —— 设计规格与实测记录（hook、文件、语音、搜索、插件契约等）
- [docs/README-full.md](docs/README-full.md) —— 旧版详细 README：全部命令、配置项、常见问题排查

## 协议与免责声明

本项目以 **[MIT License](LICENSE)** 开源，**按「现状」提供，不附带任何担保**。

**免责声明**（请完整读完）：

- 本项目通过注入 hook DLL 扩展微信功能，**违反微信用户协议**。使用可能导致**账号被封禁**、
  消息丢失或账号数据损坏。**一切后果由使用者自行承担**，作者不承担任何责任。
- 请**只**对**你自己的账号**、**你合法拥有**的数据使用本项目。抓取、分析他人聊天记录
  在多数司法管辖区**违法**。
- 使用者须自行遵守所在地法律法规及腾讯的服务条款。**请勿用于商业用途、大规模群发、
  骚扰或任何违法活动。**
- 本项目与腾讯、微信官方**无任何关联**，未获其授权或认可。

**第三方组件**：hook 来自 [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook)；
随包携带的搜索后端 [SearXNG](https://github.com/searxng/searxng) 以 **AGPL-3.0** 授权
（源码随包提供，见 `searxng/`）；另参考 [WeChatFerry](https://github.com/lich0821/WeChatFerry)、
[PyWxDump](https://github.com/xaoyaoo/PyWxDump)。各组件版权归其各自作者所有。

## 参考来源

- [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) —— 本项目**主线**用的 hook（微信 4.x），
  注入 `version.dll` 后提供本地 HTTP 接口；编译好的 DLL 与源码快照都在 `installers/` 里
- [WeChatFerry](https://github.com/lich0821/WeChatFerry) —— 保留的**另一条后端**（仅微信 3.9.x）
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump) —— 3.9.x 时代的历史记录导出工具（4.x 未采用）
- [SearXNG](https://github.com/searxng/searxng) —— 网上搜索的后端（随包携带）
