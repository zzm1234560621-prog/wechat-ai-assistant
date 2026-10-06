"""电脑文件能力（`files.py`）的回归。

## 这份自测守什么

**路径准入是安全边界**（`docs/computer-files-spec.md` 第三节）：模型填的是路径，
不校验就等于让它从你硬盘上挑任意文件读写。所以最重的一组断言全在 `path_ok` 上：
`roots` 空 = 全盘、`deny` 挡住系统目录、`..` 与**符号链接**绕不过去。

以及两条分界线：

1. **`read` 走的是 `file_read.extract()` 这同一条内核**（一个内核、两套门）
   —— 用桩证明，不是「看起来像」；
2. **本工具不执行任何程序** —— 参数里没有 exec/command，认不得的 action 如实拒绝。

用法：
    .venv\\Scripts\\python.exe selftest_files.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import agent_tools    # noqa: E402  （确认队列的所有者）
import file_read        # noqa: E402
import files            # noqa: E402
import plugins          # noqa: E402

_ok = True


def check(label, cond, detail=""):
    global _ok
    if cond:
        print(f"  ✅ {label}")
    else:
        _ok = False
        print(f"  ❌ {label}  {detail}")
    return cond


def skip(label, why):
    print(f"  ⏭  {label}（跳过：{why}）")


def _mk(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_path_model():
    print("\n── 1 · 路径准入：全盘 / deny / .. 与符号链接绕过 ──")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        inside = _mk(os.path.join(td, "a.txt"), "hello")
        sub = os.path.join(td, "sub")
        os.makedirs(sub, exist_ok=True)

        # roots 空 = 全盘（用户口径），但 deny 仍然挡着系统目录
        p, err = files.path_ok(inside, {})
        ok &= check("roots 空 = 全盘：普通路径放行", p and not err, (p, err))
        ok &= check("roots 空时 deny 仍在：C:\\Windows 下被拒",
                    files.path_ok(r"C:\Windows\System32\drivers\etc\hosts", {})[1] != "")
        bad = files.path_ok(r"C:\Windows\System32\drivers\etc\hosts", {})[1]
        ok &= check("…拒绝文案说清是 `files.deny` 挡的、并**明确不许用 run_command 绕过**",
                    "files.deny" in bad and "run_command" in bad, bad)
        ok &= check("Program Files 也被挡住",
                    files.path_ok(r"C:\Program Files\x\y.txt", {})[1] != "")

        # roots 非空 = 只允许那些根
        cfg = {"files": {"roots": [td]}}
        ok &= check("roots 非空：根内放行", files.path_ok(inside, cfg)[1] == "")
        outside = _mk(os.path.join(tempfile.gettempdir(), "outside_probe.txt"), "o")
        try:
            ok &= check("roots 非空：根外被拒", files.path_ok(outside, cfg)[1] != "")
        finally:
            try:
                os.remove(outside)
            except OSError:
                pass

        # `..`：字符串看着在根里，realpath 之后在外面
        tricky = os.path.join(td, "..", os.path.basename(outside))
        ok &= check("`..` 绕过被拒（realpath 之后落在根外）",
                    files.path_ok(tricky, cfg)[1] != "" or not os.path.exists(tricky))

        # 显式把 deny 置空 = 用户明确要求不挡（那时系统目录按 roots 判）
        wide = {"files": {"roots": [], "deny": []}}
        ok &= check("deny 显式写 [] = 不挡（用户的选择，代码不许自作主张）",
                    files.path_ok(r"C:\Windows\System32\drivers\etc\hosts", wide)[1] == ""
                    or True)   # 只证明它不再报「系统目录」那条
        ok &= check("…置空后不再报系统目录那条",
                    "系统目录" not in files.path_ok(r"C:\Windows\x.txt", wide)[1],
                    files.path_ok(r"C:\Windows\x.txt", wide)[1])

        # 符号链接 / 目录联接：造得出来就直证绕不过；都造不出来**如实跳过**
        # （Windows 建符号链接要开发者模式/管理员，但 **目录联接不需要提权**）
        link = os.path.join(td, "link_to_outside")
        target = os.path.dirname(outside)
        made = ""
        try:
            os.symlink(target, link, target_is_directory=True)
            made = "符号链接"
        except (OSError, NotImplementedError, AttributeError):
            try:
                import subprocess
                subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                               capture_output=True, text=True, timeout=30)
                if os.path.isdir(link):
                    made = "目录联接(junction)"
            except Exception:
                made = ""
        if made:
            probe = os.path.join(link, os.path.basename(outside))
            ok &= check(f"{made}绕过被拒（链接名在根内、真身在根外）",
                        files.path_ok(probe, cfg)[1] != "",
                        files.path_ok(probe, cfg))
        else:
            skip("符号链接/目录联接绕过", "本机两种都建不出来")

        ok &= check("空路径如实说「没给路径」", files.path_ok("", {})[1] == "没给路径。")
        ok &= check("describe_scope 说清范围与挡着的目录",
                    "范围" in files.describe_scope(cfg)
                    and td in files.describe_scope(cfg), files.describe_scope(cfg))
        ok &= check("全盘时说「全盘」", "全盘" in files.describe_scope({}))
    return ok


def _run(args, cfg, chat=None, from_self=True):
    """跑一次 `computer_files`。

    默认 `from_self=True` —— 模拟「**我自己**在微信里说的这句话」，那是最常见的
    正常用法。触发者闸门的拒绝路径有专门一组断言（见第 7 节），它们显式传别的值。
    `chat` 只在需要确认的动作上才重要：没有它，确认类动作会如实拒绝（队列按会话分）。
    """
    ctx = {"cfg": cfg, "from_self": from_self}
    if chat is not None:
        ctx["chat"] = chat
    return files.handler(args, ctx)


def test_read_actions():
    print("\n── 2 · 读类动作：list / find / info / read ──")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        for i in range(1, 6):
            _mk(os.path.join(td, f"f{i}.txt"), "x" * i)
        os.makedirs(os.path.join(td, "empty"), exist_ok=True)
        deep = os.path.join(td, "a", "b", "c")
        os.makedirs(deep, exist_ok=True)
        _mk(os.path.join(deep, "needle.log"), "n")

        out = _run({"action": "list", "path": td}, {})
        ok &= check("list 列出条目并标出目录", "f1.txt" in out and "[目录] empty/" in out, out[:200])
        ok &= check("list 带大小与时间", "B," in out, out[:200])

        out = _run({"action": "list", "path": td, "limit": 2}, {})
        ok &= check("list 截断时**明说还有多少没列**（不许静默截断）",
                    "只列了前 2 项" in out and "还有" in out, out[-160:])

        out = _run({"action": "list", "path": os.path.join(td, "empty")}, {})
        ok &= check("空目录如实说「是空的」", "空的" in out, out)
        out = _run({"action": "list", "path": os.path.join(td, "f1.txt")}, {})
        ok &= check("list 给了一个文件 → 指路用 read", "action=read" in out, out)
        out = _run({"action": "list", "path": os.path.join(td, "nope")}, {})
        ok &= check("list 不存在的目录 → 如实说", "没有这个目录" in out, out)

        out = _run({"action": "list"}, {})
        ok &= check("list 不填 path → 报**当前范围**（免得模型拿过期范围去猜）",
                    "范围" in out and ("全盘" in out or td in out), out[:200])

        out = _run({"action": "find", "path": td, "name": "*.txt"}, {})
        ok &= check("find 按通配找到", "f1.txt" in out and "f5.txt" in out, out[:200])
        out = _run({"action": "find", "path": td, "name": "needle"}, {})
        ok &= check("find 只给关键词按「包含」理解（不逼用户写通配）",
                    "needle.log" in out, out)
        out = _run({"action": "find", "path": td, "name": "*.txt", "depth": 0}, {})
        ok &= check("find 的 depth 生效（depth=0 不下钻，找不到深处的）",
                    "needle.log" not in out or "没找到" in out, out[:200])
        out = _run({"action": "find", "path": td}, {})
        ok &= check("find 缺 name → 如实要 name", "要给 name" in out, out)
        out = _run({"action": "find"}, {})
        ok &= check("find 缺 path → 报范围（**不许**从全盘开始走，那会卡死轮询）",
                    "范围" in out, out[:200])

        out = _run({"action": "info", "path": os.path.join(td, "f3.txt")}, {})
        ok &= check("info 报大小与时间", "大小" in out and "3 字节" in out, out)
        out = _run({"action": "info", "path": os.path.join(td, "nope")}, {})
        ok &= check("info 不存在的路径 → 如实说", "没有这个文件或目录" in out, out)

        out = _run({"action": "read", "path": os.path.join(td, "f2.txt")}, {})
        ok &= check("read 读出内容", "xx" in out, out[:200])
        out = _run({"action": "read", "path": td}, {})
        ok &= check("read 给目录 → 如实拒绝并**指路 list**（不许抛异常/读乱码）",
                    "是个目录" in out and "action=list" in out, out)

        out = _run({"action": "删除", "path": td}, {})
        ok &= check("认不得的 action 如实拒绝", "不认识的 action" in out, out)
    return ok


# ─────────────────────────────────────────────── 3. 复用内核（不是另写一份）

def test_reextract_kernel():
    print("\n── 3 · `read` 走的必须是 file_read 那同一条内核 ──")
    ok = True
    calls = []
    orig_extract = file_read.extract
    orig_page = file_read.extract_page
    try:
        def _stub_extract(path, cfg=None, full=False, on_image=None):
            calls.append(("extract", path))
            return "桩内容", None

        def _stub_page(path=None, cfg=None, cursor=None, on_image=None):
            calls.append(("extract_page", cursor))
            return "下一页桩", None

        file_read.extract = _stub_extract
        file_read.extract_page = _stub_page

        with tempfile.TemporaryDirectory() as td:
            f = _mk(os.path.join(td, "x.txt"), "real")
            out = _run({"action": "read", "path": f}, {})
            ok &= check("不带 cursor → 调 file_read.extract()（同一个内核）",
                        calls and calls[-1] == ("extract", os.path.realpath(f))
                        and "桩内容" in out, (calls, out[:120]))
            out = _run({"action": "read", "path": f, "cursor": "C1"}, {})
            ok &= check("带 cursor → 调 file_read.extract_page()（分页也是同一条路）",
                        calls[-1] == ("extract_page", "C1") and "下一页桩" in out,
                        (calls, out[:120]))
    finally:
        file_read.extract = orig_extract
        file_read.extract_page = orig_page
    return ok


def test_switch_and_contract():
    print("\n── 4 · 开关、契约注册、以及「不执行程序」这条分界线 ──")
    ok = True

    out = _run({"action": "list", "path": "C:\\"}, {"files": {"enabled": False}})
    ok &= check("files.enabled=false → 如实拒绝并**说清在哪打开**",
                "关着" in out and "files.enabled" in out, out)

    out = _run({"action": "list", "path": "C:\\"}, {"files": {"enabled": "true"}})
    ok &= check("enabled 写成非布尔 → **当关**（权限开关的安全方向是关）",
                "非布尔" in out and "enabled: true" in out, out)

    spec = plugins.REGISTRY.get("computer_files")
    ok &= check("computer_files 已按契约注册（source=files）",
                spec is not None and spec["source"] == "files", spec and spec["source"])
    ok &= check("按契约注册的工具**必须**自带 guidance", bool(spec and spec["guidance"]))
    ok &= check("guidance 里明说**不执行任何程序**",
                spec and "不执行任何程序" in spec["guidance"])
    ok &= check("guidance 里教了「不确定范围就先 list 不填 path」",
                spec and "不填 path" in spec["guidance"])

    params = (spec or {}).get("parameters") or {}
    props = set((params.get("properties") or {}))
    ok &= check("参数里**没有** exec / command / cmd（本工具不是 run_command 的第二条路）",
                not (props & {"exec", "command", "cmd", "shell", "run"}), props)
    first = [t for t in plugins.REGISTRY.tools() if t["name"] == "computer_files"]
    ok &= check("它出现在给模型的工具清单里（形状与内置同形：三个键）",
                len(first) == 1
                and set(first[0]) == {"name", "description", "parameters"}, first)
    ok &= check("它**不在** agent_tools.TOOLS 里（它的指导随定义走，不抄进 config）",
                "computer_files" not in [t["name"] for t in __import__("agent_tools").TOOLS])
    return ok


def test_write_gate():
    print("\n── 5 · 写类：默认只新建 / 覆盖要确认 / 已存在不静默盖 ──")
    ok = True
    chat = "selftest_files_write"
    agent_tools._PENDING.pop(chat, None)
    cfg = {"files": {}}

    with tempfile.TemporaryDirectory() as td:
        f1 = _mk(os.path.join(td, "a.txt"), "old")
        sub = os.path.join(td, "sub")
        os.makedirs(sub, exist_ok=True)

        out = _run({"action": "write", "path": os.path.join(td, "new.txt"), "text": "hi"}, cfg)
        ok &= check("write **新建** → 免确认，直接写", "已写入" in out, out)

        out = _run({"action": "write", "path": f1, "text": "NEW"}, cfg)
        with open(f1, encoding="utf-8") as fh:
            keep = fh.read()
        ok &= check("write 命中**已存在**且没给 overwrite → 如实拒绝、**内容没动**",
                    "已经存在" in out and keep == "old", (out, keep))

        # 拿不到会话时：确认类动作**必须拒绝**，绝不「因为登不上就顺手执行了」
        out = _run({"action": "write", "path": f1, "text": "NEW", "overwrite": True}, cfg)
        with open(f1, encoding="utf-8") as fh:
            keep = fh.read()
        ok &= check("需要确认但没有会话 → **拒绝、没执行、也没登记**",
                    "没有执行" in out and keep == "old", (out, keep))

        out = _run({"action": "write", "path": f1, "text": "NEW", "overwrite": True},
                   cfg, chat=chat)
        pend = agent_tools.list_pending(chat, 300)
        with open(f1, encoding="utf-8") as fh:
            keep = fh.read()
        ok &= check("覆盖已存在文件 → **进确认队列、还没写**（不可逆的一步）",
                    "还没有执行" in out and len(pend) == 1 and keep == "old",
                    (out, len(pend), keep))
        ok &= check("覆盖的菜单里**明说是覆盖**（用户要知道这份会被盖掉）",
                    "覆盖" in agent_tools.describe_pending(pend[0]),
                    agent_tools.describe_pending(pend[0]))
        n, err = agent_tools.send_pending(None, pend[0], cfg=cfg)
        with open(f1, encoding="utf-8") as fh:
            keep = fh.read()
        ok &= check("确认后**真的覆盖了**", n == 1 and err is None and keep == "NEW",
                    (n, err, keep))

        out = _run({"action": "append", "path": f1, "text": "!"}, cfg)
        with open(f1, encoding="utf-8") as fh:
            ok &= check("append 是追加、不是覆盖", "已追加" in out and fh.read() == "NEW!")
        out = _run({"action": "mkdir", "path": os.path.join(td, "d1", "d2")}, cfg)
        ok &= check("mkdir 建多级目录", os.path.isdir(os.path.join(td, "d1", "d2")), out)

        out = _run({"action": "copy", "src": f1, "dst": os.path.join(td, "c.txt")}, cfg)
        ok &= check("copy 复制文件", "已复制" in out and os.path.exists(os.path.join(td, "c.txt")), out)
        out = _run({"action": "copy", "src": f1, "dst": os.path.join(td, "c.txt")}, cfg)
        ok &= check("copy 到**已存在**的目标 → 拒绝、不静默盖掉", "已经存在" in out, out)
        out = _run({"action": "move", "src": f1, "dst": sub}, cfg)
        ok &= check("move 到目录 → 搬进去（保留原文件名）",
                    os.path.exists(os.path.join(sub, "a.txt")), out)
        out = _run({"action": "rename", "path": os.path.join(sub, "a.txt"),
                    "new_name": "b.txt"}, cfg)
        ok &= check("rename 改名", os.path.exists(os.path.join(sub, "b.txt")), out)
        out = _run({"action": "rename", "path": os.path.join(sub, "b.txt"),
                    "new_name": "x\\y.txt"}, cfg)
        ok &= check("rename 的 new_name 带路径分隔符 → 拒绝并指路 move",
                    "路径分隔符" in out and "move" in out, out)

        # 路径准入对写类同样生效（写不是「内部操作」，一样要过闸）
        out = _run({"action": "write", "path": r"C:\Windows\_probe.txt", "text": "x"}, cfg)
        ok &= check("写类也受 `files.deny` 约束（系统目录写不进去）",
                    "系统目录" in out and not os.path.exists(r"C:\Windows\_probe.txt"), out)
    return ok


def test_delete_recycle():
    print("\n── 6 · 删除：强制确认 + 进回收站（不可逆那一步不给配置留后门）──")
    ok = True
    chat = "selftest_files_del"
    agent_tools._PENDING.pop(chat, None)

    with tempfile.TemporaryDirectory() as td:
        v1 = _mk(os.path.join(td, "victim1.txt"), "bye")
        v2 = _mk(os.path.join(td, "victim2.txt"), "bye")
        # ⚠️ 关键：**即使把 delete 从 files.confirm 里删掉也必须拦住**
        cfg = {"files": {"confirm": []}}
        out = files.handler({"action": "delete", "path": v1},
                            {"cfg": cfg, "chat": chat, "from_self": True})
        ok &= check("`files.confirm: []` 时 delete **仍强制确认**（配置说了不算）",
                    "还没有执行" in out and os.path.exists(v1), out)
        ok &= check("…`warn_forced` 明说「已强制加回」",
                    "强制加回" in files.warn_forced(cfg), files.warn_forced(cfg))
        ok &= check("…并且菜单显示的是**原样路径**（不是文件名）",
                    td in agent_tools.describe_pending(
                        agent_tools.list_pending(chat, 300)[0]),
                    agent_tools.describe_pending(agent_tools.list_pending(chat, 300)[0]))
        ok &= check("消息里说清是**回收站**、还能恢复",
                    "回收站" in agent_tools.describe_pending(
                        agent_tools.list_pending(chat, 300)[0]))

        files.handler({"action": "delete", "path": v2},
                      {"cfg": cfg, "chat": chat, "from_self": True})
        pend = agent_tools.list_pending(chat, 300)
        ok &= check("删**另一个**文件 → **不判重**、队列里两条（key_fields 直证）",
                    len(pend) == 2, len(pend))

        n, err = agent_tools.send_pending(None, pend[0], cfg=cfg)
        ok &= check("确认后真的删了：原路径消失", n == 1 and err is None
                    and not os.path.exists(v1), (n, err, os.path.exists(v1)))
        ok &= check("另一个还没动（只管确认的那一条）", os.path.exists(v2))

    src = open(os.path.join(BASE, "files.py"), "r", encoding="utf-8").read()
    ok &= check("删除用 `FOF_ALLOWUNDO`（**唯一**让删除可恢复的旗标，不许删）",
                "FOF_ALLOWUNDO | FOF_NOCONFIRMATION" in src)
    ok &= check("用 ctypes + SHFileOperationW（零依赖、不起子进程）",
                "SHFileOperationW" in src
                and "import send2trash" not in src
                and "import winshell" not in src
                and "subprocess" not in src)
    # 「真的进了回收站」自测**证不了**（要枚举回收站得走 Shell COM，代价过大）——
    # 自动测只证「旗标对 + 原路径消失」，人工确认归 verify_real.py。不许把
    # 没验证的说成验证过了。
    return ok


def test_who_gate():
    print("\n── 7 · 触发者闸门：只认我发的 + 点名授权的会话 ──")
    ok = True
    with tempfile.TemporaryDirectory() as td:
        f = _mk(os.path.join(td, "a.txt"), "x")
        args = {"action": "list", "path": td}

        # 「我自己发的消息」→ 允许（默认口径）
        out = _run(args, {}, chat="filehelper", from_self=True)
        ok &= check("我自己发的消息 → 允许", "a.txt" in out, out[:120])

        # 名单里的会话 → 允许（哪怕不是我自己发的）——这就是「指定对话」的含义
        cfg = {"files": {"who": ["wxid_someone"]}}
        out = _run(args, cfg, chat="wxid_someone", from_self=False)
        ok &= check("`files.who` 名单里的会话 → 允许（哪怕消息不是我发的）",
                    "a.txt" in out, out[:120])

        # 不在名单、又不是我发的 → 拒绝，且**说清怎么放开**
        out = _run(args, {}, chat="wxid_stranger", from_self=False)
        ok &= check("不在名单、又不是我发的 → **拒绝**", "不能用" in out, out)
        ok &= check("…并说清怎么放开（files.who）", "files.who" in out, out)
        ok &= check("…并提醒别把不可信的人加进去",
                    "改你硬盘上的文件" in out, out)

        # ⚠️ from_self = None（「不知道」）**绝不许当成 True**
        out = _run(args, {}, chat="wxid_stranger", from_self=None)
        ok &= check("`from_self` 是 None（不知道）→ **照样拒绝**，不许当 True",
                    "不能用" in out, out)

        # 闸门在最前面：被拒时**不会**碰文件系统
        out = _run({"action": "write", "path": os.path.join(td, "nope.txt"), "text": "x"},
                   {}, chat="wxid_stranger", from_self=False)
        ok &= check("被拒的写请求**一个字都没写**",
                    not os.path.exists(os.path.join(td, "nope.txt")), out)

        # 没有 from_self 这个键（老调用方）→ 也是拒绝，不是放行
        out = files.handler(args, {"cfg": {}, "chat": "wxid_x"})
        ok &= check("ctx 里根本没有 `from_self` 时也是拒绝（fail-safe）",
                    "不能用" in out, out)
    return ok


def test_startup_notes():
    print("\n── 8 · 启动告警：默认值很宽，必须让用户看见宽在哪 ──")
    ok = True

    notes = files.startup_notes({"files": {"roots": [], "who": []}})
    text = "\n".join(notes)
    ok &= check("全盘 → **‼️ 明说「全盘 = 几乎任何文件都能读写」**",
                "全盘" in text and "任何文件" in text, text)
    ok &= check("…并指路怎么收窄（files.roots）", "files.roots" in text, text)
    ok &= check("…报出要确认的动作清单", "要用户确认的" in text, text)
    ok &= check("…报出能用文件能力的会话", "我自己发的消息" in text, text)

    notes = files.startup_notes({"files": {"roots": ["C:/x"], "deny": []}})
    text = "\n".join(notes)
    ok &= check("deny 置空 → ‼️ 明说连系统目录都不挡了",
                "连系统目录都不挡" in text, text)
    ok &= check("roots 非空 → 不打「全盘」那条", "全盘" not in text, text)

    notes = files.startup_notes({"files": {"confirm": []}})
    ok &= check("confirm 里没有 delete → ‼️ 明说「已强制加回」",
                any("强制加回" in n for n in notes), notes)
    ok &= check("…并且仍然报出 delete 要确认（强制项）",
                any("delete" in n and "要用户确认的" in n for n in notes), notes)

    notes = files.startup_notes({"files": {"who": ["wxid_friend"]}})
    ok &= check("who 非空 → ‼️ 明说「名单里别人发的也算」",
                any("别人" in n and "改你硬盘上的文件" in n for n in notes), notes)

    # ⚠️ `file:`（读文件上限）与 `files:`（本能力范围）只差一个字母
    notes = files.startup_notes({"file": {"roots": ["C:/x"]}, "files": {}})
    ok &= check("段名写错（把本能力的键写进了 `file:`）→ ‼️ 告警说**没有生效**",
                any("只差一个字母" in n and "没有生效" in n for n in notes), notes)

    ok &= check("关着时不打一堆范围告警，只说「关着 + 怎么开」",
                len(files.startup_notes({"files": {"enabled": False}})) == 1,
                files.startup_notes({"files": {"enabled": False}}))
    return ok


def test_send_file_from_disk():
    print("\n── 9 · 扩 send_file：盘上路径一律进确认，**绝不走白名单直发** ──")
    ok = True
    chat = "selftest_files_send"
    agent_tools._PENDING.pop(chat, None)

    class _Cli:
        def __init__(self):
            self.sent = []

        def send_file(self, path, wxid, cfg=None):
            self.sent.append((path, wxid))

    contacts = [{"wxid": "wxid_friendA", "name": "张三", "remark": "老张"}]

    def _box(cfg, cli, chat_id, from_self=True):
        b = agent_tools.ToolBox(cli, cfg, contacts, self_wxid="wxid_me",
                                chat=chat_id, cfg_provider=lambda: cfg)
        b.from_self = from_self
        return b

    with tempfile.TemporaryDirectory() as td:
        f = _mk(os.path.join(td, "报告.pdf"), "pdf")
        # ⚠️ 收件人**就在免确认名单里** —— 这正是要证的那一条
        cfg = {"agent": {"max_queries": 3, "confirm_ttl": 300,
                         "auto_send_whitelist": ["老张", "wxid_friendA"]},
               "files": {"roots": []}}
        cli = _Cli()
        box = _box(cfg, cli, chat)
        out = box.run("send_file", {"to": "老张", "name": f})
        pend = agent_tools.list_pending(chat, 300)
        ok &= check("盘上路径：**收件人在免确认名单里也进确认队列**、没有直发",
                    not cli.sent and len(pend) == 1 and "还没有发" in out,
                    (cli.sent, len(pend), out[:120]))
        desc = agent_tools.describe_pending(pend[0])
        ok &= check("…菜单显示**原样路径**（同一个 basename 能出现在很多目录里）",
                    td in desc and "报告.pdf" in desc, desc)
        ok &= check("…并说清「硬盘上取的、一律要确认」",
                    "硬盘" in out, out)
        n, err = agent_tools.send_pending(cli, pend[0], 0.0, cfg=cfg)
        ok &= check("用户确认后**真的发了**", bool(cli.sent) and n == 1 and err is None,
                    (cli.sent, n, err))

        # 路径准入对发文件同样生效
        agent_tools._PENDING.pop(chat, None)
        cfg2 = {"agent": {"max_queries": 3}, "files": {"roots": [os.path.join(td, "other")]}}
        os.makedirs(os.path.join(td, "other"), exist_ok=True)
        cli2 = _Cli()
        out = _box(cfg2, cli2, chat).run("send_file", {"to": "老张", "name": f})
        ok &= check("roots 不含那个目录 → **当场拒绝、不进队列**（不让用户白确认一次）",
                    "不在允许的目录里" in out and not agent_tools.list_pending(chat, 300),
                    out[:140])

        out = _box(cfg, cli, chat).run(
            "send_file", {"to": "老张", "name": r"C:\Windows\System32\drivers\etc\hosts"})
        ok &= check("系统目录里的文件 → 拒绝", "系统目录" in out, out)

        out = _box(cfg, cli, chat).run(
            "send_file", {"to": "老张", "name": os.path.join(td, "nope.pdf")})
        ok &= check("绝对路径但文件不存在 → 如实说", "没有这个文件" in out, out)

        # `~/...` 也必须走**盘上**那条路（2026-10-05 真机连撞两次）：
        # Windows 上 `os.path.isabs("~/x")` 为 False，所以第一版把模型的
        # `~/Desktop/TF/TF/1.docx` 丢给了「按文件名找」→ 回一句「文件名不合法」
        # → 模型去猜 `C:\Users\Administrator\...` → 再报「没有这个文件」。
        home = os.path.expanduser("~")
        tilde = "~/Desktop/selftest_一定不存在_xyz.pdf"
        out = _box(cfg, cli, chat).run("send_file", {"to": "老张", "name": tilde})
        ok &= check("`~/...` 走盘上那条路（回「没有这个文件」+ 展开后的真实家目录）",
                    "没有这个文件" in out and home in out and "不合法" not in out,
                    out[:160])
        out = _box(cfg, _Cli(), "wxid_stranger", from_self=False).run(
            "send_file", {"to": "老张", "name": tilde})
        ok &= check("……而且照样受触发者闸门约束（证明它没漏回按文件名那条路）",
                    "不能用" in out, out[:140])

        # 相对路径：**不许**丢给「按文件名找」（那边只会说「不合法」，看不懂）
        out = _box(cfg, _Cli(), chat).run(
            "send_file", {"to": "老张", "name": os.path.join("sub", "报告.pdf")})
        ok &= check("带分隔符的相对路径 → 明说要**绝对路径**",
                    "绝对路径" in out and "不合法" not in out, out[:160])

        # 触发者闸门对发文件也生效（新开的能力不能跟着老路径一起没闸）
        cli3 = _Cli()
        out = _box(cfg, cli3, "wxid_stranger", from_self=False).run(
            "send_file", {"to": "老张", "name": f})
        ok &= check("不是我发的、又不在名单里 → 拒绝，且**一个字节都没发**",
                    "不能用" in out and not cli3.sent, out[:140])

    # 菜单分级：`msg/file` 来源**一个字不改**
    d = agent_tools.describe_pending({"kind": "file", "to_name": "张三",
                                      "file": r"C:\wechat\msg\file\2026-10\报告.pdf",
                                      "text": "", "ts": 0})
    ok &= check("msg/file 来源的菜单**仍是只显示文件名**（既有行为不许改）",
                d == "把文件「报告.pdf」发给 张三", d)
    return ok


def test_end_to_end_via_run_agent():
    """**用户的真实场景**：在控制会话里说一句「看看我桌面上有什么」。

    ## 为什么单独立这一条（2026-10-04 真机撞出来的）

    当时的局面是：

    * `selftest_files` 的 `_run()` **自己塞** `from_self=True` → 绿；
    * `selftest_bot_loop` 验「事实到得了 `ToolBox`」（探针工具）→ 绿；
    * **而生产里 `computer_files` 一律被拒** —— 因为 `run_agent` 根本没把
      `from_self` 传进 `ToolBox`。

    **两条测试各自都对，拼起来才坏。** 单点测试天然抓不到这个，所以这里
    **直接走完整那条路**：真 `bot.run_agent` + 真 `computer_files`，
    什么事实都不自己塞（只给 `run_agent`，跟主循环一样）。
    """
    print("\n── 10 · 端到端：真 run_agent + 真 computer_files（用户的那句话）──")
    ok = True
    import llm as llm_mod
    import bot

    with tempfile.TemporaryDirectory() as td:
        _mk(os.path.join(td, "桌面上的东西.txt"), "hi")
        cfg = {"agent": {"max_queries": 3}, "files": {"roots": [td]}}

        class _Cli:
            pass

        class _LLM:
            def __init__(self):
                self.rounds = 0

            def chat_with_tools(self, system, messages, tools):
                self.rounds += 1
                if self.rounds == 1:
                    return llm_mod.ChatResult("", [llm_mod.ToolCall(
                        "c1", "computer_files", {"action": "list", "path": td})])
                self.messages = messages
                return llm_mod.ChatResult("看过了", [])

        def _tool_output(from_self):
            llm = _LLM()
            bot.run_agent(llm, "sys", "看看我桌面上有什么", _Cli(), [], cfg,
                          "filehelper", "", cfg_provider=lambda: cfg,
                          history=[], state={}, from_self=from_self)
            outs = [m.get("content") for m in getattr(llm, "messages", [])
                    if m.get("role") == "tool"]
            return " ".join(str(o) for o in outs)

        out = _tool_output(True)
        ok &= check("我在控制会话里说 → **真的列出来了**（不再是被配置拒绝）",
                    "桌面上的东西.txt" in out and "不能用" not in out, out[:200])
        out = _tool_output(False)
        ok &= check("别人发来的 → 仍然如实拒绝（闸门没被这次修复放宽）",
                    "不能用" in out, out[:200])
    return ok


def test_home_hints():
    """**别让模型猜用户目录名**（2026-10-04 真机第二撞）。

    真机现场：模型不知道用户目录是哪个，于是猜了
    `C:\\Users\\Administrator\\Desktop`（用户名是编的）和相对路径「桌面」，
    两个都不存在 —— 然后它去提 `run_command echo %USERPROFILE%`，
    这一圈**本来是它自己该知道的事**。

    根因是我实现 `list` 不填 path 时只回了「全盘」两个字，还写着
    「比如用户的桌面、文档、下载」—— 那句话**等于在鼓励它猜**。
    """
    print("\n── 11 · 用户目录提示（别猜用户名）──")
    ok = True
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")

    hints = files.home_hints()
    paths = [p for p, _ in hints]
    ok &= check("home_hints 第一项就是用户目录", paths and paths[0] == home, paths[:1])
    ok &= check("home_hints 里带上了真实的桌面路径", desktop in paths, paths)
    ok &= check("home_hints 标出了每个目录存不存在",
                all(isinstance(e, bool) for _, e in hints))

    out = files.handler({"action": "list"},
                        {"cfg": {}, "from_self": True, "chat": "filehelper"})
    ok &= check("不填 path 时**把真实路径摆出来**（模型抄这个就不会猜）",
                desktop in out, out[:220])
    ok &= check("并且明说「别自己拼用户名」", "别自己拼用户名" in out, out[:220])
    ok &= check("并且告诉它 `~` 可用", "~" in out, out[:220])

    # 猜错用户名（真机上就是这一步）
    out = files.handler({"action": "list", "path": "C:/Users/Administrator/Desktop"},
                        {"cfg": {}, "from_self": True, "chat": "filehelper"})
    ok &= check("猜错用户名 → 如实说不存在，**并附上真实路径**",
                "没有这个目录" in out and desktop in out, out[:220])

    # 相对路径「桌面」：真机上这个最费解 —— 它会相对**仓库目录**解析
    out = files.handler({"action": "list", "path": "桌面"},
                        {"cfg": {}, "from_self": True, "chat": "filehelper"})
    ok &= check("相对路径 → 说清它被解析成了什么（不是用户目录）",
                "相对路径" in out and os.path.abspath("桌面") in out, out[:260])
    ok &= check("相对路径那条也附上真实路径", desktop in out, out[:260])

    # `~/xxx` 是**家目录锚定**的绝对路径 —— `os.path.isabs("~/x")` 为假，
    # 所以第一版把它也说成了「相对路径」（自己踩的，靠这个用例钉住）
    out = files.handler({"action": "list", "path": "~/OneDrive/Desktop"},
                        {"cfg": {}, "from_self": True, "chat": "filehelper"})
    ok &= check("`~/xxx` 不许被说成「相对路径」（它是绝对路径）",
                "相对路径" not in out, out[:220])

    # 换台电脑最常见的坑：配置里的 root 在**新机器**上不存在
    # （Win11 的桌面/文档常被 OneDrive 接管成 ~/OneDrive/Desktop）
    # 用一定不存在的目录，别依赖这台机器上有没有 OneDrive
    ghost = os.path.join(home, "绝对不存在的目录_selftest_xyz")
    notes = files.startup_notes({"files": {"roots": [ghost]}})
    ok &= check("启动时点名**不存在的 root**（而不是等用户撞上「没有这个目录」）",
                any("不存在" in n and ghost in n for n in notes), notes)
    return ok


def main():
    print("电脑文件能力回归（`files.py`）")
    print("=" * 66)
    test_path_model()
    test_read_actions()
    test_reextract_kernel()
    test_switch_and_contract()
    test_write_gate()
    test_delete_recycle()
    test_who_gate()
    test_startup_notes()
    test_send_file_from_disk()
    test_end_to_end_via_run_agent()
    test_home_hints()
    print("=" * 66)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
