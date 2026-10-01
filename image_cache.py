"""微信 4.x「已解码图片」的磁盘缓存查找。

**为什么需要这个模块**

聊天里的图片在磁盘上是加密的 `.dat`：
    15 字节头 + 前 1024 字节 AES(固定密钥) + 其余 XOR 0x6C
XOR 段和文件头都已经破出来了，但**前 1024 字节那把 AES 密钥没拿到**
（静态分析到头了，见 docs/wechat4-dat-image-notes.md）。所以 .dat 解不开。

但是——**微信自己会把「渲染过的」图片缩略图以明文缓存在磁盘上**：

    <微信数据目录>\cache\<YYYY-MM>\Message\<md5(会话名)>\Thumb\
        <local_id>_<create_time>_thumb.jpg

`local_id` 和 `create_time` **就是 `Msg_<md5(会话名)>` 表里的同名两列**，
所以能直接对上。这条路完全不需要解密。

实测（2026-10-01）：缓存文件 `3661_1788747187_thumb.jpg` ↔ 表里
`local_id=3661, create_time=1788747187`，逐字吻合。

**局限（很重要，回答用户时要如实说）**

* 只有**你滚动过 / 看过的**图片才会被缓存，覆盖率不高
* 是**缩略图**，不是原图
* 缓存目录的月份是「渲染时间」不是「消息时间」，所以必须扫所有月份，
  不能拿 create_time 去推月份
* 有些文件内容其实是 PNG 但后缀写成 `.jpg`，判断类型要看魔数不要看后缀
"""
import os
import re
import time

# 缓存索引按 (账号目录, 会话哈希) 缓存，带 TTL：
# 用户一边滚微信一边缓存会变，进程内永久缓存会看不到新图。
_INDEX_CACHE = {}
_INDEX_TTL = 120.0

_THUMB_RE = re.compile(r"^(\d+)_(\d+)_thumb\.[A-Za-z0-9]+$")


def _home():
    return os.path.expanduser("~")


def data_root():
    """微信 4.x 数据根目录（xwechat_files）。找不到返回 None。"""
    p = os.path.join(_home(), "Documents", "xwechat_files")
    return p if os.path.isdir(p) else None


def account_dirs():
    """所有账号目录（形如 wxid_xxxx_1234）。"""
    root = data_root()
    if not root:
        return []
    out = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.isdir(os.path.join(d, "cache")):
            out.append(d)
    return out


def image_cache_dirs():
    """**真正的图片缓存根目录**——把发图白名单限制到「聊天里已有的图」本身。

    为什么要单独有个函数：`data_root()` 是**整个**微信数据目录
    （`~/Documents/xwechat_files`），里面除了图片缓存还有配置、db_storage、
    msg 收来的文件等等。发图的默认白名单如果直接用 data_root()，等于把整个
    微信数据目录都放行了，和「只放行图片缓存」这句话完全不是一回事。

    这个路径不是猜的，是按本模块真正扫描缩略图的路径推出来的：见 cache_index()，
    它扫的是 `<账号>/cache/<YYYY-MM>/Message/<md5>/Thumb/`，所以**图片缓存根
    就是 `<账号>/cache`**——月份目录在它下面，thumb 也在它下面。

    （同名兄弟目录 Emoticon / HttpResource / Sns / WeAppIcon 也在这个 cache 下，
    都算微信自己解码出来的缓存图片，一并放行是合理的。）

    返回账号 cache 目录的**列表**（多账号时全都要放行）；一个都没有就返回 []，
    调用方必须自己决定怎么兜底，**别在这里偷偷放宽**。
    """
    return [os.path.join(d, "cache") for d in account_dirs()]


def cache_index(chat_hash, account=None, ttl=_INDEX_TTL):
    """建索引 {(local_id, create_time): 文件路径}，跨所有月份。

    local_id / create_time 用**字符串**，和数据库里取出来的一致，
    免得 int/str 混用对不上。
    """
    accounts = [account] if account else account_dirs()
    key = (tuple(accounts), chat_hash)
    hit = _INDEX_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    idx = {}
    for acct in accounts:
        cache = os.path.join(acct, "cache")
        if not os.path.isdir(cache):
            continue
        for month in os.listdir(cache):
            thumb = os.path.join(cache, month, "Message", chat_hash, "Thumb")
            if not os.path.isdir(thumb):
                continue
            try:
                names = os.listdir(thumb)
            except OSError:
                continue
            for f in names:
                m = _THUMB_RE.match(f)
                if m:
                    idx[(m.group(1), m.group(2))] = os.path.join(thumb, f)

    _INDEX_CACHE[key] = (time.time(), idx)
    return idx


def find(chat_hash, local_id, create_time, account=None):
    """找某条消息对应的已解码缩略图路径；没有返回 None。"""
    if not chat_hash or local_id in (None, "") or create_time in (None, ""):
        return None
    idx = cache_index(chat_hash, account)
    return idx.get((str(local_id), str(create_time)))


def image_kind(path):
    """按**魔数**判断图片类型（后缀不可信，实测有 PNG 存成 .jpg）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"GIF8":
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def cache_stats():
    """统计一下缓存规模，给用户解释覆盖率时用。"""
    idx_all = {}
    total = 0
    for acct in account_dirs():
        cache = os.path.join(acct, "cache")
        if not os.path.isdir(cache):
            continue
        for month in os.listdir(cache):
            base = os.path.join(cache, month, "Message")
            if not os.path.isdir(base):
                continue
            for h in os.listdir(base):
                thumb = os.path.join(base, h, "Thumb")
                if not os.path.isdir(thumb):
                    continue
                n = sum(1 for f in os.listdir(thumb) if _THUMB_RE.match(f))
                if n:
                    idx_all[h] = idx_all.get(h, 0) + n
                    total += n
    return {"chats": len(idx_all), "files": total, "by_chat": idx_all}
