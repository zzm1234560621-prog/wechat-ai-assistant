# 语音：两个真机缺陷（2026-10-03）——细节与「别再犯」

> 本文件是 `CLAUDE.md` 的 `audio_read.py` 段的细节延伸。
> 起因：用户问「这个项目还是没有办法实现语音命令吗」，真机验证后挖出两个缺陷。
> **两条都已修，各自带回归**。

## 背景：语音其实是能用的

语音条（那个小喇叭，`local_type=34`）的音频**不落磁盘**，但微信要播放它就得在**进程内存**里
持有明文 SILK。所以 `voice_mem.py` 趁热扫内存 → `pilk` 解码 → 本地 whisper 转写。
另有一条零成本路：用户在微信里点过「转文字」后，结果会落进 `packed_info_data`（见
`live_history.voice_transcript()`）。完整可行性结论见 `docs/voice-msg-feasibility.md`。

**真机证据（2026-10-03）**：`bot.log` 里确实出现过
`[bot] 语音 → 文字（内存里的语音 + 本地转写）：你好` —— 链路是通的。

> ⚠️ **2026-10-03 晚：语音可靠性的根因与修法在 `docs/voice-reliability-2026-10-03.md`**
> ——「长语音读不出来」的根因是 `voice_mem.silk_for_duration` 的**帧数估算闸**
> （微信报的 `voicelength` 比真实音频长 20~40ms ⇒ `est` 比真实帧数大 1~2 帧 ⇒
> 内存里完整存在的候选被当场扔掉），修法是 clamp + 用消息自带的 `length` 做指纹。
> 改 `voice_mem` / `bot.read_voice_message` 前先读那一篇。

## 缺陷 ①：转写语言被写死成中文

`audio_read.py` 的 `_local()` 与 `transcribe_scored()` **两处** kwargs 都写着
`{"language": "zh", ...}`。

真机现象：用户对着麦克风说英文 **`superboynick`**，转出来是

```
你好,你好,我跟俗文貴你最近聊了什麼,今天聊了什麼,告訴我謝謝。
```

- `俗文貴` 就是 "superboynick" 被**中文词汇表强行音译**的产物；
- 剩下那句是模型为了自圆其说**编**的；
- 全句**通顺**，所以看起来像"识别不准"，实际是**完全捏造**；
- 最严重的一步：它被当作**用户的原话**送进 `run_agent`，模型据此去
  `find_contact("俗文貴")` —— **拿一段听错的话去执行**。

**为什么之前没被发现**：中文测试全部成功（写死 `zh` 正好对），英文必然失败，
而**所有自测都是绿的**（没有人断言 `language` 到底传了什么）。

**修法**：新增 `audio.language`，默认 **`auto`** = **不传该参数**，让 whisper 自己探测；
只说自己母语的人可设 `zh`。非法值**退回 auto 并告警**——whisper 遇到不认识的 language
会抛异常，那等于把「配置里一个手滑的拼写」变成「每次转写都失败」，属于静默放大故障。

**回归**：`selftest_audio.t9_language`（钉住 `auto` 不传、显式值照传、非法值退回），
`selftest_audio.t3` 把原来那条「传了 language='zh'」的断言改成了「没传 language」。

## 缺陷 ②：英文界面下**每条语音都被静默丢弃**

主循环 `bot.py` 里，`is_label_only(query)` 原本排在**语音处理之前**：

```
2818  if is_label_only(query):  → continue      ← [Audio] 8" 在这里被丢掉
2880  if local_type == 34: read_voice_message() ← 永远到不了
```

语音消息的渲染形态就是「**一个标签 + 一个时长**」，而**标签文字跟着微信界面语言走**：

| 微信界面 | `SessionTable.summary` | bot 渲染出来 |
|---|---|---|
| 中文 | `1"` | `[语音条（**读不到内容**…）] 1"` |
| **英文** | **`Audio`** | **`[Audio] 8"`** |

而 `_LABEL_ONLY_RE = ^\[[^\[\]]{1,10}\](\s*\d+"?)?$` —— 它只认「1~10 字的标签 + 可选时长」，
于是 **`[Audio] 8"` 命中**，被当成「只有类型标签的空消息」丢弃。

**后果特别恶劣，因为它静默**：用户发语音 → bot 一点反应都没有，
**连 `语音没读出来` 这类失败提示都不会打**（那段代码根本没被执行到）。
真机取证：bot 连续运行 46 分钟，用户发了 3 条语音，`bot.log` 里**一条记录都没有**。

**修法**：把「空标签检查」**移到语音处理之后**。语音先有机会转成文字；转出来 `query`
就是真文本，空标签检查自然放行；转不出来时语音那段已自己回话并 `continue`。
判据要用**结构性的 `local_type`**，不要只认标签文字（标签会随界面语言变）。

**回归**：`selftest_aixed` 里面两条——① 源码顺序断言（`read_voice_message` 必须出现在
`if is_label_only(query):` 之前，因为这个 bug 的本质是**顺序**，函数各自的行为用例全绿也抓不到）；
② `is_label_only('[Audio]  8"') is True`（钉住当初被丢的原因）。

## 还没做的一件事（已知缺口）

> ⚠️ **2026-10-03 晚纠正：下面这段的判据错了，别照它去改。**
> 正常轮询路径**本来就把 `packed_info_data` 传给了 `_render_nontext`**
> （`_v4_pickup_nontext` → `_v4_history_from_tables`，`live_history.py:790/864`），
> 而有转写时渲染出来的是我们自己的中文标签 `[语音 时长] 文本`，**与微信界面语言无关**，
> 所以 `_VOICE_TAG_RE` 认得出；`voice_already_transcribed` 的调用点外面还前置了
> `local_type == 34`，手打的 `[Audio] …` 走不到那里。
> 真正的残留缺口在**最后一道兜底** `_v4_new_messages_session`（它产出的消息没有
> `local_type`，英文界面的 summary 就是 `[Audio] 8"`）。详见
> `docs/voice-reliability-2026-10-03.md` 第七节。

英文界面**拿不到「微信自己转好的文字」那条零成本路**：`bot._VOICE_TAG_RE` 只认
`[语音]` / `[语音 时长]`（中文），不认 `[Audio]`，所以 `voice_already_transcribed()` 返回空，
只能走扫内存那条（更慢、且依赖「趁热」）。

**没顺手改它**，因为放宽这个正则会让用户**手打的** `[Audio] …` 之类文本被误当成已转写的语音。
要修的话正确做法是把 `local_type` 传进来按结构判断，而不是继续在标签文字上做加法。

## 语音语言限制（`audio.languages`，2026-10-03 第二次真机后的追加）

用户要求「限制中英文」。做法**不是**「优先中英文」而是**限制**：先 `detect_language`
探一次，探测出的语言**不在表内就如实拒绝、一个字都不给**——绝不拿表内的语言去"凑"，
因为凑出来的正是那种**通顺但捏造**的假话（实测那次被猜成法语，
转出 `Super poignée comme elle a l'air d'un chemin.`）。

- 配置：`audio.languages`，默认 `[zh, en]`；`[]` / `auto` = 关掉限制。
- 非法项逐个告警并丢掉；全非法退回默认（不静默接受看不懂的配置）。
- **显式 `audio.language: en` 优先**，此时不再探测（用户说死了就照办）。
- 老版本 `faster-whisper` 没有 `detect_language` → **照转**（可选功能不该变成硬故障）。
- 本机 `faster-whisper 1.2.1` 有该接口（实测确认后才动手）。

回归：`selftest_audio.t10_languages`（11 条断言，含「法语/日语/德语 → 拒绝且**不调用
transcribe**」）。

## 顺带修的：临时目录收成一个入口（`tempdir.py`）

本次发现 `audio_read` / `image_read` / `video_read` / `archive_read` / `mail_read`
**各自硬编码**了 `data/tmp_*`，而且**没有一处能改**。后果：受限环境
（只允许写工作区顶层的沙箱）里这些目录**建都建不了**，导致

* `selftest_audio.t7`（真跑 PyAV 切片）当场 `PermissionError`，**整份套件后面的用例
  一条都跑不到**（`t9`/`t10` 就是这样被挡住的）；
* `selftest_archive` / `selftest_video` / `selftest_mail_db` / `selftest_io_llm`
  的压缩包用例同样红。

当时的临时办法是**在测试里 monkeypatch 生产函数**（替换函数引用）——最脏的那种耦合。
现在收成 `tempdir.py`：

* `root()`：`PROJ_TMP` 有值就用它，否则仍是 `<项目>/data`（**生产默认一个字不变**）；
* `get(name)` / `sweep(name, max_age, label)`：各模块只报用途名，不再拼路径；
* `use_for_tests()`：给测试用的固定临时根（**幂等**，尊重已有的 `PROJ_TMP`）。

接线：5 个生产模块 + `file_read.export_dir()`（⚠️ **只在没配 `file.export_dir` 时**才走
临时根，用户显式配的路径一个字不动）+ `selftest_all.py`（子进程继承，设一次即全生效）
+ 5 份套件的入口。

**顺手抓到一个真 bug**：删掉 `archive_read._TMP_DIR` 后，`.7z` 那条真实解包路径
仍在用它（`NameError: name '_TMP_DIR' is not defined`）——以前被 `PermissionError`
挡在前面看不见。已改成 `_tmp_dir()`。

修完的结果：`selftest_audio` 88/88、`selftest_archive` / `selftest_video` /
`selftest_mail_db` / `selftest_io_llm` / `selftest_image_handoff` 全绿。

## 相关回归与验证

```bash
.venv/Scripts/python.exe selftest_aixed.py     # 语音接线 + 顺序断言
.venv/Scripts/python.exe selftest_audio.py     # audio.language / audio.languages
.venv/Scripts/python.exe selftest_bot_loop.py  # 主循环侧
.venv/Scripts/python.exe selftest_all.py       # 全部（临时目录自动改道到系统临时盘）
```

> 受限环境（沙箱只允许写工作区顶层）里跑自测，`selftest_all.py` 会打印一行
> `临时根目录（测试用）：…`；单个套件现在也会自己改道，不必再手工设 `PROJ_TMP`。
