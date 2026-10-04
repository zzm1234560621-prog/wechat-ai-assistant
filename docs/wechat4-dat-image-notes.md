# 微信 4.1.10.27 图片（`.dat`）格式逆向记录

记录时间 2026-10-01。目标：让 bot 能读出图片消息的内容。
**结论：格式已完全确定，XOR 段已破解，配对规则已破解；只差 AES 段的固定密钥没拿到。**
下面的内容都可以独立复现，下次接手不用重做。

---

## 一、已完成的部分

### 1.1 图片消息的位置

图片**不在** fts 全文检索库里。实测 `message_fts_v4_*` 的全部 `local_type` 只有
`1`（文本）、`42`（名片）、`48`（位置）、`(subtype<<32)|49`（appmsg），**没有图片**。

图片在 `message_N.db`（本例是 `message_0.db`）的**每个会话一张**的表里：

```
表名   Msg_<md5(会话名)>
过滤   local_type = 3
列     local_id / server_id / local_type / create_time / real_sender_id
       / status / source / message_content / packed_info_data
       / WCDB_CT_message_content / WCDB_CT_source ...
```

### 1.2 `message_content` → XML

```
message_content(hex 字符串) → unhex → zstd 解压 → XML
```

zstd 魔数 `28 B5 2F FD`。解出来的 XML：

```xml
<msg>
  <img aeskey="7941bc96ef6813cf23be026d09d16da9" encryver="1"
       cdnthumbaeskey="..." cdnthumburl="..." cdnthumblength="11761"
       cdnthumbheight="210" cdnthumbwidth="96"
       length="138402" md5="ed659638ab2414bda435f06876b5ac23"
       originsourcemd5="559b965dda088a77db994497d561eafe" ... />
  ...
</msg>
```

第一行是发送者 wxid + 冒号（4.x 的格式）。

### 1.3 ★ 配对规则（这是最有价值的一条）

图片消息的 `packed_info_data` 是 protobuf，里面藏着一个 **32 位十六进制串**：

```
08 02 10 04 1a 22 22 20 <32 个 ASCII 字符> 58 00
                      └─ 这就是下面那个 .dat 的文件名
```

**这个串就是本地 `.dat` 文件的文件名（不含后缀）**。

实测：7245 个 key 对 7188 个本地文件，**交集 7188 = 100% 命中**。

```
消息行 → packed_info_data → "3f81fefa954779b383beea1db4a25669"
       → <微信数据目录>\msg\attach\<Msg_ 表的哈希后缀>\<YYYY-MM>\Img\
         3f81fefa954779b383beea1db4a25669[_t|_h].dat
```

注意：
- 文件名**不是** XML 里的 `md5`（两者集合 7172 vs 7187，**交集 0**，是完全不同的哈希）
- 所以**不能**靠 XML 的 md5 找文件，只能靠 `packed_info_data`
- attach 目录名 = `Msg_` 表名去掉前缀（同一个哈希）
- 后缀：无 = 原图，`_t` = 缩略图，`_h` = 高清。**实测绝大多数只有 `_t.dat`**
  ——微信要你点开才会下载原图

数据目录（4.1.10.27）：`%USERPROFILE%\Documents\xwechat_files\<wxid_带后缀>\`

### 1.4 ★ `.dat` 文件格式（完全确定）

```
偏移 0-3    07 08 56 32          （"V2" 魔数，小端 dword 0x32560807）
偏移 4-5    08 07                （小端 word 0x0708）
偏移 6-9    aesSize   (u32 LE)   （实测恒为 1024）
偏移 10-13  xorSize   (u32 LE)
偏移 14     01
偏移 15...  载荷
```

- 头共 **15 字节**
- `aesSize + xorSize == cdnthumblength`（三个样本验证过）
- **文件总长 = cdnthumblength + 31**（= 15 头 + 16）
- 载荷 = **前 aesSize(1024) 字节加密方式 A** + **其后的字节：XOR 0x6C**

这个结构在 Weixin.dll 里找到了对应的**构造代码**（见 1.6），
`aesSize = min(size, 0x400)`、`xorSize = min(余下, 0x100000)`，与实测逐字节吻合。

### 1.5 ★ XOR 密钥 = `0x6C`

**证据**：8 个样本的文件末尾 2 字节**全都是 `93 b5`**，而

```
93 b5 ^ 0x6C = FF D9      ← JPEG 的 EOI（结束标记）
```

把 offset 15+1024 起到文件末尾按 `0x6C` 异或，结果的最后两个字节正是 `FF D9`，
且位置正确。所以 **XOR 段可以解**。

（顺带：`xorSize` 字段比实际 XOR 段少 16 字节，这一点没完全对上，但不影响解码。）

### 1.6 Weixin.dll 里的对应代码

用 PE 异常表（`.pdata`）定位到编解码函数：

```
RVA 0x9B2410 ~ 0x9B44BE（8366 字节），共 4 个模板实例：
  0x9B2410 / 0x9B6410 / 0x9BAE10 / 0x9BF9E0
```

函数开头：

```asm
cmp  r8d, 1                 ; 第三个参数必须为 1
jne  ...                    ; 否则直接走另一条分支
movabs r14, 0xaaaaaaaaaaaaaaaa   ; MSVC 未初始化内存填充模式（不是密钥！）
```

构造文件头的那段（RVA 0x9B2530）：

```asm
mov dword ptr [rbp+0x188], 0x32560807   ; 07 08 56 32
mov word  ptr [rbp+0x18c], 0x708        ; 08 07
mov dword ptr [rbp+0x18e], r14d         ; r14 = min(size, 0x400) = aesSize
mov dword ptr [rbp+0x192], r12d         ; r12 = min(余下, 0x100000) = xorSize
mov word  ptr [rbp+0x196], 1            ; 01
```

这是一个 15 字节长的 MSVC `std::string`（size 字段 = 0xf），所以头部就是 15 字节。

---

## 二、没拿到的：AES 段的固定密钥

**载荷前 1024 字节是 AES-ECB + 全局固定密钥**。

判断依据：抽 60 个文件比较**第一个 16 字节密文块**，只有 **2 种**（42 个 / 18 个）。
如果是 CBC 或有随机 IV，60 个文件应该出 60 种。只有 2 种说明：

1. 明文首块是**标准 JPEG 头**（两种变体，如 JFIF / EXIF）
2. 加密是**确定性的**（ECB），且**所有文件共用一把密钥**

### 已穷举排除的（都不要再试了）

| 方向 | 具体试过 | 结果 |
|---|---|---|
| 密钥来源 | 每条消息 XML 的 `aeskey`（**精确配对下** 121 文件 / 2160 组） | ❌ |
| 密钥来源 | 库里**全部 7209 个** `aeskey` | ❌ |
| 密钥来源 | `packed_info_data` 的 32 位串及其反序 | ❌ |
| 密钥来源 | `md5/sha256(aeskey)` / `md5(packed)` / `md5(packed_bytes)` 等 8 种推导 | ❌ |
| 密钥来源 | 由 XOR 密钥 0x6C 推导（全 0x6C、递增、递减、全 0、全 FF、0x37…） | ❌ |
| 密钥来源 | `media_0.db`（只有 TimeStamp/Name2Id/VoiceInfo，**无图片密钥表**） | ❌ |
| 密钥来源 | `source` 列（是 msgsource 元数据，非密钥） | ❌ |
| 密码/模式 | AES-128(hex16) / AES-256(ASCII32) | ❌ |
| 密码/模式 | ECB / CBC(IV=0) / CBC(IV=密文头) | ❌ |
| 位置/长度 | 头偏移 0/6/8/15/16/31/40+，段长 256/512/1024/2048/整段 | ❌ |
| 额外变换 | 解密后再按首字节反推单字节 XOR | ❌ |
| 额外变换 | 单字节 XOR 全爆 0-255（找 JPEG SOI） | ❌ |
| **新组合** | 先 `密文 ^ 0x6C` 再做 AES（假设 XOR 作用于整段） | ❌ |
| 表定位 | 编解码函数内的 16 字节常量（只有 `0xAA` 填充和一个跳转表） | ❌ |
| 调用链 | 从逆 S-box 反查：全 `.text` 104MB 扫 rip 相对 disp32 | **0 处** |
| 调用链 | 逆 S-box 的 8 字节绝对指针 / 4 字节 RVA / RVA+base | **0 处** |
| 调用链 | 编解码函数 0x9B2410 的 `call rel32` 直接调用点 | **0 处** |
| 调用链 | 编解码函数 0x9B2410 的 8 字节函数指针 | **0 处** |
| hook | `offset::dec_pic_call = 0x493E70` 换第三参数 0/1/2/3/4 各跑一遍 | ❌ 都不写文件 |

### 关于 hook 的 `Decode_Pic`：偏移是错的（已实锤）

`Decode_Pic` 调的是 `Weixin.dll + offset::dec_pic_call`，其中 `dec_pic_call = 0x493E70`。

实测：接口返回 `{"ret":0,"retmsg":"success"}` 但**不写任何文件**——
换三种 dst 路径、换后缀，都不写。

反汇编 `0x493E70` 证明它**不是解码函数**，而是一个处理 MSVC `std::string` 的字符串函数：

```asm
mov  rax, qword ptr [rcx + 0x10]   ; 长度
test rax, rax
je   ...                            ; 长度 0 直接返回
cmp  qword ptr [rcx + 0x18], 0x10   ; 容量 < 16 -> 用内部缓冲(SSO)
jb   ...
mov  rdx, qword ptr [rcx]           ; 否则取堆指针
```

也试过把第三参数从 `1` 换成 `0/2/3/4` 各跑一遍（一次部署全测），**仍然什么都不写**。

> 同命名空间里的 `offset::send_message = 0x1677A30` 是对的（发消息正常），
> 所以只有 `dec_pic_call` 这一条对 4.1.10.27 过期了。

**结论：要修好 `Decode_Pic`，得先拿到 4.1.10.27 真正的解码函数偏移。**

### 为什么静态分析整体走不通

多个"表存在但无人引用"的现象叠加：

- AES 逆 S-box 在 `.rdata` 里存在，但没有任何代码引用它
- 编解码函数既没有直接调用点、也没有函数指针
- 整个 DLL 里连正向 S-box、AES Rcon、标准 T-table 都没有

这强烈指向 **控制流混淆**（跳转在运行时才解析）。没有调试器的话，
纯静态到这里基本到头了。

### 为什么反查调用链走不通

AES 的**逆 S-box** 确实存在于 `Weixin.dll` 的 `.rdata`（RVA `0x875F250`，
256 字节，前 48 字节逐字节比对标准逆 S-box 完全一致）。

但它**前后紧邻的是编译期路径字符串**（比如
`E:\...\kernel\gen\protobuf...`），说明这张表是**嵌在一大块数据 blob 里的**
（静态链接库的数据段），不是常规代码用 `lea`/指针引用的数据结构。

另外整个 DLL 里：
- **没有**正向 S-box（`63 7C 77 7B...`）
- **没有** AES Rcon（`01 02 04 08 10 20 40 80 1B 36`）
- **没有** 标准 T-table（Te0 `c6 63 63 a5...` / Td0 `51 f4 a7 50...`）
- 有一个 SM4 S-box 在 RVA `0x8BC1CD0`

所以 AES 实现要么被优化成别种形式，要么这些常量在运行时生成。

---

## 三、★ 绕开解密的路：微信自己缓存了已解码的缩略图

这是整轮里**最有实用价值**的发现。微信把**已经渲染过的**图片缩略图
以**明文**缓存在磁盘上，不需要任何解密就能直接读：

```
<账号目录>\cache\<YYYY-MM>\Message\<md5(会话名)>\Thumb\
    <local_id>_<create_time>_thumb.jpg
```

- `local_id` 和 `create_time` **就是 `Msg_<hash>` 表里同名的两列**，直接照抄
- 实测验证：缓存文件 `3661_1788747187_thumb.jpg` ↔ 表里
  `local_id=3661, create_time=1788747187` 逐字对上
- 文件是真图（头是 `FF D8 FF E0 00 10 4A 46` = 标准 JFIF JPEG；
  也有内容其实是 PNG 但后缀写 `.jpg` 的，读的时候要看魔数不要看后缀）
- 缓存目录的**月份是"渲染时间"不是消息时间**，所以找文件要扫所有月份，
  不能拿 create_time 去推月份

**规模实测**：836 个文件，覆盖 45 个会话（分布在不同会话上，
例如 `0aa279e2…` 218 个、`9af2f997…` 207 个、`ab2ad322…` 54 个）。

**局限**：只有**你滚动过/看过的**图片才会被缓存，覆盖率不高；
没缓存的还是得走解密那条路。而且它是缩略图，不是原图。

顺带确认：`cache\<月>\Message\<hash>\Bubble\<id>_<时间>_b.dat` 里也有同名的
`.dat`，但那是**同样的 V2 加密格式**（头 `07 08 56 32 08 07` 一致），
XOR 0x6C 同样成立（尾部解出 `FF D9`），前 1024 字节同样需要那把 AES 密钥。

### 3.1 落地实现（已接进项目）

按这条路做了读取图片的能力：

| 文件 | 作用 |
|---|---|
| `image_cache.py` | 缓存查找：`cache/<月>/Message/<md5(会话)>/Thumb/<local_id>_<create_time>_thumb.jpg` |
| `live_history.v4_images()` | 某会话的图片消息 + 缓存路径 |
| `image_read.py` + `tools/ocr.ps1` | 系统 OCR 认图里的字（免费离线）；也可配视觉模型 |
| `agent_tools.py` | 新增工具 `find_images` / `read_image` |
| `config.yaml` | `image:` 段（`mode: off\|ocr\|vision`、`max_bytes`、`vision.*`） |

**两个实测踩到的坑**（后来都修了，记下来免得重犯）：

1. **不能只按 `local_type = 3` 过滤图片。** 4.x 里图片有两种存法：直接的
   `local_type = 3`，以及 appmsg `(subtype<<32)|49`（实测子类型 5 就是图片）。
   只按 3 过滤会漏掉一大半——缓存里那张的库记录其实是
   `local_type = 21474836529`。
   所以改成**以本地缓存为主要依据**，再补 `local_type = 3`。
2. **返回时不能简单按时间取最近 N 条。** 有缓存的往往是较早的图，
   最近的消息反而都没缓存；直接 `[-limit:]` 会把能看的全截掉，
   表现成「一张都读不了」。

**能力边界（对用户要如实说）**：只有滚动看过的图能读；是缩略图不是原图；
OCR 只认图里的字，照片/表情包这类画面内容看不懂（要视觉模型）。

---

## 四、下次怎么继续

按性价比排序：

1. **问作者**（TG 群 `@Aixed`）。他手上就有这套东西。而且现在能问得很具体：
   > `.dat` 的载荷前 1024 字节是 AES-ECB + 固定密钥，那把固定密钥是什么？
   > 或者：`Decode_Pic` 该以什么参数调？hook 里 `dec_pic_call = 0x493E70` 实测空转
   > （返回 success 但不写文件，三种 dst 路径都不写）。
   顺带可以问 `0x493E70` 对 4.1.10.27 是不是过期了。

2. **动态分析**。静态走到头了，动态能直接看到密钥：
   - 在 Weixin.dll 的 AES 解密函数下断点，读它拿到的 key 参数
   - 或者 hook `sqlite3`/文件读取，看 `.dat` 解密后的内存
   - 需要调试器（本机没装 cdb/windbg，Windows Kits 里只有 dll 没有 cdb.exe）
   - 注意：进程内有 hook，附加调试器要小心，别把微信搞崩

3. **换思路绕过解密**（不推荐，但可行）：让微信自己把图解码到磁盘再读。
   没找到现成的落盘路径；`SendImgMsg` 是发送不是接收。

---

## 五、附：已验证的复现脚本要点

```python
# 1. 找图片消息（正确配对需要 message_content 和 packed_info_data 两列）
rows = client.query_sql("message_0.db",
    "SELECT message_content, packed_info_data FROM Msg_<hash> "
    "WHERE local_type = 3 ORDER BY rowid ASC LIMIT 500 OFFSET N")

# 2. 解 XML 拿 aeskey
body = zstandard.ZstdDecompressor().decompress(
    binascii.unhexlify(mc), max_output_size=4 << 20)
aeskey = re.search(rb'aeskey="([0-9a-fA-F]{32})"', body).group(1).decode()

# 3. 从 packed_info_data 拿文件名
fname = re.search(rb"[0-9a-f]{32}", binascii.unhexlify(pk)).group(0).decode()

# 4. 定位文件
path = rf"{data_dir}\msg\attach\{msg_table_hash}\{YYYY-MM}\Img\{fname}_t.dat"

# 5. 已破解的部分：XOR 段
blob  = open(path, "rb").read()
aesz  = int.from_bytes(blob[6:10], "little")     # 1024
plain_tail = bytes(b ^ 0x6C for b in blob[15 + aesz:])
# → 结尾是 FF D9
# 未破解：blob[15 : 15+aesz] 需要 AES-ECB 解密，密钥未知
```

用到的第三方库：`zstandard`、`pycryptodome`、`pefile`、`capstone`、`numpy`（都已装进 .venv）。


## 缩略图缓存的实测覆盖率（2026-10-01 晚，从 CLAUDE.md 搬来）

**实测覆盖率（2026-10-01 晚）**：文件传输助手（`md5("filehelper")=9e20f478…`）
`cache\2026-09\…\Thumb` **1** 张、`2026-10\…\Thumb` **0** 张，而 `Bubble\` 里
**14** 个加密 `_b.dat`（当天 5 个）；全机 Thumb **913** vs Bubble **2176**。
结论：**「发给自己就能读聊天图」不成立**（最近发的基本都没有明文缩略图），
要读图就走 `file_read` 那条「**当文件发原图**」（见上面 `file_read.py`）。


含义：**「发给自己就能读聊天图」不成立**（最近发的基本都没有明文缩略图），要读图就走 `file_read` 那条「当文件发原图」。


## 非文本补捞的判据演进（2026-10-04，从 CLAUDE.md 搬来）

图片/语音/表情**不在 fts 里**（四个分片 `local_type=3` 全是 0），`_v4_new_messages_session`
又按 `summary` 过滤（图片是空串）——两条路都瞎。所以 `live_history._v4_pickup_nontext`
是唯一入口，它的判据改过两次，**别改回去**：

| 版本 | 判据 | 后果 |
|---|---|---|
| 最初 | `SessionTable.summary = ''` | 语音/表情/视频的 summary 非空 → 永远进不来（2026-10-03 真机：「我发了条语音，bot 完全没反应」） |
| 2026-10-03 | `last_msg_type NOT IN (1,49)` | **图后面紧跟一句话就被吞**：`last_msg_type` 已是 1，该会话当轮不回查 → 那张图**永久消失**（2026-10-04 真机：用户发图 + 紧接着问「图片里的价格怎么样」，bot 去别的群乱找） |
| **现在** | **会话只要有动静就回查它的消息表，再按行挑非文本** | 图不会再丢；文本/appmsg 由 fts 负责，这条路**按行丢掉**它们，所以同一条消息不会被答两遍 |

配套的两道闸：

- **稳态零开销**：`last_timestamp` 和每会话水位线 `cursors["__nonttext__"][talker]`
  相同 → 一个会话都不命中 → 一次消息表都不查。
- **一轮最多扫 `_NONTEXT_MAX_SESSIONS`(8) 个会话**（按时间从早到晚，防饿死），
  超出的写进 `cursors["__nonttext_pending__"]`，**下一轮不看 `since` 也照样扫**。
  为什么 pending 必须独立于 `since`：`__time__` 会被后续消息推着往前，一旦被闸门挡下的
  会话落到窗口之外，那张图就真丢了。

回归：`selftest_live_history._t_nonttext_pickup`（15 条，含「图后面跟一句话照样报」、
「同一批的文本不许重复报」、「洪水只扫 8 个 + pending 下一轮照样扫」）。


## 控制会话发来的图：`read_image` 不填参数就能读（2026-10-04）

用户在控制会话（文件传输助手）发一张图、紧接着问「图片里的价格怎么样」时，
那张图**不进消息流**：`bot.stash_control_media` 把它收进素材暂存区就 `continue` 了
（这是「发一次，之后说发给谁」那个设计）。所以模型手里**没有 local_id**，
但那张图的**明文副本就在本地**（`data/stash/`，来自微信刚发出去的临时原图）。

于是 `t_read_image` 加了一条：**两个参数都不填** → `_read_stashed_image()`
读暂存区里**最新那条、且 `talker` 和当前会话对得上**的图。三条规矩：

1. **只认控制会话那条**——别处的图必须点名 `contact` + `local_id`，
   否则模型会读到别处的图、答非所问；
2. 读不出来（没明文副本 / `image.mode=off` / OCR 失败）**如实说 + 建议用户
   把图「以文件方式」再发一次**，**绝不编图里的内容**；
3. 顺带修掉一个真 bug：老代码 `_image_collector(f"{nm} 的图")` 里的 `nm`
   **根本没定义**，于是「有缓存、能读」那条路一读就 `NameError`
   （没有测试覆盖，一直没被发现）。

回归：`selftest_policy.test_read_image`（6 条：缓存图能读 + 不崩、
不填参数读暂存区、别处的图不读、没明文时如实说、参数只给一半要拒绝）。


## ⚠️ 补漏的「新鲜度信号」以前是坏的：`session.db` **17 小时没落盘**（2026-10-04 晚）

**真机现场**：用户 16:42 在控制会话发了一条 **4 秒语音**，`bot.log` 里**没有任何一行**
跟它有关 —— 不是「读不出来」，是**根本没送到**（连 `[bot] 自聊模式，处理自己的消息: …`
都没打），bot 只回了后面那句文本「1」。语音本身好好的躺在消息表里：

```
Msg_9e20f478899dc29eb19741386f9343c8（= md5("filehelper")）
  local_id=848  local_type=34(语音)  create_time=16:42:55
  local_id=849  local_type=1 (文本)  create_time=16:47:55   ← 那句「1」
  local_id=850  local_type=1         create_time=16:48:04   ← bot 的回复
```

**根因：候选闸用的时钟是坏的。** `_v4_pickup_nontext` 当时是
`SELECT … FROM SessionTable WHERE last_timestamp >= since`，而 `since` 来自**消息**时钟
（`__time__`，16:56）。可是真机上：

| 库 | 最后落盘 | 里面最新的时间 |
|---|---|---|
| `session.db` / `-wal` | **2026-10-04 00:16:37** | `MAX(last_timestamp)=00:16:25` |
| `message_fts.db` | 16:56:13 | 消息到 16:56:23 |
| `message_0.db` | 17:12:54 | 消息到 16:57:28 |

于是停掉 bot、只读查询复现得很干脆：

```
SELECT username, last_timestamp, last_msg_type FROM SessionTable
  WHERE last_timestamp >= 1791104183   →  0 行      ← 当时真实的 since（16:56）
  WHERE last_timestamp >= 1791044185   →  1 行      ← 会话表自己的最大值（00:16）
```

**0 个候选会话 ⇒ 语音/图片/表情/视频全丢**，而且**连一行日志都没有**。
`data/state.json` 里 `__nonttext__` 那 74 条水位线**全是 10-03 及以前的**
（10-04 一条都没有）—— 这条补捞路从 00:16 起就死了。这就是早就记过的「吞图」。
（顺带一提：`session.db` 不落盘会让**所有**依赖它的路一起瞎，包括
`_v4_new_messages_session` 与 `_v4_new_messages_tables` 那两条兜底路。）

**修法：换成一个跟 session.db 无关、且天生新鲜的新鲜度信号 ——
`message_0.db` 的 `sqlite_sequence`。** 它是 SQLite 自己维护的 AUTOINCREMENT 计数器：
每个 `Msg_<md5(会话)>` 一行、`seq` 就是该会话**最新一条的 `local_id`**
（实测 855 = 该表最新行的 local_id，含那条语音）。一次查询拿到**全部**会话，且随每次插入更新。

| | 旧（坏） | 新 |
|---|---|---|
| 新鲜度信号 | `SessionTable.last_timestamp`（依赖落盘） | `sqlite_sequence.seq`（= 最大 `local_id`） |
| 水位线 | `cursors["__nonttext__"]`（会话时钟秒） | `cursors["__nonttext_seq__"]`（**local_id**） |
| 判据 | `last_timestamp >= since`（**全局时间窗**） | `seq > 水位线`（**每会话精确**） |
| 时间窗 | 必须靠它挑候选 | **彻底不要**（见下） |

**「本批里别的会话来了条更晚的消息」再也推不动它**（旧实现拿 `__time__` 当窗口，
正是这样把整条会话排除掉的）：判据只有每会话自己的 `local_id`。只有**还没打过基线**的
会话（第一次跑 / `state.json` 丢了 / 刚出现的会话）才用
`time.time() - _NONTEXT_BOOTSTRAP_WINDOW`（默认 1800 秒）兜一道 —— 只认刚发生的，
绝不把历史回放一遍。`__nonttext__` 那份旧水位线**已退役并直接删掉**（两个 owner
说两套话正是要避免的）。

⚠️ **别改回去什么**：

1. **不许**把候选判据换回 `SessionTable.last_timestamp`（或任何依赖 session.db 落盘的列）
   —— 那正是这个 bug（1 行日志都没有的那种）。
2. `_NONTEXT_BOOTSTRAP_WINDOW` 那道兜底闸**不许删**：删了会在「第一次跑 / state 丢了」时
   把每个会话最后 10 条历史里的图片语音全当新消息回放。
3. 水位线必须是 **local_id 语义**（`__nonttext_seq__`）：换成时间戳就又回到「精确不了」。
4. 一轮 8 个会话 + `__nonttext_pending__` 两道闸**不许删**（hook 不支持并发）。
5. 这一条修的是**送达**，不是**转写**：送达之后能不能读出字，仍然由 `voice_mem`
   （内存里找 SILK + 长度指纹 + `voice.scan_seconds`）说话，读不出来照样如实说
   （见 `voice-reliability-2026-10-03.md`）。**音频已经不在内存的旧语音谁也救不回来** ——
   上面那条 16:42 的语音，没读出来就是因为这个，不是因为送达。

回归：`selftest_live_history._t_nonttext_pickup`（16 条，水位线 = 最大 local_id /
旧水位线退役 / 图后面跟一句话照样报 / 洪水只扫 8 个 + pending 照样扫）、
`_t_poll_window_keeps_nontext`（本批那条晚 1 小时的消息**推不动**语音）。
真机只读侦察脚本：`_audit/probe_voice_drop.py`。

### 同一条信号也用在了**兜底收消息路**上（2026-10-04 晚，同一场事故）

真机同时暴露了第二件事：`session.db` 与 `message_fts.db` 的**句柄一起失效**
（`get database handle which named … failed`，`force_rescan` 无效）时，
`_v4_new_messages_tables`（没有 fts 时的退路）拿 `_v4_active_talkers`（= session.db）
当「谁有新消息」的信号 —— 于是候选为空，**bot 完全收不到消息，连文本都收不到**，
而 `message_0.db` 明明好好的。

改法：那条路的候选改成**两个来源取并集** —— 老来源（`SessionTable.last_timestamp`，
session.db 活着时行为一字不变）∪ `sqlite_sequence`（跟落盘无关、永远新鲜），
每个会话一份 `__msg_seq__` 水位线（local_id 语义）。
⚠️ **第一次只打基线、不回放历史**（`[live] 已为 N 个会话建立 local_id 基线`）：
不然 ~290 个会话会在同一轮里各查一次消息表（把 hook 压死），还会把几十分钟的积压重放。
代价说清楚：基线之前那批（停机/瞎掉期间的消息）**不会被这条路补报** ——
它们本来也只该「只通知、不自动回复」。

回归：`selftest_live_history._t_fallback_survives_dead_session_db`
（第一次只打基线 / 之后 text + 语音条都收得到 / 不重复报）。

