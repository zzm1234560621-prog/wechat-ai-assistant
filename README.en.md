# WeChat AI Assistant

**Turn your personal WeChat into an AI assistant that can read, write, and actually operate your computer.**

It reads your local chat history in real time to answer questions, and it can also send messages to other people on your behalf or reply automatically;
the whole conversation happens in the native WeChat window — no second client to install.

[**English**](README.en.md) | [简体中文](README.md)

---

## ⚠️ Read these four first

1. **Ban risk**: this project injects a hook DLL into the WeChat process to send and receive messages, which **violates the WeChat Terms of Service**.
   Low-frequency personal use is normally fine, but a crackdown can get your account banned — test with a throwaway account, and **use at your own risk**.
2. **Version lock: it must be exactly WeChat PC 4.1.10.27.** The hook is compiled against the function
   offsets of that one version — any other version **fails silently**: the DLL loads normally and the
   installer still reports success, but nothing ever listens on 30001 and all you see is the assistant
   repeatedly saying it cannot reach 30001. Do not update WeChat either (the install script turns
   auto-update off for you).
3. **The assistant must run as administrator**: voice messages are read from the WeChat process memory,
   and Windows does not let a low-integrity process read a high-integrity one — if the assistant is not
   elevated, voice transcription will never work. You will get one UAC prompt at startup; click Yes.
4. **Legality**: only handle data from **your own account, data you legitimately own**. Scraping other people's chat history without authorization is illegal.

## What it can do

- **Reply for you**: pick a person or a group and the AI answers with context — inferring the right tone and how to address them; review mode is available, so drafts go to you for approval first
- **Send messages proactively**: broadcasts (by group/tag/group member), scheduled tasks and reminders, keyword watching — it can even send batch greetings for you
- **Read files and images**: Word / Excel / PPT / PDF, recursive archives, email, SQLite, images, voice-message transcription, video audio tracks
  (voice-to-text needs a one-time **optional component** install: step ③ of `一键部署.bat`, or double-click `可选组件.bat`)
- **Web search**: for anything beyond your local material, with sources in the answer (self-hosted SearXNG, free, no API key; the backend **ships with the package** —
  one optional-component install and it is on, off by default)
- **Touch files on your computer**: list / search / read / write / copy / move / delete to Recycle Bin (deletes require confirmation)
- **Unsend echo**: when the other side unsends a message, the assistant echoes the original text back to you
- **Extensible**: drop a `.py` into `plugins/` and you have one more feature (plugin contract: [docs/plugin-contract-spec.md](docs/plugin-contract-spec.md))

## Download and install

**Environment**: Windows 10/11 64-bit · WeChat PC **4.1.10.27** · **64-bit Python 3.11** (3.8–3.12 work)

### Option A · Download the prebuilt package (recommended, non-developers take this path)

1. Go to **[Releases](https://github.com/zzm1234560621-prog/wechat-ai-assistant/releases)** and download the latest package (about 240MB)
2. Unzip it anywhere (keep Chinese characters and spaces out of the path, to save yourself trouble)
``一键部署.bat` and just keep pressing Enter**, about 15 minutes
4. Day to day, use **`助手.bat`**: `[3]` start / `[4]` stop / `[5]` status / `[6]` logs

This package **ships with**: the official WeChat 4.1.10.27 installer, the compiled hook DLL, a hook source snapshot,
the bundled SearXNG (search backend), all the documentation and the self-test scripts.

### Option B · Run from source (developers)

```powershell
git clone https://github.com/zzm1234560621-prog/wechat-ai-assistant.git
cd wechat-ai-assistant
```

⚠️ **The repository does not contain the two WeChat installers** (`WeChatWin_4.1.10.27.exe` 239MB,
`WeChatSetup-3.9.12.51.exe` 285MB): they are too large and ship only with the Release package.
On the source route, get **WeChat 4.1.10.27** yourself — the version must match exactly (see "Read these four first" above) — or just use the zip from Option A.

Once you have the source, it works exactly like Option A: double-click **`一键部署.bat`**.

## How it works

The main line in one sentence: **use [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) to turn WeChat into a local HTTP service; everything else is an ordinary program.**

```
WeChat PC 4.1.10.27 ──[inject version.dll]──> local HTTP service 127.0.0.1:30001
                                                ▲ read:  POST /QueryDB/execute   (send SQL directly)
                                                │ write: POST /SendTextMsg, /SendImgMsg
                                                ▼
   bot.py polls the database for new messages every 5s ──> run it if it's a command, else call the LLM (with a tool loop) ──> reply
```

- **Receiving is polling**: this hook **has no push interface**, it can only be queried, so the bot looks for new messages once per `poll_interval` (default **5 seconds**)
  — second-level, not millisecond-level.
- **Sending is just an HTTP POST**: `/SendTextMsg` sends text, `/SendImgMsg` sends images
  (**ordinary files go through it too**; the "Img" in the name is upstream history).
- **All queries go through `live_history.py`**: it adapts to both the WeChat 3.9.x and 4.1.x database layouts; don't call the hook raw anywhere else.
- **The hook does not support concurrency**: every query and send is serialized (queries also have a budget gate). That is why it is "slower but stable",
  and it is the precondition for not crashing WeChat.
- **Model channels** support both the official Anthropic and OpenAI-compatible protocols; `/provider` switches provider in one step
  (DeepSeek, Claude, Tongyi, Kimi, Zhipu, OpenAI, local Ollama).

> This hook exposes exactly 8 endpoints: `/SendTextMsg`, `/SendImgMsg`, `/ForwardXMLMsg`, `/Decode_Pic`,
> `/GetSelfProfile`, `/QueryDB/execute`, `/QueryDB/GetAllDBName`, `/QueryDB/status` —
> there is no "receive message" endpoint, which is exactly why polling is mandatory.

## Project structure

```
bot.py                 main loop: poll → command / LLM → reply
live_history.py        the only entry point for querying the WeChat DB (adapts to both the 3.9.x / 4.1.x schemas)
agent_tools.py         the tool layer for the LLM + pending-confirmation mechanism + query budget
plugins.py / plugins/  plugin contract (single source of truth for tools and events) + user plugin directory
llm.py / providers.py  the two model protocols (Anthropic / OpenAI-compatible) + provider presets
file_read.py etc.      read files / images / voice / video / archives / email / databases
files.py               operate local files (list/search/read/write/copy/move/delete to Recycle Bin)
scheduler.py           scheduled tasks (send a message on time / remind me / ask a question)
health.py etc.         log rotation, logout alerts, usage stats, read-only status page
console.py / *.bat     local console (助手.bat menu, 一键部署.bat, 可选组件.bat)
tools/                 packaging, diagnostics, OCR/Office/resize and other scripts
docs/                  design specs and real-machine test records
searxng/               the bundled search backend (used by web search)
installers/            hook DLL and install scripts (+ the WeChat installer in the Release package)
```

## More documentation

- [CLAUDE.md](CLAUDE.md) — the authoritative notes on architecture, the hook's iron rules and the pitfalls (read it before changing code)
- [docs/](docs/) — design specs and measured records (hook, files, voice, search, plugin contract, and more)
- [docs/README-full.md](docs/README-full.md) — the old detailed README: every command, config option and troubleshooting entry

## License and disclaimer

This project is open source under the **[MIT License](LICENSE)**, provided **"as is", without warranty of any kind**.

**Disclaimer** (please read it in full):

- This project extends WeChat's functionality by injecting a hook DLL, which **violates the WeChat Terms of Service**. Using it may get your **account banned**,
  lose messages, or corrupt your account data. **You bear all consequences yourself**; the author accepts no responsibility whatsoever.
- Please use this project **only** with **your own account** and **data you legitimately own**. Scraping or analyzing other people's chat history
  is **illegal** in most jurisdictions.
- Users must comply with the laws of their own jurisdiction and with Tencent's terms of service. **Do not use it for commercial purposes, large-scale mass messaging,
  harassment, or any illegal activity.**
- This project has **no affiliation whatsoever** with Tencent or the official WeChat team, and is neither authorized nor endorsed by them.

## References

- [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) — the hook this project's **main line** uses (WeChat 4.x);
  injecting `version.dll` provides the local HTTP interface; the compiled DLL and a source snapshot are both in `installers/`
- [WeChatFerry](https://github.com/lich0821/WeChatFerry) — the **other backend** we kept (WeChat 3.9.x only)
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump) — a 3.9.x-era history export tool (not adopted for 4.x)
- [SearXNG](https://github.com/searxng/searxng) — the backend for web search (bundled with the package)