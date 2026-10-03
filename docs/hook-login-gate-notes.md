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

## 验证顺序（换过 DLL 之后必须走一遍）

1. 退出微信 → 部署 DLL（见 `_audit/deploy_version_dll.ps1`，**需要管理员**）→ 启动微信（重新扫码）；
2. `GET http://127.0.0.1:30001/QueryDB/status` → `IsLogin: 1`、`LoginGate: "ok..."`；
3. `.venv\Scripts\python.exe verify_real.py`（**先停 bot**）；
4. 发一条消息确认发送功能没被新二进制弄坏。

## 相关

- 登录门禁为什么要等「就绪」而不是一加载就置 1：见
  `inline_weixin_dll_load.cpp` 里那段注释（无条件置 1 实测崩过三次微信）。
- 微信数据目录的查找顺序：`image_cache.py` 的 `data_root()`。
