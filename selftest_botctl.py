"""botctl.py 自测：进程控制的安全性与纯逻辑（**绝不真的启停 bot**）。

这个模块的本事是「杀进程」，所以自测的第一要务不是覆盖功能，而是**证明它不会乱杀**：
  * `stop(dry_run=True)` **一次 taskkill 都不许发**（试运行就只是算，不动手）；
  * `start()` 在「已经有一个在跑」和「venv 没就绪」时**必须先返回、不许拉起进程**；
  * `chain_root()` **不许越过 venv 边界**（走到 cmd/终端/别的程序前面就得停）。

所有真实交互（`owner` / `proc_table` / `_run` / `env.venv_ready`）都注入假实现，
所以这份自测**不会碰真 bot、不会关真进程**。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import botctl      # noqa: E402

_PASS = 0
_OK = True


def check(label, cond, extra=""):
    global _PASS, _OK
    _PASS += 1
    if cond:
        print(f"  ✅ {label}")
    else:
        _OK = False
        print(f"  ❌ {label}" + (f"  → {extra}" if extra else ""))
    return bool(cond)


def sec(t):
    print(f"\n── {t} ──")


# 真实 `netstat -ano -p TCP` 的样式（含同端口的 ESTABLISHED 行，用来验证只认 LISTENING）
NETSTAT_SAMPLE = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1156
  TCP    127.0.0.1:39001        0.0.0.0:0              LISTENING       55044
  TCP    127.0.0.1:39001        127.0.0.1:51234        ESTABLISHED     55044
  TCP    127.0.0.1:30001        0.0.0.0:0              LISTENING       52120
  TCP    [::1]:39002            [::]:0                 LISTENING       999
"""


def t1_parse_netstat():
    sec("T1 · parse_netstat：只认 LISTENING，认对端口")
    check("找到 39001 的持有者", botctl.parse_netstat(NETSTAT_SAMPLE, 39001) == 55044,
          botctl.parse_netstat(NETSTAT_SAMPLE, 39001))
    check("同一个端口的 ESTABLISHED 行**不会**干扰（没有它就会挑错）",
          botctl.parse_netstat(
              "  TCP    127.0.0.1:39001        127.0.0.1:5            ESTABLISHED     11111\n",
              39001) is None)
    check("别的端口不误报", botctl.parse_netstat(NETSTAT_SAMPLE, 135) == 1156)
    check("30001（hook）也能认", botctl.parse_netstat(NETSTAT_SAMPLE, 30001) == 52120)
    check("IPv6 行也认（[::1]:39002）",
          botctl.parse_netstat(NETSTAT_SAMPLE, 39002) == 999)
    check("没有这个端口 → None", botctl.parse_netstat(NETSTAT_SAMPLE, 12345) is None)
    check("空输入 → None（不炸）", botctl.parse_netstat("", 39001) is None)
    check("None 输入 → None（不炸）", botctl.parse_netstat(None, 39001) is None)
    check("垃圾输入 → None（不炸）",
          botctl.parse_netstat("hello\nworld\n", 39001) is None)
    check("PID 不是数字 → None（不猜）",
          botctl.parse_netstat(
              "  TCP    127.0.0.1:39001    0.0.0.0:0    LISTENING   abc\n", 39001) is None)


def t2_chain_root():
    sec("T2 · chain_root：把转发器带上，但**不许越过 venv 边界**")
    V = r"D:\proj\.venv"
    table = {
        100: (0, r"C:\Windows\explorer.exe"),            # 桌面（不在 venv）
        200: (100, r"C:\Windows\system32\cmd.exe"),      # 终端（不在 venv）
        300: (200, V + r"\Scripts\python.exe"),          # 转发器（在 venv）
        400: (300, r"C:\Python311\python.exe"),          # 真解释器（不在 venv）
    }
    check("从真解释器走到 venv 转发器", botctl.chain_root(table, 400, V) == 300,
          botctl.chain_root(table, 400, V))
    check("**停在 cmd 前面**（不越过 venv 去杀终端）",
          botctl.chain_root(table, 300, V) == 300)
    check("只有一层时返回自己", botctl.chain_root(table, 400,
                                               r"C:\somewhere\else") == 400)
    check("pid 不在表里 → 原样返回（不猜父进程）",
          botctl.chain_root(table, 999, V) == 999)
    check("空表 → 原样返回", botctl.chain_root({}, 400, V) == 400)
    # 自环不能死循环
    cyc = {5: (5, V + r"\Scripts\python.exe")}
    check("自己指向自己也不会死循环", botctl.chain_root(cyc, 5, V) == 5)
    check("大小写/斜杠不同也算在 venv 里（normpath+normcase）",
          botctl.chain_root({300: (0, r"D:\PROJ\.venv\Scripts\python.exe")},
                            300, r"d:/proj/.venv\Scripts") in (300,),
          "startswith 判定要经 normcase/normpath")


def t3_fmt_health():
    sec("T3 · fmt_health：读不出来就说读不出来")
    check("None → 明确说读不到", "读不到" in botctl.fmt_health(None))
    check("不是 dict → 也说读不到", "读不到" in botctl.fmt_health(["x"]))
    t = botctl.fmt_health({"poll_count": 31, "last_poll_age_seconds": 0.0,
                           "login_ok": True, "poll_errors": {}, "hook_errors": 0,
                           "send_failures": 0})
    check("正常快照能读", "轮询：31 次" in t and "登录：正常" in t, t)
    check("分片错误无时显示「无」", "分片错误：无" in t, t)
    bad = botctl.fmt_health({"poll_count": 3, "last_poll_age_seconds": "不是数字",
                             "login_ok": False, "poll_errors": {"fts_0": ["x", 1]},
                             "hook_errors": 2, "send_failures": 1})
    check("age 不是数字也不炸", "轮询：3 次" in bad, bad)
    check("登录异常要标出来", "异常" in bad, bad)
    check("有分片错误时报项数", "1 项" in bad, bad)
    check("hook 报错数出来了", "hook 报错：2" in bad, bad)


def t4_stop_dry_run_never_kills():
    sec("T4 · **试运行绝不动手**（这是这个模块最要紧的一条）")
    calls = []
    old_owner, old_table, old_run = botctl.owner, botctl.proc_table, botctl._run
    # ⚠️ 假表里的路径必须用**真实的 venv 目录**：chain_root 是拿它做前缀判断的，
    #    随便编一个 `D:\proj\.venv` 就永远认不出来（第一版就是这么错的，
    #    于是试运行只报「持锁 pid」、看不出转发器那一层）。
    venv_dir = os.path.join(botctl.env.BASE, ".venv")
    fake_table = {
        24688: (2892, os.path.join(venv_dir, "Scripts", "python.exe")),   # 转发器
        55044: (24688, r"C:\Python311\python.exe"),                      # 真解释器
    }
    try:
        botctl.owner = lambda *a, **k: 55044
        botctl.proc_table = lambda *a, **k: fake_table

        def _spy(cmd, timeout=25):
            calls.append(list(cmd))
            return 0, ""
        botctl._run = _spy

        ok, msg = botctl.stop(dry_run=True)
        check("返回成功（试运行本身不失败）", ok, msg)
        check("**一次 taskkill 都没发**",
              not any("taskkill" in (c[0] if c else "") for c in calls), calls)
        check("**一次命令都没发**（试运行连 netstat 都不该自己发）", calls == [], calls)
        check("但报告里说清了**真正会停谁**（含转发器）",
              "24688" in msg and "转发器" in msg, msg)

        botctl.owner = lambda *a, **k: None
        ok2, msg2 = botctl.stop(dry_run=True)
        check("没在跑时试运行也 ok 且如实说", ok2 and "本来就没在跑" in msg2, msg2)
    finally:
        botctl.owner, botctl.proc_table, botctl._run = old_owner, old_table, old_run


def t5_start_guards():
    sec("T5 · start 的前置判断：该拦就拦，**不许拉起进程**")
    old_owner, old_ready = botctl.owner, botctl.env.venv_ready
    old_popen = botctl.subprocess.Popen
    spawned = []
    try:
        def _spy_popen(*a, **k):
            spawned.append((a, k))
            raise AssertionError("不该拉起进程")
        botctl.subprocess.Popen = _spy_popen

        botctl.owner = lambda *a, **k: 55044          # 已经有一个在跑
        ok, msg = botctl.start()
        check("已经在跑 → 拒绝启动", not ok and "已经有一个在跑" in msg, msg)
        check("并且**没有拉起任何进程**", spawned == [], spawned)

        botctl.owner = lambda *a, **k: None
        botctl.env.venv_ready = lambda *a, **k: False
        ok, msg = botctl.start()
        check("venv 未就绪 → 拒绝启动并指向 install.bat",
              not ok and "install.bat" in msg, msg)
        check("同样没有拉起进程", spawned == [], spawned)
    finally:
        botctl.owner, botctl.env.venv_ready = old_owner, old_ready
        botctl.subprocess.Popen = old_popen


def t6_status_and_tail():
    sec("T6 · status / tail：读不到就说读不到")
    old_owner = botctl.owner
    try:
        botctl.owner = lambda *a, **k: None
        t = botctl.status_text()
        check("没在跑时明说「没有在跑」", "没有在跑" in t, t)
        check("并给出怎么启动", "botctl.py start" in t, t)
        botctl.owner = lambda *a, **k: 12345
        t2 = botctl.status_text()
        check("在跑时报出 pid", "12345" in t2 and "在跑" in t2, t2)
        check("在跑时给出日志路径", "bot.log" in t2, t2)
    finally:
        botctl.owner = old_owner

    out = botctl.tail(3)
    check("tail 不会抛异常（文件在不在都返回字符串）", isinstance(out, str), type(out))
    old_log = botctl.LOG_PATH
    try:
        botctl.LOG_PATH = os.path.join(os.path.dirname(__file__), "根本没有这个日志.log")
        out2 = botctl.tail(3)
        check("日志不存在 → 如实说读不到", "读不到" in out2, out2)
    finally:
        botctl.LOG_PATH = old_log


def main():
    print("=" * 60)
    print("botctl.py 自测（**绝不真的启停 bot**；所有真实交互都注入假实现）")
    print("=" * 60)
    t1_parse_netstat()
    t2_chain_root()
    t3_fmt_health()
    t4_stop_dry_run_never_kills()
    t5_start_guards()
    t6_status_and_tail()
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
