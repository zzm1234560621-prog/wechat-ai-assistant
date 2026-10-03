# 微信语音通话（打电话）——调研结论、接口与红线

> 为什么单独一份：CLAUDE.md 有约 64KB 的**指令预算**，超了尾部会被静默截掉
> （2026-10-02 我把这块写进 CLAUDE.md，直接把「参考资料」那段挤掉了）。
> 所以细节一律放这儿，CLAUDE.md 只留一行指针。

---

## 一、结论速查

### ⛔ 最终定案（2026-10-03）：**通过 hook 做不到打电话**

三条**互相独立**的证据：

1. **真机对比**（同一次抓取里）：我们发一条类型 50 的邀请消息，只触发
   `type_dispatch(rdx=0x32)` + `payload_ctor` **1 次**；而**真人手动拨号**那一通是
   `payload_ctor` ↔ `voipmsg_layer`(`0x2319D00`) **交替 18 次**，另有 `serializer` / `type50_handler`，
   且 payload 里有个**随机生成的 8 位 id**（我们那边是 `"00000000"`）。
   ⇒ **发起通话由微信客户端的通话状态机驱动，不是"发一条消息"。**
2. **官方 hook 4.1.10.27 的 8 个 HTTP 接口没有一个和通话有关**：
   `SendTextMsg` / `SendImgMsg` / `ForwardXMLMsg` / `Decode_Pic` / `GetSelfProfile` /
   `QueryDB/execute` / `QueryDB/GetAllDBName` / `QueryDB/status`。
3. **官方 README 自述**：main 分支**已移除 `PB/NetSceneSendPB`**（发送原始协议包的能力），
   而 4.1.10.27 **没有** 3.9.10.16 那种「协议版」。

**要继续只剩"自己实现微信 voip 协议"** —— 那是**项目级**工作量，不是加一个功能。

| 问题 | 答案 |
|---|---|
| 微信语音通话能不能程序化发起？ | ❌ **不能**（2026-10-03 定案） |
| 那条消息层是什么？ | `Weixin.dll` RVA **`0x2319D00`**，虚表 RVA **`0x82FBCB8`**（⚠️ 本文件早期写的 `0xA2FBCB8` 是抄写错误） |
| 走 `/voipinvite` CGI 吗？ | **不走**。CGI 链 `0x2A5DA80` ← `0x4BAB450` ← `0x4BAC4A0` 在真人打语音通话时**一次都没命中** |
| 邀请消息长什么样？ | 已完整拿到：**277 字节 XML，全是常量或 0**（见下） |
| 发出去会怎样？ | 微信**会**把它当类型 50 处理（`type_dispatch rdx=0x32` + `payload_ctor` 命中），**但不弹呼叫窗、对方不响** |
| 项目侧现状 | **代码与闸门保留**（`t_call` / `callgate.py` / `aixed_api.call_voip` / hook 的 `/CallVoip`）；**对外描述已删**（`TOOLS` 那条 call、两份 config 的 `system_prompt`、README 那节）——见文末「2026-10-03 决定」。`action=call` 到点**如实报错，绝不降级成发文本** |
| 替代路线 | 到点强提醒（零风险）/ 安卓 ADB 真拨号 / 企业微信 PSTN |

### 实测过的"能做到什么"（2026-10-03）

* 仓库源码里**实装了 `/CallVoip`**（`src/SendTextMsg.cpp` + `src/wx_send.cpp`）：
  复用文本消息那套 `send_message`，把 `type` 换成 50、正文换成邀请 XML。
  - `via="text"`（默认）：用 `TextMessage`（虚表 `0x8279358`）。
  - `via="object"`：用**微信自己的原语**造对象 —— `HeapAlloc(0x2D8)` →
    `0xA04560`（通用消息构造器，写虚表 `0x81D2458`）→ 填 `+0x18`/`+0x38`/`+0x58` 双方 wxid、
    `+0x180` 正文、`+0x1c0` msgsource → `0xA1B1B0(obj, 0x32)` 分发。
  - **两条都实测过：对象虚表正确（`base+0x81D2458`）、微信不崩、但都不进通话状态。**
* 静态追出的调用链：`0x4D93DB0`（7KB 状态机分发器）→ `0x172CF60` / `0x172B830` →
  `0xA19E90(obj, **src**)`（从 `src` 对象填 0x2D8 消息对象）→ `0xA1B820`（造+发）→
  `0xA1FB00` → `0xA208C0` → `0x2319D00`。
  **卡点**：`src` 的结构未知，而它带着被叫 wxid 和那批通话字段。

### 完整的邀请负载（真机实拍，277 字节，含 hex 核对）

```xml
<voipinvitemsg><roomid>0</roomid><key>0</key><status>0</status><invite_type>1</invite_type></voipinvitemsg>
<voipextinfo><recvtime>0</recvtime></voipextinfo>
<voiplocalinfo><wording_type>4608</wording_type><duration>0</duration><display_content></display_content></voiplocalinfo>
```

`wording_type=4608` = `0x1200`，在**三个独立来源**上一致：真机 XML、探针内嵌对象模板
`+0x1bc`、`Weixin.dll` 偏移 `0x867BB28` 处的字段名表。
服务端下发的东西（`roomid`/`inviteid`/`identity`/`timestamp`）**只出现在挂断之后的
`VoIPBubbleMsg` 状态消息里**，发起时用不到。
`Weixin.dll` 里那张字段名表：`VoIPBubbleMsg voipinvitemsg roomid invite_type voipextinfo
recvtime voiplocalinfo wording_type display_content diaplay_content room_type roomkey`
（`diaplay_content` 是**微信自己的拼写错误**，正好证明它是手写表）。

---

## 一之二、2026-10-02 的旧结论（保留作史，**已被上面取代**）

## 二、真实链路是怎么确认的（不是推断）

1. 用自建静态工具（向量化交叉引用：`call rel32` 与 `lea rip+disp` 满足同一恒等式
   `field(d) == T - text_va - d - 4`，指令长度自动抵消；capstone 逐条校验；`.pdata` 反查函数边界）
   在 183MB 的 `Weixin.dll` 上把 446,808 个函数建表，定位到 voip 信令与 UI 层。
2. 发现线上部署的 `version.dll` 其实是**探针版**：注册了 `/CallVoip`、`/Probe/arm`、
   `/Probe/status`、`/Probe/dumpinfo`、`/Probe/callfn`、`/Probe/readmem`、`/Probe/log`、
   `/Probe/netlog`、`/Probe/sendsteps`、`/Probe/replaySend`、`/SendTypedMsg` 等路由，
   自带 19 个已定位目标（含 `0x2319D00`「voipinvitemsg 消息层（本次真走这条）」）。
3. `POST /Probe/arm` 装 19/19 钩子 → **用户真人给一个联系人打了一次语音通话** →
   抓到 225 次命中：`0x2319D00` 命中 14 次，而 CGI 链**零命中**。
4. 内存里同时出现双方 wxid（`wxid_aaaaaaaaaaaa` = 自己，`wxid_bbbbbbbbbbbb` = 被叫）。

## 三、`/CallVoip` 的接口契约（两次报错逐字换来）

```
POST /CallVoip   {"wxid": <被叫 wxid>, "self": <自己的 wxid>}
```

* **不是 `wxidorgid`**。写错时回 `{"error":"需要 wxid 和 self"}`，一个字都不会拨。
* **前置条件**：hook 必须先「抓到过 `AddMessage` 的包装对象」——
  也就是**要先有人在这个微信里正常收发过一条消息**。没抓到过时回
  `{"error":"还没有抓到 AddMessage 的上下文/包装对象——先在微信里发一条普通消息让它被调用一次"}`。
* 返回里带 `error` 也是**失败**；即使成功也**只许说「邀请已发出」，不许说「对方接到了」**。

### 它的实现要点（从探针 DLL 逆出来的）

处理体是 `FUNC rva=0x49ed0`（size `0x1c8c`，用报错文案 `"需要 wxid 和 self"` 当锚点定位）。
读的参数：`wxid`、`self`、`poke`、`invite_type`、`wording_type`；
输出的：`msg_addr`、`pokes_applied`、`msg_dump`。骨架：

```
call operator new(0x300)            ; 0x300 字节对象
add  rbx, 0xa04560                  ; rbx = Weixin.dll + 0xA04560
call rbx                            ; **微信自己的构造函数**
mov  dword ptr [obj+0xc], 0x32
; 两个 wxid 串 → obj+0x18 / +0x38（经 Weixin!0x4f460 赋值、Weixin!0x504e0 取 c_str）
call operator new(0x300)
add  rcx, 0x20 ; call rbx           ; 第二个对象同样构造
mov  dword ptr [obj2+0x2c], 0x32
; 填 obj2+0x38 / +0x58，然后处理 invite_type / wording_type / poke 并发出
```

**关键 RVA**：`Weixin!0xA04560` = 消息对象构造函数；`Weixin!0x4F460` = 字符串赋值辅助；
`Weixin!0x504E0` = 取 `c_str`；对象 `0x300` 字节；字段 `+0xc`/`+0x2c` 写 `0x32`(=50)。

## 四、🔴 两条操作红线（都是实测踩出来的）

1. **永不调用 `GET /QueryDB/GetAllDBName`。**
   它是**内存扫描实现**（`getDatabaseInfo()` 先 `m_dbs.clear()` 再 `searchDatabases()`），
   我调了一次直接把 HTTP 连接打断、微信随后重启回登录界面。
   `aixed_api.ping` 的注释早写着「实测会 500」——**唯一允许的调用点是 `live_history.force_rescan()`**。
2. **钩子装着的时候，不要用 hook 自己的 HTTP 端点触发微信动作。**
   `arm`(19/19) + `POST /SendTextMsg` 发一条消息 → 连接被强制断、**微信进程直接终止**（无转储）。
   推断是钩子自己写的 HTTP 响应又走了被钩住的路径、形成重入。
   要触发微信动作就**让人在 UI 上做**，或者先 `POST /Probe/disarm`。

补充实测：钩子装着时**在 UI 里发消息也会卡死**（用户原话「一发消息就卡爆了」）——
因为探针每命中一次同步写 ~3KB（含 400 个栈字），一条消息命中上百次。

## 五、项目侧的落地（已完成的部分）

* `callgate.py` —— 三道闸：
  * `agent.call_voip`：能力闸，**默认 false**，严格 `is True`（写 `"true"`/`1` 都当关）；
  * `agent.call_quiet_hours`：免打扰，默认 `"23:00-07:00"`，**跨零点判得对**，空串 = 不设；
  * `agent.call_max_per_day`：上限，默认 3，按**滑动 24 小时**（不是自然日），
    账本 `data/calls.jsonl`（只记时间/wxid/显示名，**不记通话内容**）。
* `agent_tools` 的 `call` 工具 —— **只登记待确认**（`kind="call"`），绝不自己拨；
  被闸挡下时**一条待确认都不留**（不让用户白确认一次）。
* `bot.py` 确认分支 —— 回「确认」后才拨，**拨之前再判一次闸**（登记到确认之间配置可能被改、
  也可能跨过免打扰边界）；拨号失败**绝不自动重试**（第一次可能已经通了）。
* `bot.py` 的定时任务 —— `scheduler.run_due(..., call=call_task)` 接上了，
  **定时任务也不绕过免打扰**；判不过就如实报错，**绝不降级成发文本**。
* 回归：`selftest_call.py`（45 项，含跨零点、坏账本、重启恢复、绝不降级）。

## 六、还差什么（下一步）

1. **在仓库 hook 源码里写一个「安静的」`/CallVoip`**：不逐次写日志、不挂 `ws2_32`，
   用 `Weixin!0xA04560` 造对象 + 填两个 wxid + 处理 `invite_type`，
   并复用 hook 自己抓到的 `AddMessage` 包装对象。
   仍需把探针 DLL 里 `0x49ed0` 的**余下部分**逆完（`invite_type` / `wording_type` / `poke` /
   最后"发出去"那一跳——那一步大概是通过对象自己的虚表调的，所以没有 `add reg, imm` 特征）。
2. 编译、部署（**换 DLL 要结束微信 + 重新扫码**），做一次真机验证。
3. ⚠️ 部署前注意：线上那个探针版 DLL 的源码已失传，**别拿仓库树重编的 DLL 直接覆盖它**
   （会删掉整套探针）。已备份为
   `installers/wechat-4.1.10.27/version_live_20261002_3112622B.dll`（602112 字节，SHA256 前缀 `3112622B`）。

## 七、本次调研的脚本（都在 `_audit/`，全部只读）

| 脚本 | 作用 |
|---|---|
| `probe_voip_xref_fast.py` | 向量化交叉引用定位（27 锚点、446,808 函数建表） |
| `probe_voip_callers.py` | 调用图回溯（同一恒等式抓 call/jmp/lea） |
| `probe_callvoip_impl.py` | 用报错文案定位并反汇编 `/CallVoip` 处理体 |
| `probe_dll_diff.py` | 线上 DLL vs 仓库 DLL 字符串差分（**发现探针路由、避免误覆盖**） |
| `analyze_probe_log.py` | 分析探针日志（命中统计、调用顺序、抓到的字符串） |
| `probe_route_disasm.py` / `probe_find_handlers.py` | 探针 DLL 的路由与 lambda 定位 |
| `通话功能-逆向进度与恢复.md` | 八轮完整过程记录（含每一处失败与纠错） |

**已知的坑**：PowerShell 用 `*>` 重定向会把输出写成 UTF-16LE，`read` 会判成 binary，
要先转 UTF-8 再读。


---

## 2026-10-03 决定：**只删描述**（用户口径）

原话：「只删除描述就行，然后提醒那个真要」。落地如下：

**删掉的（对外描述）**

- `agent_tools.TOOLS` 里**那条 `call` 定义**（模型因此看不到它、不会再主动提打电话）；
- **两份 config 的 `system_prompt`** 里那几段教模型怎么打电话 / `mode=call` 的文字；
- `README.md` 的「打电话」那一节、定时任务表里 `/定时 加通话` 那一行；
- `bot.py` 帮助文本里的 `/定时 加通话` 那一行、`scheduler._USAGE` 里同一条；
- `schedule` 工具说明与 `mode` 枚举里的 `call`。

**保留的（代码与闸门，整条不动）**

- `agent_tools.ToolBox.t_call`（**登记待确认、永不自己拨**）、`callgate.py` 三道闸、
  `aixed_api.call_voip`、hook 源码里的 `/CallVoip` 与 `SendVoip*`、探针 `hook_voip.cpp/.h`；
- `selftest_call.py` 照旧跑（46 项全绿）；老的通话定时任务到点照旧**如实报错**；
- 于是 `t_call` 是**故意的孤儿处理器**：`selftest_tool_registry.py` 里有一条**带原因**的例外
  （`intended_orphans`），别的孤儿照样算失败。

**要恢复可见**：把 `TOOLS` 里那条 call 加回去（原文在 git 历史里），别的都不用改。
**别顺手删代码** —— 用户明确只要求删描述；`agent_tools.ToolBox.t_call` 的 docstring 里
也写了这条来龙去脉。
