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

用法：python selftest_policy.py
"""
import os
import subprocess
import sys
import tempfile

import agent_tools
import image_cache

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
    _reset_pending()
    print("\n" + "=" * 50)
    print("全部通过 ✅" if ok else "有失败项 ❌")
    print("=" * 50)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
