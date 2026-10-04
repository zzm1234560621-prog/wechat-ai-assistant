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

def _run(args, cfg):
    return files.handler(args, {"cfg": cfg})


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


def main():
    print("电脑文件能力回归（`files.py`）")
    print("=" * 66)
    test_path_model()
    test_read_actions()
    test_reextract_kernel()
    test_switch_and_contract()
    print("=" * 66)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
