# 微信 4.1.10.27：**自己发出去的图**的明文原图在哪（2026-10-01 实测）

结论先行：用户**在微信界面里发图**时，微信会把**原图明文**暂存在

```
<微信数据目录>\<账号>\temp\RWTemp\<YYYY-MM>\<某个哈希>\<32 位 hex>.<jpg|png|gif…>
```

这是「自己发出去的图」**唯一**能拿到的明文——正式落盘的只有加密 `.dat`，
`cache\<月>\Message\<hash>\Thumb\` 里也**常常没有**（本机 513 / 521 两条都没有）。

## 实测记录（别靠猜，这条是量出来的）

| 事实 | 实测值 |
|---|---|
| 触发 | 2026-10-01 23:19，用户在微信界面里往「文件传输助手」发了一张图 |
| 明文文件 | `temp\RWTemp\2026-10\c59d52c9…\9c1cfe73….jpg`，**45352 字节**，头 `FF D8 FF E0 00 10 JFIF`（真 JPEG） |
| 创建时间 | **23:19:41**；而消息行 `create_time` 是 **23:19:43** —— **明文比消息行早约 2 秒** |
| 同刻的正式落盘 | `msg\attach\<hash>\<月>\Img\<hex>.dat`（**加密** V2；`size = cdnthumblength + 31`） |
| `cache/Thumb` | **没有**这条的缩略图 |
| 临时目录的寿命 | 实测里面**只剩最新那一张**（老的会被清理）→ 必须**收到消息就复制走** |
| 能不能按名字配对 | **不能**：文件名 hex ≠ 内容 md5（`8982174b…`）≠ XML 的 `md5`（`4ebeb785…`），也不出现在 XML 里 |

## 关联规则（**必须按时间，不能拿「最新那个」**）

取「**创建时间 ≤ 消息时间 + 2 秒**、且在 90 秒窗口内**最新**」的那一个。

为什么不能图省事拿最新那个：用户完全可能**刚给张三也发了一张**，那张的创建时间在消息
**之后**，按「最新」挑就会**发错图**——而发消息**不可逆**。

实现：`image_cache.sent_plaintext_candidates(msg_ts)`（列目录 + 魔数确认）
→ `agent_tools.pick_plaintext(cands, msg_ts)`（纯函数，好测）
→ `agent_tools.capture_sent_plaintext(...)`。

## 落地（谁负责哪一段）

| 位置 | 职责 |
|---|---|
| `image_cache.sent_plaintext_candidates()` | 只读列 `temp/RWTemp`，按窗口 + 魔数过滤，返回 `[(ctime, path)]` |
| `agent_tools.pick_plaintext()` | 纯函数：按上面那条规则挑一份（Windows 上没法伪造 ctime，所以它单独可测） |
| `agent_tools.capture_sent_plaintext()` | 挑一份 → `assets.keep_file()` 复制进 `data/stash/` → 生成素材条目 |
| `assets.keep_file()` | **复制**（不移动、不碰微信目录），按**内容 md5**命名，天然去重 |
| `bot.stash_control_media()` | 控制会话收到「自己发的图片」时，**优先**走这条；退路是缓存缩略图 → 消息引用 |
| `agent_tools.t_send_asset()` | 有明文就 `send_image()` 发（唯一能真正发出去的路，见 `hook-forward-xml-broken.md`） |

## 边界与坑

- **只读微信目录**：只复制，不移动、不删、不改。明文是临时文件，复制走之后微信怎么清理都不影响素材。
- **副本会跟着素材一起清**：`assets._sweep_stash()` 在「顶掉最老的素材」和 `/素材 清空`
  时，把 `data/stash/` 里**连真源也不引用**的副本删掉（不然就是「用户每发一张图，硬盘永久留一份」）。
  它**只删暂存目录第一层**，别处的路径一根手指都不碰；而且判定「有人引用」时会**连
  `data/assets.json` 真源一起看**——只看调用方那批的话，自测会把真机上刚收的副本删掉（踩过）。
- **发送时的白名单**：素材暂存目录**不进** `allowed_image_dirs()`（那个函数的语义是
  「用户配的目录 + 微信缓存根」，不该多一个人为入口）。`send_pending()` 里单独放行
  暂存目录的路径——理由是那些路径**由 bot 复制时记下、不是模型填的**；别的路径照旧
  整批过白名单（回归：`selftest_assets` 的两条「暂存目录放行 / 非暂存目录拒发」）。
- **回执要说清这张发不发得出去**：有明文才说「说发给谁我就发」；只有消息引用时说
  「这张发不出去」并给出办法。不许让用户以为「已暂存 = 随时能发」。
- **表情包 / 视频未验证**：`capture_sent_plaintext` 目前只在 `kind == "图片"` 时调用
  （表情在 RWTemp 里有没有明文、`SendImgMsg` 认不认 gif 都还没测）。
- `send_image`（`/SendImgMsg`）**成功也无条件回 `ret:0`**，所以真机验收必须
  **读库确认新增了图片消息**（或肉眼看微信里有没有出现），不能只看返回值。
- **自测必须把 `assets.STASH_DIR` 指到临时目录**（`selftest_assets.main()` / 
  `selftest_bot_loop.t_stash_media` 都做了）：链路里会 stash/clear，而 clear 现在会删副本
  ——不指开就会删真机上刚收的那张（踩过一次）。

## 验收记录（2026-10-01 23:3x）

```
latest_media(filehelper) -> local_id=523 ts=1790867983
capture_sent_plaintext() -> 明文原图 9c1cfe73….jpg（45352 B）
                         -> data\stash\8982174b29a814c0b99909e3c6203236.jpg
assets.stash()           -> local_id=523 可发=True（同一条的「只有引用」版本被它顶掉）
SendImgMsg               -> {'ret': 0, 'retmsg': 'success'}
读库                     -> 新增 local_id=525 图片消息  ✅ 真的到了
```

回归用例：`selftest_assets.test_plaintext_capture`（窗口/魔数/挑错/复制/端到端）、
`selftest_bot_loop.t_stash_media` 的「明文原图被收下」两项。
