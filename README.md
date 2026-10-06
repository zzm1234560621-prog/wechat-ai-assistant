# 微信 AI 助手

**把你的个人微信，变成一个能读会写、还能动手操作你电脑的 AI 助手。**

它实时读本地聊天记录来回答问题，也能代你给别人发消息、自动回复；
全程在微信原生窗口里说话，不用装第二个客户端。

[English](README.en.md) | **简体中文**

---

## ⚠️ 先读这四条

1. **封号风险**：本项目向微信进程注入 hook DLL 来收发消息，**违反微信用户协议**。
   个人低频自用一般没事，官方严打时可能封号——建议用小号测试，**风险自担**。
2. **版本锁死：必须恰好是微信 PC 4.1.10.27**。hook 是按**这一个版本**的函数偏移编译的——
   换个版本它**注不进去，而且不报错**：DLL 会被微信正常加载、脚本还写着「已放置，成功」，
   但 30001 永远没人监听，你只看到助手一直「连不上 30001」。装好后也别升级微信（安装脚本会帮你关掉自动更新）。
3. **助手要以管理员身份运行**：语音条要读微信进程内存，而 Windows 不允许低权限进程读高权限进程的内存
   ——不提权开着微信，语音就永远读不出来。启动时会弹一次 UAC，点「是」即可。
4. **合规**：只处理**你自己账号、你合法拥有**的数据。未经授权抓取他人聊天记录是违法的。

## 它能做什么

- **代你回复**：指定某个人或某个群，AI 结合上下文替你回，并据此推断该用什么语气、怎么称呼对方；可开审核，草稿先发给你确认
- **主动发消息**：群发（可按分组/标签/群成员）、定时任务与到点提醒、关键词监听，也能帮你批量发祝福
- **读文件读图**：Word / Excel / PPT / PDF、压缩包递归、邮件、SQLite、图片、语音条转文字、视频音轨
  （语音转文字要装一次**可选组件**：`一键部署.bat` 的第 ③ 步，或双击 `可选组件.bat`）
- **联网搜索**：问本机资料之外的事，回答带来源（自建 SearXNG，免费无 API key；后端**随包携带**，
  装一次可选组件即可，默认关）
- **碰你电脑上的文件**：列 / 搜 / 读 / 写 / 复制 / 移动 / 删到回收站（删除强制确认）
- **撤回原文回显**：对方撤回的内容，助手把原文回显给你
- **可扩展**：往 `plugins/` 丢一个 `.py` 就多一个功能（插件契约见 [docs/plugin-contract-spec.md](docs/plugin-contract-spec.md)）

## 下载与安装

**环境**：Windows 10/11 64 位 · 微信 PC **4.1.10.27** · **64 位 Python 3.11**（3.8~3.12 可用）

### 方式 A · 下载成品包（推荐，非开发者走这条）

1. 到 **[Releases](https://github.com/zzm1234560621-prog/wechat-ai-assistant/releases)** 下载最新那个包（约 240MB）
2. 解压到任意目录（路径别带中文和空格，省得踩坑）
``一键部署.bat`，一路回车**，大约 15 分钟
4. 日常用 **`助手.bat`**：`[3]` 启动 / `[4]` 停 / `[5]` 看状态 / `[6]` 看日志

这个包里**自带**：微信 4.1.10.27 官方安装程序、编译好的 hook DLL、hook 源码快照、
随包携带的 SearXNG（搜索后端）、全部文档与自测脚本。

### 方式 B · 从源码跑（开发者）

```powershell
git clone https://github.com/zzm1234560621-prog/wechat-ai-assistant.git
cd wechat-ai-assistant
```

⚠️ **仓库里不含两个微信安装程序**（`WeChatWin_4.1.10.27.exe` 239MB、
`WeChatSetup-3.9.12.51.exe` 285MB）：它们太大，只跟着 Release 包发。
从源码这条路请自己准备 **微信 4.1.10.27**——版本必须严格一致（见上面「先读这四条」），或者干脆用方式 A 的 zip。

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

## 参考来源

- [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) —— 本项目**主线**用的 hook（微信 4.x），
  注入 `version.dll` 后提供本地 HTTP 接口；编译好的 DLL 与源码快照都在 `installers/` 里
- [WeChatFerry](https://github.com/lich0821/WeChatFerry) —— 保留的**另一条后端**（仅微信 3.9.x）
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump) —— 3.9.x 时代的历史记录导出工具（4.x 未采用）
- [SearXNG](https://github.com/searxng/searxng) —— 网上搜索的后端（随包携带）