# 语音输入（音频 → 文字）规格

> 状态：设计已与用户确认（2026-10-01）。**A 先落地，(a) 同步+上限；长录音如果 (a) 不够用再单独评审 (b) 后台线程。**
> B（语音条可行性）是**只读报告**，见 `docs/voice-msg-feasibility.md`，不在此规格的实现范围内。

## 一、目标与非目标

**目标**：让助手能"听懂"**以文件形式收到/发出的音频**（`.m4a` / `.mp3` / `.wav` / `.amr` / `.ogg` / `.aac` / `.flac`），
转成文字后当普通内容喂给模型 —— 用户可以说「把刚才那个录音转成文字」「那段录音里说了什么」。

**非目标（明确不做，别顺手扩）**：
- ❌ **微信语音条**（`local_type=34`）：拿不到音频字节，先在 `docs/voice-msg-feasibility.md` 里做可行性，**不在本规格里实现**。
- ❌ **发语音 / TTS**：hook 只能发文本/图片/转发，做不到（要重编译 hook）。
- ❌ **语音通话**：早已确认做不到。
- ❌ 实时/流式转写、说话人分离、翻译。

## 二、设计

**入口：复用 `read_file`，不加新工具。**
`agent_tools.t_read_file` → `file_read.locate(name)` → `file_read.extract(path, cfg)`
→（新增音频分支）`audio_read.transcribe(path, cfg)`。

理由：工具清单不动、system_prompt 不动、用户话术自然（「读文件」本来就是这个工具）；
与 CLAUDE.md 里「`image_read.py` / `file_read.py` 都是把本地文件变成文字」的家族一致。

**新模块 `audio_read.py`**（与 `image_read.py` 对称）：

| 函数 | 职责 |
|---|---|
| `backend(cfg)` | `local`（默认）/ `cloud`，非法值当 local 并告警 |
| `available(cfg)` | 返回 `(能否用, 说明)`；说明里必须写清**缺什么、怎么补** |
| `transcribe(path, cfg)` | 返回 `(text, err)`；`err` 非空时**绝不返回半截文本当成功** |
| `_duration(path)` | 用 PyAV 读时长（拿不到就返回 None，只按体积卡） |

- **本地**：`faster-whisper`，`device="cpu"`、`compute_type="int8"`；模型档位 `audio.model`（默认 `small`）。
- **云端**：OpenAI 兼容 `POST {base_url}/audio/transcriptions`，`urllib` + 手工 multipart（**不新增硬依赖**）。

## 三、硬约束（都是不可谈的）

1. **跑在收消息那条线程上 → 必须有硬上限，超限如实拒绝。**
   `audio.max_seconds`（默认 **120**）+ 复用 `file.max_bytes`（默认 30MB，`file_read` 已有）。
   拒绝文案要给出「截短，或调 `audio.max_seconds`」，**不静默截断音频**。
2. **绝不在聊天里静默下模型。** 模型下载只在用户显式执行
   `python audio_read.py --setup`（走 `HF_ENDPOINT=https://hf-mirror.com`，因为 `huggingface.co` 在本机不通）。
   缺模型时的返回必须是「还没下模型，执行这条命令」。
3. **隐私：默认 `local` → 音频一个字节都不出本机。** 只有显式配成 `cloud` 才上传；
   **上传必须打日志**（学 `redact` 的「命中数要打日志」），绝不悄悄上传。
4. **失败如实、且可照做**：没装依赖 / 没下模型 / 没配 key / 解码失败 / 转出空文本 ——
   每种都要一句能直接照做的话，**不许返回空字符串假装成功**，不许降级到别的后端。
5. **`faster-whisper` 不随主程序安装**（`requirements.txt` 可选段）：
   不装也能起 bot，用到时才提示怎么装。
6. **`av<19` 是硬约束**（2026-10-06 另一台电脑真机换来的，**别去掉**）：
   `faster-whisper 1.2.1` 内部是 `av.open(input_file, mode="r", metadata_errors="ignore")`，
   而 **PyAV 19 把这个参数删了**（18.1.0 还接受）⇒ 装了「最新 av」的机器上
   **每一条转写都抛 `TypeError: open() got an unexpected keyword argument 'metadata_errors'`**，
   **语音条和音频文件一起读不出来**，用户只看到「解析失败 / 没读出来」。
   真机对照：那台 `av 19.0.1` → 全废；本机 `av 18.1.0` → 同一条语音转出「你好 你好」。
   - 安装线：`envsetup.OPTIONAL_PIP` 的 voice 与 formats 两项都写 `av<19`
     （formats 也钉，否则「先装语音、后装格式包」会把 av 升到 19，**悄悄**再弄坏一次）；
     `requirements.txt` 的注释段同样写明这条命令。
   - 判据（**不许猜版本号**）：`audio_read.av_conflict()` 拿一个空流去调
     `av.open(..., metadata_errors="ignore")`——抛 `TypeError` 且提到这个参数名 = 不支持；
     抛别的异常（空数据不是合法容器）= 参数被接受了。它接在 `available()` 里，
     所以 `audio_read.py --status` 会**直接把该跑的命令打出来**。
   - 回归：`selftest_audio`（把 `av.open` 换成"像 PyAV 19 那样拒收"的桩）、
     `selftest_install`（两项 specs 都钉 `av<19`）。

## 四、配置

```yaml
audio:
  backend: local          # local（默认，音频不出本机）| cloud
  model: small            # tiny / base / small / medium
  max_seconds: 120        # 超了如实拒绝（跑在轮询线程上，不许长时间占着）
  cloud:
    base_url: https://api.siliconflow.cn/v1
    api_key: ""
    model: FunAudioLLM/SenseVoiceSmall
```

## 五、验收（可观测）

| # | 验收条件 |
|---|---|
| 1 | 说「把 <音频文件名> 转成文字」→ 模型调 `read_file` → 返回转写文本 |
| 2 | `pdf/docx/xlsx/pptx/文本` 的老行为**一个字不变**（回归用例钉住） |
| 3 | 超 `audio.max_seconds` 或超 `file.max_bytes` → 明确拒绝 + 给出怎么改 |
| 4 | 缺 `faster_whisper` / 缺模型 / 缺 key → 三种各自的照做指引（**不**返回空文本） |
| 5 | `backend: local` 时**零网络调用**（用例断言） |
| 6 | `backend: cloud` 时上传打日志 |
| 7 | 崩溃/异常不传染：转写失败只影响这一条文件读取，bot 继续轮询 |

## 六、测试

新增第 12 份 `selftest_audio.py`（**不联网、不下模型、不碰 hook**，用假 backend + 临时文件）：
扩展名分派、上限拒绝、三种"缺东西"的文案、local 零网络、cloud multipart 编码（对假 HTTP 服务）、
空结果不当成功。`selftest_all.py` 登记第 12 份。

## 七、B 的边界（写下来防止混进来）

语音条的任何实现（AES、hook 重编译、读微信自带转文字）都属于 B，
**必须先有 `docs/voice-msg-feasibility.md` 的结论和用户批准**，不许顺手塞进 A。
