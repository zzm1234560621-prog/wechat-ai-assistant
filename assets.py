"""控制会话的「素材暂存区」：发一次图/表情，之后说「发给谁」就能再发。

用户要的能力：在文件传输助手（控制会话）里发一张图或一个表情，之后只说一下
「发给张三」就把它发出去，想发几次发几次。

做法：控制会话来了图片/表情/视频，bot 把那条消息的**原始 XML** 存在这里
（`live_history.message_xml` 取、`agent_tools.t_send_asset` 发）。存的是 XML、
不是图片副本。

**为什么走转发 XML、而不是复制一份图片文件**（定下来的方案，别改回去）：
  * 用户**自己发出去**的图，微信在磁盘上只有 AES 加密的 `.dat`，那把密钥项目里
    没拿到（`docs/wechat4-dat-image-notes.md` 的全部结论都在说这件事）；
  * 明文顶多有微信渲染过的**缩略图**，而且表情包基本没有；
  * 转发原始 XML 不需要解密，原图和动图都保留。

**代价（上层的话术必须照这个来）**：hook 的 `/ForwardXMLMsg` 成功也回 `ret:0`
（见 `aixed_api.send_xml` 的说明），所以「转发其实没成」没法自动发现。回给用户的
只能是「已提交发送」，**不许**说成「对方一定收到了」；真机验收要肉眼确认一次。

落盘 `data/assets.json`。**故意不并进 `data/state.json`**：那份是「轮询游标 +
待确认队列」的单一真源（CLAUDE.md 的硬规矩），素材库是另一码事，混进去会把那条
规矩变糊。文件坏了/读不出来只告警、当空——状态文件不该挡住启动（和 state.json
同一姿势）。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "data", "assets.json")

# 默认容量：够用，又不至于让「发给谁」变成要先挑半天（用户定的 5 条）。
CAP_DEFAULT = 5

# 素材类型 -> 量词。给用户看的话要像人话：「那张图」「第 2 个表情」。
_UNIT = {"图片": "张图", "表情": "个表情", "视频": "个视频"}


def _warn(msg):
    """打一条必须被看见的告警。走 stderr：bot.log / 控制台都收得到。

    故意不引 logging——项目里全是 print，引 logging 会改变日志形状（和
    agent_tools._warn 同一个理由）。重点是**不能静默**。
    """
    print(f"⚠️ 素材暂存区：{msg}", file=sys.stderr)


def _key(item):
    """素材的去重键。**有 local_id 用 local_id，没有就用文件名**。

    为什么不能只看 `(talker, local_id)`：明文文件那条路（Route C）的素材**可能没有
    local_id**（用户以「文件」方式发来的图，我们按文件名定位），全都算成
    `(talker, "")` 会互相顶掉——第二条一进来就把第一条当重复删了。
    """
    t = str((item or {}).get("talker") or "")
    lid = str((item or {}).get("local_id") or "")
    if lid:
        return (t, "id:" + lid)
    return (t, "path:" + os.path.basename(str((item or {}).get("path") or "")))


def load(path=None):
    """读暂存区，返回列表（**最新的在末尾**）。文件不在/坏了 -> 空列表 + 告警。

    只留**发得出去**的条目：`xml`（原始消息引用，等 hook 的转发修好才有用）或
    `path`（明文图片文件，Route C，用已验证的 send_image 发）。两样都没有的条目
    留在列表里只会让「发给谁」拿到一个发不出去的东西。
    """
    p = path or PATH
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        _warn(f"读不了 {p}（{e}），这次当空的用。")
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        _warn(f"{p} 里的形状不对（不是列表），这次当空的用。")
        return []
    return [it for it in items
            if isinstance(it, dict) and (it.get("xml") or it.get("path"))]


def save(items, path=None):
    """原子写（同目录临时文件 + os.replace，和 state.json 同一姿势）。

    写失败只告警、**绝不抛**：记不住素材是小事，让收消息那条链断掉是大事
    ——内存里的暂存区这轮照样能用。
    """
    p = path or PATH
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": list(items or [])}, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError as e:
        _warn(f"写不了 {p}（{e}），这次的素材只在这轮里有。")


def entry_from_media(m, xml, now=None):
    """`live_history.latest_media()` 的一行 + 原始 XML -> 一条暂存素材。

    `image`（微信的明文缩略图，可能为 None）在这条路上**也是能发的**：
    转发接口坏掉之后（见 `wx_send_xml.cpp` 里那段「安全拒绝」的注释），
    发图只能靠明文，所以 `plaintext_of()` 会优先用它。
    """
    return {
        "kind": str((m or {}).get("kind") or ""),
        "talker": str((m or {}).get("talker") or ""),
        "local_id": str((m or {}).get("local_id") or ""),
        "local_type": (m or {}).get("local_type"),
        "xml": xml,
        "image": (m or {}).get("image"),
        "path": "",
        "source": "xml" if xml else "",
        "msg_time": (m or {}).get("time") or "",
        "ts": float(now if now is not None else time.time()),
    }


def entry_from_file(path, kind="图片", talker="", local_id="", now=None):
    """把一份**明文图片文件**收成素材（转发坏掉之后唯一真正发得出去的来源）。

    `path` 必须是**微信自己落盘的明文**（`msg/file/<月>/<原名>`），由调用方用
    `file_read.locate()` 解析出来——**不接受任何从模型/聊天内容里来的路径**。
    """
    p = str(path or "")
    return {
        "kind": str(kind or "图片"),
        "talker": str(talker or ""),
        "local_id": str(local_id or ""),
        "local_type": None,
        "xml": "",
        "image": None,
        "path": p,
        "source": "file",
        "name": os.path.basename(p),
        "msg_time": "",
        "ts": float(now if now is not None else time.time()),
    }


def plaintext_of(item):
    """这条素材有没有**能直接发出去的明文图片**。没有返回 ""。

    优先 `path`（原图），退到 `image`（微信缓存的缩略图）。
    **只认真的还在磁盘上的文件**——微信的缓存会被清理，条目还在、文件没了的情况
    必须让它发不出去（如实报错），而不是让 send_image 去撞一个不存在的路径。
    """
    for k in ("path", "image"):
        p = str((item or {}).get(k) or "")
        if p and os.path.isfile(p):
            return p
    return ""


def stash(item, cap=CAP_DEFAULT, path=None):
    """放一条素材进去并落盘。返回 (items, added, dropped)。

    * `added=False`：这条已经在暂存区里（同会话同 local_id，没有 id 时按文件名）——
      同一张图被重复报上来（重启后游标回退、补漏路径重放）不会叠成两条。
    * `dropped`：这次顶掉了最老的几条（容量是用户定的，静默丢弃不允许——调用方
      要把这件事说出来）。
    * 既没有 xml 也没有明文路径的条目**直接拒绝**（抛 ValueError）：那种条目存下去也
      转发不了，混进暂存区等于让后面「发给谁」拿到一个发不出去的东西。
    """
    it = item or {}
    if not (it.get("xml") or it.get("path")):
        raise ValueError("素材既没有 xml 也没有明文路径，不存")

    items = load(path)
    dup = any(_key(it) == _key(item) for it in items)
    if dup:
        items = [it for it in items if _key(it) != _key(item)]
    items.append(item)

    cap = max(1, int(cap or CAP_DEFAULT))
    dropped = max(0, len(items) - cap)
    if dropped:
        items = items[-cap:]
    save(items, path)
    return items, (not dup), dropped


def cap_of(cfg):
    """从配置里解析容量 `assets.max_items`。**唯一一处**钳制逻辑。

    写歪了只告警 + 钳制（1~20），不静默放大——和 `agent.max_queries` 同一姿势：
    配置写 9999 不该真的变成「存 9999 条」。
    """
    raw = ((cfg or {}).get("assets") or {}).get("max_items", CAP_DEFAULT)
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = CAP_DEFAULT
    fixed = max(1, min(cap, 20))
    if fixed != cap:
        _warn(f"assets.max_items={raw} 越界，已钳制为 {fixed}（允许 1~20）")
        cap = fixed
    return cap


def pick(items, which=None):
    """按序号取素材。返回 (item, 错误文本)。

    `which` 的语义：**1 = 最近一条**（用户只说「发给谁」时的默认），2 = 更早一条，
    依次类推。None / 空 / "最近" / "最新" 一律当 1。

    越界、不是整数、暂存区是空的都返回 (None, 人话错误)——**绝不静默退回最近一条**：
    用户说了「第 2 张」而拿了第 1 张，就是发错东西。
    """
    items = list(items or [])
    if not items:
        return None, ("素材暂存区是空的。请先在文件传输助手里发一张图或一个表情，"
                      "再说「发给谁」。")
    if which in (None, "", "最近", "最新", "刚", "刚才", "上一个", "上一张"):
        return items[-1], None
    try:
        n = int(which)
    except (TypeError, ValueError):
        return None, f"「{which}」不是素材编号。说「第几张」或「最近一张」。"
    if n < 1 or n > len(items):
        return None, f"暂存区里只有 {len(items)} 条素材，没有第 {n} 条。"
    return items[-n], None


def label(item, rank=1):
    """给用户看的指代：「那张图」/「第 2 个表情」。"""
    unit = _UNIT.get(str((item or {}).get("kind") or ""), "条素材")
    return f"那{unit}" if int(rank or 1) <= 1 else f"第 {int(rank)} {unit}"


def this_label(kind):
    """「刚收到的这一条」的指代：这张图 / 这个表情 / 这个视频。

    回执要说人话：「已暂存**这张图**」而不是「已暂存这张图片」「已暂存这张表情」——
    量词按类型走（顺手把中文写对，用户才不会觉得是机器在瞎说）。
    """
    return f"这{_UNIT.get(str(kind or ''), '条素材')}"


def rank_of(items, item):
    """这条素材是「第几条」（1 = 最近）。找不到返回 None。"""
    for i, it in enumerate(reversed(list(items or [])), 1):
        if _key(it) == _key(item):
            return i
    return None


def list_lines(items):
    """`/素材` 的人话清单。编号就是 `pick` 认的 which。"""
    items = list(items or [])
    if not items:
        return ["素材暂存区是空的。在文件传输助手里发一张图或一个表情，我就记下来。"]
    lines = [f"素材暂存区（{len(items)} 条，发新的会顶掉最老的）："]
    for rank, it in enumerate(reversed(items), 1):
        lines.append(f"  {rank}) {it.get('kind') or '素材'}（{_ago(it.get('ts'))}）")
    lines.append("说「发给张三」用第 1 条；要更早的就带上编号，例如「发给张三 第2张」。")
    return lines


def clear(path=None):
    """清空暂存区，返回清掉几条。"""
    n = len(load(path))
    save([], path)
    return n


def _ago(ts):
    try:
        d = max(0.0, time.time() - float(ts or 0))
    except (TypeError, ValueError):
        return "时间未知"
    if d < 60:
        return "刚刚"
    if d < 3600:
        return f"{int(d // 60)} 分钟前"
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    return f"{int(d // 86400)} 天前"
