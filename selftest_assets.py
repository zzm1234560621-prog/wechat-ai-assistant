"""素材暂存区（assets.py）+ send_asset 工具 + 「转发连发」的回归自测。

**不联网、不碰 30001、不需要微信**：全部是假客户端 + 临时文件 + 纯函数
（`latest_media` 那条用假 query_sql，并把 image_cache 的账号目录临时屏蔽，
免得测试结果取决于本机微信缓存里恰好有什么图）。

覆盖 2026-10 这轮的新功能（「在文件传输助手发一次图/表情，之后说发给谁就能发」）：

  1. `live_history.media_kind`：图片/表情/视频（含 appmsg 编码）认得出来，
     语音/文件/链接这些**转发不了的类型一律不认**（不许收进暂存区骗用户）。
  2. `live_history.latest_media`：只查 Msg_ 表、按 local_id 倒序、挑出媒体、
     标出 is_self；3.9.x（没有 contact.db）返回空。
  3. `assets`：落盘/读回、容量顶掉最老的、重复暂存不叠两条、按序号取、
     **序号越界绝不静默退回最近一条**、坏文件只告警不当掉。
  4. `send_asset` 工具：名单内直接转发、名单外只登记待确认（带 label）、
     连发受 `agent.max_send_count` 钳制、暂存区空着时如实报错、
     发到一半失败要带上「已经发出 N 条」。
  5. `send_pending` 对转发认 `count`（素材那条路要连发），而 `forward_message`
     那条路仍然只发一次（它是另一件事）。
  6. 补存（`_sync_latest_asset`）：「图 + 文字挤在同一个轮询间隔」时也要发得出去；
     取不到那张更新图的原文时**一张都不发**，绝不退回发旧的那张。

用法：python selftest_assets.py
"""
import json
import os
import sys
import tempfile
import time

import agent_tools
import assets
import live_history

SELF_WXID = "wxid_self_0001"
CHAT = "filehelper"
XML_IMG = "<msg><img aeskey=\"deadbeef\" md5=\"abc\" /></msg>"
XML_OLD = "<msg><img aeskey=\"cafebabe\" md5=\"def\" /></msg>"

_ok = True


def check(label, cond, extra=""):
    global _ok
    ok = bool(cond)
    _ok = _ok and ok
    print(f"  {'✅' if ok else '❌'} {label}{('  ' + str(extra)) if extra and not ok else ''}")
    return ok


class _Rec:
    """假客户端：只记账发出去什么，不碰任何网络（同 selftest_policy 的姿势）。"""

    def __init__(self, boom_at=None):
        self.calls = []
        self.boom_at = boom_at

    def _hit(self, kind, wxid, payload):
        if self.boom_at is not None and len(self.calls) + 1 == self.boom_at:
            raise RuntimeError("模拟发送失败（第 %d 次）" % self.boom_at)
        self.calls.append((kind, wxid, payload))

    def send_text(self, msg, wxid):
        self._hit("text", wxid, msg)

    def send_image(self, path, wxid):
        self._hit("image", wxid, path)

    def send_xml(self, xml, wxid):
        self._hit("xml", wxid, xml)


_ZERO = [{"1": 1}]


class _V4Stub:
    """只认 latest_media 要用的那几条查询的假客户端。

    * `contact.db` -> is_wechat4 的探针
    * `message_0.db` -> 分片探针 / Name2Id 取自己的 rowid / Msg_<hash> 取行
    其它库一律抛异常（`_v4_msg_dbs` 就是靠抛异常把 message_1..7 滤掉的）。
    """

    def __init__(self, rows=None, self_rowid=7, has_v4=True):
        self.rows = rows or []
        self.self_rowid = self_rowid
        self.has_v4 = has_v4
        self.queries = []

    def query_sql(self, db, sql):
        self.queries.append((db, sql))
        s = " ".join(str(sql).split())
        if db == "contact.db":
            if not self.has_v4:
                raise RuntimeError("没有 contact.db（3.9.x）")
            return _ZERO
        if db == "message_0.db":
            if "sqlite_master" in s:
                return _ZERO
            if "FROM Name2Id" in s:
                return [{"rowid": self.self_rowid}]
            if s.startswith("SELECT local_id, local_type, real_sender_id, create_time FROM Msg_"):
                return self.rows
            raise RuntimeError("意料之外的 SQL: " + s)
        raise RuntimeError("没有这个库: " + db)


def _box(cfg=None, contacts=None, client=None):
    return agent_tools.ToolBox(client or _Rec(), cfg or {"agent": {}},
                               contacts or [], SELF_WXID, CHAT)


def _cfg(**agent):
    base = {"agent": {"max_send_count": 2, "send_interval": 0,
                      "auto_send_whitelist": ["张三"]}}
    base["agent"].update(agent)
    return base


def _item(lid, kind="图片", xml=XML_IMG, ts=1000.0, talker=CHAT):
    return {"kind": kind, "talker": talker, "local_id": str(lid), "local_type": 3,
            "xml": xml, "image": None, "msg_time": "", "ts": ts}


def _write(path, data=b"\x89PNG\r\n\x1a\n0123"):
    """造一个「明文图片文件」，用来验明文那条路（只要文件真实存在就够）。"""
    with open(path, "wb") as f:
        f.write(data)
    return path


# ------------------------------------------------------- 1. media_kind

def test_media_kind():
    print("\n── media_kind：认得能转发的媒体，不认转发不了的 ──")
    check("图片 local_type=3", live_history.media_kind(3) == "图片")
    check("视频 local_type=43", live_history.media_kind(43) == "视频")
    check("表情 local_type=47", live_history.media_kind(47) == "表情")
    check("appmsg 图片 (5<<32)|49", live_history.media_kind((5 << 32) | 49) == "图片")
    check("appmsg 表情 (8<<32)|49", live_history.media_kind((8 << 32) | 49) == "表情")
    check("appmsg 视频 (44<<32)|49", live_history.media_kind((44 << 32) | 49) == "视频")
    check("文本 local_type=1 不收", live_history.media_kind(1) == "")
    check("语音 local_type=34 不收（转不了）", live_history.media_kind(34) == "")
    check("appmsg 文件 (6<<32)|49 不收", live_history.media_kind((6 << 32) | 49) == "")
    check("appmsg 链接/引用不收", live_history.media_kind((57 << 32) | 49) == "")


# ---------------------------------------------------- 2. latest_media

def test_latest_media():
    print("\n── latest_media：只读 Msg_ 表、倒序挑媒体、标出 is_self ──")
    live_history.set_self_wxid(SELF_WXID)
    rows = [
        {"local_id": 100, "local_type": 1, "real_sender_id": 7, "create_time": 1000},
        {"local_id": 99, "local_type": 3, "real_sender_id": 7, "create_time": 990},
        {"local_id": 98, "local_type": (5 << 32) | 49, "real_sender_id": 8,
         "create_time": 980},
        {"local_id": 97, "local_type": 47, "real_sender_id": 7, "create_time": 970},
        {"local_id": 96, "local_type": 34, "real_sender_id": 8, "create_time": 960},
    ]
    stub = _V4Stub(rows)

    # image_cache 会去扫真实账号目录——把账号列表临时清空，让结果与本机缓存无关
    import image_cache
    real_accounts = image_cache.account_dirs
    image_cache.account_dirs = lambda: []
    try:
        got = live_history.latest_media(stub, CHAT, limit=3)
    finally:
        image_cache.account_dirs = real_accounts

    check("只挑出 3 条媒体（文本和语音都不算）", len(got) == 3, got)
    check("最近的在最前", [g["local_id"] for g in got] == ["99", "98", "97"],
          [g["local_id"] for g in got])
    check("自己发的图 is_self=1", got[0]["is_self"] == 1)
    check("别人发的 appmsg 图 is_self=0", got[1]["is_self"] == 0)
    check("类型名带下来", [g["kind"] for g in got] == ["图片", "图片", "表情"])
    check("没有可解码缩略图时 image=None（不假装有图）", got[0]["image"] is None)
    check("只碰了 contact.db / message_N.db（没去别处瞎查）",
          all(db == "contact.db" or db.startswith("message_")
              for db, _s in stub.queries),
          sorted({db for db, _s in stub.queries}))
    check("确认是探到 message_0.db 就停手了（分片探测是既成姿势）",
          "message_0.db" in {db for db, _s in stub.queries})

    v3 = _V4Stub(rows, has_v4=False)
    check("3.9.x（没有 contact.db）返回空，由调用方如实报错",
          live_history.latest_media(v3, CHAT) == [])


# --------------------------------------------------------- 3. 暂存区

def test_store(tmp):
    print("\n── 暂存区：落盘 / 容量 / 去重 / 按序号取 ──")
    p = os.path.join(tmp, "assets.json")
    check("文件不存在时是空的", assets.load(p) == [])

    items, added, dropped = assets.stash(_item(1), cap=2, path=p)
    check("第一条：added=True 没顶掉东西", added and dropped == 0)
    check("读回来一条", len(assets.load(p)) == 1)

    assets.stash(_item(2, ts=1001.0), cap=2, path=p)
    items, added, dropped = assets.stash(_item(3, ts=1002.0), cap=2, path=p)
    check("满了以后顶掉最老的", dropped == 1 and [i["local_id"] for i in items] == ["2", "3"],
          items)

    items, added, dropped = assets.stash(_item(2, ts=1003.0), cap=2, path=p)
    check("重复暂存同一条：added=False 且不叠两条",
          (not added) and len(items) == 2 and [i["local_id"] for i in items] == ["3", "2"],
          items)

    check("按序号 1 = 最近一张", assets.pick(items, None)[0]["local_id"] == "2")
    check("按序号 2 = 更早一张", assets.pick(items, 2)[0]["local_id"] == "3")
    it, err = assets.pick(items, 3)
    check("序号越界**不静默退回最近一条**", it is None and "第 3 条" in err, err)
    it, err = assets.pick(items, "第二张")
    check("给个不是数字的编号也给错误", it is None and err, err)
    it, err = assets.pick([], None)
    check("空暂存区的错误要说「先发一张」", it is None and "先" in err, err)

    check("label 说人话（最近）", assets.label(_item(1), 1) == "那张图")
    check("label 带编号", assets.label(_item(1), 2) == "第 2 张图")
    check("表情的量词", assets.label(_item(1, kind="表情"), 1) == "那个表情")
    check("rank_of 能反查第几条", assets.rank_of(items, items[-1]) == 1)

    lines = assets.list_lines(items)
    check("/素材 清单第一条是编号 1", lines[1].strip().startswith("1)"))
    check("/素材 清单不出现 wxid", "wxid" not in "\n".join(lines))

    check("清空返回条数", assets.clear(p) == 2 and assets.load(p) == [])

    # 坏文件：只告警、当空，不许抛（状态文件不该挡住启动）
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    check("坏文件当空的用（不抛异常）", assets.load(p) == [])
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"items": "形状不对"}, f)
    check("形状不对也当空的用", assets.load(p) == [])
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"items": [{"kind": "图片"}, _item(9)]}, f)
    check("既没有 xml 也没有明文的条目读出来时丢掉（存了也发不出去）",
          [i["local_id"] for i in assets.load(p)] == ["9"])

    # 明文条目（Route C）：只有 path、没有 xml，也必须能读回来、也必须发得出去
    img = _write(os.path.join(tmp, "明文图.jpg"))
    if os.path.exists(p):
        os.remove(p)
    items, added, dropped = assets.stash(
        assets.entry_from_file(img, kind="图片", talker=CHAT, local_id=""), path=p)
    check("没有 xml、只有明文路径的素材能存进去", added and len(items) == 1, items)
    check("读回来时不被丢掉（明文是唯一发得出去的东西）",
          len(assets.load(p)) == 1)
    check("plaintext_of 认到明文文件", assets.plaintext_of(items[-1]) == img)
    check("明文路径不存在时 plaintext_of 返回空（缓存会被清理，别让 send_image 撞空路径）",
          assets.plaintext_of({"path": os.path.join(tmp, "没有这个.jpg")}) == "")
    check("明文优先于缩略图",
          assets.plaintext_of({"path": img, "image": "C:/x.jpg"}) == img)

    # 没有 local_id 的两条明文素材**不许互相顶掉**（去重键要按文件名退一步）
    img2 = _write(os.path.join(tmp, "明文图2.jpg"), b"\xff\xd8\xff\xd9")
    items, added, dropped = assets.stash(
        assets.entry_from_file(img2, kind="图片", talker=CHAT, local_id=""), path=p)
    check("两条没有 local_id 的明文素材不会互相顶掉",
          len(items) == 2 and dropped == 0, items)

    try:
        assets.stash({"kind": "图片", "local_id": "1"}, path=p)
        raised = False
    except ValueError:
        raised = True
    check("既没有 xml 也没有明文的条目直接拒绝存", raised)

    e = assets.entry_from_media(
        {"kind": "图片", "talker": CHAT, "local_id": "5", "local_type": 3,
         "image": "C:/x.jpg", "time": "12:00", "_ts": 123}, XML_IMG, now=999.0)
    check("entry_from_media 把 XML 原样存下来", e["xml"] == XML_IMG)
    check("entry_from_media 记住来源（会话+local_id）",
          e["talker"] == CHAT and e["local_id"] == "5" and e["ts"] == 999.0)


# ---------------------------------------------------- 4. send_asset

def _contacts():
    return [{"wxid": "wxid_zhangsan", "name": "张三", "remark": "张三", "alias": ""},
            {"wxid": "wxid_lisi", "name": "李四", "remark": "", "alias": ""}]


def test_send_asset(tmp):
    print("\n── send_asset：明文才发得出去 / 只有消息引用必须如实拒绝 ──")
    p = os.path.join(tmp, "assets_send.json")
    real_path = assets.PATH
    assets.PATH = p
    try:
        agent_tools._PENDING.clear()

        # 空暂存区：必须如实报错，不许发任何东西
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三"})
        check("空暂存区：如实说没有素材", "暂存区是空的" in out, out)
        check("空暂存区：一条都没发", cli.calls == [])
        check("空暂存区：也没登记待确认", agent_tools.list_pending(CHAT) == [])

        # 只有 xml（转发接口在真机上会把微信搞崩、已禁用）→ 如实拒绝，一条都不发
        assets.stash(_item(1, xml=XML_OLD, ts=1000.0), path=p)
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三"})
        check("只有消息引用：如实说发不了",
              "只有**原始消息引用**" in out and "发不了" in out, out)
        check("只有消息引用：一条都没发", cli.calls == [])
        check("只有消息引用：也没登记待确认", agent_tools.list_pending(CHAT) == [])
        check("拒绝时给了能走通的办法（以「文件」方式再发一次）", "文件" in out, out)

        # 明文素材（两张，用来验 which / count）
        img_old = _write(os.path.join(tmp, "发1.jpg"))
        img_new = _write(os.path.join(tmp, "发2.jpg"), b"\xff\xd8\xff\xdb1234")
        assets.stash(assets.entry_from_file(img_old, talker=CHAT), path=p)
        assets.stash(assets.entry_from_file(img_new, talker=CHAT), path=p)

        # 名单内：直接发明文（send_image），发的是**最近那条**
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        agent_tools._SENT_IMAGE.clear()
        out = box.run("send_asset", {"to": "张三"})
        check("名单内直接发：走 send_image（不是 send_xml）",
              len(cli.calls) == 1 and cli.calls[0][0] == "image", cli.calls)
        check("发的是最近那条明文文件", cli.calls[0][2] == img_new)
        check("发给正确的 wxid", cli.calls[0][1] == "wxid_zhangsan")
        check("回话说人话（不出现 wxid）",
              "已把那张图发给 张三。" == out and "wxid" not in out, out)
        check("发图也算「我刚发过图」——回显不该被当成新消息",
              "wxid_zhangsan" in agent_tools._SENT_IMAGE)

        # 指定第 2 张（第 1 张是 img_new、第 2 张是 img_old）
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三", "which": 2})
        check("which=2 发的是更早那张", cli.calls[0][2] == img_old)
        check("which=2 的回话带编号", "第 2 张图" in out, out)

        # 连发：受 max_send_count 钳制（配置 2，模型填 7）
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三", "count": 7})
        check("连发次数被 max_send_count 钳制", len(cli.calls) == 2, cli.calls)
        check("连发回话说明发了 2 次", "连发 2 次" in out, out)

        # 发到一半失败：必须带上「已经发出 N 条」（发消息不可逆）
        cli = _Rec(boom_at=3)
        box = _box(_cfg(max_send_count=5), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三", "count": 3})
        check("中途失败：说的是已经发出 2 条/张", "已经成功发出 2 条/张" in out, out)
        check("中途失败：没有走「一条都没发」那个分支",
              "这条还没有发出去" not in out, out)

        # 名单外：只登记待确认（图片按「一串路径」连发）
        cli = _Rec()
        box = _box(_cfg(max_send_count=5), _contacts(), cli)
        out = box.run("send_asset", {"to": "李四", "count": 3})
        check("名单外一条都没发", cli.calls == [])
        pend = agent_tools.list_pending(CHAT)
        check("登记了一条待确认", len(pend) == 1)
        check("待确认带 label + 3 份明文路径 + count（确认后连发 3 次）",
              pend and pend[0].get("label") == "那张图"
              and list(pend[0].get("image") or []) == [img_new] * 3
              and pend[0].get("count") == 3, pend)
        desc = agent_tools.describe_pending(pend[0])
        check("待确认描述说人话且不含 wxid",
              desc == "发给 李四 那张图", desc)
        check("工具回话让用户回「确认」", "确认" in out and "尚未发送" in out, out)

        # 确认之后：send_pending 按路径串连发
        cli = _Rec()
        n, err = agent_tools.send_pending(cli, pend[0], interval=0.0)
        check("确认后按 count 连发 3 次图片",
              n == 3 and len(cli.calls) == 3 and not err, (n, cli.calls))
        check("三次发的是同一个文件", {c[2] for c in cli.calls} == {img_new})

        # 反向保证：forward_message 那条路仍然只发一次（send_pending 的分派没被改坏）
        agent_tools._PENDING.clear()
        agent_tools.set_pending(CHAT, "wxid_zhangsan", "张三", "转发一条消息",
                                xml=XML_IMG)
        cli = _Rec()
        n, _err = agent_tools.send_pending(cli, agent_tools.pop_pending(CHAT), 0.0)
        check("forward_message 那条路（没填 count）仍然只发一次", n == 1, n)
    finally:
        assets.PATH = real_path
        agent_tools._PENDING.clear()


def test_sync_latest(tmp):
    """图 + 文字落在同一个轮询间隔里：暂存区还没有它时，也要发得出去。

    这是真机上一定会撞上的窗口：`_v4_pickup_nontext` 只在「会话最后一条是非文本」
    时才回查消息表，所以「发图 → 立刻打字」这种时序下那张图永远不会被暂存。
    `t_send_asset` 在真的要发素材时补一次（普通消息上一次都不查）。
    """
    print("\n── 补存：图与文字挤在同一个轮询间隔里，也要发得出去 ──")
    import live_history

    real_path = assets.PATH
    old_latest = live_history.latest_media
    old_xml = live_history.message_xml
    assets.PATH = os.path.join(tmp, "sync_assets.json")
    race1 = "<msg><img md5=\"race1\" /></msg>"
    race2 = "<msg><img md5=\"race2\" /></msg>"
    thumb = _write(os.path.join(tmp, "补存的明文缩略图.jpg"))

    def _media(lid, ts=2000, image=None):
        return [{"talker": CHAT, "local_id": str(lid), "local_type": 3, "kind": "图片",
                 "is_self": 1, "image": image, "time": "", "_ts": ts}]

    try:
        # 1) 更新的那条**有明文**（微信缓存的缩略图）-> 补存明文并直接发出去，
        #    而且**不该去取 XML**（转发已经废了，取它没意义还多一次查库）
        live_history.latest_media = lambda client, talker, limit=1: _media(77, image=thumb)
        live_history.message_xml = lambda client, talker, lid: (_ for _ in ()).throw(
            AssertionError("有明文时不该去取 XML"))
        agent_tools._SENT_IMAGE.clear()
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三"})
        check("暂存区空着也能发：把控制会话里最新那张的**明文**补存进来",
              len(cli.calls) == 1 and cli.calls[0][0] == "image"
              and cli.calls[0][2] == thumb, (out, cli.calls))
        check("补存的这条确实进了暂存区",
              [i["local_id"] for i in assets.load()] == ["77"])

        # 2) 更新的那条**没有明文**、XML 也取不到 -> 一张都不发（绝不退回发旧的）
        live_history.latest_media = lambda client, talker, limit=1: _media(79)
        live_history.message_xml = lambda client, talker, lid: ""
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三"})
        check("既没有明文也没有原文：一张都不发（不发旧的那张）",
              cli.calls == [] and "一张都没发" in out, out)
        check("这种情况要告诉用户「以文件方式重发一次」", "文件" in out, out)

        # 3) 更新的那条没有明文、但有 XML -> 存下引用，但**发不出去要说清楚**
        live_history.latest_media = lambda client, talker, limit=1: _media(78)
        live_history.message_xml = lambda client, talker, lid: race2
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三"})
        check("只有 XML 的新图：补存引用、但如实说发不了",
              cli.calls == [] and "只有**原始消息引用**" in out, out)

        # 4) 最新那条是**我自己刚发出去**的回显 -> 不补存，发暂存区里已有的那条明文
        #    （先把暂存区摆成「最新一条有明文」，否则撞上上一步那条只有引用的——
        #     那种情况下**拒绝**才是对的：绝不退回发旧图）
        assets.clear()
        assets.stash(assets.entry_from_file(thumb, talker=CHAT, local_id="77"))
        agent_tools.remember_sent_image(CHAT)
        live_history.latest_media = lambda client, talker, limit=1: _media(
            80, ts=time.time(), image=thumb)
        cli = _Rec()
        box = _box(_cfg(), _contacts(), cli)
        out = box.run("send_asset", {"to": "张三"})
        check("最新那条是自己刚发的回显 -> 不补存，发暂存区里已有的明文",
              len(cli.calls) == 1 and cli.calls[0][2] == thumb, (out, cli.calls))
        agent_tools._SENT_IMAGE.clear()
    finally:
        assets.PATH = real_path
        live_history.latest_media = old_latest
        live_history.message_xml = old_xml


def test_describe_old_kinds():
    print("\n── describe_pending：老的五类不被新加的分支带偏 ──")
    cases = [
        ({"to_name": "张三", "text": "你好"}, "发给 张三「你好」"),
        ({"to_name": "张三", "text": "你好", "count": 3},
         "发给 张三「你好」（连发 3 次）"),
        ({"to_name": "张三", "image": "C:/a.jpg"}, "发给 张三 一张图片（a.jpg）"),
        ({"to_name": "张三", "xml": XML_IMG}, "转发一条消息给 张三"),
        ({"to_name": "张三", "text": "草稿", "kind": "auto"},
         "自动回复草稿 → 发给 张三：草稿"),
    ]
    for item, want in cases:
        got = agent_tools.describe_pending(item)
        check(f"{want}", got == want, got)
    shell = agent_tools.describe_pending({"to_name": "", "cmd": "dir /b", "kind": "shell"})
    check("shell 仍显示命令原文", shell == "本机命令「dir /b」", shell)


def main():
    print("=" * 60)
    print("素材暂存区 / send_asset —— 回归自测（不联网、不碰 hook、不需要微信）")
    print("=" * 60)
    test_media_kind()
    test_latest_media()
    with tempfile.TemporaryDirectory() as tmp:
        test_store(tmp)
        test_send_asset(tmp)
        test_sync_latest(tmp)
    test_describe_old_kinds()
    print("\n" + "=" * 60)
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
