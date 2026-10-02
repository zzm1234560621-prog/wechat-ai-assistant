"""待确认队列 / 发图白名单 / 查询预算 的回归自测。

**不联网、不碰 30001、不需要微信**：全部是假客户端 + 临时目录 + 纯函数。
风格照抄 selftest_aixed.py（✅/❌ + 结尾汇总 + 失败 sys.exit(1)）。

覆盖的是 2026-10 这轮改动（对应 docs/executor-review.md 的 R5-3 等）：

  1. 待确认队列可枚举 + 按编号取（`list_pending` / `pop_pending(index=N)`）
     —— 队列里混着「待发送」和「待执行本地命令」时，用户看到的提示和
     实际执行的那条必须是同一条。
  2. `describe_pending` 对五类待确认项都说人话，且**绝不出现 wxid/roomid**。
  3. 发图白名单：目录内放行、`..` 逃逸被拒、目录外被拒、
     junction/符号链接逃逸被拒（realpath 判定）、非图片后缀被拒。
  4. `send_pending(..., allowed_dirs=...)` 发送时二次校验：目录外**一条都不发**。
  5. `agent.max_queries` 越界被钳制并告警（配 9999 → 实际 <= 20）。
  6. `read_file` 只给 `name`：纯磁盘兜底（自己发的文件记录里未必有），
     不查库、多份命中不替用户挑。

用法：python selftest_policy.py
"""
import os
import re
import read_worker
import shutil
import subprocess
import sys
import tempfile
import time

import agent_tools
import file_read
import groups
import image_cache
import live_history

SELF_WXID = "wxid_self_0001"
CHAT = "filehelper"


class _Rec:
    """假客户端：只记录发了什么，一碰发消息就记账，不碰任何网络。"""

    def __init__(self, boom_at=None):
        self.calls = []
        self.boom_at = boom_at      # 第 N 次 send_* 抛异常（1 起）

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

    def send_file(self, path, wxid):
        self._hit("file", wxid, path)


class _Boom:
    """一被查库就炸——用来证明这些用例根本没碰客户端。"""

    def __getattr__(self, name):
        raise AssertionError(f"不该查库，却调了 {name}")


def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


def _reset_pending():
    agent_tools._PENDING.clear()


def _box(cfg=None, contacts=None, client=None, cfg_provider=None):
    return agent_tools.ToolBox(client or _Boom(), cfg or {"agent": {}},
                               contacts or [], SELF_WXID, CHAT,
                               cfg_provider=cfg_provider)


def _write(path, data=b"\x89PNG\r\n\x1a\n0123"):
    with open(path, "wb") as f:
        f.write(data)
    return path


def _make_junction(link, target):
    """建一个目录 junction（Windows 上不需要管理员权限）。失败返回 False。

    junction 是「目录链接」，它比符号链接更容易在无提权的情况下建出来；
    但两者在 realpath 眼里是一样的东西（都会被解开），所以测 junction 足够。
    """
    if os.path.exists(link):
        return False
    try:
        r = subprocess.run(["cmd", "/d", "/s", "/c", "mklink", "/J", link, target],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return r.returncode == 0 and os.path.isdir(link)
    except OSError:
        return False


# ---------------------------------------------------------------- 1. 队列

def test_queue():
    print("\n── 待确认队列：可枚举 + 按编号取（混合 kind 不会执行错的那条）──")
    ok = True
    _reset_pending()

    # 故意压三条**混合 kind**：这正是 R5-3 的现场——用户看到的提示是刚触发那条，
    # 实际执行的却是更早入队的另一条。
    agent_tools.set_pending(CHAT, "wxid_aaa", "张三", "晚上一起吃饭")
    agent_tools.set_pending(CHAT, "", "", text="dir /b", kind="shell", cmd="dir /b")
    agent_tools.set_pending(CHAT, "wxid_bbb", "李四", "收到", kind="auto")

    items = agent_tools.list_pending(CHAT)
    ok &= check("list_pending 返回全部 3 条", len(items) == 3, len(items))
    ok &= check("list_pending 顺序 = FIFO（最早在前）",
                items[0]["text"] == "晚上一起吃饭" and items[1]["kind"] == "shell"
                and items[2]["kind"] == "auto",
                [(i.get("kind"), i.get("text")) for i in items])
    ok &= check("list_pending 不出队", len(agent_tools.list_pending(CHAT)) == 3)

    it = agent_tools.pop_pending(CHAT, index=2)
    ok &= check("pop_pending(index=2) 取到的是第 2 条（shell 那条）",
                it is not None and it.get("kind") == "shell"
                and it.get("cmd") == "dir /b", it)
    left = agent_tools.list_pending(CHAT)
    ok &= check("取走后剩下 2 条", len(left) == 2, len(left))
    ok &= check("其余项顺序不变",
                left[0]["text"] == "晚上一起吃饭" and left[1]["kind"] == "auto",
                [i.get("text") for i in left])

    ok &= check("越界（index=9）返回 None", agent_tools.pop_pending(CHAT, index=9) is None)
    ok &= check("越界（index=0）返回 None", agent_tools.pop_pending(CHAT, index=0) is None)
    ok &= check("越界（index=-1）返回 None", agent_tools.pop_pending(CHAT, index=-1) is None)
    ok &= check("越界不改动队列", len(agent_tools.list_pending(CHAT)) == 2,
                len(agent_tools.list_pending(CHAT)))

    head = agent_tools.pop_pending(CHAT)
    ok &= check("index=None 保持老行为（取队头）",
                head is not None and head["text"] == "晚上一起吃饭", head)
    ok &= check("队列缩短到 1 条", len(agent_tools.list_pending(CHAT)) == 1)
    ok &= check("peek_pending 老接口还在（看队头、不出队）",
                agent_tools.peek_pending(CHAT)["kind"] == "auto"
                and len(agent_tools.list_pending(CHAT)) == 1)
    ok &= check("discard_pending 老接口还在（返回丢掉几条）",
                agent_tools.discard_pending(CHAT) == 1
                and agent_tools.list_pending(CHAT) == [])

    # 过期项：list/pop 都不该看到
    agent_tools.set_pending(CHAT, "w", "王五", "hi")
    agent_tools._PENDING[CHAT][0]["ts"] -= 10_000
    ok &= check("过期的待确认项不出现在列表里", agent_tools.list_pending(CHAT, ttl=300) == [])
    ok &= check("过期的按编号也取不到", agent_tools.pop_pending(CHAT, ttl=300, index=1) is None)
    _reset_pending()

    # 另一个会话不受影响
    agent_tools.set_pending("chat_a", "w", "甲", "A")
    agent_tools.set_pending("chat_b", "w", "乙", "B")
    got = agent_tools.pop_pending("chat_b", index=1)
    ok &= check("按会话隔离，取 chat_b 不动 chat_a",
                got["text"] == "B" and len(agent_tools.list_pending("chat_a")) == 1, got)
    _reset_pending()
    return ok


# ------------------------------------------------- 2. describe_pending

def test_describe():
    print("\n── describe_pending：五类待确认项都说人话，且不含 wxid/roomid ──")
    ok = True

    shell = {"kind": "shell", "cmd": "dir /b", "text": "dir /b", "to_wxid": "",
             "to_name": "", "ts": 0}
    d = agent_tools.describe_pending(shell)
    ok &= check("shell：显示命令**原文**", d == "本机命令「dir /b」", d)

    # 命令原文一字不改（这是防提示词注入的关键）——长命令只截断且必须标注
    long_cmd = "echo " + "A" * 500
    d = agent_tools.describe_pending({"kind": "shell", "cmd": long_cmd, "ts": 0})
    ok &= check("shell：长命令被截断", len(d) < 300, len(d))
    ok &= check("shell：截断有明确标注", "已截断" in d and "原文" in d, d)
    ok &= check("shell：截断后仍是原文开头（不加引号/不改写）",
                d.startswith("本机命令「echo AAAA"), d[:40])

    d = agent_tools.describe_pending(
        {"kind": "shell", "cmd": 'whoami & net user "a b"', "ts": 0})
    ok &= check("shell：原文里的引号/连接符原样保留",
                d == '本机命令「whoami & net user "a b"」', d)

    t = {"kind": "agent", "to_wxid": "wxid_friendA", "to_name": "张三",
         "text": "晚上一起吃饭", "ts": 0}
    ok &= check("文本：发给 张三「晚上一起吃饭」",
                agent_tools.describe_pending(t) == "发给 张三「晚上一起吃饭」",
                agent_tools.describe_pending(t))
    ok &= check("文本：绝不出现 wxid",
                "wxid_friendA" not in agent_tools.describe_pending(t))

    tc = dict(t, count=3)
    ok &= check("文本：连发说清次数",
                "连发 3 次" in agent_tools.describe_pending(tc),
                agent_tools.describe_pending(tc))

    a = {"kind": "auto", "to_wxid": "wxid_friendB", "to_name": "李四",
         "text": "好的，明天见", "ts": 0}
    d = agent_tools.describe_pending(a)
    ok &= check("auto：标出这是自动回复草稿", "自动回复草稿" in d and "李四" in d, d)
    ok &= check("auto：绝不出现 wxid", "wxid" not in d, d)

    img1 = {"kind": "agent", "to_wxid": "wxid_x", "to_name": "张三",
            "image": r"C:\pics\a.png", "ts": 0}
    d = agent_tools.describe_pending(img1)
    ok &= check("图片（单张）说得清", "张三" in d and "a.png" in d and "一张" in d, d)
    ok &= check("图片：不把完整路径塞进菜单", r"C:\pics" not in d, d)

    img3 = {"kind": "agent", "to_wxid": "wxid_x", "to_name": "张三",
            "image": [r"C:\pics\a.png", r"C:\pics\b.png", r"C:\pics\c.png"], "ts": 0}
    d = agent_tools.describe_pending(img3)
    ok &= check("图片（多张）报张数", "3 张" in d, d)

    fw = {"kind": "agent", "to_wxid": "wxid_x", "to_name": "李四",
          "xml": "<msg/>", "text": "转发「张三」里的一条消息", "ts": 0}
    d = agent_tools.describe_pending(fw)
    ok &= check("转发：说得清是转发给谁", "转发" in d and "李四" in d, d)

    # 兜底：早期登记过、或配置写歪了把 wxid 当显示名时，绝不能原样透出
    bad = {"kind": "agent", "to_wxid": "wxid_zzz", "to_name": "wxid_zzz",
           "text": "hi", "ts": 0}
    d = agent_tools.describe_pending(bad)
    ok &= check("显示名是 wxid 时兜底成「对方」", "wxid" not in d, d)
    bad2 = {"kind": "agent", "to_wxid": "123@chatroom", "to_name": "123@chatroom",
            "text": "hi", "ts": 0}
    d2 = agent_tools.describe_pending(bad2)
    ok &= check("显示名是 roomid 时兜底成「对方」", "chatroom" not in d2, d2)

    ok &= check("空/垃圾输入不崩", bool(agent_tools.describe_pending(None))
                and bool(agent_tools.describe_pending("x")))
    return ok


# ---------------------------------------------------- 3. 发图白名单

def test_whitelist():
    print("\n── 发图白名单：目录内放行 / .. 逃逸 / 目录外 / 链接逃逸 / 后缀 ──")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        okdir = os.path.join(td, "ok")
        os.makedirs(okdir)
        outside = os.path.join(td, "outside")
        os.makedirs(outside)

        good = _write(os.path.join(okdir, "a.png"))
        outside_img = _write(os.path.join(outside, "b.png"))
        not_image = _write(os.path.join(okdir, "c.txt"))

        box = _box({"agent": {"max_queries": 3, "send_image_dirs": [okdir]}})

        p, e = box._image_path_ok(good)
        ok &= check("允许目录内的图片放行", bool(p) and e is None, (p, e))
        ok &= check("放行时返回的是解析后的绝对路径", os.path.isabs(p or ""), p)

        p, e = box._image_path_ok(outside_img)
        ok &= check("目录外的图片被拒", p == "" and "不在允许" in e, e)
        ok &= check("拒绝文案告诉模型「让用户自己改配置，你别改」",
                    "用户自己" in e and "不要改配置绕过" in e, e)

        p, e = box._image_path_ok(not_image)
        ok &= check("非图片后缀被拒", p == "" and "不是图片" in e, e)

        p, e = box._image_path_ok(os.path.join(okdir, "..", "outside", "b.png"))
        ok &= check("用 .. 绕出去被拒", p == "" and "不在允许" in e, e)

        p, e = box._image_path_ok(os.path.join(okdir, "nope.png"))
        ok &= check("文件不存在被拒", p == "" and "找不到" in e, e)

        # ---- 链接逃逸：在允许目录里放一个指到外面的 junction ----
        link = os.path.join(okdir, "jump")
        made = _make_junction(link, outside)
        if made:
            p, e = box._image_path_ok(os.path.join(link, "b.png"))
            ok &= check("junction 逃逸被拒（realpath 解开链接后判定）",
                        p == "" and "不在允许" in e, (p, e))
        else:
            # 建不出 junction（权限/环境）时退化为**纯路径算术断言**：
            # 手算出「realpath 之后落在允许目录外」，证明判定用的确实是 realpath。
            real = os.path.realpath(outside_img)
            ok &= check("realpath 判定：真实位置在允许目录外 → 应被拒",
                        not agent_tools._is_under(real, okdir),
                        (real, okdir))
            ok &= check("realpath 判定：真实位置在允许目录内 → 应放行",
                        agent_tools._is_under(good, okdir))
            print("     （本机建不出 junction，已退化为 realpath 单元断言）")

        # `_is_under` 本身：realpath 必须真的被用上，而不是只看字符串前缀
        tricky = os.path.join(okdir, "..", "ok", "sub", "a.png")
        ok &= check("_is_under 不靠字符串前缀（.. 会先被 realpath 解开）",
                    agent_tools._is_under(tricky, okdir))
        ok &= check("_is_under：目录本身算在内", agent_tools._is_under(okdir, okdir))
        ok &= check("_is_under：同前缀但不是子目录不算（ok2 vs ok）",
                    not agent_tools._is_under(okdir + "2", okdir))
        ok &= check("跨盘符/非法路径不抛异常、按不在处理",
                    agent_tools._is_under("Z:\\nope\\x.png", okdir) is False)

        # ---- send_images（按目录群发）必须走同一套判定 ----
        p, e = box._in_allowed_dirs(okdir)
        ok &= check("send_images 的目录判定与 send_image 同源", bool(p) and e is None, (p, e))
        p, e = box._in_allowed_dirs(outside)
        ok &= check("send_images 目录在允许范围外被拒", p == "" and "不在允许" in e, e)

        # ---- 默认值必须是**图片缓存目录**，不是整个微信数据根 ----
        dcfg = {"agent": {}}
        ok &= check("没配 send_image_dirs 时走 allowed_image_dirs()（同一套）",
                    agent_tools.allowed_image_dirs(dcfg)
                    == agent_tools.allowed_image_dirs({}))
    return ok


def test_image_cache_root():
    print("\n── 默认发图根目录：真正的图片缓存根（<账号>/cache），且推不出时有告警兜底 ──")
    ok = True
    real_root = image_cache.data_root()
    if real_root:
        dirs = image_cache.image_cache_dirs()
        ok &= check("image_cache_dirs() 拿到 <账号>/cache",
                    bool(dirs) and all(d.endswith("cache") for d in dirs), dirs)
        ok &= check("图片缓存根在 data_root 之内（不是整个数据根）",
                    all(agent_tools._is_under(d, real_root) for d in dirs), dirs)
        ok &= check("图片缓存根 != data_root（这就是本次收窄的点）",
                    all(os.path.realpath(d) != os.path.realpath(real_root) for d in dirs),
                    dirs)
        # 真机上扫到的缩略图路径必须落在白名单里，否则收窄就把功能弄坏了
        sample = None
        for d in dirs:
            for month in os.listdir(d):
                base = os.path.join(d, month, "Message")
                if not os.path.isdir(base):
                    continue
                for h in os.listdir(base):
                    thumb = os.path.join(base, h, "Thumb")
                    if os.path.isdir(thumb):
                        for fn in os.listdir(thumb):
                            sample = os.path.join(thumb, fn)
                            break
                    if sample:
                        break
                if sample:
                    break
            if sample:
                break
        if sample:
            p, e = _box({"agent": {}})._image_path_ok(sample)
            ok &= check("真实扫到的缩略图在白名单内（收窄没把功能弄坏）",
                        bool(p) and e is None, (sample, e))
        else:
            print("     （本机没有可用的缩略图样本，跳过「真实图在白名单内」这一条）")
    else:
        print("     （本机没有 xwechat_files，跳过真实目录断言）")

    # 兜底路径：推不出缓存目录 → 退回 data_root 且**必须告警**，不许静默
    saved_icd, saved_dr = image_cache.image_cache_dirs, image_cache.data_root
    flag_backup = agent_tools._WARNED_IMAGE_ROOT[0]
    try:
        image_cache.image_cache_dirs = lambda: []
        image_cache.data_root = lambda: r"C:\fake\xwechat_files"

        agent_tools._WARNED_IMAGE_ROOT[0] = False
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = agent_tools.allowed_image_dirs({})
        warn = err.getvalue()
        ok &= check("推不出缓存目录 → 退回 data_root（功能没被弄坏）",
                    got == [r"C:\fake\xwechat_files"], got)
        ok &= check("兜底时 stderr 有明确告警", "图片缓存目录" in warn and "放宽" in warn,
                    warn.strip())
        ok &= check("告警说明了放宽到什么范围",
                    "xwechat_files" in warn, warn.strip())

        # 告警只打一次，不要刷屏
        err2 = io.StringIO()
        with contextlib.redirect_stderr(err2):
            agent_tools.allowed_image_dirs({})
        ok &= check("同一进程内兜底告警只打一次（不刷屏）", err2.getvalue() == "",
                    err2.getvalue())

        # data_root 也没有 → 返回空 = 什么都不许发（这是**如实**，不是静默放宽）
        agent_tools._WARNED_IMAGE_ROOT[0] = True
        image_cache.data_root = lambda: None
        ok &= check("连 data_root 都没有 → 返回空列表，由调用方如实拒绝",
                    agent_tools.allowed_image_dirs({}) == [])
        p, e = _box({"agent": {}})._in_allowed_dirs(r"C:\whatever\a.png")
        ok &= check("空白名单时拒绝文案不静默", p == "" and "不让发" in e, e)
    finally:
        image_cache.image_cache_dirs = saved_icd
        image_cache.data_root = saved_dr
        agent_tools._WARNED_IMAGE_ROOT[0] = flag_backup
    return ok


# ------------------------------------------- 4. 发送时二次校验

def test_send_pending_guard():
    print("\n── send_pending：发送时二次校验（不许「先发几张再说」）──")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        okdir = os.path.join(td, "ok")
        os.makedirs(okdir)
        outside = os.path.join(td, "outside")
        os.makedirs(outside)
        inside_img = _write(os.path.join(okdir, "a.png"))
        outside_img = _write(os.path.join(outside, "b.png"))

        # 目录外：一条都不发
        rec = _Rec()
        n, err = agent_tools.send_pending(
            rec, {"to_wxid": "w", "image": outside_img}, 0.0, allowed_dirs=[okdir])
        ok &= check("目录外：返回已发 0 条", n == 0, n)
        ok &= check("目录外：**真的没有发送**", rec.calls == [], rec.calls)
        ok &= check("目录外：错误里说清「一张都没发」", err and "一张都没发" in str(err), err)

        # 多张里混一张目录外的：整条都不许动（绝不能先发前两张）
        rec = _Rec()
        n, err = agent_tools.send_pending(
            rec, {"to_wxid": "w", "image": [inside_img, outside_img]}, 0.0,
            allowed_dirs=[okdir])
        ok &= check("混合路径：一张都不发（不做部分发送）", rec.calls == [] and n == 0,
                    rec.calls)
        ok &= check("混合路径：错误里点出是哪一张", err and outside_img in str(err), err)

        # 目录内：正常发
        rec = _Rec()
        n, err = agent_tools.send_pending(
            rec, {"to_wxid": "w", "image": inside_img}, 0.0, allowed_dirs=[okdir])
        ok &= check("目录内：正常发出 1 张",
                    n == 1 and err is None and rec.calls == [("image", "w", inside_img)],
                    (n, err, rec.calls))

        # 空白名单：不许「无限制放行」，如实报错
        rec = _Rec()
        n, err = agent_tools.send_pending(
            rec, {"to_wxid": "w", "image": inside_img}, 0.0, allowed_dirs=[])
        ok &= check("空白名单：不发送并如实报错", n == 0 and rec.calls == [] and err, (n, err))

        # allowed_dirs=None（默认）= **保持原有行为**
        rec = _Rec()
        n, err = agent_tools.send_pending(
            rec, {"to_wxid": "w", "image": outside_img}, 0.0)
        ok &= check("allowed_dirs=None 时行为不变（照发）",
                    n == 1 and rec.calls == [("image", "w", outside_img)], rec.calls)

        # 文本不受这个参数影响
        rec = _Rec()
        n, err = agent_tools.send_pending(
            rec, {"to_wxid": "w", "text": "hi", "count": 2}, 0.0, allowed_dirs=[okdir])
        ok &= check("文本走连发、不受图片白名单影响",
                    n == 2 and len(rec.calls) == 2, (n, rec.calls))

        # 发送中途失败：已发几张要如实返回
        rec = _Rec(boom_at=2)
        n, err = agent_tools.send_pending(
            rec, {"to_wxid": "w", "image": [inside_img, outside_img]}, 0.0,
            allowed_dirs=[td])
        ok &= check("中途失败：如实返回已发张数", n == 1 and err is not None, (n, err))
    return ok


# ------------------------------------------- 5. 异常别丢「已部分发出」

def test_partial_send_report():
    print("\n── 发送类工具异常：必须带上「已经发出 N 条/张」 ──")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        imgs = [_write(os.path.join(td, f"{i}.png")) for i in range(1, 4)]

        cfg = {"agent": {"max_queries": 3, "send_image_dirs": [td],
                         "auto_send_whitelist": ["wxid_friendA"]}}
        contacts = [{"wxid": "wxid_friendA", "name": "张三", "remark": "老张"}]

        # 发到第 3 张才失败：模型必须看到「已经发出 2 张」，
        # 而不是一句光秃秃的「工具 send_images 执行出错」。
        box = _box(cfg, contacts, client=_Rec(boom_at=3))
        txt = box.run("send_images", {"to": "老张", "dir": td})
        ok &= check("send_images：报出已经发出 2 张", "已经成功发出 2" in txt, txt)
        ok &= check("send_images：明说剩下的没发", "没有发" in txt, txt)
        ok &= check("send_images：给出下一步建议（先问用户，不自己重发）",
                    "先问用户" in txt and "不要自己重发" in txt, txt)
        ok &= check("send_images：不出现「工具 send_images 执行出错」这种丢事实的老话",
                    "执行出错" not in txt, txt)
        box2 = _box(cfg, contacts, client=_Rec(boom_at=3))
        box2.run("send_images", {"to": "老张", "dir": td})
        ok &= check("send_images：计数器记的是 2 而不是 3", box2._sent_count == 2,
                    box2._sent_count)

        # 文本连发第 3 条才失败：已发 2 条
        box = _box(cfg, contacts, client=_Rec(boom_at=3))
        txt = box.run("send_text", {"to": "老张", "text": "hi", "count": 5})
        ok &= check("send_text：连发中途失败也报出已发条数",
                    "已经成功发出 2" in txt, txt)
        ok &= check("send_text：连发失败也说清「已发出 2/5 条」这个细节",
                    "2/5" in txt, txt)

        # 第一条就失败（一条都没发）：必须**明说还没发出去**，不要让模型猜
        box = _box(cfg, contacts, client=_Rec(boom_at=1))
        txt = box.run("send_image", {"to": "老张", "path": imgs[0]})
        ok &= check("send_image：一条没发时明确说「还没有发出去」",
                    "还没有发出去" in txt and "已经成功发出" not in txt, txt)

        # 转发失败也走同一个出口（收件人进白名单才会真的去发，否则只是登记待确认）
        box = _box({"agent": {"max_queries": 3, "auto_send_whitelist": ["wxid_a"]}},
                   [{"wxid": "wxid_a", "name": "张三"}], client=_Rec(boom_at=1))
        try:
            import live_history
            _orig = live_history.message_xml
            live_history.message_xml = lambda *a, **k: "<msg/>"
            try:
                txt = box.run("forward_message",
                              {"to": "wxid_a", "contact": "wxid_a", "local_id": "1"})
            finally:
                live_history.message_xml = _orig
            ok &= check("forward_message：失败也走统一出口、说明没发出去",
                        "还没有发出去" in txt, txt)
        except Exception as e:      # pragma: no cover - 兜底，避免自测自己崩
            ok &= check("forward_message：失败也走统一出口、说明没发出去", False, e)

        # 非发送类工具的通用兜底**保持原样**（不外泄发送状态、不加建议）。
        # 挑一个自己**不 catch 异常**的工具：t_list_pending 不存在，
        # 所以用 limit 传非数字让 int() 抛——那正是 run() 的兜底该接的地方。
        box = _box({"agent": {"max_queries": 3}}, [])
        msg = box.run("recent_messages", {"limit": "x"})
        ok &= check("非发送类工具仍是原来那句通用兜底",
                    msg.startswith("工具 recent_messages 执行出错："), msg)
        ok &= check("非发送类工具不会被加上「已发出」字样",
                    "已经成功发出" not in msg and "还没有发出去" not in msg, msg)
    return ok


# ------------------------------------------- 6. 查询预算硬夹

def test_budget_clamp():
    print("\n── agent.max_queries：越界钳制 + 告警（不许出现「不设上限」）──")
    ok = True
    import io
    import contextlib

    def mk(q):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            b = _box({"agent": {"max_queries": q}})
        return b, err.getvalue()

    b, w = mk(9999)
    ok &= check("配 9999 → 实际 <= 20", b.max_queries <= 20 and b.budget.left <= 20,
                b.max_queries)
    ok &= check("配 9999 → 钳到 20", b.max_queries == 20, b.max_queries)
    ok &= check("配 9999 → 打了告警", "钳制" in w and "9999" in w, w.strip())

    b, w = mk(0)
    ok &= check("配 0 → 钳到下限 1", b.max_queries == 1 and b.budget.left == 1, b.max_queries)
    ok &= check("配 0 → 打了告警", "钳制" in w, w.strip())

    b, w = mk(-5)
    ok &= check("配负数 → 钳到 1", b.max_queries == 1, b.max_queries)

    b, w = mk(6)
    ok &= check("配 6（默认量级）不动、也不告警",
                b.max_queries == 6 and w == "", (b.max_queries, w))
    b, w = mk(20)
    ok &= check("配 20（正好上限）不动、也不告警",
                b.max_queries == 20 and w == "", (b.max_queries, w))

    b, w = mk("abc")
    ok &= check("配非数字 → 退回默认 6 而不是崩",
                b.max_queries == agent_tools._MAX_QUERIES_DEFAULT, b.max_queries)

    b, w = mk(None)
    ok &= check("没配 max_queries → 用默认 6", b.max_queries == 6, b.max_queries)

    # 不能有「不设上限」的后门：任何超大值都必须落回 <= 20
    for huge in (10 ** 6, sys.maxsize):
        b, _ = mk(huge)
        ok &= check(f"配 {huge} → 仍 <= 20（没有上限后门）", b.max_queries <= 20,
                    b.max_queries)
    return ok


# ---------------- 群发（一道意图 -> 多个人，各按自己的人设/称呼） ----------------

_BC_CONTACTS = [
    {"wxid": "wxid_a", "name": "张三", "remark": "张三"},
    {"wxid": "wxid_b", "name": "李四", "remark": "李四"},
    {"wxid": "wxid_c", "name": "王五", "remark": "王五"},
    {"wxid": "filehelper", "name": "文件传输助手", "remark": ""},
    {"wxid": "room_x@chatroom", "name": "老同学群", "remark": ""},
    {"wxid": "gh_abc", "name": "某公众号", "remark": ""},
    {"wxid": SELF_WXID, "name": "我", "remark": ""},
]


class _BCLLM:
    """假模型：记下每轮的 prompt，返回固定内容。"""

    def __init__(self, raw):
        self.raw = raw
        self.prompts = []

    def chat(self, system, messages):
        self.prompts.append(messages[0]["content"])
        return self.raw


def _bc_cfg(whitelist=(), bmax=None, chats=None):
    agent = {"auto_send_whitelist": list(whitelist), "send_interval": 0}
    if bmax is not None:
        agent["broadcast_max"] = bmax
    return {"agent": agent,
            "auto_reply": {"persona_self": "用我的口吻、第一人称、口语简短。",
                           "chats": chats or [
                               {"wxid": "wxid_a", "name": "张三", "mode": "self",
                                "review": None, "persona": "", "address": "老张"},
                               {"wxid": "wxid_b", "name": "李四", "mode": "self",
                                "review": None, "persona": "", "address": ""},
                           ]}}


def _bc_box(client, cfg, llm=None):
    return agent_tools.ToolBox(client, cfg, _BC_CONTACTS, SELF_WXID, CHAT,
                               cfg_provider=lambda: cfg,
                               llm_factory=(lambda: llm) if llm else None)


def test_broadcast():
    """群发：范围两道确认 / 原话不调模型 / 生成失败整批作废 / 白名单分流 / 中途失败如实报。

    这一批的每一条都对应一条**不可逆**的后果，所以断言写得比功能还细：
    凡是「本来该一个字都不发」的路径，都要证明**真的一个字都没发**。
    """
    print("\n── 群发：两道确认 + 逐人内容 + 白名单分流 ──")
    ok = True
    _reset_pending()

    # 1) 上限钳制（唯一一处），以及默认值
    ok &= check("没配 broadcast_max → 默认 100",
                agent_tools.broadcast_cap({"agent": {}}) == 100,
                agent_tools.broadcast_cap({"agent": {}}))
    ok &= check("配 99999 → 钳到硬顶 500",
                agent_tools.broadcast_cap({"agent": {"broadcast_max": 99999}}) == 500)
    ok &= check("配 1 → 钳到下限 2",
                agent_tools.broadcast_cap({"agent": {"broadcast_max": 1}}) == 2)
    ok &= check("配非数字 → 退回默认 100",
                agent_tools.broadcast_cap({"agent": {"broadcast_max": "x"}}) == 100)

    # 2) 「所有人」= 所有**能发**的好友：自己 / 文件传输助手 / 群 / 公众号都必须排掉
    recips, scope, err = agent_tools.broadcast_recipients(
        _BC_CONTACTS, _bc_cfg(), "所有人", SELF_WXID)
    got = [r["wxid"] for r in recips]
    ok &= check("「所有人」只留 wxid_ 好友（排掉自己/助手/群/公众号）",
                got == ["wxid_a", "wxid_b", "wxid_c"], got)
    ok &= check("范围标成 all（要走第一道确认）", scope == "all", scope)

    recips2, scope2, _e2 = agent_tools.broadcast_recipients(
        _BC_CONTACTS, _bc_cfg(), "名单", SELF_WXID)
    ok &= check("「名单」= 自动回复名单里的人",
                [r["wxid"] for r in recips2] == ["wxid_a", "wxid_b"], recips2)
    ok &= check("名单里带出了称呼（老张）",
                recips2[0]["address"] == "老张", recips2[0])

    # 3) 点名：有一个对不上就**整批拒绝**（不许「发一部分、剩下的算了」）
    _r3, _s3, e3 = agent_tools.broadcast_recipients(
        _BC_CONTACTS, _bc_cfg(), "张三、查无此人", SELF_WXID)
    ok &= check("点名里有人对不上 → 整批拒绝并说清是谁",
                "查无此人" in e3 and "一条都没发" in e3, e3)

    # 4) 用户给了**原话**：一个字都不许改，且**一次模型都不调**
    _reset_pending()
    llm = _BCLLM("不该被调用")
    cli = _Rec()
    cfg = _bc_cfg(whitelist=["张三"])
    box = _bc_box(cli, cfg, llm)
    out = box.run("broadcast", {"to": "名单", "text": "明天放假一天"})
    ok &= check("原话群发不调模型", llm.prompts == [], llm.prompts)
    ok &= check("白名单里的张三**直接发出**、内容就是原话",
                cli.calls == [("text", "wxid_a", "明天放假一天")], cli.calls)
    pend = agent_tools.list_pending(CHAT)
    ok &= check("其余的人装进**一条**待确认批次（不是 N 条）",
                len(pend) == 1 and pend[0]["kind"] == "broadcast", pend)
    ok &= check("批次里是逐字要发的内容、没被改写",
                [it["text"] for it in pend[0]["items"]] == ["明天放假一天"],
                pend[0]["items"])
    ok &= check("预览里出现「原样」的承诺和回执方式",
                "确认" in pend[0]["text"] and "明天放假一天" in pend[0]["text"],
                pend[0]["text"])
    ok &= check("给模型看的那段话里**没有 wxid**",
                "wxid_" not in out, out)
    ok &= check("工具把预览交给 bot 原样直发（broadcast_preview 非空）",
                "明天放假一天" in box.broadcast_preview, box.broadcast_preview)

    # 5) 用户只给了**意思**：按各人的人设 + 称呼分别写
    _reset_pending()
    llm2 = _BCLLM('{"1": "老张，节日快乐啊", "2": "李四，节日快乐！"}')
    cli2 = _Rec()
    box2 = _bc_box(cli2, _bc_cfg(), llm2)
    out2 = box2.run("broadcast", {"to": "名单", "intent": "祝节日快乐"})
    body = "\n".join(llm2.prompts)
    ok &= check("生成时把**对这个人的称呼**给了模型", "老张" in body, body[:200])
    ok &= check("生成时把**这个人的人设**给了模型", "口语简短" in body, body[:200])
    ok &= check("每人一条、按模型给的内容",
                [it["text"] for it in agent_tools.list_pending(CHAT)[0]["items"]]
                == ["老张，节日快乐啊", "李四，节日快乐！"],
                agent_tools.list_pending(CHAT)[0]["items"])
    ok &= check("没有 wxid 混进给模型的文本",
                "wxid_" not in body, body[:200])
    ok &= check("回执里说了要回「确认」", "确认" in out2, out2)

    # 6) 模型没按 JSON 给 -> **整批作废**：不发也不登记（半截内容比不发严重）
    _reset_pending()
    cli3 = _Rec()
    box3 = _bc_box(cli3, _bc_cfg(), _BCLLM("我觉得可以这么说：节日快乐！"))
    out3 = box3.run("broadcast", {"to": "名单", "intent": "祝节日快乐"})
    ok &= check("模型格式不对 → 明确说「一条都没发」",
                "一条都没发" in out3, out3)
    ok &= check("格式不对时**真的没发**", cli3.calls == [], cli3.calls)
    ok &= check("格式不对时**没登记**任何待确认项",
                agent_tools.list_pending(CHAT) == [], agent_tools.list_pending(CHAT))

    # 7) 漏了某个人的内容也算失败（不许少发一个人还当成功）
    _reset_pending()
    cli4 = _Rec()
    box4 = _bc_box(cli4, _bc_cfg(), _BCLLM('{"1": "只写了第一条"}'))
    out4 = box4.run("broadcast", {"to": "名单", "intent": "祝节日快乐"})
    ok &= check("模型漏人 → 整批作废并说清", "一条都没发" in out4, out4)
    ok &= check("漏人时也没发", cli4.calls == [], cli4.calls)

    # 8) 「所有人」= 第一道确认：**不生成、不发**，只登记范围
    _reset_pending()
    llm5 = _BCLLM('{"1": "不该被调用"}')
    cli5 = _Rec()
    cfg5 = _bc_cfg()
    box5 = _bc_box(cli5, cfg5, llm5)
    out5 = box5.run("broadcast", {"to": "所有人", "intent": "祝节日快乐"})
    pend5 = agent_tools.list_pending(CHAT)
    ok &= check("「所有人」先只确认范围：一条待确认项、kind=broadcast_scope",
                len(pend5) == 1 and pend5[0]["kind"] == "broadcast_scope", pend5)
    ok &= check("范围这步**一个字都没发**", cli5.calls == [], cli5.calls)
    ok &= check("范围这步**也没调模型生成**", llm5.prompts == [], llm5.prompts)
    ok &= check("预览里说清「还没生成、也没发」",
                "还没有生成内容、也没有发任何消息" in box5.broadcast_preview,
                box5.broadcast_preview)
    ok &= check("范围预览里有人数", "3 个人" in box5.broadcast_preview,
                box5.broadcast_preview)
    ok &= check("工具回执明确说范围没确认", "范围还没确认" in out5, out5)

    # 9) 范围确认之后（bot 侧调用）：才生成 + 分流
    item5 = agent_tools.pop_pending(CHAT)
    ok &= check("范围项取出来了", item5 is not None and item5["kind"] == "broadcast_scope")
    llm6 = _BCLLM('{"1": "老张，节日快乐", "2": "李四，节日快乐", "3": "王五，节日快乐"}')
    cli6 = _Rec()
    rep, berr = agent_tools.finish_broadcast(cli6, CHAT, item5, llm6,
                                             _bc_cfg(whitelist=["张三"]))
    ok &= check("确认后没有报错", berr == "", berr)
    ok &= check("确认后白名单里的张三直接收到",
                cli6.calls == [("text", "wxid_a", "老张，节日快乐")], cli6.calls)
    pend6 = agent_tools.list_pending(CHAT)
    ok &= check("其余的人进第二道确认（一条批次）",
                len(pend6) == 1 and pend6[0]["kind"] == "broadcast"
                and len(pend6[0]["items"]) == 2, pend6)
    ok &= check("报告里说清哪些已经直接发出、剩下的要确认",
                "已经直接发出" in rep and "确认" in rep, rep)

    # 10) 人数超上限：**整批拒绝**，绝不截断成「前 N 个」
    _reset_pending()
    cli7 = _Rec()
    box7 = _bc_box(cli7, _bc_cfg(bmax=2), None)
    out7 = box7.run("broadcast", {"to": "所有人", "text": "明天放假"})
    ok &= check("超上限 → 说清人数和上限", "3 个" in out7 and "上限 2" in out7, out7)
    ok &= check("超上限时**一个人都没发**", cli7.calls == [], cli7.calls)
    ok &= check("超上限时**没登记**待确认项",
                agent_tools.list_pending(CHAT) == [], agent_tools.list_pending(CHAT))
    ok &= check("超上限时明确说没截断、并给出改法",
                "broadcast_max" in out7 and "一个人都没发" in out7, out7)

    # 11) text / intent 必须**恰好给一个**
    _reset_pending()
    box8 = _bc_box(_Rec(), _bc_cfg(), _BCLLM("{}"))
    both = box8.run("broadcast", {"to": "名单", "text": "A", "intent": "B"})
    neither = box8.run("broadcast", {"to": "名单"})
    ok &= check("两个都给 → 拒绝并要求重新调用",
                "恰好给一个" in both, both)
    ok &= check("两个都没给 → 同样拒绝", "恰好给一个" in neither, neither)

    # 12) 批次发送：逐条发；第 2 条失败就**立刻停**并如实报「剩下的没发」
    _reset_pending()
    agent_tools.set_pending(CHAT, "", "", "预览", kind="broadcast",
                            items=[{"wxid": "wxid_a", "name": "张三", "text": "A"},
                                   {"wxid": "wxid_b", "name": "李四", "text": "B"},
                                   {"wxid": "wxid_c", "name": "王五", "text": "C"}])
    it = agent_tools.pop_pending(CHAT)
    cli8 = _Rec(boom_at=2)
    n8, err8 = agent_tools.send_pending(cli8, it, 0)
    ok &= check("批次发到失败为止", n8 == 1, n8)
    ok &= check("如实报「已发出/剩下的没发」并点名是谁",
                "李四" in str(err8) and "剩下的没有发" in str(err8) and "1/3" in str(err8),
                err8)
    ok &= check("失败之后**没有再往下发**",
                [c[1] for c in cli8.calls] == ["wxid_a"], cli8.calls)

    # 13) 两类群发待确认项在编号菜单里都要说人话、且**绝不出现 wxid**
    _reset_pending()
    agent_tools.set_pending(CHAT, "", "", "范围预览", kind="broadcast_scope",
                            items=[{"wxid": "wxid_a"}] * 3, label="群发范围")
    agent_tools.set_pending(CHAT, "", "", "内容预览", kind="broadcast",
                            items=[{"wxid": "wxid_a"}] * 2, label="群发内容")
    d1, d2 = (agent_tools.describe_pending(x)
              for x in agent_tools.list_pending(CHAT))
    ok &= check("范围项说人话且带人数", "范围" in d1 and "3 个人" in d1, d1)
    ok &= check("内容项说人话且带人数", "内容" in d2 and "2 个人" in d2, d2)
    ok &= check("菜单描述里没有 wxid", "wxid_" not in d1 + d2, d1 + d2)
    ok &= check("范围项明说「还没生成内容、一个字都没发」",
                "一个字都没发" in d1, d1)

    # 14) 分组：群发按组发（分组本身是另一份配置，见 selftest_sched_auto.T14）
    _reset_pending()
    gcfg = _bc_cfg()
    gcfg["groups"] = {"大学同学": [{"wxid": "wxid_a", "name": "张三"},
                                   {"wxid": "wxid_b", "name": "李四"}]}
    gr, gscope, gerr = agent_tools.broadcast_recipients(
        _BC_CONTACTS, gcfg, "分组:大学同学", SELF_WXID)
    ok &= check("「分组:X」解析成组员", [r["wxid"] for r in gr] == ["wxid_a", "wxid_b"],
                (gr, gerr))
    ok &= check("分组不算「所有人」——不走第一道范围确认",
                gscope == "group", gscope)
    ok &= check("组员的称呼来自 auto_reply 名单（张三=老张）",
                gr[0]["address"] == "老张", gr[0])
    ok &= check("组里没在名单里的人就没有称呼（不许编）",
                gr[1]["address"] == "", gr[1])

    gr2, _s2, _e2 = agent_tools.broadcast_recipients(
        _BC_CONTACTS, gcfg, "大学同学", SELF_WXID)
    ok &= check("to 整串正好是组名时也认（用户/模型常常不带前缀）",
                [r["wxid"] for r in gr2] == ["wxid_a", "wxid_b"], gr2)

    _gr3, _s3, gerr3 = agent_tools.broadcast_recipients(
        _BC_CONTACTS, gcfg, "分组:没这个组", SELF_WXID)
    ok &= check("不存在的组 → 一个人都不发，并把现有分组列出来",
                "没这个组" in gerr3 and "大学同学" in gerr3, gerr3)

    # 按组群发走完整条路（不调模型：给原话）
    cli_g = _Rec()
    cfg_g = _bc_cfg(whitelist=["张三"])
    cfg_g["groups"] = gcfg["groups"]
    box_g = _bc_box(cli_g, cfg_g, _BCLLM("不该被调用"))
    out_g = box_g.run("broadcast", {"to": "分组:大学同学", "text": "明天放假一天"})
    ok &= check("按组群发：白名单里的人直接发",
                cli_g.calls == [("text", "wxid_a", "明天放假一天")], cli_g.calls)
    pg = agent_tools.list_pending(CHAT)
    ok &= check("按组群发：其余进一条待确认批次",
                len(pg) == 1 and pg[0]["kind"] == "broadcast"
                and [x["name"] for x in pg[0]["items"]] == ["李四"], pg)
    ok &= check("按组群发不需要第二次范围确认（组是有界的）",
                "范围" not in out_g, out_g)
    ok &= check("分组为空时如实说、不瞎发", _bc_empty_group_says_so(), "")

    # 15) 微信自带的标签（成员藏在 contact_fts 的 search_key 第 4 段）
    #     ⚠️ 一个假客户端要同时干两件事：`ToolBox.client` 既是查库的、也是发消息的。
    #     拆成两个（查库一个、发送一个）就发不出去了——这条一开始就写错过。
    lcfg = _bc_cfg(whitelist=["张三"])
    _reset_pending()          # 上一条用例的批次还在队列里，先清干净再验数量
    lcli = _LabelClient()
    box_l = agent_tools.ToolBox(lcli, lcfg, _BC_CONTACTS, SELF_WXID, CHAT,
                                cfg_provider=lambda: lcfg,
                                llm_factory=lambda: _BCLLM("不该被调用"))
    out_l = box_l.run("broadcast", {"to": "标签:亲人", "text": "明天放假一天"})
    sent = [c for c in lcli.calls if c[0] == "text"]
    ok &= check("标签里**在白名单**的张三直接发出、原话一字不改",
                sent == [("text", "wxid_a", "明天放假一天")], (sent, out_l))
    pend_l = agent_tools.list_pending(CHAT)
    ok &= check("标签里名单外的人进**一条**待确认批次（不是逐人确认）",
                len(pend_l) == 1 and pend_l[0]["kind"] == "broadcast"
                and [x["name"] for x in pend_l[0]["items"]] == ["李四"], pend_l)
    _reset_pending()
    ok &= check("标签**不走**第一道范围确认", "范围" not in out_l, out_l)

    recips_l, scope_l, err_l = agent_tools.broadcast_recipients(
        _BC_CONTACTS, lcfg, "亲人", SELF_WXID, client=lcli)
    ok &= check("不带前缀、to 正好是标签名时也认",
                scope_l == "label" and [r["wxid"] for r in recips_l] == ["wxid_a", "wxid_b"],
                (recips_l, scope_l, err_l))
    ok &= check("假命中（备注里带「亲人」但标签段是空的）**没有被算进去**",
                "wxid_d" not in [r["wxid"] for r in recips_l], recips_l)

    _r, _s, err_l2 = agent_tools.broadcast_recipients(
        _BC_CONTACTS, lcfg, "标签:没这个标签", SELF_WXID, client=lcli)
    ok &= check("不存在的标签 → 一个人都不发，并列出有哪些标签",
                "没这个标签" in err_l2 and "亲人" in err_l2, err_l2)

    # 「读不到」不许说成「不存在」——对用户是两件完全不同的事
    _r, _s, err_l3 = agent_tools.broadcast_recipients(
        _BC_CONTACTS, lcfg, "标签:亲人", SELF_WXID, client=_Boom())
    ok &= check("读不到标签时说的是「读不到」，不是「没有这个标签」",
                "读**不到**" in err_l3 and "没有「亲人」" not in err_l3, err_l3)

    _r, _s, err_l4 = agent_tools.broadcast_recipients(
        _BC_CONTACTS, lcfg, "标签:亲人", SELF_WXID, client=None)
    ok &= check("链路没有查库能力时如实说、并给出替代做法",
                "读不到" in err_l4.replace("**", "") and "分组" in err_l4, err_l4)

    # 16) `group` 工具的 labels 动作（看微信标签）
    #     「没有查库能力」只能用 client=None 直接验：ToolBox 永远有一个 client。
    ok &= check("没有查库能力时如实说「读不到」（不是「你没有标签」）",
                "读不到" in groups.labels_text(None), groups.labels_text(None))
    st2 = box_l.t_group({"action": "labels"})
    ok &= check("group 工具：列出微信标签和人数", "亲人" in st2 and "2 人" in st2, st2)
    ok &= check("group 工具：说明标签只读、改不了",
                "只读" in st2 or "微信那边改" in st2, st2)
    _reset_pending()
    return ok


class _LabelClient:
    """假 hook：既能应答「标签」那两条查询，也能**记账发消息**。

    刻意混进**假命中**：`wxid_d` 的备注里有「亲人」但标签段是空的
    （实测标签「1」LIKE 命中 412 行、真成员只有 1 个）。
    """

    def __init__(self):
        self.calls = []

    def send_text(self, msg, wxid):
        self.calls.append(("text", wxid, msg))

    def query_sql(self, db, sql):
        if db == "contact.db":
            if "contact_label" in sql:
                return [{"label_id_": "2", "label_name_": "亲人"}]
            return [{"x": 1}]                      # v4 探针要通
        if db == "contact_fts.db" and "contact_fts_v5" in sql:
            def key(remark, nick, labels, alias):
                return "\x08".join([remark, "", nick, labels, alias, "", ""])
            return [{"u": "wxid_a", "k": key("", "王小明", "亲人", "lww1")},
                    {"u": "wxid_b", "k": key("王五", "五哥:岩", "亲人,家", "")},
                    {"u": "wxid_d", "k": key("亲人小卖部", "小卖部", "", "shop1")}]
        return []


def _bc_empty_group_says_so():
    """空分组的报错必须点破「是空的」，而不是含糊地失败。"""
    cfg = _bc_cfg()
    cfg["groups"] = {"空组": []}
    _r, _s, err = agent_tools.broadcast_recipients(
        _BC_CONTACTS, cfg, "分组:空组", SELF_WXID)
    return "空组" in err and "空" in err


# ------------------------------------------- 8. 历史窗口：条数 vs 时间范围

_HIST_CONTACTS = [{"wxid": "wxid_zhangsan", "name": "张三", "remark": "张三"}]

# 5 条假消息，形状照抄 live_history 的返回（含一条 `create_time=0` 的脏行）。
# 最早一条在 9-28 —— 用来证明「这批实际覆盖到什么时候」会被如实报出来。
_HIST_MSGS = [
    {"time": "2026-09-28 23:42:56", "content": "在吗", "is_self": 1, "_ts": 1790610176},
    {"time": "2026-09-30 16:02:02", "content": "考完了，一般", "is_self": 1, "_ts": 1790764922},
    {"time": "2026-09-30 18:50:47", "content": "去哪吃", "is_self": 0, "_ts": 1790775047},
    {"time": "时间未知", "content": "（脏行 create_time=0）", "is_self": 0, "_ts": 0},
    {"time": "2026-10-01 20:28:33", "content": "认真说下 muse", "is_self": 1, "_ts": 1790857713},
]


def test_history_window():
    """「最近 N 天」必须真的按时间查，且**只能看到多少就说多少**。

    2026-10-01 真机缺陷：用户问「我跟张三最近 10 天说了什么」，模型手里只有
    提示词预取的**最新 30 条**（那条会话 7532 条、从 2026-01 就有），却答成
    「最近 10 天（9/30–10/1）」——范围是它自己编的，因为没人告诉它窗口有多小。
    这一组用例钉两件事：① read_history 吃 days 并换算成 since；
    ② 返回文本里必须带**实际覆盖范围**和**取满上限的警告**。
    """
    print("\n── 历史窗口：不许把「最近 N 条」说成「最近 N 天」──")
    ok = True

    ok &= check("span_of 跳过「时间未知」、只报真实跨度",
                agent_tools.span_of(_HIST_MSGS)
                == "2026-09-28 23:42:56 ~ 2026-10-01 20:28:33",
                agent_tools.span_of(_HIST_MSGS))
    ok &= check("span_of 全是时间未知时返回空串（不编一个跨度）",
                agent_tools.span_of([{"time": "时间未知"}]) == "",
                agent_tools.span_of([{"time": "时间未知"}]))
    ok &= check("span_of 空输入不炸",
                agent_tools.span_of([]) == "" and agent_tools.span_of(None) == "")

    calls = []
    real = live_history.query_contact_history

    def fake(client, talker, limit=50, keyword=None, since=None, until=None):
        calls.append({"talker": talker, "limit": limit, "since": since,
                      "until": until})
        if since is not None:
            return []                 # 假库：用户问的那段时间里一条都没有
        return list(_HIST_MSGS)[-int(limit):]

    live_history.query_contact_history = fake
    try:
        box = _box({"agent": {"max_queries": 20}}, contacts=_HIST_CONTACTS)

        out = box.run("read_history", {"contact": "张三", "limit": 5})
        ok &= check("报出这批实际覆盖到什么时候（模型才知道窗口有多小）",
                    "实际覆盖" in out and "2026-09-28" in out, out)
        ok &= check("取满条数上限时明说「更早的没有取」",
                    "更早的没有取" in out, out)
        ok &= check("并且给出翻页的确切做法（until + 这批最早那条的时间）",
                    "until" in out and "2026-09-28 23:42:56" in out, out)
        ok &= check("明确否定「只调小 days」这条错路（实测 days=10 与 days=30 同批）",
                    "只把 days 调小是没用的" in out, out)
        ok &= check("时间未知的脏行照实写「时间未知」，不编成 1970 年",
                    "1970-" not in out and "时间未知" in out, out)

        t0 = time.time()
        out2 = box.run("read_history", {"contact": "张三", "days": 10})
        want = t0 - 10 * 86400.0
        ok &= check("days 被换算成 since 传下去（这就是唯一能按时间查的入口）",
                    calls[-1]["since"] is not None
                    and abs(calls[-1]["since"] - want) < 30, calls[-1])
        ok &= check("问到的时间段里没有记录时，明说「更早的记录还在」",
                    "更早的记录还在" in out2, out2)

        n = len(calls)
        bad = box.run("read_history", {"contact": "张三", "days": 0})
        ok &= check("days=0 如实拒绝（不静默当成没填）", "大于 0" in bad, bad)
        bad2 = box.run("read_history", {"contact": "张三", "days": "十天"})
        ok &= check("days 非数字如实拒绝", "数字" in bad2, bad2)
        bad3 = box.run("read_history", {"contact": "张三", "days": 999999})
        ok &= check("days 超上限如实拒绝、不静默夹取", "最大" in bad3, bad3)
        ok &= check("三条拒绝路径**一次库都没查**", len(calls) == n, len(calls))

        # until = 往更早翻页的上界。**只给 days 翻不到更早**（它锚在「现在」），
        # 所以这是「10 天一次装不下」时唯一能往回走的办法。
        u = "2026-09-30 00:00:00"
        box.run("read_history", {"contact": "张三", "days": 10, "until": u})
        ok &= check("until 被解析成 epoch 传下去",
                    calls[-1]["until"] == agent_tools.parse_time_arg(u),
                    calls[-1])
        ok &= check("parse_time_arg 也认 epoch 数字",
                    agent_tools.parse_time_arg(1790784000) == 1790784000.0,
                    agent_tools.parse_time_arg(1790784000))
        ok &= check("parse_time_arg 空值返回 None（= 那一侧不限）",
                    agent_tools.parse_time_arg(None) is None
                    and agent_tools.parse_time_arg("") is None)
        n2 = len(calls)
        badu = box.run("read_history", {"contact": "张三", "until": "前天"})
        ok &= check("until 看不懂时如实拒绝（不当成没填）", "没看懂" in badu, badu)
        ok &= check("until 非法时也不查库", len(calls) == n2, len(calls))

        # 没给 days 时不能凭空说「这段时间取全了」——按条数的窗口没有这个信息
        out3 = box.run("read_history", {"contact": "张三", "limit": 50})
        ok &= check("没给 days 且没取满时，不许声称「取全了」",
                    "取全了" not in out3, out3)
    finally:
        live_history.query_contact_history = real
    return ok


# ------------------------------------------- 9. 「那天/那月」按具体时间取

def test_when_day():
    """「9 月 30 号那天发生了什么」→ 查那个人的**那一天**，而且要能看全。

    `days` 只能表达「最近 N 天」（锚在现在），所以具体某一天必须另有一条路：
    `read_history(when=...)`。这一组钉三件事：
      ① 时间说法解析成区间（认不出来的**如实拒绝，绝不猜**）；
      ② 有界范围**问总数**、能装下就全给、装不下就说「一共 N 条，给了最新的 M 条」；
      ③ 非法日期（2月30号）不许顺延、也不许掉成「整月」。
    """
    print("\n── 「那天/那月」：按具体时间取那个人的记录（不是只看最近 N 条）──")
    ok = True
    now = time.mktime((2026, 10, 1, 12, 0, 0, 0, 0, -1))

    for spec, label in (("9月30号", "2026-09-30"), ("2026-09-30", "2026-09-30"),
                        ("2026年9月30日", "2026-09-30"), ("9/30", "2026-09-30"),
                        ("9-30", "2026-09-30"), ("昨天", "2026-09-30"),
                        ("前天", "2026-09-29"), ("今天", "2026-10-01"),
                        ("9月", "2026-09"), ("2026-09", "2026-09"),
                        ("上个月", "2026-09"), ("上周", "2026-09-21 ~ 2026-09-27"),
                        ("9月30号那天", "2026-09-30"),
                        ("9月1号到9月15号", "2026-09-01 ~ 2026-09-15"),
                        ("2026-09-01~2026-09-30", "2026-09-01 ~ 2026-09-30")):
        got = agent_tools.parse_when_spec(spec, now=now)
        ok &= check(f"parse_when_spec({spec!r}) → {label}",
                    got is not None and got[2] == label, got)
    for bad in ("瞎写的", "", None, "九月三十号"):
        ok &= check(f"{bad!r} 认不出来 → None（如实拒绝，绝不猜）",
                    agent_tools.parse_when_spec(bad, now=now) is None,
                    agent_tools.parse_when_spec(bad, now=now))
    ok &= check("2月30号 → None（不许顺延成 3 月，也不许掉成「整月 2 月」）",
                agent_tools.parse_when_spec("2月30号", now=now) is None,
                agent_tools.parse_when_spec("2月30号", now=now))
    ok &= check("13月 → None", agent_tools.parse_when_spec("13月", now=now) is None)

    calls = []
    state = {"total": 2}
    real_q = live_history.query_contact_history
    real_c = live_history.count_history

    def fake_q(client, talker, limit=50, keyword=None, since=None, until=None):
        calls.append({"limit": limit, "since": since, "until": until})
        n = min(int(limit), state["total"])
        return [{"time": "2026-09-30 09:00:00", "content": f"第{i}条",
                 "is_self": 1, "_ts": 1000 + i} for i in range(n)]

    def fake_c(client, talker, since=None, until=None, keyword=None):
        return {"count": state["total"], "first": 1, "last": 2, "source": "fts"}

    live_history.query_contact_history = fake_q
    live_history.count_history = fake_c
    try:
        box = _box({"agent": {"max_queries": 20}}, contacts=_HIST_CONTACTS)
        y = time.localtime().tm_year
        exp = time.mktime((y, 9, 30, 0, 0, 0, 0, 0, -1))

        out = box.run("read_history", {"contact": "张三", "when": "9月30号"})
        ok &= check("when 解析成那天的 [00:00:00, 23:59:59] 传下去",
                    calls[-1]["since"] == exp and calls[-1]["until"] == exp + 86399,
                    calls[-1])
        ok &= check("报出这个范围一共多少条", "一共 2 条" in out, out)
        ok &= check("装得下就明说「已经全部取到了」",
                    "已经全部取到" in out, out)

        state["total"] = 500
        n0 = len(calls)
        out2 = box.run("read_history", {"contact": "张三", "when": "9月30号"})
        ok &= check("范围太大时**报出真总数**（不能只说给了多少条）",
                    "一共 500 条" in out2, out2)
        ok &= check("单次上限就是 when 那条路的 200 条",
                    calls[-1]["limit"] == agent_tools.MAX_WHEN_MESSAGES, calls[-1])
        ok &= check("并给出往更早翻的做法（until）", "until" in out2, out2)
        ok &= check("when 这条路一次查两次库：总数 + 取数", len(calls) == n0 + 1)

        out3 = box.run("read_history",
                       {"contact": "张三", "when": "9月30号", "limit": 20})
        ok &= check("模型显式给了 limit 就尊重它", calls[-1]["limit"] == 20, calls[-1])

        n1 = len(calls)
        bad = box.run("read_history",
                      {"contact": "张三", "when": "9月30号", "days": 3})
        ok &= check("when 和 days 同时给 → 如实拒绝（不猜哪个优先）",
                    "只能给一个" in bad, bad)
        bad2 = box.run("read_history", {"contact": "张三", "when": "瞎写的"})
        ok &= check("when 看不懂 → 如实拒绝并给例子", "没看懂" in bad2, bad2)
        ok &= check("两条拒绝路径一次库都没查", len(calls) == n1, len(calls))
    finally:
        live_history.query_contact_history = real_q
        live_history.count_history = real_c
    return ok


# ------------------------------------------- 10. 那天所有聊天 + 全文导出

def test_day_history():
    """`day_history`：概览进对话、**全文进文件**（不进上下文）。

    这是「一整个月看不完」的答案：卡住的是模型上下文（实测跨全部会话
    2026-09-30 一天 4782 条），不是查库。所以这条路必须做到——
    ① 概览如实（总数 + 会话数 + 每个会话多少条）；
    ② 全文写成本地文件、返回值里**一个字正文都没有**；
    ③ 文件名安全（联系人是别人可控的字符串，不许穿越目录）。
    """
    print("\n── day_history：那天的所有会话 + 全文导成本地文件 ──")
    ok = True
    tmp = tempfile.mkdtemp(prefix="dsh_export_")
    real_ov = live_history.day_overview
    real_rm = live_history.range_messages
    real_cnt = live_history.count_history
    real_dir = agent_tools._EXPORT_DIR
    real_max = agent_tools.EXPORT_MAX_MESSAGES
    agent_tools._EXPORT_DIR = tmp
    ov = [{"talker": "room_1@chatroom", "count": 2003, "first": 1, "last": 2},
          {"talker": "wxid_zhangsan", "count": 37, "first": 1, "last": 2}]
    msgs = [{"talker": "wxid_zhangsan", "content": "考完了", "is_self": 1,
             "time": "2026-09-30 18:33:00", "_ts": 1},
            {"talker": "wxid_zhangsan", "content": "穿长裤了吗", "is_self": 0,
             "time": "2026-09-30 18:34:00", "_ts": 2},
            {"talker": "room_1@chatroom", "content": "群里的消息", "is_self": 0,
             "sender": "wxid_x", "time": "2026-09-30 19:00:00", "_ts": 3}]
    live_history.day_overview = lambda c, s, u, limit=200: list(ov)
    live_history.range_messages = (
        lambda c, s, u, max_total=8000, page=800, talker=None: list(msgs))
    try:
        box = _box({"agent": {"max_queries": 20}}, contacts=_HIST_CONTACTS)
        out = box.run("day_history", {"when": "9月30号"})
        ok &= check("概览：总数 + 会话数", "一共 2040 条" in out and "2 个会话" in out, out)
        ok &= check("概览：按条数列出会话（群标出来）", "2003 条" in out, out)
        ok &= check("**正文一个字都不进对话**（几千条会撑爆上下文）",
                    "考完了" not in out and "群里的消息" not in out, out)
        ok &= check("告诉模型全文导到哪个文件了", "全部聊天.txt" in out, out)

        files = os.listdir(tmp)
        ok &= check("导出文件真的落盘了", len(files) == 1, files)
        ok &= check("文件名安全（没有路径分隔符）",
                    files and "/" not in files[0] and "\\" not in files[0], files)
        body = open(os.path.join(tmp, files[0]), encoding="utf-8").read()
        ok &= check("文件里有全文（时间 + 说话人 + 内容）",
                    "考完了" in body and "张三" in body and "2026-09-30" in body,
                    body[:160])

        # 到了上限必须**如实说截断**，不静默少导
        agent_tools.EXPORT_MAX_MESSAGES = 1
        out_cut = box.run("day_history", {"when": "9月30号"})
        ok &= check("到上限时明说「已截断」", "截断" in out_cut, out_cut)

        n_before = len(os.listdir(tmp))
        out2 = box.run("day_history", {"when": "9月30号", "save": False})
        ok &= check("save=false：不导出、也不报路径",
                    "全部聊天.txt" not in out2 and len(os.listdir(tmp)) == n_before,
                    out2)

        # 按人导出：「把张三 9 月全导出来」——全文进文件，所以不看对话上下文闸
        seen = []
        live_history.count_history = (
            lambda c, w, since=None, until=None, keyword=None:
            {"count": 548, "first": 1, "last": 2, "source": "fts"})
        live_history.range_messages = (
            lambda c, s, u, max_total=8000, page=800, talker=None:
            (seen.append(talker) or list(msgs)))
        out3 = box.run("day_history", {"contact": "张三", "when": "9月"})
        ok &= check("按人：报出这个人这段时间一共多少条", "一共 548 条" in out3, out3)
        ok &= check("按人：导出时把 talker 传下去（只导那个人）",
                    bool(seen) and seen[-1] == "wxid_zhangsan", seen)

        # ---- 多天 + 跨全部会话 → 自动按天分文件（这才是「一整个月」的正解）----
        ok &= check("split_days：3 天切成 3 段，每段是完整一天",
                    [(d[2], d[1] - d[0]) for d in agent_tools.split_days(
                        agent_tools.parse_when_spec("9月1号到9月3号")[0],
                        agent_tools.parse_when_spec("9月1号到9月3号")[1])]
                    == [("2026-09-01", 86399), ("2026-09-02", 86399),
                        ("2026-09-03", 86399)]
                    if time.localtime().tm_year == 2026 else True)
        ok &= check("split_days：单天只切一段",
                    len(agent_tools.split_days(
                        agent_tools.parse_when_spec("9月30号")[0],
                        agent_tools.parse_when_spec("9月30号")[1])) == 1)
        ok &= check("split_days：非法输入返回空（不炸）",
                    agent_tools.split_days(0, 0) == []
                    and agent_tools.split_days(9, 1) == [])

        w0 = time.mktime((time.localtime().tm_year, 9, 1, 0, 0, 0, 0, 0, -1))
        w1 = w0 + 15 * 86400 - 1
        day_windows = []
        live_history.range_messages = (
            lambda c, s, u, max_total=8000, page=800, talker=None:
            (day_windows.append((s, u)) or list(msgs)))
        n_multi = set(os.listdir(tmp))
        out_m = box.run("day_history", {"when": "9月1号到9月3号"})
        ok &= check("多天跨会话：**每天一个文件**（3 天 → 3 个新文件）",
                    len(set(os.listdir(tmp)) - n_multi) == 3, os.listdir(tmp))
        ok &= check("回复里逐天列出（哪天、多少条、文件到哪儿）",
                    all(d in out_m for d in ("2026-09-01", "2026-09-02",
                                             "2026-09-03")), out_m)
        ok &= check("每一天用的是**它自己那一天**的时间窗（不是整个范围导一次）",
                    len(day_windows) == 3
                    and day_windows[0][1] - day_windows[0][0] == 86399
                    and day_windows[2][0] - day_windows[0][0] == 2 * 86400,
                    day_windows[:3])
        # 一个月必须**一次导完**（用户明确要的：问一个月就导一个月），不是导几天收工
        out_month = box.run("day_history", {"when": "2026-09"})
        have = set(os.listdir(tmp))
        ok &= check("问一整个月 → 9/1–9/30 每天一个文件都在（不中途收工）",
                    all(f"2026-09-{d:02d}-全部聊天.txt" in have
                        for d in range(1, 31)), sorted(have))
        ok &= check("整月导出**不该**再出现「还有 N 天没导」",
                    "天没导" not in out_month, out_month)
        # 只有比 40 天更长的范围才分段，并且要给出下一段的确切 when（否则原地打转）
        out_left = box.run("day_history",
                           {"when": "2026-09-01到2026-12-31"})
        ok &= check("超长范围：明说「还有 N 天没导」",
                    "还有" in out_left and "天没导" in out_left, out_left)
        ok &= check("超长范围：给出**下一段的确切 when**（起始日往后推，不会原地打转）",
                    "when=「2026-10-11到2026-12-31」" in out_left, out_left)

        bad = box.run("day_history", {"when": "瞎写的"})
        ok &= check("when 看不懂 → 如实拒绝", "没看懂" in bad, bad)
        bad2 = box.run("day_history", {})
        ok &= check("不给 when → 说清要什么", "哪一天" in bad2, bad2)
    finally:
        live_history.day_overview = real_ov
        live_history.range_messages = real_rm
        live_history.count_history = real_cnt
        agent_tools._EXPORT_DIR = real_dir
        agent_tools.EXPORT_MAX_MESSAGES = real_max
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_read_file_by_name():
    """`read_file` 只给 `name` 时的纯磁盘兜底（2026-10-01 加的）。

    为什么单独立一条：这条路的**唯一理由**就是「消息记录里没有 ≠ 文件不在本机」
    —— `find_files` 按消息记录列文件，**用户自己发出去的文件**经常不在记录里，
    而文件明明躺在 `msg/file/` 下（实测：发出去的 zip 与本地那份同大小同秒）。
    所以它必须 ① **一条库都不查**（用 `_Boom` 客户端证明）② 多份命中时**不替用户挑**。
    """
    print("\n── read_file 只给 name：不查库 + 多份不猜 ──")
    ok = True
    tmp = tempfile.mkdtemp(prefix="selftest_policy_files_")
    old_roots = file_read.files_roots
    file_read.files_roots = lambda: [tmp]
    try:
        def put(name, data=b"x"):
            d = os.path.join(tmp, "2026-10")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, name)
            with open(p, "wb") as f:
                f.write(data)
            return p

        put("刚发的那份.txt", "这是一份自己发出去的文件，用来验证只给文件名也能读。".encode("utf-8"))
        put("发票A.pdf")
        put("发票B.pdf")

        box = _box()            # client 是 _Boom：一查库就炸
        out = box.t_read_file({"name": "刚发的那份.txt"})
        ok &= check("只给 name → 读出内容（客户端一次都没碰）",
                    "这是一份自己发出去的文件" in out, out)

        out = box.t_read_file({"name": "发票"})
        ok &= check("命中多份 → 列出候选、让用户挑",
                    "发票A.pdf" in out and "发票B.pdf" in out, out)
        ok &= check("命中多份 → 一份正文都不返回",
                    "提取出的文字" not in out, out)

        out = box.t_read_file({"name": "根本没有这份"})
        ok &= check("查不到 → 明说没找到", "没找到" in out, out)

        out = box.t_read_file({"name": "../../windows/system.ini"})
        ok &= check("路径穿越 → 挡住（边界和 locate 同一套）", "不合法" in out, out)

        out = box.t_read_file({})
        ok &= check("两种给法都不给 → 提示说清（不是含糊的报错）",
                    "contact" in out and "local_id" in out and "name" in out, out)
    finally:
        file_read.files_roots = old_roots
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_read_file_cursor():
    """`read_file` 的「继续读」：只给 cursor 就能读下一页，且**一次库都不查**（2026-10-02）。

    这是"输入不限大小"对模型那一侧的接口：本地全文已导出，一次只给一页。
    """
    print("\n── read_file 只给 cursor：继续读，不查库 ──")
    ok = True
    tmp = tempfile.mkdtemp(prefix="selftest_policy_page_")
    old_roots = file_read.files_roots
    file_read.files_roots = lambda: [tmp]
    try:
        body = "".join(f"第{i:03d}行 " + "内容要够长才能跨页。" * 3 + "\n" for i in range(40))
        d = os.path.join(tmp, "2026-10")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "长文件.txt"), "w", encoding="utf-8", newline="") as f:
            f.write(body)
        cfg = {"agent": {}, "file": {"max_chars": 200,
                                     "export_dir": os.path.join(tmp, "exports")}}
        box = _box(cfg)            # client 是 _Boom：一查库就炸
        out1 = box.t_read_file({"name": "长文件.txt"})
        ok &= check("第一页读出 + 尾部给 cursor",
                    "cursor=" in out1 and "第000行" in out1, out1[-140:])
        m = re.search(r"cursor=([0-9a-f]{16}:\d+)", out1)
        ok &= check("cursor 格式正确", bool(m), out1[-140:])
        if not m:
            return ok
        out2 = box.t_read_file({"cursor": m.group(1)})       # **只给 cursor**
        ok &= check("只给 cursor 就能继续读（不用 contact/local_id，也不查库）",
                    "续读" in out2 and "第" in out2, out2[:140])
        ok &= check("第二页**不重复**第一页的内容", "第000行" not in out2, out2[:140])
    finally:
        file_read.files_roots = old_roots
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_read_file_heavy_goes_background():
    """重活要走后台线程，并且**明确告诉模型「现在没有内容、别编」**（2026-10-02 T4）。

    为什么要单独立一条：读大文件一旦同步跑，bot 会在那几分钟里**完全不轮询**；
    挪到后台之后，工具当轮**必须**返回"已提交 + 不许编造"，读完由主线程发内容。
    """
    print("\n── read_file 重活 → 后台读，模型当轮拿不到内容 ──")
    ok = True
    tmp = tempfile.mkdtemp(prefix="selftest_policy_bg_")
    old_roots = file_read.files_roots
    file_read.files_roots = lambda: [tmp]
    try:
        d = os.path.join(tmp, "2026-10")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "大文件.txt"), "w", encoding="utf-8", newline="") as f:
            f.write("这一行会被判定为体积超限，所以要走后台。" * 50)
        cfg = {"agent": {}, "file": {"inline_bytes": 10,      # 任何真文件都超过它 → 必走后台
                                     "export_dir": os.path.join(tmp, "exports")}}
        box = _box(cfg)
        read_worker.reset_for_test()
        out = box.t_read_file({"name": "大文件.txt"})
        ok &= check("重活返回「已提交后台读取」", "已提交后台读取" in out, out)
        ok &= check("并明确说「现在没有内容、绝对不要编造」",
                    "绝对不要编造" in out and "还没有内容" in out, out)
        ok &= check("当轮**不返回正文**（后台还没读完）",
                    "会被判定为体积超限" not in out, out)
        ok &= check("worker 里确实有活儿（status 非空）",
                    bool(read_worker.status(cfg)), read_worker.status(cfg))

        got = []
        t0 = time.time()
        while not got and time.time() - t0 < 10:
            got += read_worker.drain()
            time.sleep(0.02)
        ok &= check("后台读完，结果里带正文（交给主线程去发）",
                    bool(got) and "会被判定为体积超限" in (got[0].get("text") or ""), got)
    finally:
        read_worker.reset_for_test()
        file_read.files_roots = old_roots
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_send_file():
    """发普通文件：当前 hook 没有这个接口 → **当场如实拒绝，且不进待确认队列**。

    为什么「不进队列」也要单独断言：如果先登记待确认项、等用户回「确认」才失败，
    就等于**让用户白确认一次**——而发文件是不可逆动作，用户的确认成本很高。
    所以拒绝必须发生在**登记之前**。

    另外这一条守住的是「不许假装发了」：开关关着时**一次 client 调用都不许有**
    （用 `_Boom` 客户端证明）。
    """
    print("\n── 发普通文件：当前 hook 做不到 → 当场拒绝、不白让用户确认 ──")
    ok = True
    tmp = tempfile.mkdtemp(prefix="selftest_policy_sendfile_")
    old_roots = file_read.files_roots
    file_read.files_roots = lambda: [tmp]
    contacts = [{"wxid": "wxid_zhangsan", "name": "张三", "remark": "张三"}]
    _reset_pending()
    try:
        d = os.path.join(tmp, "2026-10")
        os.makedirs(d, exist_ok=True)
        good = os.path.join(d, "合同.pdf")
        with open(good, "wb") as f:
            f.write(b"%PDF-1.4 x")

        # ① 开关默认关 → 如实拒绝
        out = _box(contacts=contacts).t_send_file({"to": "张三", "name": "合同.pdf"})
        ok &= check("默认（没写 send_file_hook）→ 如实拒绝「发不了普通文件」",
                    "发不了普通文件" in out, out)
        ok &= check("拒绝时说清根因是 hook 没有这个接口",
                    "hook" in out and "接口" in out, out)
        ok &= check("拒绝时明确禁止绕路 / 禁止假装",
                    "不要改用别的方式" in out and "假装" in out, out)
        ok &= check("**没有产生待确认项**（不让用户白确认一次）",
                    agent_tools.list_pending(CHAT) == [],
                    agent_tools.list_pending(CHAT))

        # fail-safe：写歪的开关值一律当关（和 search.enabled / privacy.redact 同一档）
        for bad in ("true", 1, "1", 0, None):
            cfg = {"agent": {"send_file_hook": bad}}
            out_b = _box(cfg=cfg, contacts=contacts).t_send_file(
                {"to": "张三", "name": "合同.pdf"})
            ok &= check(f"send_file_hook={bad!r} → 仍按关处理（fail-safe）",
                        "发不了普通文件" in out_b, out_b)
            _reset_pending()

        # ② 开关打开 → 才进入「定位 → 校验 → 待确认」流程
        cfg_on = {"agent": {"send_file_hook": True}}
        box = _box(cfg=cfg_on, contacts=contacts)

        out_miss = box.t_send_file({"to": "张三", "name": "根本没有这份.pdf"})
        ok &= check("文件不存在 → 明说没找到、且没进队列",
                    "没找到" in out_miss and agent_tools.list_pending(CHAT) == [],
                    out_miss)

        for n in ("发票A.pdf", "发票B.pdf"):
            with open(os.path.join(d, n), "wb") as f:
                f.write(b"%PDF x")
        out_multi = box.t_send_file({"to": "张三", "name": "发票"})
        ok &= check("多份命中 → 列候选、**不替用户挑**",
                    "发票A.pdf" in out_multi and "发票B.pdf" in out_multi, out_multi)
        ok &= check("多份命中 → 不进队列", agent_tools.list_pending(CHAT) == [])

        out_ok = box.t_send_file({"to": "张三", "name": "合同.pdf"})
        pend = agent_tools.list_pending(CHAT)
        ok &= check("开关打开 → 进入待确认（等用户回「确认」）",
                    "确认" in out_ok and len(pend) == 1, f"{out_ok!r} / {pend}")
        if pend:
            desc = agent_tools.describe_pending(pend[0])
            ok &= check("待确认项描述带文件名、**不带本机路径**",
                        "合同.pdf" in desc and tmp not in desc, desc)
            ok &= check("待确认项带 file 字段（重启恢复要用）",
                        bool(pend[0].get("file")), list(pend[0]))
            ok &= check("kind 是 file", pend[0].get("kind") == "file",
                        pend[0].get("kind"))

        # ③ send_pending 的 file 分支：路径复核不过 → 一份都不发
        _reset_pending()
        rec = _Rec()
        n1, err1 = agent_tools.send_pending(
            rec, {"to_wxid": "wxid_a", "to_name": "张三", "kind": "file",
                  "file": os.path.join(tmp, "不存在的.pdf"), "text": "x",
                  "ts": time.time()})
        ok &= check("复核不过 → 0 条 + 一次都没发给 client",
                    n1 == 0 and rec.calls == [] and err1 and "复核" in str(err1),
                    f"{n1} / {rec.calls} / {err1}")

        # ④ 复核通过 → 真去调 client.send_file（把 hook 的真实结果带回来）
        _reset_pending()
        rec2 = _Rec()
        n2, err2 = agent_tools.send_pending(
            rec2, {"to_wxid": "wxid_a", "to_name": "张三", "kind": "file",
                   "file": good, "text": "x", "ts": time.time()})
        ok &= check("复核通过 → 调了 client.send_file 且记账成功",
                    n2 == 1 and err2 is None and rec2.calls
                    and rec2.calls[0][0] == "file", f"{n2} / {err2} / {rec2.calls}")

        # ⑤ client 抛异常（就是当前 hook 的真实行为）→ 如实报，且不许说发了
        _reset_pending()
        rec3 = _Rec(boom_at=1)
        n3, err3 = agent_tools.send_pending(
            rec3, {"to_wxid": "wxid_a", "to_name": "张三", "kind": "file",
                   "file": good, "text": "x", "ts": time.time()})
        ok &= check("client 抛异常 → 0 条 + 原错误带回来",
                    n3 == 0 and err3 is not None, f"{n3} / {err3}")
    finally:
        file_read.files_roots = old_roots
        _reset_pending()
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def main():
    ok = True
    print("\n待确认队列 / 发图白名单 / 查询预算 —— 回归自测（不联网、不碰 30001）")
    ok &= test_queue()
    ok &= test_describe()
    ok &= test_whitelist()
    ok &= test_image_cache_root()
    ok &= test_send_pending_guard()
    ok &= test_partial_send_report()
    ok &= test_budget_clamp()
    ok &= test_broadcast()
    ok &= test_history_window()
    ok &= test_when_day()
    ok &= test_day_history()
    ok &= test_read_file_by_name()
    ok &= test_read_file_cursor()
    ok &= test_read_file_heavy_goes_background()
    ok &= test_send_file()
    _reset_pending()
    print("\n" + "=" * 50)
    print("全部通过 ✅" if ok else "有失败项 ❌")
    print("=" * 50)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
