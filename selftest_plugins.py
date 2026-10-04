"""插件契约（`plugins.py`）的回归。

## 这份自测守什么

**判据（`docs/plugin-contract-spec.md` 2.2）**：注册表里 `source="builtin"` 的工具清单
必须与 `agent_tools.TOOLS` **逐条相等、含顺序**。这是「零行为变化」的可验证定义 ——
本轮把派发从 `getattr(self, f"t_{name}")` 改成查注册表，靠它证明没偷偷改变
模型看到的清单与调用路径。

以及契约的**加载期**规矩：重名 / 缺字段 / 缺 guidance / 非法 confirm / 非法 mode /
声明未实现的 `worker` —— 一律**响亮失败**，绝不静默跳过、绝不静默覆盖。

## 为什么这些必须失败而不是「凑合能用」

插件出问题只有三种：写错了、名字撞了、声明了做不到的东西。三种都必须在**加载那一刻**
让用户看见 —— 等哪天模型调到它才发现，那就是本项目最大的坑（静默失效）。
`worker` 那条尤其：静默按 `inline` 跑等于一边阻塞微信一边声称自己没阻塞。

用法：
    .venv\\Scripts\\python.exe selftest_plugins.py
"""
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import agent_tools      # noqa: E402  （导入即触发内置工具注册）
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


def _raises(fn):
    """跑 fn，返回 (是否抛 PluginError, 异常文本)。"""
    try:
        fn()
    except plugins.PluginError as e:
        return True, str(e)
    except Exception as e:                                  # 抛错类型也要较真
        return False, f"{type(e).__name__}: {e}"
    return False, ""


class _Stub:
    """最小上下文桩：只提供 `ctx()`，够验 handler 拿到的东西对不对。"""

    def __init__(self, chat="chat1", from_self=None):
        self.chat = chat
        self.self_wxid = "wxid_me"
        self.user_query = "把那个文件删了"
        self.from_self = from_self
        self.is_group = "@chatroom" in chat

    def ctx(self):
        return {"chat": self.chat, "self_wxid": self.self_wxid, "cfg": {},
                "from_self": self.from_self, "is_group": self.is_group,
                "user_query": self.user_query}


def _good_plugin_spec(name, **over):
    spec = {"name": name, "description": "一个测试工具",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda args, ctx: "ok", "guidance": "用户说测试时调用。"}
    spec.update(over)
    return spec


# ─────────────────────────────────────────────── 1. 零行为变化

def test_zero_behavior_change():
    print("\n── 1 · 零行为变化：注册表的内置视图 == agent_tools.TOOLS ──")
    ok = True

    builtin = [t for t in plugins.REGISTRY.tools()
               if plugins.REGISTRY.get(t["name"])["source"] == "builtin"]
    tools = agent_tools.TOOLS

    ok &= check(f"条数一致（注册表 {len(builtin)} / TOOLS {len(tools)}）",
                len(builtin) == len(tools))
    ok &= check("逐条相等、**含顺序**（名字序列）",
                [t["name"] for t in builtin] == [t["name"] for t in tools],
                f"{[t['name'] for t in builtin][:6]} vs {[t['name'] for t in tools][:6]}")
    ok &= check("description 逐条相等",
                [t["description"] for t in builtin] == [t["description"] for t in tools])
    ok &= check("parameters 逐条相等（同一对象即可，不比深度拷贝）",
                all(a["parameters"] is b["parameters"]
                    for a, b in zip(builtin, tools)))

    # 形状必须是 MCP 的 tool 形状：**只有**这三个键。
    # 多塞键会让 llm.py 的协议转换和以后接 MCP 都要多写一层翻译。
    keys = {tuple(sorted(t.keys())) for t in plugins.REGISTRY.tools()}
    ok &= check("tools() 每条只含 name/description/parameters（MCP 形状）",
                keys == {("description", "name", "parameters")}, keys)

    # 内置工具的模型指导在两份 config 的 system_prompt 里（既有设计），
    # 所以注册表里它们的 guidance 是空的 —— 这不是漏，别「顺手补上」。
    empty_guide = [n for n in plugins.REGISTRY.builtin_names()
                   if plugins.REGISTRY.get(n)["guidance"]]
    ok &= check("内置工具的 guidance 留空（它们的指导在两份 config 里）",
                not empty_guide, empty_guide)
    return ok


# ─────────────────────────────────────────────── 2. 加载期必须响亮失败

def test_load_failures():
    print("\n── 2 · 契约违规一律加载期失败（不静默跳过/不静默覆盖）──")
    ok = True

    reg = plugins.Registry()
    reg.register_tool(_good_plugin_spec("dup"), source="p1")

    hit, msg = _raises(lambda: reg.register_tool(_good_plugin_spec("dup"), source="p2"))
    ok &= check("重名 → 失败，并且**点名**是谁和谁撞了",
                hit and "dup" in msg and "p1" in msg and "p2" in msg, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        {"description": "d", "parameters": {"type": "object"},
         "handler": lambda a, c: "", "guidance": "g"}, source="p1"))
    ok &= check("缺 name → 失败", hit and "name" in msg, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        {"name": "x1", "parameters": {"type": "object"},
         "handler": lambda a, c: "", "guidance": "g"}, source="p1"))
    ok &= check("缺 description → 失败", hit, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        {"name": "x2", "description": "d", "handler": lambda a, c: "", "guidance": "g"},
        source="p1"))
    ok &= check("缺 parameters → 失败", hit, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        {"name": "x3", "description": "d", "parameters": {"type": "object"},
         "guidance": "g"}, source="p1"))
    ok &= check("既没 handler 也没 method → 失败", hit and "handler" in msg, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        {"name": "x4", "description": "d", "parameters": {"type": "object"},
         "handler": lambda a, c: "", "method": "t_x4", "guidance": "g"}, source="p1"))
    ok &= check("handler 和 method 同时给 → 失败", hit, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        {"name": "x5", "description": "d", "parameters": {"type": "object"},
         "handler": "不是函数", "guidance": "g"}, source="p1"))
    ok &= check("handler 不可调用 → 失败", hit, msg)

    # 插件**必须**自带模型指导：插件名不可能预先写在 config.example.yaml 里，
    # 指导不随定义走就没地方去了 —— 那正是 send_asset 那次失效的形状。
    hit, msg = _raises(lambda: reg.register_tool(
        {"name": "x6", "description": "d", "parameters": {"type": "object"},
         "handler": lambda a, c: ""}, source="p1"))
    ok &= check("插件缺 guidance → 失败（内置才允许空）",
                hit and "guidance" in msg, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        _good_plugin_spec("x7", confirm="sometimes"), source="p1"))
    ok &= check("confirm 非法值 → 失败", hit and "confirm" in msg, msg)

    hit, msg = _raises(lambda: reg.register_tool(
        _good_plugin_spec("x8", mode="threads"), source="p1"))
    ok &= check("mode 非法值 → 失败", hit and "mode" in msg, msg)

    # ⚠️ 关键一条：worker **未实现**，声明它必须失败并说清原因，
    # 绝不许静默按 inline 跑（那等于一边阻塞微信一边声称没阻塞）。
    hit, msg = _raises(lambda: reg.register_tool(
        _good_plugin_spec("x9", mode="worker"), source="p1"))
    ok &= check("mode=\"worker\" → 失败（本轮未实现）",
                hit and "worker" in msg and "未实现" in msg, msg)
    ok &= check("worker 的报错里给出下一步（照 read_worker 的样板实现）",
                "read_worker" in msg, msg)
    return ok


# ─────────────────────────────────────────────── 3. 派发

def test_dispatch():
    print("\n── 3 · 派发：只有注册表这一条路 ──")
    ok = True

    reg = plugins.Registry()

    # 内置工具：注册表里存的是**方法名**，在实例上解析
    class _Box:
        def __init__(self):
            self.chat = "chat1"
            self.self_wxid = "wxid_me"
            self.user_query = "q"
            self.from_self = None
            self.is_group = False

        def ctx(self):
            return {"chat": self.chat, "self_wxid": self.self_wxid, "cfg": {},
                    "from_self": self.from_self, "is_group": self.is_group,
                    "user_query": self.user_query}

        def t__probe(self, args):
            return "内置:" + str(args.get("v"))

    reg.register_tool({"name": "bi", "description": "d",
                       "parameters": {"type": "object"}, "method": "t__probe"},
                      source="builtin")
    fn = reg.resolve("bi", _Box())
    ok &= check("内置工具解析到实例方法", fn is not None and fn({"v": 7}) == "内置:7")

    # 插件工具：handler 拿到的必须是 (args, ctx)，ctx 里要有那几个事实
    seen = {}

    def _handler(args, ctx):
        seen.clear()
        seen.update(ctx)
        return "插件:" + str(args.get("v"))

    reg.register_tool(_good_plugin_spec("pl", handler=_handler), source="myplug")
    fn = reg.resolve("pl", _Stub(chat="chat1", from_self=None))
    ok &= check("插件工具 handler 收到 args 并返回文本", fn({"v": 9}) == "插件:9")
    ok &= check("ctx 里有 chat / self_wxid / user_query",
                seen.get("chat") == "chat1" and seen.get("self_wxid") == "wxid_me"
                and seen.get("user_query") == "把那个文件删了", seen)
    ok &= check("ctx 里有 from_self（**事实**位，未知时是 None 不是 True）",
                "from_self" in seen and seen["from_self"] is None, seen)

    ok &= check("未知名字 → resolve 返回 None（调用方据此回「没有这个工具」）",
                reg.resolve("查无此工具", _Box()) is None)
    ok &= check("注册顺序就是清单顺序",
                reg.names() == ["bi", "pl"], reg.names())
    return ok


# ─────────────────────────────────────────────── 4. 真 ToolBox 端到端

def test_real_toolbox():
    print("\n── 4 · 真 ToolBox.run：认内置工具、未知名字照旧 ──")
    ok = True

    cfg = {"agent": {"max_queries": 3}, "shell": {"enabled": True}}

    class _Client:
        pass

    box = agent_tools.ToolBox(_Client(), cfg, [], self_wxid="wxid_me", chat="chat1",
                              cfg_provider=lambda: cfg)

    # t_run_command 缺参数时的返回是纯参数校验、不碰客户端也不查库 ——
    # 拿它当「派发**确实走到了真处理器**」的证据（而不是走到了那句「没有名为 X」）。
    out = box.run("run_command", {})
    ok &= check("内置工具经 run() 走到真处理器（返回参数校验而不是「没有这个工具」）",
                "参数不全" in out and "没有名为" not in out, out)

    out = box.run("查无此工具", {})
    ok &= check("未知名字仍返回「没有名为 X 的工具。」",
                out == "没有名为 查无此工具 的工具。", out)

    ok &= check("ctx() 的 is_group 从 chat 直接推得（@chatroom）",
                box.ctx()["is_group"] is False
                and agent_tools.ToolBox(_Client(), cfg, [], chat="123@chatroom",
                                        cfg_provider=lambda: cfg).ctx()["is_group"] is True)
    return ok


# ─────────────────────────────────────────────── 5. 依赖方向

def test_dependency_direction():
    print("\n── 5 · 依赖方向：注册表谁也不 import（不许反过来）──")
    code = ("import sys, plugins;"
            "bad = [m for m in ('agent_tools', 'files', 'bot') if m in sys.modules];"
            "print(','.join(bad))")
    try:
        p = subprocess.run([sys.executable, "-c", code], cwd=BASE,
                           capture_output=True, text=True, timeout=60)
        pulled = (p.stdout or "").strip()
        check("独立解释器里 import plugins 不拉起 agent_tools / files / bot",
              pulled == "", f"被拉起了：{pulled}；stderr={p.stderr[-200:]}")
    except Exception as e:                                   # pragma: no cover
        check("依赖方向检查能跑起来", False, f"{type(e).__name__}: {e}")
    return True


def _mkplug(td, name, body):
    with open(os.path.join(td, name + ".py"), "w", encoding="utf-8") as f:
        f.write(body)


_GOOD_SPEC = ('{"name": "%s", "description": "d", "parameters": {"type": "object"},'
              ' "handler": lambda a, c: "from-plugin", "guidance": "g"}')


def test_load_dir():
    print("\n── 6 · 插件目录加载：失败隔离 / 半加载回滚 / 开关 ──")
    ok = True
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        _mkplug(td, "_example", "def setup(reg):\n    raise RuntimeError('模板不该被加载')\n")
        _mkplug(td, "good", "def setup(reg):\n    reg.register_tool(%s)\n"
                            % (_GOOD_SPEC % "good_tool"))
        _mkplug(td, "broken", "def setup(reg)\n    pass\n")          # 语法错
        _mkplug(td, "nosetup", "X = 1\n")                            # 没有 setup
        _mkplug(td, "half", "def setup(reg):\n"
                            "    reg.register_tool(%s)\n"
                            "    raise RuntimeError('注册到一半炸了')\n"
                            % (_GOOD_SPEC % "half_tool"))
        # 插件叫 json.py：**不许**遮蔽标准库（我们不加 sys.path）
        _mkplug(td, "json", "def setup(reg):\n    pass\n")

        before_json = sys.modules.get("json")
        reg = plugins.Registry()
        logs = []
        rep = plugins.load_dir(directory=td, cfg={}, log=logs.append, registry=reg)

        ok &= check("`_` 开头的文件不加载",
                    "_example" not in rep["loaded"]
                    and not any(n == "_example" for n, _ in rep["failed"]),
                    rep)
        ok &= check("好插件加载成功", rep["loaded"] == ["good", "json"], rep["loaded"])
        ok &= check("语法错的插件进 failed、**其余照常**、加载器不抛异常",
                    any(n == "broken" for n, _ in rep["failed"])
                    and "good" in rep["loaded"], rep)
        ok &= check("缺 setup 的插件进 failed 且说清要 setup",
                    any(n == "nosetup" and "setup" in w for n, w in rep["failed"]), rep)
        ok &= check("插件工具进了注册表，source 是插件名",
                    reg.has("good_tool") and reg.get("good_tool")["source"] == "good",
                    reg.get("good_tool"))

        # ⚠️ 半加载回滚：注册了一个工具再抛错 → 那个工具**不许**留在表里。
        # 留着就是「模型看得到工具、但插件的其它东西没生效」这种最难查的状态。
        ok &= check("半加载的插件：它注册的工具被整份回滚掉",
                    not reg.has("half_tool") and "half" in [n for n, _ in rep["failed"]],
                    reg.names())
        ok &= check("回滚只清自己的（好插件的工具还在）", reg.has("good_tool"))

        ok &= check("标准库没被插件顶掉（没把插件目录加进 sys.path）",
                    sys.modules.get("json") is before_json, sys.modules.get("json"))

        ok &= check("逐个打印了加载结果（往目录丢文件就能跑，这事不能是暗的）",
                    any("已加载" in s for s in logs)
                    and any("加载失败" in s for s in logs), logs)

        # 开关
        rep_off = plugins.load_dir(directory=td, cfg={"plugins": {"enabled": False}},
                                   log=logs.append, registry=plugins.Registry())
        ok &= check("plugins.enabled=false → 一个都不加载",
                    rep_off["loaded"] == [] and rep_off["failed"] == [], rep_off)

        rep_dis = plugins.load_dir(directory=td, cfg={"plugins": {"disabled": ["good"]}},
                                   log=logs.append, registry=plugins.Registry())
        ok &= check("plugins.disabled=[good] → 只跳过它、其余照常",
                    "good" not in rep_dis["loaded"] and "json" in rep_dis["loaded"],
                    rep_dis)

        # 判错方向：只有显式 false 才算关。
        # 写成字符串 "false" 如果被判成「关」，用户的插件会**静默不加载**
        # —— 那正是本项目最忌讳的失效，所以宁可当开并**告警**。
        logs2 = []
        rep_str = plugins.load_dir(directory=td, cfg={"plugins": {"enabled": "false"}},
                                   log=logs2.append, registry=plugins.Registry())
        ok &= check("enabled 写成字符串 \"false\" → 不当成关（避免静默不加载）",
                    "json" in rep_str["loaded"], rep_str["loaded"])
        ok &= check("…但必须**告警**说清怎么写才关",
                    any("enabled" in s for s in logs2), logs2)

    # 目录不存在 = 正常（一份插件都没有），不是错误
    with tempfile.TemporaryDirectory() as td:
        rep_none = plugins.load_dir(directory=os.path.join(td, "not-there"),
                                    cfg={}, log=lambda s: None,
                                    registry=plugins.Registry())
        ok &= check("插件目录不存在 → 空报告、不报错",
                    rep_none["loaded"] == [] and rep_none["failed"] == [], rep_none)
    return ok


def test_scoped_source():
    print("\n── 7 · 插件的 source 由加载器钉死（回滚要靠它）──")
    ok = True
    reg = plugins.Registry()
    scoped = plugins.ScopedRegistrar(reg, "myplug")
    spec = _good_plugin_spec("scoped_tool")
    spec["source"] = "我想自己填"
    scoped.register_tool(spec, source="也不想让你填")

    got = reg.get("scoped_tool")
    ok &= check("插件填的 source 被忽略，一律用插件名",
                got is not None and got["source"] == "myplug", got and got["source"])
    ok &= check("rollback_source 能精确清掉这个插件的东西",
                reg.rollback_source("myplug") == ["scoped_tool"]
                and not reg.has("scoped_tool"), reg.names())
    ok &= check("回滚别的 source 不会误伤",
                reg.rollback_source("没这个插件") == [])
    return ok


def test_events():
    print("\n── 8 · 生命周期事件：异常隔离 / 串联 / 慢就停用 ──")
    ok = True
    import time as _t

    logs = []
    reg = plugins.Registry(log=logs.append)
    seen = []

    hit, msg = _raises(lambda: reg.register_event("on_mesage", lambda c: None, source="p1"))
    ok &= check("事件名拼错 → 加载期失败（静默注册一个永不触发的回调最难查）",
                hit and "on_mesage" in msg, msg)
    hit, msg = _raises(lambda: reg.register_event("on_start", "不是函数", source="p1"))
    ok &= check("回调不可调用 → 失败", hit, msg)

    reg.register_event("on_message", lambda c: seen.append("first"), source="p1")
    reg.register_event("on_message", lambda c: 1 / 0, source="p2")
    reg.register_event("on_message", lambda c: seen.append("third"), source="p3")
    reg.emit("on_message", plugins.make_ctx(chat="c"), cfg={})
    ok &= check("一个插件抛异常，其余照常被调用（绝不许把消息循环带下去）",
                seen == ["first", "third"], seen)
    ok &= check("异常被记进日志", any("抛异常" in s for s in logs), logs)

    reg2 = plugins.Registry(log=lambda s: None)
    reg2.register_event("before_reply", lambda t, c: t + "A", source="a")
    reg2.register_event("before_reply", lambda t, c: t + "B", source="b")
    ok &= check("before_reply 按注册顺序**串联**（后一个看到前一个的结果）",
                reg2.before_reply("x", cfg={}) == "xAB", reg2.before_reply("x", cfg={}))
    reg2.register_event("before_reply", lambda t, c: None, source="c")
    reg2.register_event("before_reply", lambda t, c: 42, source="d")
    ok &= check("返回 None / 非字符串 = **不改**（写坏了只等于不生效，绝不吞回复）",
                reg2.before_reply("x", cfg={}) == "xAB", reg2.before_reply("x", cfg={}))
    reg2.register_event("before_reply", lambda t, c: 1 / 0, source="e")
    ok &= check("before_reply 里抛异常 → 文本不变（绝不许变成空串）",
                reg2.before_reply("x", cfg={}) == "xAB", reg2.before_reply("x", cfg={}))

    logs3 = []
    reg3 = plugins.Registry(log=logs3.append)
    calls = []
    reg3.register_tool({"name": "slow_tool", "description": "d",
                        "parameters": {"type": "object"},
                        "handler": lambda a, c: "h", "guidance": "g"}, source="slow")

    def _slow(ctx):
        calls.append(1)
        _t.sleep(0.02)

    reg3.register_event("on_message", _slow, source="slow")
    cfg = {"plugins": {"slow_ms": 1, "disable_after": 3}}
    reg3.emit("on_message", None, cfg=cfg)
    reg3.emit("on_message", None, cfg=cfg)
    ok &= check("慢事件先只告警，还没停用",
                len(calls) == 2 and "slow" not in reg3.disabled_plugins(), calls)
    ok &= check("告警里说清它跑在**收消息那条线程**上（用户要知道代价）",
                any("线程" in s for s in logs3), logs3)
    reg3.emit("on_message", None, cfg=cfg)
    ok &= check("连续超阈值 → 自动停用并**明说是谁**",
                "slow" in reg3.disabled_plugins()
                and any("已自动停用" in s and "slow" in s for s in logs3), logs3)
    ok &= check("停用同时撤掉它的工具", not reg3.has("slow_tool"), reg3.names())
    n_before = len(calls)
    reg3.emit("on_message", None, cfg=cfg)
    ok &= check("停用后它的回调**不再被调用**", len(calls) == n_before, calls)

    reg4 = plugins.Registry(log=lambda s: None)
    hits = []

    def _flaky(ctx):
        hits.append(1)
        if len(hits) % 2 == 1:
            _t.sleep(0.02)

    reg4.register_event("on_message", _flaky, source="flaky")
    for _ in range(6):
        reg4.emit("on_message", None, cfg={"plugins": {"slow_ms": 1, "disable_after": 2}})
    ok &= check("「连续」= 中间一次快就重新计（慢/快交替不会被误停用）",
                "flaky" not in reg4.disabled_plugins(), reg4.disabled_plugins())

    reg5 = plugins.Registry(log=lambda s: None)
    reg5.register_event("on_message", lambda c: _t.sleep(0.02), source="never")
    for _ in range(10):
        reg5.emit("on_message", None, cfg={"plugins": {"slow_ms": 1, "disable_after": 0}})
    ok &= check("disable_after: 0 → 只告警、永不自动停用",
                "never" not in reg5.disabled_plugins(), reg5.disabled_plugins())

    c = plugins.make_ctx(chat="123@chatroom", cfg={"a": 1})
    ok &= check("make_ctx：from_self 默认 None（「不知道」绝不许当成 True）",
                c["from_self"] is None, c)
    ok &= check("make_ctx：is_group 从 chat 直接推得", c["is_group"] is True, c)
    ok &= check("make_ctx：形状就是契约那六个键（唯一所有者）",
                set(c) == {"chat", "self_wxid", "cfg", "from_self", "is_group",
                           "user_query"}, set(c))
    ok &= check("ToolBox.ctx() 走的就是 make_ctx（不是第二份形状）",
                set(agent_tools.ToolBox(None, {}, [], chat="c").ctx()) == set(c))
    return ok


def main():
    print("插件契约回归（`plugins.py`）")
    print("=" * 66)
    test_zero_behavior_change()
    test_load_failures()
    test_dispatch()
    test_real_toolbox()
    test_dependency_direction()
    test_load_dir()
    test_scoped_source()
    test_events()
    print("=" * 66)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
