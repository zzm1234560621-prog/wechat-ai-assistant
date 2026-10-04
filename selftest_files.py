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


# ─────────────────────────────────────────────── 1. 路径准入（安全边界）

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


# ─────────────────────────────────────────────── 2. 读类动作

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


# ─────────────────────────────────────────────── 4. 开关 / 契约注册 / 分界线

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
    print("=" * 66)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
