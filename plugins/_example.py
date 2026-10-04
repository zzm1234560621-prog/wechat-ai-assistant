"""插件模板 —— **这个文件不会被加载**（文件名以 `_` 开头，加载器跳过它）。

要写自己的插件：把本文件复制成 `plugins/我的插件.py`（**不要** `_` 开头）再改。
改完**重启助手**才生效（运行期不重载插件）。

契约（权威）：`docs/plugin-contract-spec.md`。三样东西可以注册：

    reg.register_tool(spec)                      # 给模型加一个工具
    reg.register_event(name, fn)                 # 挂生命周期事件（只观察）
    reg.register_pending_kind(...)               # 加一类「等你回确认」的动作

## 四条最容易踩的（都是有意设计，不是限制）

1. **`guidance` 必填**。它是**给模型的用法指导**，会随工具定义一起进系统提示。
   写它的理由很具体：2026-10-02 `send_asset` 的指导只加进了本机 config，
   发布包里一个字都没有，于是「开发机上好用、朋友拿到包静默失效」。
2. **事件跑在收消息那条线程上**。hook 不支持并发，所以：

   * 插件**自己的线程绝不许碰 hook**（会直接把微信搞崩）；
   * 单次事件必须快（默认超 `plugins.slow_ms`=500ms 就告警，连续超
     `plugins.disable_after`=5 次会**自动停用你**并说明原因）；
   * **`worker` 执行模式本轮还没实现** —— 声明它会在加载期失败。
     以后要接网络/进程 I/O（MCP、IDE 之类）必须先实现它，见规格第五节。
3. **只有 `before_reply` 能改行为**（改回复文本），别的都只观察。
   返回 `None` 或非字符串 = 不改。路由（回不回、回给谁）不许插件插手。
4. **工具要发消息/删文件这类不可逆动作，必须走确认闸**：声明
   `confirm="always"`，或者在你的处理器里只 `set_pending`、一个字都不执行。

## 一个最小例子（复制出去把注释删掉就是能用的插件）
"""
# ↑ 上面的 docstring 只是说明。真正的插件从下面这行开始。


def setup(reg):
    """入口 —— **必须叫这个名字**。没有它，插件会在加载期失败并说清原因。

    `reg` 是一个**限定视图**：只能注册，不能查询或回滚；`source` 由加载器按
    文件名钉死（回滚半加载的插件要靠它精确匹配）。
    """

    # ── 1) 一个工具 ─────────────────────────────────────────────────
    #
    # `parameters` 用 JSON Schema，形状与 `agent_tools.TOOLS` 里那条**完全同形**
    # —— 那形状恰好就是 MCP 的 tool 形状，照抄它以后接 MCP 是零翻译。
    def _hello_handler(args, ctx):
        who = str((args or {}).get("who") or "你")
        # ctx 是**只读**的：{chat, self_wxid, cfg, from_self, is_group, user_query}
        # `from_self` 默认 None = **不知道**，绝不许当成 True（权限判断靠它）。
        return f"你好，{who}。（这一轮服务的是会话 {ctx.get('chat')}）"

    reg.register_tool({
        "name": "hello_demo",              # 不得与内置或其他插件重名（撞了加载期失败）
        "description": "演示用的打招呼工具。",
        "parameters": {
            "type": "object",
            "properties": {"who": {"type": "string", "description": "跟谁打招呼"}},
            "required": [],
        },
        "handler": _hello_handler,
        "guidance": ("用户说「打个招呼试试」时调用 hello_demo。"
                     "这个工具只是演示用的，不要因为别的理由调用它。"),
        # "confirm": "always",   # 要发消息/删文件那类不可逆动作就打开它
    })

    # ── 2) 一个只观察的事件 ─────────────────────────────────────────
    #
    # 六个事件：on_start(cfg) / on_message(ctx) / before_reply(text, ctx) /
    #           after_reply(text, ctx) / on_tool(name, args, result, ctx) /
    #           on_tick(n)
    # 只有 before_reply 能改行为（返回新文本；返回 None/非字符串 = 不改）。
    _seen = {"n": 0}

    def _on_message(ctx):
        _seen["n"] += 1
        print(f"[hello_demo] 第 {_seen['n']} 条消息，来自会话 {ctx.get('chat')}")

    reg.register_event("on_message", _on_message)

    # 想改回复文本就挂这个（注意：**别拿它去改确认菜单/群发预览** ——
    # 那些是 bot 原样直发的，改写它等于把确认闸做废）：
    #
    # def _sign(text, ctx):
    #     return text + "\n\n—— 来自我的助手"
    # reg.register_event("before_reply", _sign)

    # ── 3) 一类「等你回确认」的动作（可选，较进阶）─────────────────
    #
    # 用法见 `files.py` 里 `register_pending_kind("fileop", ...)` 那段：
    # describe_fn 负责把要执行的内容**原样**摆给用户看（不许转述），
    # apply_fn 是用户回「确认」之后真正执行的那一步，
    # `key_fields` **必须**声明 —— 它决定「两条动作是不是同一件事」，
    # 少声明会让两条**不同**的动作被判重、只留一条（用户照菜单确认时做错事）。
