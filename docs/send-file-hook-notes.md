# 发普通文件：hook 侧需要什么（材料，不是实现）

> 这份文档**故意不写代码**。写一个没验证过的内部调用 offset 塞进 DLL，
> 等于拿你正在用的微信做实验——而这块区域**已经崩过一次微信**（见下面「已有证据」）。
> 所以这里只记录：**现状核实结果** + **要做什么** + **怎么验**，让接手的人（人或未来的我）
> 有据可依，而不是凭空猜。工程项目侧已经全部就绪（见最后一节）。

## 一、现状核实（2026-10-02，有据可查）

**hook 暴露的接口全集**（来源：`docs/aixed-api.postman.json`，作者自己的 API 集合）：

```
POST /SendTextMsg          {"wxidorgid": ..., "msg": ...}
POST /SendImgMsg           {"wxidorgid": ..., "path": ...}
POST /ForwardXMLMsg        {"to_wxid": ..., "content": "<msg>...</msg>"}
POST /Decode_Pic           {"src_path": ..., "dst_path": ...}
POST /GetSelfProfile       {}
POST /QueryDB/execute      {"optDbName": ..., "SQL": ...}
POST /QueryDB/GetAllDBName {}
POST /QueryDB/status       (GET)
```

**没有发文件的接口。** 而且唯一能「搬运一条已有消息」的 `ForwardXMLMsg`：

- `src/wx_send_xml.cpp:538-553` 只认 `<img ` / `<videomsg ` / `<emoji ` 三种，
  其余类型 `XmlType::OTHER` → `return false`（**连转发别人的文件都不支持**）；
- 更关键的是 `wx_send_xml.cpp:555-578`：**即便类型认出来了也直接 `return false`**。
  注释里写明了原因——

  > 2026-10-01 23:0x 真机实测：**这一段调用会把微信进程带崩，不要打开。**
  > 请求发出后 30001 立刻断开（WinError 10054），Weixin 进程没了、
  > **连 crashinfo 的 .dmp 都没留**。也就是说：修好解析等于把「报 500」升级成「崩微信」。

**结论：当前 hook 版本上，「发普通文件」和「转发别人的文件」都做不到。**
这不是配置问题，也不是「再试一次就好」。

## 二、要做什么（hook 侧）

### 1. 新路由

在 hook 的 HTTP 路由里加一个（名字可自定，本项目按 `/SendFileMsg` 对接）：

```
POST /SendFileMsg   {"wxidorgid": "<wxid>", "path": "<本机文件路径>"}
返回：成功 ret:0；失败给出非 0（**不要**无条件 ret:0，见下）
```

⚠️ **返回值要真实**：现在的 `SendImgMsg` **成功也无条件回 `ret:0`**，导致
「发图其实没成」本地发现不了（`aixed_api` 与 `assets.py` 的注释都专门警告过）。
发文件如果也这么干，用户会以为发出去了。**新接口必须能区分成功/失败。**

### 2. 定位「发送文件」的内部函数

这是唯一需要真机逆向的一步。已知信息：

- 项目里 hook 的做法是「手搓 C++ 对象 + 硬编码 vtable/偏移 + 裸调 `g_weixinBase + Offsets::XXX`」；
- `src-4.1.10.27/.../xdb/xwechat_offsets.h` 目前**几乎是空的**（14 字节），
  所以**没有任何现成 offset 可抄**；
- 同项目里 `dec_pic_call` 有过「偏移过期」的先例——**偏移是按微信版本走的**，
  而本项目锁的是 **4.1.10.27**。

### 3. 三个已知可疑点（接手前先看这三条）

都是 `wx_send_xml.cpp` 那段注释里留下的，做新接口时会**同样踩到**：

1. **内存分配方式**：`Memory::Allocate` 用的是 `VirtualAlloc`，不是 CRT 堆。
   而这些字符串/结构对象如果被微信析构，就会用 heap free 去释放 `VirtualAlloc` 的内存
   → **堆损坏（0xC0000374）**。作者当时的对策是「干脆全不释放」。
   新接口必须想清楚这块内存的归属：要么让微信完全不持有，要么用它能接受的方式分配。
2. **偏移可能过期**：`FORWARD_XML_CALL` / `IMAGE_DATA_VTABLE` 这类常量都是按版本写死的。
3. **别把「报错」修成「崩溃」**：作者的原话——修好解析之后从「HTTP 500」变成了
   「微信进程消失」。**能报错就报错，比崩掉好得多。**

## 三、怎么验（顺序不能变）

1. **先写一个只读的探测**：确认新路由能被调用、且**不改任何微信内部状态**
   （比如先让它只做参数校验 + 返回「未实现」）。这一步验证路由通了。
2. **在微信窗口前，一条一条试**：不要连着发。每试一条：
   - 看 `http://127.0.0.1:30001/QueryDB/status` 还在不在；
   - 看 `%APPDATA%\Tencent\xwechat\crashinfo\reports\Weixin_*.dmp` 有没有新转储；
   - 看**微信窗口**里那条消息是不是真的发出去了（不是只看 `ret:0`）。
3. **每崩一次都要重新扫码登录**——所以别盲目重试，一次改动只试一条。
4. 通过之后再改 `config.yaml` 的 `agent.send_file_hook: true`，然后在微信里让助手发一份文件，
   肉眼确认对方收到了。

## 四、工程项目侧已经就绪（换 hook 后不用改代码）

| 环节 | 位置 | 状态 |
|---|---|---|
| 客户端方法 | `aixed_api.AixedClient.send_file(path, wxid)` → `POST /SendFileMsg` | ✅ 已写 |
| 工具 | `agent_tools.t_send_file`（`send_file`） | ✅ 已写 |
| 文件定位 + 边界 | 复用 `file_read.pick()`：**只认微信 `msg/file/` 下**、按文件名（含 `(1)` 重名退让）、**多份命中不替用户挑** | ✅ 已写 |
| 确认闸门 | 名单外走 `set_pending(kind="file")`；`send_pending` 里有 `file` 分支 | ✅ 已写 |
| 发送前二次校验 | 确认之前会**再定位一次**，定位不到就「一份都不发」并如实说 | ✅ 已写 |
| 能力开关 | `agent.send_file_hook`（默认 `false`，`is True` 严格判定） | ✅ 已写 |
| 默认行为 | 关着时**当场如实拒绝**，而且**不进待确认队列**（不让用户白确认一次） | ✅ 已写 |
| 重启恢复 | `bot.restore_pending` 会恢复 `file` 字段 | ✅ 已写 |
| 回归 | `selftest_policy` / `selftest_bot_loop` 覆盖「开关关着如实拒绝、不进队列」等 | ✅ 已写 |

**所以真做起来只需要：hook 侧加一个能用的 `/SendFileMsg`，然后把开关改成 `true`。**

## 五、顺带记录：`forward_message` 也是死的

同一个原因（`ForwardXMLMsg` 全部 `return false`），**「转发别人的消息」这条功能当前整体不可用**。
`forward_message` 工具仍然注册着（保留接口形状），但工具说明里已经明确写了
「当前 hook 上这条路是死的，调用一定会失败，失败时要照实说、不要绕」。
它的失败路径是诚实的（`_request` 的 `_check` 遇到非 `ret:0` 会抛，工具会把错误原样带回）。

这一条和「发文件」是**同一个根因**：**只要能恢复 `ForwardXMLMsg`，转发和转发文件会一起回来**
（因为微信转发文件本质就是转发那条文件的 XML）。所以如果要在 hook 上投入，
**先修 `ForwardXMLMsg` 是性价比最高的那一件事**——修好它，两个功能一起活。
