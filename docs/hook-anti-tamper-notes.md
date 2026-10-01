# aixed hook 的"防篡改"：开源版有什么、缺什么

结论先写：**开源快照只实现了"藏"，没实现"骗"。真正的防篡改（伪造客户端检测数据）在付费版里。**

记录时间 2026-09-30。对照的两份源码：

- `installers/wechat-4.1.10.27/src-4.1.10.27/WeChat-Hook-4.1.10.27`（项目内，用户改过）
- `C:\Users\zzm12\Downloads\WeChat-Hook-411027_.zip`（2026-06-24 版）

两份全量 diff 后只有 4 处不同（README 链接、global.h 空行、`inline_weixin_dll_load.cpp`、
新增 `other_version_fix.md` 与 `防止微信自动更新/`），**代码实质相同**。

---

## 一、微信侧可能的检测面

| 检测面 | 说明 | 线索来源 |
|---|---|---|
| 模块枚举 | `EnumProcessModules` / `CreateToolhelp32Snapshot` 列出进程内模块 | — |
| 代码段完整性 | 对 Weixin.dll 代码段做 **CRC32** | TG 群 2026-08-18，Byboy 回答"crc32" |
| 客户端检测数据上报 | `ClientCheckData`，含时间派生校验值 | `test_timestamp_reverse.py` 注释 |
| 探测调用 | `WcProbe` / `CCD` / `NtQuery` | README 称已从 main 分支移除 |

TG 群里唯一一次正面讨论（2026-08-17/18）：

> 白展堂：微信的反作弊机制，是怎么检测到 DLL劫持、DLL注入 等非常规行为的？
> Byboy：**crc32**
> 李琳娜：谁让你全劫持了，不会自己想办法吗 人家都给你提供一半思路了

然后就没了，**群里没有可行方案**。

---

## 二、开源快照实现的：只解决"别被看到"

| 措施 | 位置 | 作用 |
|---|---|---|
| 完整代理 version.dll | `src/version_proxy.cpp` | 假 version.dll 把 **17 个导出函数**全部转发给 `System32\version.dll`。模块名 + 导出表看起来是正常系统 DLL |
| 只在主进程驻留 | `dllmain.cpp` `IsMainWeixinProcess()` | 命令行含 `--type=` / `--crashpad-handler` 的子进程 `FreeLibraryAndExitThread` 自卸 |
| PEB 摘链 | `src/hideself.cpp` `HideModuleFromPEB()` | 从 InLoadOrder / InMemoryOrder / InInitializationOrder 三条链表移除自己 |
| xLog 钩子 | `src/hook_xlog.cpp` | 挂着微信 xLog，**当前只 `OutputDebugString` 读日志**，未做处理；且只在 `#ifdef _DEBUG` 下安装 |

这四层都只回答"别被枚举到"，不回答"已经被看到了怎么办"。

---

## 三、开源快照缺失的：付费版那半

1. **WcProbe hook 只剩声明，无实现**

   `src/inline_weixin_dll_load.cpp` 末尾：

   ```cpp
   using FnWcProbe = void(__fastcall*)(void* out_buffer, void* out_len, void* input_param);
   FnWcProbe g_Original = nullptr;
   ```

   全仓库搜 `FnWcProbe` / `g_Original` **只命中这两行**——没有 detour 函数、没有 `Hook_Call`、
   没有任何赋值。是作者剥离 WcProbe hook 时漏删的残骸。
   （项目内那份连这两行都没有。）

   从签名看：`out_buffer` / `out_len` 说明这是个**产生输出缓冲的探测调用**，
   detour 的作用应是把探测结果替换成"干净"数据，`g_Original` 存原函数指针。

2. **缺的两块硬信息**（开源快照里一个字都没有）
   - `WcProbe` 在 Weixin.dll 里的**偏移**
   - `out_buffer` 的**数据结构**

3. **ClientCheckData 的伪造上报** —— 见下节，算法有了，上报链路没有。

---

## 四、唯一漏出的线索：`ClientCheckData::GetSystemTimeCheckValue`

`test_timestamp_reverse.py`（zip 根目录）是作者**逆算法时的验证脚本，没删干净**。
注释里保留了 Weixin.dll 里的原函数签名：

```
UINT64 ClientCheckData::GetSystemTimeCheckValue(size_t dwTimeStamp)
```

算法（伪代码，来自该脚本注释）：

```
now = dwTimeStamp                      # 按 64 位处理
arr = little_endian_bytes(now, 8)
arr[3] ^= 0x66                         # byte[3] 和 byte[4] 各 XOR
arr[4] ^= 0x66
return little_endian_uint64(arr)
```

验证向量：输入 `1774840639` → 输出 `66149018431`。

`ClientCheckData` 是微信**客户端检测数据上报**的结构。逆出这个算法后就能算出合法校验值，
进而伪造一份"干净"的 ClientCheckData 上报，让服务端判定客户端未被篡改。
这就是付费版防篡改的核心，也是群里 "ccd 算法"、"收 win/mac ccd算法" 说的东西。

---

## 四之二、开源版 vs 付费版：差在哪些地方

差异来源有两处：README 里那句"已移除"的清单，以及 TG 群里作者对功能问题的回答。

### 4.2.1 README 明说被移除的（= 付费版独有）

`README.md` 第 11 行：

> 当前 main 分支已移除 VMP 保护、远程授权校验、PB/NetSceneSendPB、CDN、
> WcProbe/CCD/NtQuery 相关 Hook 安装与处理代码。

| 被移除的东西 | 属于哪一类 | 说明 |
|---|---|---|
| **VMP 保护** | 自身加固 | VMProtect 加壳，防别人逆自己的 DLL |
| **远程授权校验** | 授权 | 付费版联网校验 license；开源版完全没有，所以 dll 拷走就能用（只能发消息） |
| **PB / NetSceneSendPB** | 发送能力 | 直接构造协议包发送（NetScene 是微信的协议层），不依赖 UI 层调用 |
| **CDN** | 下载能力 | 走微信 CDN 拉图片/视频等资源 |
| **WcProbe / CCD / NtQuery Hook** | **防篡改** | 就是本文第三节说的那套——hook 微信的探测函数 |

### 4.2.2 TG 群里体现出的功能差异

付费版被作者确认过的能力（群里逐条问出来的）：

| 功能 | 群里确认 | 开源版 |
|---|---|---|
| 发文本 / 图片 / XML 转发 | README 有接口 | ✅ 有 |
| QueryDB 查库（好友、群、消息） | `g_IsLogin` 被移除，需自己置位才通 | ⚠️ 需自行打补丁（本项目已做） |
| 视频号（直链下载、评论、点赞） | 2026-04     | ❌ 无 |
| 朋友圈评论（完整版） | 2026-02 / 03 | ❌ 无 |
| 公众号评论 / 评论回复 / 文章 A8key | 2026-02 / 09 | ❌ 无 |
| 小程序 code / `getLatestUserKey` | 2025-12 / 03 | ❌ 无 |
| 红包消息、收款通知 | 2026-04 / 05 | ❌ 无 |
| **防撤回** | — | ✅ 有（`Patch_Revoke()`） |
| 过低版本检测 | 作者反复宣传 | ⚠️ 有代码，但偏移是 4.1.8.67 的，当前被注释 |
| 自动回复 | 2026-07 说"在开发中" | ❌ 无 |
| ws / wss 远程访问 + web ui | 2026-07 上线 | ❌ 无 |
| **防篡改** | 作者称"My hook 不会提示外挂" | ❌ **无** |

### 4.2.3 一句话概括

开源版 = **能收发消息 + 能查库 + 防撤回**，加一层"藏"。
付费版 = 上面全部 + **协议直连发送 + 全量业务接口 + 防篡改**。

价格口径（群里）：4.1.x 与协议版 **800/年、绑定设备不绑微信**；
3.9 老 hook 早年卖的是**永久授权**（有人 800 用了 4 年还包升级）。

---

## 五、现状与风险判断

- 开源版**没有防篡改**，只有"藏"这一层遮挡，本质是赌没人认真查。
- 更麻烦的是它同时**主动改微信代码**：
  - `Patch_Revoke()` 往 Weixin.dll 写 `NOP; JMP`（当前启用）
  - `Patch_Low_Version_m2()` 往代码里写 4 字节补丁（当前被注释，且循环有 bug：
    数组 12 项、`for (i < 3)` 只打 3 个）
  - MinHook 的 inline hook 也会改代码段

  改代码段恰恰是最容易触发 CRC 校验的操作——所以开源版的做法**增加了**被检出的面，
  而不是减少了。
- 偏移全部是 **4.1.8.67 / 4.1.10.27 的硬编码值**（`include/global.h`）。换版本必须重新核对，
  写错偏移会直接写坏微信内存。

### 两份快照别混用

同一个文件里 xLog 钩子的偏移不同：zip 版是 `WeixinDll_Offset(0x108678)`（启用），
项目内那份是 `0xF22C1`（已注释）。说明两份来自不同 commit。

---

## 六、如果以后要补

可行性上，从开源快照出发是**补不齐**的——缺的偏移和结构体只能靠逆向 Weixin.dll 得到。
可选路径按成本排序：

1. 买付费版 DLL（作者提供的是全功能 + 防篡改）
2. 自己逆 4.1.10.27 的 Weixin.dll，定位 WcProbe 并还原 out_buffer 结构
3. 不管检测面，接受现状风险

相关：`README.md` 的版本锁死与封号风险说明、`config.yaml` 里 `poll_interval` 的说明
（hook 不支持并发查询，调太勤会把自己拖死）。
