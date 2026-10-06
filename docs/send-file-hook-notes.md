# 发普通文件：**已经能用**（2026-10-02 真机证实）——不要再照旧文档去改 C++

> ## ⚠️ 先读这一段：这份文档的前身是错的
>
> 旧版这份文档的标题是「hook 侧需要什么（材料，不是实现）」，结论是
> **「当前 hook 版本上发普通文件做不到，必须改 hook 的 C++ 重编译 version.dll」**。
> **这个结论已被真机实测推翻。**
>
> 真相：**发普通文件走的就是现成的 `POST /SendImgMsg`**，一行 C++ 都不用改、
> 不用重编译、不用重启微信、也没有崩微信风险。叫 `/SendFileMsg` 的那条路由
> **根本不存在（实测 HTTP 404）**——旧文档把「这个名字不存在」误读成了「这个能力不存在」。
>
> 所以本文档现在是**接线说明 + 实测证据**，不是「待实现的需求」。

---

## 一、实测证据（2026-10-02，本机 4.1.10.27）

```
POST http://127.0.0.1:30001/SendImgMsg
{"wxidorgid":"filehelper","path":"C:\\Users\\<you>\\Documents\\xwechat_files\\<wxid>\\msg\\file\\2026-01\\xxx.xlsx"}
→ {"ret":0,"retmsg":"success"}
```

回查「文件传输助手」的 `Msg_<md5("filehelper")>` 表（发送前 `MAX(local_id)=587`），新增那行：

| 文件 | `local_id` | `local_type` | 解读 |
|---|---|---|---|
| `Mentor–Student Matching List.xlsx` | 588 | **25769803825** | `(6<<32)\|49` = **文件消息**（不是图片的 3） |
| `A.zip`（复测另一种后缀） | 592 | **25769803825** | 同样是文件消息 |

两条的 `status = 2`、`upload_status = 2`（作对照：同批发的文本消息 `local_type=1`、`upload_status=0`）。

把 588 的 `message_content`（hex + zstd，用 `live_history.decode_msg_content` 解）解出来，
是**一条完整的文件消息 XML**，服务端签发的字段一个不缺：

```xml
<msg><appmsg appid="wx6618f1cfc6c132f8" sdkver="0">
  <title>Mentor–Student Matching List.xlsx</title>
  <type>6</type>
  <appattach>
    <totallen>10304</totallen>                                    <!-- 与磁盘字节数一致 -->
    <attachid>@cdn_305f…_9e96e220ab180089dd71935c964ab422_1</attachid>
    <fileext>xlsx</fileext>
    <cdnattachurl>305f020100044b30490201000204458051fa…</cdnattachurl>
    <aeskey>9e96e220ab180089dd71935c964ab422</aeskey>              <!-- 与 attachid 尾段同源 -->
    <encryver>0</encryver>
    <fileuploadtoken>v1_…</fileuploadtoken>
```

**安全侧（同样实测）**：调用前后 `GET /QueryDB/status` 都是 `{"IsLogin":1,…}`；
`%APPDATA%\Tencent\xwechat\crashinfo\reports` 的转储数 **6 → 6（无新增）**；微信进程存活。

**旁证**：上游 `aixed/WeChat-Hook` 的 `update.log` 写着
「**20260727 发送图片等接口统一改为发送文件类接口** …… 已经过测试发 gif jpg png wxgf
mp4 exe **xlsx** zip txt 类别文件正常」——我们实测证实了其中的 xlsx 与 zip。
端点名里的 "Img" 是历史遗留，**别按名字猜能力**。

## 二、工程侧接线（已全部落地）

| 环节 | 位置 | 说明 |
|---|---|---|
| 端点选择 | `aixed_api.send_file_via(cfg)` | 默认 `"imgmsg"` → `/SendImgMsg`；可配 `"filemsg"` → `/SendFileMsg`（留给将来真带该接口的 hook）。**写歪的值回退 imgmsg**（这是能力不是安全闸，写错一个词不应变成「发不了」） |
| 客户端方法 | `aixed_api.AixedClient.send_file(path, wxid, cfg=None)` | 按 `send_file_via` 打端点 |
| 工具 | `agent_tools.t_send_file`（`send_file`） | 定位（两种来源，见下两行）→ 解析收件人 → 能力闸 → 微信来源按名单直发 / **盘上来源与名单外一律进待确认** |
| 能力开关 | `agent.send_file`（**默认 true**） | 只有显式写 `false`/`0` 才关；关掉时**当场如实拒绝且不进待确认队列** |
| 文件定位①（微信） | `file_read.pick()` | **只认微信 `msg/file/` 下**、按文件名（含 `(1)` 重名退让）、**多份命中不替用户挑** |
| 文件定位②（电脑） | `files.path_ok()` | 模型给**绝对路径**（2026-10-04 T9 扩）：过 `files.roots` / `files.deny` + 触发者闸门 `files.who_allows` → 带 `extra={src: disk}`，**一律进确认队列、绝不走名单直发** |
| 确认闸门 | `set_pending(kind="file")` + `send_pending(..., cfg=cfg)` | **发文件永远要用户回「确认」**（不可逆动作） |
| 发送前二次校验 | `send_pending` 的 `file` 分支 | 确认之前**再判一次**：微信来源要求「按文件名还能重新定位到同一个文件」；盘上来源要求**按当前配置再过一次 `files.path_ok`**。不过就「一份都不发」并如实说 |
| 重启恢复 | `bot.restore_pending` | 恢复 `file` 字段 |
| 回归 | `selftest_policy.test_send_file`（25 条）+ `selftest_files.py` 的盘上来源段（带反证） | 默认进待确认 / 关掉时如实拒绝且不进队列 / 禁止绕路禁止假装 / 端点解析与写歪回退 / 复核不过不发 / `cfg` 被带到 client / **盘上来的零确认直发会被反证抓出来** |
| 模型可见文本 | `selftest_tool_registry.py` §6 | `TOOLS['send_file'].description` 与两份 config 的 system_prompt **都要写明两种来源**（文件名 / 绝对路径）。2026-10-05 真机踩过：放宽只写在代码与本文档里，模型照旧回「send_file 只能发微信里收/发过的文件」 |

**不要**为了「能发任意文件」而把 `file_read.pick()` 的**读取**边界放开（读那条路仍只认
`msg/file/`）；发文件是**另一条**准入（`files.path_ok` + 磁盘来源强制确认），两条别合并。
`agent.send_image_dirs` 是**第三条**边界（发图用的），也不要合并。

## 三、留下的、真实存在的边界

1. **`forward_message`（转发别人的消息）仍然是死的**：`src/wx_send_xml.cpp` 里
   `ForwardXmlMessage` 对所有类型都 `return false`——注释写明 2026-10-01 真机实测
   **这一段调用会把微信进程带崩**（请求发出后 30001 立刻断开 WinError 10054、
   Weixin 进程消失、**连 crashinfo 的 .dmp 都没留**），所以作者安全关闭了它。
   工具说明里已写清「这条路是死的，调用一定会失败，失败时要照实说、不要绕」。
   本文档**不**建议去恢复它：它和「发文件」不是同一个根因（旧文档那句
   「修好 `ForwardXMLMsg` 就等于发文件也能用了」**已经作废**——发文件本来就能用）。
2. **`SendImgMsg` 成功也无条件回 `ret:0`**，所以**本地发现不了「其实没发出去」**。
   判断发出去了没有，要靠回查 `Msg_` 表（看 `local_type=(6<<32)|49`）或肉眼看微信窗口，
   **不能只看 `ret`**。
3. 本机实测用的文件都在 `msg/file/` 下（就是 `file_read.pick` 的边界内），
   hook 侧对路径有没有别的限制**尚未探明**；换目录发之前先按第 4 节的办法验一次。

## 四、以后要动这块时的验证顺序（别只信 `ret:0`）

1. 记下当前状态：`GET /QueryDB/status`、`crashinfo/reports/*.dmp` 的**数量**、
   `Msg_<md5(收件人)>` 的 `MAX(local_id)`。
2. 发**一条**，不要连着发。
3. 复核：`local_id` 有新增吗？新增那条的 `local_type` 是 `25769803825`（文件）还是 3（图片）？
   `status`/`upload_status` 是不是 2？解出的 XML 里 `title/totallen/fileext/attachid` 对不对？
   **微信窗口里那条消息长什么样？**
4. 看转储数有没有变、`IsLogin` 还在不在。有变化就停下来，别盲目重试。
5. **看日志里有没有 `[bot] 跳过（这是自己刚发出的文件）`** —— 有那条才说明「发出去的文件回显」
   被认掉了；没有的话就会**重复发**（见下）。

### ⚠️ 发出去的文件会回显成一条新消息（2026-10-03 真机踩到「为什么会重复发」）

现场：文件发出去之后，**那条文件消息会被轮询当成「新消息」捞回来**，于是又被理解成
「用户让我发这个文件」→ 再登记一条待确认；用户每回一次「确认」就多收一份，看起来像
「重复发」。根因和图片那条一模一样（图片早就有 `is_own_image` 兜着），**文件这条路一直缺**。

修法（对齐图片那一套）：
- `agent_tools.remember_sent_file(talker)` —— 在 `send_pending` 的 **file 分支发成功后**调用；
- `agent_tools.is_own_file(talker, ts)` —— 主循环在「非文本消息」分支里认回显，命中就打
  `[bot] 跳过（这是自己刚发出的文件）` 并跳过；
- 回归：`selftest_policy.test_own_file_echo()`（7 条）+ `test_send_file` 里那条
  「发出文件后记下了『刚发过文件』」。

**局限照实说**：判据只有「会话 + 30 秒时间窗」，没有文件名/md5（hook 的 `SendImgMsg`
成功也无条件回 `ret:0`，拿不到新消息的 local_id）。所以窗口内**同一个会话里别人真发来一个文件**
会被这条误吞——窗口给得短、且只在「刚发过文件」时才生效，把概率压到最低。
另外：**运维侧从外部直接发文件（不走 bot）不会记这笔**，那种回显不会被认掉；
清掉已经压在队列里的旧待确认项用 `_audit/clear_pending_file.py`（只清 `kind="file"`）。

## 五、附录：历史上做过的离线逆向（结论仍有效，但**不再需要**）

2026-10-02 为了「改 C++ 发文件」这条（后来发现不必走的）路，做过一轮**只读**逆向，
产物在 `_audit/hook-re-file-send.md`（814 行）与 `_audit/` 下若干脚本。仍然有用的结论：

- `namespace offset`（`include/global.h`）里的偏移**对本版有效**：`send_message=0x1677A30`
  是真函数头（`.pdata` 边界 `0x1677A30..0x1677D39`）、`txt_message_ctr=0x6B2C30`、
  几组 vtable 都落在 `.rdata` 且指向本 DLL。**但 `send_message` 不是「发消息总入口」**：
  它只把 `arg1+8` 的 `vector<shared_ptr>` 拷进 `new(0x108)` 对象再调 `0x2bbcb0`，
  **函数内没有类型分派**——类型来自对象自带的 vtable。
- `create_param2=0xDF40` 是 **7 参数**函数（读 `[rsp+0x28]` 写进 `out+0xD0`），
  而 hook 只传 6 个 → **第 7 参数永远是栈垃圾**。这是确定性的 ABI 缺口
  （**实测不影响发文件**，因为那条路根本没走到这儿）。
- **旧版（4.1.5.30）那套 `namespace Offsets` 全部失效**：`IMAGE_FIELD_VTABLE` 现在指向
  一段 ASCII 字符串、几个 `*_VTABLE` 指向随机/加密数据、`FORWARD_XML_CALL=0x1CF3D20`
  **不是函数头**（而是函数 `0x1CF2FA0+0xD80` 的中间指令），且整个命名空间的常量
  **零代码引用**。这解释了当年那次「一放开就崩微信」——拿错地址从指令中间开始 call。
  **别按旧常量去 call 任何东西。**
- 文件消息的判别码是 appmsg 里的 `<type>6</type>`（`local_type` 高 32 位也是 6），
  与 `live_history._V4_FILE_TYPE = (6<<32)|49` 一致。
- 真机文件消息 XML 样板留在 `_audit/file_msg_1.xml` / `file_msg_2.xml`，
  本次发出去那条留在 `_audit/sent_588.xml`。
