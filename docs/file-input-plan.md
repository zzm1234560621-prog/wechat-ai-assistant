# 任意文件与图片通读 —— 实施计划

> 上游规格：`docs/file-input-spec.md`（2026-10-02 用户批准）。**本计划只做规格里写下的范围**，
> 规格没写的不许顺手加；要改范围先改规格并让用户点头。
> 计划放在 `docs/` 平铺（和 `voice-input-spec.md` / `fixes-2026-10.md` 同一层）：
> 这个仓库的持久文档不用 `docs/aegis/` 那套目录，**仓库既有的约定优先于工具的默认路径**。

## 目标

微信里发来的**任何文件**（现代/老 Office、PDF 含扫描页、压缩包、邮件、音频、视频、图片、任意纯文本）
都能本地全文解析（输入不限大小），把**文字与图片**交给模型；老行为一个字不变。

## 基线 / 权威引用

- 规格：`docs/file-input-spec.md`（决策六条、硬约束六条、验收十条、配置项）
- 家族规格：`docs/voice-input-spec.md`（音频那条已实现，本计划只放宽它的时长上限）
- 铁律：`CLAUDE.md`「hook 使用铁律」「改代码时的约定」「素材暂存」
- 加密图背景：`docs/wechat4-dat-image-notes.md`（别人发来的 `.dat` 图**不在**范围）

## TDD Route

- mode: `off`（Aegis 引导里的默认；无用户显式 strict 要求）
- decision: `skipped`（不写 RED/GREEN 步骤）
- authority: `AEGIS_DSD_TDD=off` + 仓库约定「每处改动配回归自测」
- test posture: 实现与回归用例**同批**交付；用例落在现有 `selftest_*.py` 家族里
- verification: 新增用例 + 现有 11 份自测全绿；真机样本由用户在**普通命令行**复核（沙箱起不了 Office/子进程）

## 兼容边界（不许破）

1. `file_read.extract(path, cfg)` / `locate(name)` / `search_files(...)` 现有调用点与语义**不变**（只加可选参数）。
2. `image.mode` 默认仍是 `ocr`：不配任何新键时，行为与今天**逐字节相同**。
3. `read_file` 老参数（`contact` + `local_id`）与返回文案保持可用；新增 `cursor` 是可选参数。
4. `agent_tools.TOOLS` 与 `config.yaml` 的 `system_prompt` **必须同步改**（仓库规矩）。
5. 解压炸弹封顶**只能调大、不能关**；`file.max_bytes: 0` 才等于不限。

## 任务（按顺序，每个都可独立验证）

### T1 · `file_read.py`：上限语义 + 内容嗅探

> 状态：✅ 已做（`selftest_io_llm.t10`）
- 文件：`file_read.py`、`selftest_io_llm.py`
- 最小改动：
  - `_cfg()` 拆出 `max_bytes` 的**显式 0 判据**（现在 `int(v or 30MB)` 会把 0 当没配 → 静默违背"不限大小"）
  - 新增 `unpack_cap(cfg)`：`file.max_unpack`（默认 200MB）；配 0/负数 → 退回默认并告警（**封顶不可关**）
  - 新增 `inline_bytes(cfg)`：交给 `agent_tools` 判断重活
  - 新增 `sniff(path)`：魔数（PDF/ZIP/OLE2/RAR/7z/gzip/图片）+ 可解码性 → `text | binary | <容器类型>`
  - `extract()`：未知后缀先嗅探；是文本按文本读，二进制如实说，容器/老格式先回"还没接线"（T6/T7 接上）
- 验证：`selftest_io_llm.py` 新增用例（0=不限、超限拒绝、嗅探正负例、原行为回归）
- 兼容：`file.max_bytes` 不配时仍是 30MB

### T2 · 分页与导出

> 状态：✅ 已做（`selftest_io_llm.t11`）
- 文件：`file_read.py`、`agent_tools.py`（`read_file` + `TOOLS`）、`config.yaml`、`config.example.yaml`
- 最小改动：全文导出到 `file.export_dir`，返回「前 max_chars 字 + cursor」；`read_file` 加可选 `cursor`；导出目录清理（超期/超 50 份，**打日志**）
- 验证：不重不漏用例；system_prompt 两处同步
- 兼容：不给 cursor 时返回与今天相同（只是多了尾部提示）

### T3 · Office 内嵌图片 + PDF 扫描页

> 状态：✅ 已做（`selftest_io_llm.t12` + 真机扫描件 PDF 0.9 秒）
- 文件：`file_read.py`、`image_read.py`（调用 T5 的通道）
- 最小改动：抽 `word|ppt|xl/media/*` 与 PDF 页图 → 走图片通道 → 文字拼进正文并**标明来源**（"第 N 张图/第 N 页"）
- 验证：现场构造含 png 的 docx/xlsx/pptx；PDF 用盘上真件
- 兼容：纯文字文档输出不变（不夹空图段）

### T4 · `read_worker.py` + bot 接线 + 异步契约

> 状态：✅ 已做（`selftest_read_worker.py`）
- 文件：新增 `read_worker.py`、`bot.py`、`agent_tools.py`、新增 `selftest_read_worker.py`
- 最小改动：单线程串行队列、job 超时、`data/readjobs.json` 启动残留提示、`read.queue_max`；bot 主循环 drain 后**由主线程发消息**（同 `scheduler action=ask`）；工具重活分支返回「已提交 + 别编造」
- 验证：顺序 / 超时 / **零查库**（`_Boom` 客户端断言）/ 队满如实说 / 重启残留提示
- 硬约束：worker 绝不碰 hook、绝不查库、绝不发送

### T5 · 图片交付四模式（`image_read.py` + `llm.py`）

> 状态：✅ 已做（`selftest_image_handoff.py` + `selftest_bot_loop.t_inline_image_round`）
- 文件：`image_read.py`、`llm.py`、`agent_tools.py`、`bot.py`、新增 `selftest_image_handoff.py`
- 最小改动：`mode: off|ocr|vision|inline`、`ocr_first`（免费优先）、`send_max_bytes`、`downscale`、md5 描述缓存、`max_per_round`；anthropic 通道加图片块翻译（openai 通道实测已透传）；**图片绝不进 `bot.dialog_*`**
- 验证：四模式 / OCR 有字时**零次**视觉调用 / 模型不收图时如实说 / 缓存命中不重复计费 / 记忆里无 base64

### T6 · `legacy_office.py` 多引擎降级

> 状态：✅ 已做（`selftest_legacy_office.py` 19 项 + 真机 .doc/.xls/.ppt）
- 文件：新增 `legacy_office.py`、`file_read.py`、新增 `selftest_legacy_office.py`
- 最小改动：`Office COM → WPS COM → LibreOffice headless → antiword(.doc) → olefile 粗略抽取 → 如实说`；只读、无窗口、带超时、**每次报出用的是哪个引擎**；`.xls` 优先走 `xlrd`
- 验证：桩测顺序与缺失提示；真机 16 个老格式（**用户普通命令行复核**）

### T7 · `archive_read.py`

> 状态：✅ 已做（`selftest_archive.py` 19 项 + 真机 1GB zip 0.8 秒）
- 文件：新增 `archive_read.py`、`file_read.py`、新增 `selftest_archive.py`
- 最小改动：zip/7z/rar 递归、`archive.max_depth`/`max_members`、共享解压预算
- 验证：递归层级、成员数上限、炸弹仍被拒、rar 缺外部解压器时如实说

### T8 · 文档与配置同步

> 状态：✅ 已做（本节；规格验收记录 + `docs/fixes-2026-10.md` + CLAUDE.md 瘦身）
- 文件：`CLAUDE.md`、`config.yaml`、`config.example.yaml`、`docs/fixes-2026-10.md`
- 内容：新模块与铁律、`read_file` 新参数、`image.mode` 四值、验收记录（含沙箱限制说明）

### P2 · 视频与长音视频

> 状态：✅ 已做（`selftest_video.py` 24 项 + `selftest_audio.t7`；画面 OCR 与分段续读是真跑验证的；音轨转写待装模型后真跑）
- 文件：新增 `video_read.py`、`audio_read.py`（分段）、`selftest_audio.py` / 新 `selftest_video.py`
- 内容：PyAV 抽音轨（`video.max_seconds: 1800` 单段）+ 抽帧（走图片通道）；cursor 续读下一段；`audio.max_seconds` 一并放宽到 1800 并说明理由

### P3 · 邮件 / 数据库 / 压缩包补全

> 状态：✅ 已做（`selftest_mail_db.py` 28 项：真 `.eml`/真 SQLite/真 7z；`.msg` 只有逻辑 + 如实说，真文件待用户给样本；`.rar` 缺外部解压器）
- 文件：`file_read.py`、`archive_read.py`、新增 `selftest_mail.py`
- 内容：`.eml`（标准库，附件递归）、`.msg`（可选 `extract-msg`）、`.sqlite/.db` 只读限量；7z/rar 完善

## 每个任务的完成判据（统一）

1. 新增/修改的用例**全绿**；**现有 11 份自测一份都不许红**。
2. 失败路径都有「照做指引」的文案，**不许空字符串假装成功**。
3. 改动过的契约（工具参数 / 配置键 / system_prompt）**三处同步**：代码、`config.yaml`、`config.example.yaml`。
4. 真机才验得动的项（Office COM / 视频 / Ollama）单独列进 `docs/fixes-2026-10.md` 的**待用户复核**段，并写清"要在不受限的普通命令行里跑"。

## 风险与回退

| 风险 | 回退 |
|---|---|
| 新后台线程引入并发问题 | 只碰磁盘 + 模型 HTTP；出问题可把 `file.inline_bytes` 调大，让一切都走同步老路 |
| 嗅探误判把二进制当文本 | 双判据（魔数 + 可解码性）；拿不准就如实说读不了 |
| 图片费用 | `max_per_round` / `send_max_bytes` / 降采样 / md5 缓存；把 `image.mode` 拨回 `ocr` 即可归零 |
| 老格式转换卡住 | 超时强杀 + 引擎顺序可配；把 `legacy.engines` 设为具体引擎或 `off` |

## 执行路线

`inline`（任务之间有依赖，且要贴着 `CLAUDE.md` 的密集约定做；不需要 subagent 协调开销）。
`User confirmation required: no`（无付费/破坏性/外部动作；Office COM 与视频的真机复核会单独请用户跑）。
