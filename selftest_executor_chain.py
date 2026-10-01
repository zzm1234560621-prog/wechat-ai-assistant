"""本地执行「确认闸门」链路自测：模型提命令 → 待确认 → 用户回「确认」→ 才真跑。

跑：.venv/Scripts/python.exe selftest_executor_chain.py

为什么单独有这个文件（和 executor_selftest.py 的分工）：
  * `executor.py` 的自测 + `executor_selftest.py` 管**执行本身对不对**
    （超时、截断、编码、退出码、引号路径……）。
  * 这个文件管**闸门对不对**——「微信消息 = 远程执行入口」这条链上，
    模型能做什么、用户要做什么、命令什么时候才会真的执行。
    这是本功能最关键的安全边界，值得单独钉死。

它不碰微信、不碰 hook、不联网：ToolBox 传的是 None（shell 这条路本来就不该调 client），
执行的命令都是 `echo` 之类的无害短命令，副作用用一个临时文件当探针。

覆盖的规矩（都是原始设计里写死的）：
  1. 模型调 run_command **只登记、绝不执行**；
  2. 待确认里存的是**命令原文**，显示给用户的和真跑的是同一串；
  3. 用户回「确认」后才真跑，结果如实回报（退出码/超时/截断）；
  4. shell.enabled=false 时**如实报错**，不登记、不执行、不静默降级；
  5. auto_ok 只认**整条精确相等**（用户自己写进配置的旁路，模型构造不出命中）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_tools
import bot

_FAIL = []
HERE = os.path.dirname(os.path.abspath(__file__))
SIDE = os.path.join(HERE, "_selftest_executor_side.txt")

# 项目根目录当工作目录
CFG = {"agent": {"confirm_ttl": 300},
       "shell": {"enabled": True, "timeout": 20, "max_output": 2000,
                 "cwd": HERE, "auto_ok": []}}
CHAT = "filehelper"


def chk(cond, msg):
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        _FAIL.append(msg)


def cleanup():
    if os.path.exists(SIDE):
        os.remove(SIDE)


def box(cfg):
    # client=None：本地执行这条路不该碰微信客户端，传 None 顺带证明它没被用到
    return agent_tools.ToolBox(None, cfg, [], "", CHAT, lambda: cfg)


def main():
    cleanup()
    cmd = f'echo made > "{SIDE}" && dir /b'

    print("1) 模型提出命令：只登记，绝不执行")
    agent_tools._PENDING.clear()
    out = box(CFG).run("run_command", {"command": cmd})
    chk(cmd in out, "工具返回里带**命令原文**（模型据此复述给用户）")
    chk("尚未执行" in out, "工具返回明说「尚未执行」")
    chk(not os.path.exists(SIDE), "副作用文件没被创建 → 命令确实一个字都没跑")
    item = agent_tools.peek_pending(CHAT, 300)
    chk(item is not None, "登记了一条待确认")
    chk(bool(item) and item.get("kind") == "shell", "kind == shell")
    chk(bool(item) and item.get("cmd") == cmd,
        "待确认里存的是命令**原文**，一字不差（用户审的就是真命令）")
    chk(bool(item) and not item.get("to_wxid"), "shell 项没有收件人，不会被误发给别人")

    print("\n2) 确认词必须严格：随口一句 ok 不许在本机真跑命令")
    # 本地执行比「发消息」更不可逆，所以比 kind=agent 更严：
    # 发错消息还能解释，命令跑下去就跑了。
    # 这里钉的是「宽松确认词 = 确认发送会认，但 shell 必须不认」这条界线。
    loose_ok = [w for w in ("ok", "yes", "y", "发送", "发吧", "可以发")
                if bot.is_confirm(w)]
    chk(len(loose_ok) >= 6, f"宽松确认词确实被 is_confirm 认（{loose_ok}）")
    for loose in loose_ok:
        chk(not bot.is_strict_confirm(loose),
            f"「{loose}」能触发普通发送，但**不算** shell 的确认")
    for strict in ("确认", "确定", "确认发送"):
        chk(bot.is_strict_confirm(strict), f"「{strict}」才算 shell 的确认")
        chk(bot.is_confirm(strict), f"「{strict}」同时也是普通确认词")
    # 闸门判定逻辑本身（照 bot.py 确认分支的判断写，钉住它别被改松）
    def shell_intercepts(word):
        """队头是 shell 时，这个回复会不会被拦下来（True = 不执行）。"""
        return (not bot.is_strict_confirm(word)) and (not bot.is_cancel(word))

    chk(shell_intercepts("ok") is True, "队头是 shell 且用户只说 ok → 拦下，不执行")
    chk(shell_intercepts("发送") is True, "队头是 shell 且用户说「发送」→ 也拦下")
    chk(shell_intercepts("确认") is False, "用户说「确认」→ 不拦，正常执行")
    chk(shell_intercepts("不发") is False, "用户说「不发」→ 不拦（走取消分支）")

    print("\n3) 没有「确认」这条消息，命令永远不跑")
    chk(not os.path.exists(SIDE), "过了上述所有步骤，命令依然没执行")

    print("\n4) 用户回「确认」→ 才真跑，且如实回报")
    pending = agent_tools.pop_pending(CHAT, 300)
    chk(pending is not None, "取出待确认项")
    text = bot.shell_command_text(pending, CFG)
    chk(os.path.exists(SIDE), "副作用文件被创建 → 命令真的执行了")
    chk("命令：" in text and cmd in text, "结果文本带命令原文")
    chk("状态：完成" in text and "退出码 0" in text, "如实报告成功与退出码")
    chk("_selftest_executor_side.txt" in text, "命令输出真的回来了")

    print("\n5) 失败/超时要说实话（不粉饰）")
    r = bot.shell_command_text({"cmd": "exit 3"}, CFG)
    chk("失败" in r and "退出码 3" in r, "非零退出码报成失败")
    r = bot.shell_command_text({"cmd": "ping -n 20 127.0.0.1 > nul", "timeout": 2}, CFG)
    chk("超时" in r, "超时明说是超时")
    r = bot.shell_command_text({"cmd": "for /L %i in (1,1,900) do @echo 0123456789"}, CFG)
    chk("微信里只发前" in r, "输出太长时明说只发了一段（不假装输出就这么长）")

    print("\n6) shell.enabled=false：如实报错，不登记、不执行")
    cleanup()
    off = {"agent": {"confirm_ttl": 300}, "shell": {"enabled": False, "cwd": HERE}}
    agent_tools._PENDING.clear()
    out = box(off).run("run_command", {"command": f'echo made > "{SIDE}"'})
    chk("没开" in out, "明确说「本地执行没开」")
    chk("没有" in out, "明说没登记、没执行")
    chk(agent_tools.peek_pending(CHAT, 300) is None, "关闭时不登记待确认")
    chk(not os.path.exists(SIDE), "关闭时命令没跑")

    print("\n7) auto_ok：只有用户写进配置的整条原文才免确认")
    cleanup()
    ok_cfg = {"agent": {"confirm_ttl": 300},
              "shell": {"enabled": True, "timeout": 20, "cwd": HERE,
                        "auto_ok": [f'echo made > "{SIDE}"']}}
    agent_tools._PENDING.clear()
    b = box(ok_cfg)
    out = b.run("run_command", {"command": f'echo made > "{SIDE}"'})
    chk(os.path.exists(SIDE), "整条精确命中 → 免确认直接执行（用户自己指定的旁路）")
    chk("已直接执行" in out, "返回里写明「已直接执行」")
    chk(agent_tools.peek_pending(CHAT, 300) is None, "命中的不进待确认队列")

    cleanup()
    evil = f'echo made > "{SIDE}" & del /q no-such-file-xyz'
    b.run("run_command", {"command": evil})
    chk(not os.path.exists(SIDE), "名单项后面拼接 `& ...` **不命中** → 没执行")
    chk(agent_tools.peek_pending(CHAT, 300) is not None, "这种命令走待确认，要用户确认")
    agent_tools._PENDING.clear()

    print("\n8) 坏配置不能变成「全都免确认」（fail-safe）")
    chk(agent_tools._auto_ok_hit("dir", None) is False, "auto_ok=None → 不免确认")
    chk(agent_tools._auto_ok_hit("dir", "dir") is False, "auto_ok 写成字符串 → 不免确认")
    chk(agent_tools._auto_ok_hit("dir", [5, None]) is False, "非字符串项不匹配")
    chk(agent_tools._auto_ok_hit("", [""]) is False, "空命令不免确认")

    print("\n9) 空命令 / 非法 timeout：明确报错")
    agent_tools._PENDING.clear()
    out = box(CFG).run("run_command", {"command": "   "})
    chk("参数不全" in out, "空命令报参数不全")
    chk(agent_tools.peek_pending(CHAT, 300) is None, "空命令不登记待确认")
    out = box(CFG).run("run_command", {"command": "dir", "timeout": "abc"})
    chk("秒数" in out, "非法 timeout 明确报错")
    chk(agent_tools.peek_pending(CHAT, 300) is None, "报错的命令不登记")
    agent_tools._PENDING.clear()

    cleanup()
    print()
    if _FAIL:
        print(f"失败 {len(_FAIL)} 项 ❌")
        for f in _FAIL:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        cleanup()
