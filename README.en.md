# WeChat AI Assistant

**Turn your personal WeChat into an AI assistant that can read, write, and actually operate your computer.**

It reads your local chat history in real time to answer questions, and it can also send messages to other people on your behalf or reply automatically;
the whole conversation happens in the native WeChat window — no second client to install.

[**English**](README.en.md) | [简体中文](README.md)

---

## ⚠️ Read these three first

1. **Ban risk**: this project injects a hook DLL into the WeChat process to send and receive messages, which **violates the WeChat Terms of Service**.
   Low-frequency personal use is normally fine, but a crackdown can get your account banned — test with a throwaway account, and **use at your own risk**.
2. **Version lock**: the hook is compiled against **one specific WeChat version**, and any WeChat update breaks it. Once it is installed, make sure WeChat auto-update stays off
   (the install script turns it off for you).
3. **Legality**: only handle data from **your own account, data you legitimately own**. Scraping other people's chat history without authorization is illegal.

> This is a **personal project for the author's own use**: the features grew out of the author's actual needs, not out of a general-purpose product plan.
> It can be installed on other computers (there is a one-click deploy), but if you hit trouble, read the troubleshooting section and `docs/` first — they contain records of what actually happened on real machines.

## What it can do

- **Ask your history**: "what did I talk about with Zhang San", "what have we talked about recently" — it queries the local database live, no export needed first
- **Reply for you**: pick a person or a group and the AI answers with context; review mode is available, so drafts go to you for approval first
- **Send messages proactively**: broadcasts (by group/tag/group member), scheduled tasks and reminders, keyword watching
- **Read files and images**: Word / Excel / PPT / PDF, recursive archives, email, SQLite, images, voice-message transcription, video audio tracks
  (voice-to-text needs a one-time **optional component** install, see "Optional components" below)
- **Web search**: for anything beyond your local material, with sources in the answer (self-hosted SearXNG, free, no API key; the backend **ships with the package** —
  one optional-component install and it is on, off by default)
- **Touch files on your computer**: list / search / read / write / copy / move / delete to Recycle Bin (deletes require confirmation)
- **Unsend echo**: when the other side unsends a message, the assistant echoes the original text back to you
- **Runtime care**: log rotation, logout alerts, token usage stats, a read-only status page
- **Extensible**: drop a `.py` into `plugins/` and you have one more feature (plugin contract, see below)

## Download and install

**Environment**: Windows 10/11 64-bit · WeChat PC **4.1.10.27** · **64-bit Python 3.11** (3.8–3.12 work)

### Option A · Download the prebuilt package (recommended, non-developers take this path)

1. Go to **[Releases](https://github.com/zzm1234560621-prog/wechat-ai-assistant/releases)** and download
   `wechat-ai-assistant-<date>.zip` (about 240MB)
2. Unzip it anywhere (keep Chinese characters and spaces out of the path, to save yourself trouble)
3. **Double-click `一键部署.bat` and just keep pressing Enter**, about 15 minutes
4. Day to day, use **`助手.bat`**: `[3]` start / `[4]` stop / `[5]` status / `[6]` logs

This package **ships with**: the official WeChat 4.1.10.27 installer, the compiled hook DLL, a hook source snapshot,
the bundled SearXNG (search backend), all the documentation and the self-test scripts.

The package does **not** contain (deliberately): your chat history, local configuration, real API keys, the Python virtual environment, or voice models
(`.venv` and the models are created and downloaded on your own machine by step ③ — copying them across machines always breaks).
The `config.yaml` / `settings.json` in the package are **examples**, with empty keys.

### Option B · Run from source (developers)

```powershell
git clone https://github.com/zzm1234560621-prog/wechat-ai-assistant.git
cd wechat-ai-assistant
```

⚠️ **The repository does not contain the two WeChat installers** (`WeChatWin_4.1.10.27.exe` 239MB,
`WeChatSetup-3.9.12.51.exe` 285MB): they are too large and ship only with the Release package.
On the source route, get **WeChat 4.1.10.27** yourself — the version must match exactly (the reason is in
step `⓪` under "Deployment notes" below) — or just use the zip from Option A.

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

## Deployment notes

> ⚠️ **The assistant must run as administrator** (a hard constraint since 2026-10-06, the same on this machine and on every machine it is deployed to).
> The reason is not "we felt like elevating" but **voice messages**: voice requires reading the WeChat process memory, and Windows does not let
> a lower-privilege process read a higher-privilege process's memory — if you run WeChat elevated, an assistant at normal privilege can never read
> a single voice message.
> So `助手.bat` / `启动助手.bat` / the "start the assistant" step of the one-click deploy **all raise one UAC prompt**;
> just click "Yes". That is the only elevation in the whole flow (details and the pitfalls we hit are in
> [docs/admin-elevation-notes.md](docs/admin-elevation-notes.md)).

### One-click deploy: double-click `一键部署.bat`, press Enter all the way

**That is the single action** (it is equivalent to `助手.bat` → `[9]`, minus having to hit the menu key). It does six things for you in the real order:

```
double-click 一键部署.bat  →  press Enter all the way
                   │
                   ├─ ⓪ check the WeChat version (if it's missing / not 4.1.10.27, install the bundled copy)
                   ├─ ① install the hook into WeChat (raises UAC, click "Yes")
                   ├─ ② install Python dependencies (creates the virtualenv automatically, needs network, a few minutes the first time)
                   ├─ ③ optional components (voice-to-text / web search / file format pack / semantic search; **it will ask you**, skippable)
                   ├─ ④ start the assistant (it needs admin rights, **UAC pops up here**, click "Yes")
                   └─ ⑤ configure the model in place (pick a provider + paste the API key, no typing inside WeChat)
```

> **What is ③ "optional components"?** Those dependencies are **not installed along with the main program** (voice downloads a few hundred MB of local models,
> search needs a dedicated virtual environment of its own, semantic search drags in hundreds of MB of torch), so we ask you once, separately.
> **Skipping them does not affect** chatting, sending messages, reading files or scheduling at all; to install or turn them off later, double-click **`可选组件.bat`**.
> See the "Optional components" section below.

After that, day to day you just double-click **`助手.bat`** (a menu): `[3]` start / `[4]` stop / `[5]` status / `[6]` logs.

- **The only prerequisite**: this computer needs **64-bit Python** (if `一键部署.bat` can't find it, it tells you exactly which command to run:
  `winget install -e --id Python.Python.3.11`, then double-click it once more; if you installed the 32-bit one, it stops you as well).
- **The WeChat version must be 4.1.10.27, do not skip this step**: the hook is compiled against the function offsets of **this one version**;
  on any other version it will not attach — and it **does not report an error**: WeChat loads the DLL normally, the install script even prints "已放置，成功" ("placed, success"),
  but nothing ever listens on 30001, and all you see is the bot endlessly saying "连不上 30001" ("can't connect to 30001").
  Step `⓪` checks it for you: if it is wrong, it installs the bundled `installers\wechat-4.1.10.27\WeChatWin_4.1.10.27.exe`
  (silent install, no clicking needed; but it kills the WeChat process, so **you will have to scan the QR code and log in again**).
  Installing it by hand works too: double-click that exe, and when it asks "你已安装新版本的微信，安装更早的版本？" ("you already have a newer WeChat installed — install an earlier version?"), click **「继续安装」** ("continue installation").
- After installing, **restart WeChat** and open `http://127.0.0.1:30001/QueryDB/status` in a browser; if it returns JSON, the hook works
  (`IsLogin: 1` = logged in). Then send a message in WeChat's "文件传输助手" (File Transfer) and you're ready to go.

### The assistant keeps printing "hook loaded, but the database won't open" (「hook 已加载，但数据库打不开」) — what now

First, remember one thing: **the `version.dll` in the package and the one currently in use in the WeChat directory are two different files.**
Unzipping a new package, installing dependencies and configuring the model **will not** replace the copy in the WeChat directory — only the "install hook" step does.
So this shape has happened: the package is the newest, the logs are clean, yet the functionality is still old (because an old DLL is sitting inside WeChat).

Three short commands pinpoint it (no administrator needed, and they run fine while the assistant is up):

```powershell
(Get-Item "C:\Program Files\Tencent\Weixin\version.dll").Length
Invoke-RestMethod http://127.0.0.1:30001/QueryDB/status | ConvertTo-Json -Depth 5
Get-Content <package dir>\installers\wechat-4.1.10.27\hook-install-log.txt
```

| What you see | Conclusion | What to do |
|---|---|---|
| The first one is **519168** (the new build is **527360**) | What's inside WeChat is **still the old hook** | `助手.bat` → `[8]` → `[7]` → **`[4]` replace version.dll only**, then **restart WeChat** |
| The third one reports "文件不存在" ("file does not exist") | **The install-hook step never ran** (that log is the first thing it writes) | Same as above; if the log doesn't exist, don't suspect anything else |
| `status` has **no** `LoginGateInfo` field | The file was replaced, but **WeChat was not restarted** (the DLL only loads at process start) | Quit WeChat completely (tray icon too) → reopen → scan the QR code to log in |
| `LoginGateInfo` is there but `IsLogin: 0` | The new hook is running, it just **hasn't seen the core database get written yet** | Wait a minute and run the second command again; if it never moves, WeChat is not really logged in |
| `LoginGateInfo` is there and `IsLogin: 1` | ✅ It works | Start the assistant (`助手.bat` → `[3]`, **click "Yes" on the UAC prompt**) |

Two more places where you can burn effort for nothing:

- **One-click configuration stops at "第 0 步：微信版本" ("step 0: WeChat version")**. It says outright there that "后面的步骤一步都没执行"
  ("none of the later steps ran at all") — seeing that line means **the hook install still hasn't happened**, not that configuration is done. Switch WeChat to 4.1.10.27 and press `[9]` again.
- **The elevation window flashes and closes**: the scripts that install the hook / replace the DLL run in a new administrator window, and the result is written at the same time to
  `installers\wechat-4.1.10.27\hook-install-log.txt` (or `hook-fix-log.txt`), so you can `Get-Content` it any time.

For a more thorough one-stop diagnosis: `助手.bat` → `[8]` → run `hook_doctor.py` (or the file of the same name at the package root).

### Optional components (voice-to-text / web search / file format pack / local semantic search)

Their **code is in the package**, but their dependencies and models **are not** (voice downloads a few hundred MB of local models; search needs a dedicated
virtual environment of its own; semantic search drags in torch) — so they need an explicit one-time install. Skipping them does not affect chatting, sending messages, reading files or scheduling at all.

| Component | What gets installed | Download size | How to turn it on afterwards |
|---|---|---|---|
| Voice-to-text | `faster-whisper` + `pilk` | local model (size follows `audio.model`, default `small`, about **464MB**, via the hf-mirror mirror) | usable right after install; tune it in the `audio` section of `config.yaml`; with `backend: local` **not one byte of audio leaves your machine** |
| Web search | the package **ships SearXNG source**, and builds a dedicated venv inside its directory | a dozen-odd MB of dependencies | **turns itself on after install** (writes `search.enabled` into `settings.json`) and starts the service; `search.autostart` is on by default (the assistant brings it up at startup too) |
| File format enhancement pack | `av` (video), `extract-msg` (.msg email), `py7zr`/`rarfile` (archives), `xlrd`/`olefile` (old Office), `Pillow` (images embedded in PDFs) | tens of MB, usable **immediately** after install | no switch to flip — it can read a few more formats, and it says so when one is missing |
| Local semantic search | `sentence-transformers` (drags in **torch**, the heaviest item in this table) | hundreds of MB of dependencies + local models | turns `semantic.enabled` on after install; **building the index first asks you "stop the assistant → build the index → start it back up"** (if you decline, only the command is left) |

Entry points: step `③` of `一键部署.bat`, or double-click **`可选组件.bat`** (which can also show status and toggle
"install automatically from now on"). **All four are installed by default**, so **pressing Enter all the way gets you the whole set**;
only two places in the entire flow stop to ask: **building the semantic index needs the assistant stopped**, and pressing `n` when you want to skip one item.

Three things worth knowing:

- **Models and venvs are never copied across machines**: models are hundreds of MB and torch is bigger; and a venv records absolute paths, so copying it always breaks
  (the same rule as the assistant's own `.venv`). That is why the package carries only source, and everything is created and downloaded on your own machine.
- **Installing `rarfile` alone is not enough for `.rar`**: it is only a shell; actual extraction needs an external program (unrar / 7-Zip / bsdtar).
  The status screen reports these two things **separately** and will not report "rarfile installed" as "`.rar` can be read".
- "Install automatically from now on" is recorded in **`optional` in `settings.json`**. Turning it off only means **no more automatic installs**;
  whatever is already installed stays. It is written to `settings.json` rather than `config.yaml` because the program never writes back the commented `config.yaml`.

### Doing it step by step (if the one-click fails, or you want to control each step yourself)

<details>
<summary>Click to expand: install the hook by hand / install dependencies / start the bot / configure the model</summary>

**① Install the hook (this step needs administrator)** — open PowerShell as administrator, then run: (the one-click path doesn't need you to elevate yourself; it raises UAC for you)

```powershell
Set-Location installers\wechat-4.1.10.27

# drop version.dll + use ACLs to block WeChat auto-update (log: hook-install-log.txt)
powershell -NoProfile -ExecutionPolicy Bypass -File .\do_hook_install.ps1

# only needed when WeChat isn't 4.1.10.27: silently install the copy bundled with the repo (log: install-log.txt)
powershell -NoProfile -ExecutionPolicy Bypass -File .\do_install.ps1
```

The scripts detect the WeChat install directory and the logged-in user automatically, so nothing has to be edited when you change computers or drives. After installing, **restart WeChat**,
and `http://127.0.0.1:30001/QueryDB/status` returning JSON means you're done (if it won't connect: the DLL is in the wrong place / the WeChat version is wrong /
security software blocked it). To remove the hook: `do_remove_hook.ps1`, or rename `version.dll` in the WeChat directory and restart WeChat.

**② Install the Python dependencies**

```powershell
# double-click install.bat: create the virtualenv → install from requirements.txt → generate 启动助手.bat
# or manually:
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**③ Start it**: double-click `启动助手.bat` (it stops when the window closes); to keep it resident in the background use `助手.bat` → `[3]`,
and to start it at boot use `助手.bat` → `[8]` → `[6]`.

**④ Configure the model**: `助手.bat` → `[8]` → `[1]`, or double-click `配置模型.bat`; you can also send these in WeChat's "文件传输助手" (File Transfer):

```
/provider          list the available providers
/provider 1        pick the 1st one (DeepSeek); sets protocol + endpoint + model for you
/api sk-your-key   set the key, and test connectivity on the spot
```

Once it is configured, just send a message in WeChat to ask a question. Send `/help` to see every command.

</details>

### Repackaging it yourself

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_package.ps1
```

The output is `dist\wechat-ai-assistant-<date>.zip` (about 240MB). Private data, real API keys, `.venv`
and install logs never make it into the package (the script runs its own check and does `exit 1` if it finds any).
The other person unzips it → **double-clicks `一键部署.bat`** (one file carries the whole flow) → or follows `从这里开始.txt` in the package.

## Known limitations and "things this project deliberately does not do"

Honesty beats looking good; these are **deliberate**:

- **Windows only**, and only the one version **WeChat PC 4.1.10.27** (the hook is compiled against function offsets).
- **Receiving is polling** (5 seconds by default), so it is not "millisecond instant replies"; and the hook **does not support concurrency**, so
  all queries/sends are serialized — that is the precondition for stability, not a performance problem.
- **Sending voice messages / making voice calls is impossible** (the hook has no such endpoints). The `call` capability in scheduled tasks has been **retired**;
  when its time comes it **reports the error honestly** and will not quietly turn into a text message.
- **Multiple accounts: cancelled**, not supported.
- **Network connectors such as an MCP server / IDE bridge**: the plugin contract **pins the contract only, with no implementation** —
  declaring one fails at load time; we would rather it not start than let it sit there jamming WeChat while claiming it isn't.
- **`read_image` can only read thumbnail caches WeChat has written**; images sent as **files** are plaintext originals and much clearer.
  When it can't read something it says "看不了" ("can't view it") and **will not make content up**.
- Any **message send, file delete or command run** is an irreversible action, and all of them go through the "pending confirmation" gate.
  The assistant's role is "do work for you", so **by default it only trusts messages you sent yourself**.

## Plugging other software into WeChat (e.g. vibe coding tools)

**This section is a placeholder: the next version will plug vibe coding tools in** — when a coding
tool finishes a task, needs your confirmation, or hits an error, it tells you right in WeChat; you
can also tell it from WeChat to keep going.

This version does not spell out how to wire it up yet. The two underlying interfaces that already
exist (this assistant's plugin contract, the hook's local HTTP) are documented under `docs/`:
[docs/plugin-contract-spec.md](docs/plugin-contract-spec.md); the hook's eight endpoints are listed
under "How it works" above.

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

**Third-party components**: the hook comes from [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook);
the bundled search backend [SearXNG](https://github.com/searxng/searxng) is licensed under **AGPL-3.0**
(source provided with the package, see `searxng/`); we also referenced [WeChatFerry](https://github.com/lich0821/WeChatFerry) and
[PyWxDump](https://github.com/xaoyaoo/PyWxDump). Copyright of each component belongs to its respective authors.

## References

- [aixed/WeChat-Hook](https://github.com/aixed/WeChat-Hook) — the hook this project's **main line** uses (WeChat 4.x);
  injecting `version.dll` provides the local HTTP interface; the compiled DLL and a source snapshot are both in `installers/`
- [WeChatFerry](https://github.com/lich0821/WeChatFerry) — the **other backend** we kept (WeChat 3.9.x only)
- [PyWxDump](https://github.com/xaoyaoo/PyWxDump) — a 3.9.x-era history export tool (not adopted for 4.x)
- [SearXNG](https://github.com/searxng/searxng) — the backend for web search (bundled with the package)
