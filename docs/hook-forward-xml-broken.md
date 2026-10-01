# hook 的 `/ForwardXMLMsg`（转发原始 XML）在 4.1.10.27 上不可用 —— 实测与处置

记录时间：2026-10-01 23:00 前后。**这是一次真实的崩溃换来的结论，别把这里的处置改回去。**

## 结论（一句话）

`POST /ForwardXMLMsg` 在 4.1.10.27 上**会把微信进程带崩**。已在本项目里改成
**安全拒绝**（返回 `ret=1`），并且在 Python 侧**不再使用转发**。
「素材暂存」这个功能因此只能走**明文图片 + `SendImgMsg`**（见 `assets.py` 顶部）。

## 现场（怎么一步步确认的）

1. **先撞的是 HTTP 500。** 用一张自己发出去的图（`filehelper` 会话 `local_id=513`）调
   `/ForwardXMLMsg`，接口回 `HTTP 500`（空 body）。
2. **在 hook 源码里定位到 500 的原因**：
   - `src/wx_send_xml.cpp` 的 `ExtractXmlFields()` 对 IMAGE 分支用
     `std::stoi(ExtractBetween(xml, "hdlength=\"", "\""))` 等六处解析；
   - 而**普通图片消息的 XML 里没有 `hdlength`**（实测 513 那条：只有
     `aeskey / encryver / cdnthumbaeskey / cdnthumburl / cdnthumblength /
     cdnthumbheight / cdnthumbwidth / cdnmidheight / cdnmidwidth / cdnhdheight /
     cdnhdwidth / cdnmidimgurl / length / hevc_mid_size`，**没有 hdlength、也没有 md5、
     没有 cdnbigimgurl**）；
   - `std::stoi("")` 抛 `std::invalid_argument`，而 `src/ForwardXMLMsg.cpp` 的路由
     **只把 JSON 解析包在 try 里**，转发调用在 try 外 → 异常冒到 httplib → 500。
3. **第一次修复**：`ToIntSafe()`（缺字段/非数字一律当 0）+ 路由级 try/catch。编译、部署。
4. **修完之后更糟**：500 没了，请求真的走进了下面那段
   「手搓 C++ 对象 + 硬编码 vtable/偏移 + 裸调 `g_weixinBase + Offsets::FORWARD_XML_CALL`」
   ——**微信进程当场消失**：请求发出后 30001 立刻断开（`WinError 10054`），
   `Weixin.exe` 没了、`crashinfo` 里**连 .dmp 都没留**（此前 5 次崩溃都有 dmp）。
   即：代码从来没走到这一步过（解析那行一直先抛），所以这段调用在 4.1.10.27 上
   **从未被验证**。
5. **最终处置**：`ForwardXmlMessage()` 在类型判断之后**直接 `return false`**
   （安全拒绝 → `ret=1/fail` → 上层如实说「发不了」），并保留 `ToIntSafe` 的修复
   ——那是真 bug，将来这条路能跑时还需要它。

## 两个可疑点（谁要接手先看这两条）

1. `Memory::Allocate` 用的是 `VirtualAlloc`，**不是 CRT 堆**；这些伪造的
   `std::string` 对象如果被微信析构，就会用 `operator delete` 去释放非堆内存 = 堆损坏。
   作者自己显然也撞过（源码里那行注释：「清理的话会崩溃」，于是干脆全部不释放、一路泄漏）。
2. `Offsets::FORWARD_XML_CALL` / `IMAGE_DATA_VTABLE` / `IMAGE_FIELD_VTABLE` 等偏移
   可能对本版微信已经过期——同项目里 `offset::dec_pic_call` 就有过一模一样的先例
   （见 `docs/wechat4-dat-image-notes.md` 的「关于 hook 的 Decode_Pic」）。

## 重新打开这条路的正确姿势

1. 用调试器/静态分析在本版 `Weixin.dll` 上**重新定位**转发/发送图片的入口与各 vtable；
2. 把伪造对象的内存分配改成与微信一致（CRT 堆 / 让微信自己拷贝后不管），
   或者改成「只读入参、不交出所有权」的形态；
3. **在微信窗口里盯着**一个一个验证：每崩一次都要重新扫码登录，
   所以**不许盲目重试**（本机 2026-10-01 已经为一个错误的判断多付了两次扫码）。

## 部署/回滚（本机）

- 线上 DLL 在 `C:\Program Files\Tencent\Weixin\version.dll`；换 DLL 必须先结束微信进程，
  且需要管理员权限（UAC）。
- 部署脚本：`%TEMP%\hook_probe_src\deploy_forwardfix.ps1`（按哈希备份 → 结束微信 →
  换入 → 校验 SHA256）。它把当前在用的 DLL 备份到
  `%TEMP%\hook_probe_src\backup\version_<hash8>.dll`。
- 回滚：把备份改回 `version.dll`（同样要结束微信、要管理员），再启动微信扫码。
- ⚠️ 源码快照有两份：仓库里的 `installers/wechat-4.1.10.27/src-4.1.10.27/` 与
  `%TEMP%\hook_probe_src\`（含别人的 VoIP 探针改动）。**线上那份是从 probe 树编出来的**，
  别拿仓库树直接覆盖——增量编译时只重编改动过的文件，才不会把别人的改动弄没。

## Python 侧的对应处置

- `assets.py`：素材分两种——`xml`（原始消息引用，**发不出去**，留着等 hook 修好）
  与 `path`（**明文图片文件**，用 `send_image` 发，唯一真正能发的方式）。
  `plaintext_of()` 只认**磁盘上真实存在**的文件（微信缓存会被清理）。
- `agent_tools.t_send_asset`：只发明文；只有引用时**如实说发不了**，并给出两个能走通的办法
  （以「文件」方式再发一次 / 把图放进 `agent.send_image_dirs` 允许的目录）。
- `bot.stash_control_media`：回执里就说明「这张发不发得出去」——不让用户以为
  「已暂存 = 随时能发」。
