# self_wxid 的来源与核实（2026-10-05 三级 → 2026-10-06 四级 + 核实）

> 文件名还是旧的「three-sources」：链接别改。**现在的实现是四级 + 核实**，
> 唯一所有者是 `aixed_api.resolve_self_wxid()`（`bot.py` 启动与 `verify_real.py` 都走它）。

## 事故二（2026-10-06 第二台部署机）：**认错了人**，比认不出更糟

第一版三级来源补上了「从 contact 表认」之后，第二台电脑上又出现了「重复回复」。
这次不是认不出，而是**认错**：

```
[bot] 自己的 wxid = wxid_q73……                 ← 自动认出来的（contact 表那条判据；
                                                值这里脱敏了：它可能是**别人**的 wxid）
[bot] 命令回复: 选一个服务商，然后发 /provider <编号>：
[bot] 收到 filehelper: 选一个服务商，然后发 /provider <编号>：   ← 收到的是**它自己刚发的**
```

对照开发机（`self_wxid` 填在 config 里）同一场景应该是 `[bot] 跳过（这是自己刚发出的回复）`。
差别就在「我是谁」：那台机器上 `sender_id` 根本不是 `wxid_q73…` ⇒ `is_self` **恒为 0**
⇒ 「我刚发出去的回复」回显回来时跟对方的新消息长得一样，于是它一遍遍自己答自己。
**全程不报错**，用户只看到「重复回复」。

> 「重复回复」还有**另一半**成因（同一天抓到）：不是认错人，而是「自己刚发过的话」只活在内存里 +
> 「启动前 2 分钟内算新消息」。换台电脑一登录、历史被同步进本机库时一样会自己答自己 ——
> 见 **`docs/restart-catchup-notes.md`**。

根因是 ③ 那条判据本身：`SELECT username FROM contact WHERE username LIKE 'wxid\_%' …
LIMIT 1` —— 「自己是 contact 表里第一个 `wxid_` 开头的行」是**从一台机器上观察到的行序**，
不是证明。换台机器就可能落到别人身上。

## 修法：第四级 + 对所有「经验值」做核实

| # | 来源 | 性质 | 谁核实 |
|---|---|---|---|
| ① | `config.yaml` 的 `self_wxid` | 用户手填，最高优先，不查任何库 | 不核实（用户明确说的） |
| ② | hook `/GetSelfProfile` | 有的构建给、有的不给（本构建 `SelfInfo` 从不被写入） | —— |
| ③ | 本机微信**账号目录名**（`account_dir_wxids()`，走 `find_self_wxid`） | **离线、确定**：目录名是微信自己写的（`<wxid>_<数字>`） | 只有一个账号时才直接用；多个**绝不替用户挑** |
| ④ | 从 `contact` 表认（`detect_self_wxid()`） | **行序经验**，可能是别人 | **必须落在 ③ 的账号目录里**，否则否掉 |

判定表（`aixed_api.resolve_self_wxid`）：

* ③ 只有一个账号 → 直接用它（连 `contact` 表都不查）；
* ③ 有多个、④ 的猜测在 ③ 里 → 采用，日志标明「已核实」；
* ③ 有多个、④ 的猜测**不在** ③ 里 → **否掉**，如实说「认不出」（并列出本机账号让用户填）；
* ③ 一个都找不到（微信数据搬过盘/非默认位置）→ 照旧兜底用 ④，但**如实标注「未核实」**。

## 认不出自己时的兜底闸门（与身份无关）

「哪条消息是我发的」要靠 `self_wxid`；而「**这句话是我刚发出去的**」不需要 ——
它是文本一字不差 + 就在刚才（`bot._SENT_RECENT` / `is_own_reply`）。

以前这个闸门**嵌在 `if msg.from_self():` 分支里面**（`bot.py` 主循环），于是身份一坏，
它压根不会被问到 —— 这正是事故二的直接机制。现在多了一条：

* `live_history.self_identity_ok()`：只读上次观察（`_v4_new_messages` 每轮本来就要查一次
  自己的 fts rowid，顺手记下来，**零额外请求**）；
* `bot.looks_like_own_echo_without_identity(from_self, identity_ok, text)`：
  **只在确认认不出自己时**，把「我刚发过的原文」当自己的回显跳过。

正常机器（`identity_ok=True`）一个字都不变——不会因为「对方恰好说了和我们上一条回复
一样的话」就静默不回。图片/文件的同类闸门（`is_own_image` / `is_own_file`，会话+时间窗）
本来就在 from_self 分支**之前**，这一条是把文本补齐。

## 症状（换台电脑才出现）

「另一台电脑装完包，bot 不回复 / 回复得很怪」。现场日志：

```
[live] ⚠️ 慢查询 1.15s  db=message_fts.db
[live] ⚠️ message_0.db 连续失败 3 次：熔断 30 秒
[bot] 收到 filehelper: 你好                      ← 通了
[bot] 已回复: 你好 👋 …                          ← 也回了
[bot] 收到 filehelper: 收到，这条是系统的自检通知…   ← 但这是 bot 自己上一条原话
```

`verify_real.py` 同时给出唯一那个 ❌：

```
❌ 拿不到自己的 wxid —— 历史里将分不清「我」和「对方」；请在 config.yaml 填 self_wxid
```

**不是编的，是它把自己以前说过的话当成用户的新提问再答了一遍。**

## 根因（三级来源全空）

| 来源 | 打开包（部署机） | 开发机 |
|---|---|---|
| `config.yaml` 的 `self_wxid` | **空**（`build_package.ps1` 第 48 行把 `config.example.yaml` 拷成 `config.yaml`，示例里是 `""`） | 填了本机那个 wxid（`wxid_xxx…`） |
| hook `/GetSelfProfile` | **不给 wxid**（本构建返回里没有 `wxid`/`userName`，日志写「已登录，但取不到 wxid」） | 同样给不出 |
| 代码兜底 | **没有** | 用不到 |

所以**纯换台机器就命中，不报错、只是功能歪** —— 项目里最忌讳的那种静默失效。

## 修法二：唯一所有者 + 接线

1. **`aixed_api.resolve_self_wxid(cfg, client, backend)`** —— 现在只有这一份实现，
   返回 `(wxid, 来源, 查了什么, 说明/告警)`。`bot.py` 启动和 `verify_real.py` 都调它
   （`verify_real.resolve_self_wxid` 只是个转发包装）。
2. 四级来源见上表；`aixed_api.account_dir_wxids()` 是 ③ 的入口，`detect_self_wxid()` 是 ④ 的。
3. 认出来的来源与核实结果，bot 启动时与 `verify_real.py` 都会**打出来**（不许静默）；
   认不出时那几句会写明后果与修法（跑 `find_self_wxid.py --apply`）。
4. `find_self_wxid.py`：**离线**小工具（不碰 hook、不跑 bot、不解密库）——
   微信 4.x 的账号目录名就是 `<wxid>_<数字>`（`<保存位置>\xwechat_files\wxid_xxx_2895`），
   所以装完就能把值填好，不用等 bot 跑起来。

## 换台电脑怎么办（两条路，先做第一条）

### 路线 A：只改配置（一分钟，不搬代码）

1. 在**那台电脑**的项目目录跑：
   ```powershell
   .\.venv\Scripts\python.exe find_self_wxid.py
   ```
   它列出本机所有账号（`wxid_xxxx`）。**多个账号时不替你挑**——填错就是拿别人的号发消息。
2. 把要用的那个填进 `config.yaml`：
   ```yaml
   self_wxid: "wxid_你的账号"
   ```
   或者直接 `--apply`（只改这**一行**，先备份成 `config.yaml.bak-selfwxid-<时间戳>`；
   找不到这一行就不改，只告诉你怎么加）。
3. 重启助手，日志里应看到：
   ```
   [bot] 自己的 wxid = wxid_xxxx（来源：config.yaml）
   ```
   **看到「（来源：config.yaml）」就是对的**：手填的值不经过核实，也最稳。

### 路线 B：把这次修复也搬过去（要自动认的时候）

在**开发机**上跑（默认写到**这台机器的桌面**下的 `wechat-ai-assistant-sync`，换台电脑也对）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_to_other_pc.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_to_other_pc.ps1 -Dest "E:\助手同步"
```

（`pwsh` 是 PowerShell 7 的命令名；没装 7 的机器用 `powershell`，本机就是这种情况。
默认目标**不能写死某台机器的路径**——`selftest_portable.py` 会拦「本机项目绝对路径」。）

然后把那个目录里的文件拷到另一台电脑的项目目录覆盖。**注意两件事**：

* `config.yaml` 是用户填过模型/key 的那份，**别用包里的 config 覆盖它**；同步脚本
  也不拷 `config.yaml` / `settings.json` / `data\`（那是本机凭证与状态）。
* `data\state.json` **绝对不要拷过去**：它带旧游标，`state.resume_window` 内的旧消息会被
  判成「重启补齐」，**只通知、不自动回复**，看起来就是「在跑但不回」。

没有网络/U 盘时最简单的搬运方式：项目本身支持**发普通文件**——
把上一步生成的目录压成 zip，在微信里发给**文件传输助手**，到那台电脑上收下来解压。

## 验证（在那台电脑上）

```powershell
# 1) 先停 bot（真机自检要求它停着，两路查询同时压 hook 会把微信搞崩）
#    停：助手.bat → [4]；或关掉 启动助手.bat 那个窗口
.\.venv\Scripts\python.exe verify_real.py
```

期望看到其中一条（**不再是 ❌**）：

```
✅ 自己的 wxid 拿到了（来源：config.yaml）：wxid_o…
✅ 自己的 wxid 拿到了（来源：hook 接口）：wxid_o…
⚠️ 自己的 wxid 是从 contact 表里认出来的：wxid_o…（能跑，但建议写进 config.yaml 的 self_wxid）
```

最后一条说明代码同步成功了、但配置还空着——建议顺手把值抄进 `config.yaml`。
**如果看到的是 `❌ 拿不到自己的 wxid` 加一句「猜出来的 … 已否掉」**：那次自动识别
认到别人身上了，`find_self_wxid.py --apply` 把本机账号填进去就好（机器不会因此
自答自己——兜底闸门会拦，见上面「认不出自己时的兜底闸门」）。

## 回归

| 文件 | 钉住什么 |
|---|---|
| `selftest_aixed.py` | `detect_self_wxid` 的判据、只查 `contact.db`、坏值不乱拿、查不动给空串；**四级优先级 + 核实**（唯一账号用目录名 / 对得上采用 / 对不上否掉 / 找不到目录标未核实）；`self_identity_ok()` 的四种取值 |
| `selftest_install.py`「打包机」段 | `config.example.yaml` 里有这一行、`bot.main` 与 `verify_real` 都走同一个入口、「四级 + 核实」用假客户端**真跑** |
| `selftest_bot_loop.py` | 兜底闸门（认不出自己 + 文本刚发过 → 跳过；认得自己 → 不拦；位置在 `from_self` 分支**之前**） |
| `selftest_self_wxid.py` | 账号目录名 → wxid 的形状（含 10 条「认不准就不认」反例）、`--apply` 只动一行 + 先备份 + 找不到就不改 |

## 别改回去的地方

* **认不出必须给空串**，不许拿「第一个 `wxid_` 开头的行」之外的东西凑数（猜错＝拿别人的号发消息）。
  2026-10-06 真机证明这条不是理论：那次就是 ④ 认错了人，而且不报错。
* **④ 必须过 ③ 的核实**（`account_dir_wxids()`）。多账号时不许替用户挑——列出来让他填。
* `contact.db` 查询要带 `ESCAPE '\'`：下划线是 LIKE 通配符。
* **兜底闸门只在 `self_identity_ok()` 为假时生效**，不许改成无条件跳过：
  否则「对方恰好说了和我们上一条回复一样的话」会被静默丢掉。
* `find_self_wxid.py --apply` **只改 `self_wxid` 那一行**并先备份。项目平时**从不回写
  `config.yaml`**（手写注释不能被机器冲掉），这里是显式例外，代价必须承担。
* 同步脚本**绝不拷** `config.yaml` / `settings.json` / `data\`。
