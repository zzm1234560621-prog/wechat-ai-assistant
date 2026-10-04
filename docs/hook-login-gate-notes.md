# hook 的「登录就绪」判据：为什么它必须读微信自己记的保存位置

记录时间 2026-10-03。这条是**我们自己的补丁**踩的坑，不是上游 hook 的。

## 结论

`version.dll` 里那个「等登录就绪再放行 QueryDB」的判据
（`src/inline_weixin_dll_load.cpp` 的 `WxDbWrittenSinceLoad()` / `LoginReadyThread()`），
**不能把数据目录写死成 `%USERPROFILE%\Documents\xwechat_files`**。
微信 4.x 允许把「文件保存位置」改到别的盘，改了之后那个默认路径**根本不存在**，
于是判据永远不成立。

## 症状（2026-10-03 真机）

```
GET  http://127.0.0.1:30001/QueryDB/status
     {"IsLogin": 0, "hWeixin": 140710313852928}
POST /QueryDB/execute  {"optDbName":"session.db", ...}
     查库 session.db 失败：get database handle which named session.db failed
GetSelfProfile -> 全空
```

**看起来像「掉登录」**（`CLAUDE.md` 的 debug 顺序第 0 步就是先分诊这个），
但微信其实登录得好好的：`<保存位置>\xwechat_files\<账号目录>\db_storage\session.db`
当时正被实时写入（探针跑的同一秒）。

唯一的错处是：**判据看的是一个不存在的目录**。表现是「不报错、不打日志、
所有查询回空/failed」，正是这个项目最怕的那种静默失效。

## 为什么会这样：重复 owner

Python 侧早就踩过同一个坑，并且**已经修好**了 ——
`image_cache._wechat_save_roots()` 会去读

```
%APPDATA%\Tencent\xwechat\config\<哈希>.ini
```

那个文件的内容就是**一行路径**（本机实测：9 个字节，形如 `E:\wxdata`）。
`image_cache.data_root()` 因此在本机正确地返回 `<那一行路径>\xwechat_files`。

C++ 侧是第二个 owner，但没跟着改 → 两份实现里只有一份是对的。
**修的时候两边必须一起看**：判据（一行、像盘符路径、`<它>\xwechat_files` 存在）
要完全一致，否则下次还会分叉。

## 修复

`WxDbWrittenSinceLoad()` 现在按优先级试多个候选根：

1. 微信自己记的保存位置（读上面那个 ini，判据与 `image_cache._wechat_save_roots()` 一致）；
2. 历史默认位置 `%USERPROFILE%\Documents\xwechat_files`。

另外把「为什么没放行」变成了**可读的**：`LoginGateNote()` 会把结论透到
`GET /QueryDB/status` 的 `LoginGate` 字段（空串 = 正常）。
一个候选根都不存在时**立刻**写出去；等了 30 分钟还没命中也会写。
以后再遇到，一眼就能看到是判据没找到数据，而不是去猜「是不是掉登录了」。

## 补充（2026-10-04 真机）：ini 里可能写的不是路径，而是 `MyDocument:`

另一台电脑（微信 4.1.10.27、**保存位置就是默认的 Documents**）上，
`%APPDATA%\Tencent\xwechat\config\<哈希>.ini` 的内容是：

```
MyDocument:
```

——**不是盘符路径**，是微信自己「用默认文档目录」的标记（本机开发机那份是 `D:\wechat`，
所以这个 ini 有两种形态）。后果很具体：

- `WechatSaveRoots()` 的判据是「一行、像盘符/UNC 路径」（`X:\` 或 `\\`），`MyDocument:` **会被跳过**；
- 于是这台机器上**唯一的候选根就是第 2 条兜底** `%USERPROFILE%\Documents\xwechat_files`。

所以那条兜底**不是可有可无的**：把 `MyDocument:` 当成路径处理是错的（拼出
`MyDocument:\xwechat_files`），但把兜底删掉、只信 ini，会让**所有用默认保存位置的机器**
（也就是绝大多数人）全部卡在门禁上。Python 侧同理：`image_cache._wechat_save_roots()`
也不认它，靠 `data_root()` 的默认位置兜底。**两边都要留着这一层。**

⚠️ 第二条（2026-10-04 实测 + 当天已修）：**装 hook 用的一定得是带下面这套判据的构建。**

- 出事的是**包里那份 `version.dll`**：它一直是厂商原版（483840 / `5ABB5002`）。它的**源码快照**里
  `g_IsLogin` **没有任何置 1 的路径**（`grep g_IsLogin` 全仓只有「定义 = 0」「`db_mgr` 两处只读」
  「`LoginReadyThread` 置 1」）。原因源码里写着（`inline_weixin_dll_load.cpp` 第 166-168 行）：
  > 原版靠一个「登录检测 hook」把 g_IsLogin 置 1，那段代码在开源快照里被移除了
  > （README: main 分支已移除 ...Hook 安装与处理代码），所以 g_IsLogin 恒为 0，
  > db_mgr.cpp 里 if (!g_IsLogin) 的门禁永远成立，QueryDB 永远返回空。

  装它上去的表现就是本文开头那种「hook 通了、查询全失败、`IsLogin` 恒 0」，而且 JSON 里
  **没有 `LoginGate` 字段**（带这套判据的构建会有）。
- ⚠️ **但别把这条推成「厂商的成品二进制一定不能用」**：被删掉的只是**源码快照**里那段登录 hook，
  厂商随包发布的那个 DLL 里可能还留着它（二进制里没有「找新鲜库」的字符串，只说明它用的不是
  这套判据，**不能证明它不置 `g_IsLogin`**）。判据只能现场看 `IsLogin` / `GetSelfProfile` /
  库能不能查。
- ✅ **2026-10-04 已换掉**：包里的 `installers/wechat-4.1.10.27/version.dll` 现在是**开发机现役
  那份带门禁的构建**（499200 / `9FBD1340`，就是本文件说的「找新鲜库」那一代），厂商原版归档为
  `version_old_backup.dll`（483840 / `5ABB5002`，**别再拷回去**）。
  `do_hook_install.ps1` 拷的就是它；`selftest_portable.py` §6 有一条盯着「包里这份必须带门禁字符串」。
  另外两份带判据的构建也留在包里备用：`version_live_20261002_3112622B.dll`（= 开发机 10/02 现役）、
  `version_loginready.dll`（10/01 现役）。

⚠️ 第三条（2026-10-04 本机实测）：这套自编门禁的 `g_IsLogin` **只置 1、从不置回 0**
（见上面的 grep 结论：除了 `LoginReadyThread` 置 1，没有任何复位点）。
后果：微信掉登录、或自己重启回登录界面时，**`IsLogin` 仍然是 1**，而所有 QueryDB 一律回
`get database handle which named xxx.db failed`、`GetSelfProfile` 是空的。
本机现场就是这个状态：`bot.log` 每 5 秒一行
`[bot] 轮询心跳 #N ⚠️ 分片查询失败：message_fts_v4_0(9次)…`、游标不动，
而 `/QueryDB/status` 却是 `{"IsLogin": 1}`。
所以「掉登录」的判据**不能只看 `IsLogin`**：要 `IsLogin: 1` **且** `GetSelfProfile` 非空
**且** 库真能查。`health` 那条「掉登录告警」在这类构建上因此形同虚设，别再拿它当唯一凭据。

✅ **好消息（同一天实测）：重新扫码登录之后它自己好了。** 不用重启微信、也不用重启助手 ——
`live_history._probe_heal` 每 45 秒会 `force_rescan` 一次，登录之后那一次扫描就命中了句柄表；
现场表现是 `data/state.json` 里 `message_fts_v4_*` 游标重新有值并且开始推进、心跳那行的
「分片查询失败」从 5 个分片缩回只剩 `message_0.db`（后者本来「常常解析不出句柄」，是已知现象）。
所以遇到「`IsLogin: 1` + 查询全失败」：**先看微信窗口在不在登录态 → 扫码 → 等一两分钟看自愈**，
**别急着停 bot、更别急着重启微信**（重启会再掉一次登录态）。

⚠️ 但 `GetSelfProfile` **只能当单向证据**：源码快照里 `SelfInfo` **只有读、没有任何写入**
（`grep SelfInfo`：`global.h` 声明、`global.cpp` 定义、`GetSelfProfile.cpp` 九处 `resp[...] = SelfInfo.xxx`，
**没有一处赋值**）—— 写它的那段和 `g_IsLogin` 一样在被删掉的代码里。所以：

- **非空** ⇒ 微信确实登录着（厂商成品二进制里那段还在的话就会填）；这是可信的正向证据；
- **空** ⇒ **说不清**：可能是没登录，也可能只是这份 DLL 没那段代码。
  从开源快照编出来的 DLL，这一项**永远是空的**，别拿它报「掉登录」。
- 唯一不受 DLL 影响的判据是**窗口本身**（是聊天列表还是二维码）+ 账号库里 `.db` 有没有近期写入。

## 验证顺序（换过 DLL 之后必须走一遍）

1. 退出微信 → 部署 DLL（见 `_audit/deploy_version_dll.ps1`，**需要管理员**）→ 启动微信（重新扫码）；
2. `GET http://127.0.0.1:30001/QueryDB/status` → `IsLogin: 1`、`LoginGate: "ok..."`；
3. `.venv\Scripts\python.exe verify_real.py`（**先停 bot**）；
4. 发一条消息确认发送功能没被新二进制弄坏。

## 相关

- 登录门禁为什么要等「就绪」而不是一加载就置 1：见
  `inline_weixin_dll_load.cpp` 里那段注释（无条件置 1 实测崩过三次微信）。
- 微信数据目录的查找顺序：`image_cache.py` 的 `data_root()`。
