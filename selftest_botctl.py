"""botctl.py 自测：进程控制的安全性与纯逻辑（**绝不真的启停 bot**）。

这个模块的本事是「杀进程」，所以自测的第一要务不是覆盖功能，而是**证明它不会乱杀**：
  * `stop(dry_run=True)` **一次 taskkill 都不许发**（试运行就只是算，不动手）；
  * `start()` 在「已经有一个在跑」和「venv 没就绪」时**必须先返回、不许拉起进程**；
  * `chain_root()` **不许越过 venv 边界**（走到 cmd/终端/别的程序前面就得停）。

所有真实交互（`owner` / `proc_table` / `_run` / `env.venv_ready`）都注入假实现，
所以这份自测**不会碰真 bot、不会关真进程**。
"""
import os
import shutil
import sys
import tempfile

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

        # ── 提权那一道闸（2026-10-06 用户定的硬约束：助手永远跑在管理员上）──
        # 自测里**绝不真弹 UAC**：把 admin 那两件事换成桩。
        old_admin = (botctl.admin.ensure_elevated, botctl.admin.is_admin)
        try:
            botctl.env.venv_ready = lambda *a, **k: True

            # ① UAC 被拒 → 必须**如实失败**，且不许拉起任何进程（绝不偷偷降级跑）
            botctl.admin.ensure_elevated = lambda **kw: (
                False, "提权被取消（UAC 里点了「否」）", False)
            botctl.admin.is_admin = lambda: False
            ok2, msg2 = botctl.start()
            check("提权被拒 → 启动失败（**绝不静默降级成普通权限**）",
                  not ok2 and "取消" in msg2, msg2)
            check("……并且一个进程都没拉起", spawned == [], spawned)

            # ② 刚在另一个提权窗口里把 bot 拉起来了 → 这一份必须**什么都不做**
            #    （否则两个助手同时轮询 hook，实测会把微信搞崩）
            calls = []
            botctl.admin.ensure_elevated = lambda **kw: (
                calls.append(kw) or (True, "已在新窗口里以管理员身份启动", True))
            botctl.admin.is_admin = lambda: False
            ok3, msg3 = botctl.start()
            check("刚拉起提权进程 → 本进程不再拉进程（防两个助手同时跑）",
                  ok3 and spawned == [], (ok3, msg3, spawned))
            check("……提权时把 **pythonw**（无窗口）和 bot.py 传下去了",
                  calls and calls[0].get("exe", "").lower().endswith("pythonw.exe")
                  and calls[0].get("argv", [""])[0].endswith("bot.py"), calls)

            # ③ 本来就是管理员 → 正常往下走（这里只验"过了闸"，真 spawn 由别的用例管）
            botctl.admin.ensure_elevated = lambda **kw: (True, "已经是管理员", False)
            botctl.admin.is_admin = lambda: True
            botctl.owner = lambda *a, **k: 55044      # 装作已经跑起来，避免真拉进程
            ok4, msg4 = botctl.start()
            check("已经是管理员 → 照常启动（走原有的 owner 判定）",
                  not ok4 and "已经有一个在跑" in msg4, msg4)
        finally:
            botctl.admin.ensure_elevated, botctl.admin.is_admin = old_admin
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


def t7_search_service():
    sec("T7 · 配套搜索服务（SearXNG）：启 / 停 / 看 / 随助手起 —— **绝不真起真停**")
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    HOME = os.path.join(BASE_DIR, "..", "searxng")          # 随便一个「像」的目录，只用来看字符串
    GONE = os.path.join(BASE_DIR, "根本没有这个目录")

    # 所有真实交互都注入假的：认端口的 netstat、HTTP 探针、Popen、日志文件、pid 探活。
    NAMES = ("search_owner", "search_probe", "search_ready", "search_home",
             "search_enabled", "search_python", "SEARCH_LOG",
             "SEARCH_PID_FILE", "pid_alive")
    saved = {n: getattr(botctl, n) for n in NAMES}
    saved_popen = botctl.subprocess.Popen
    saved_run = botctl._run
    spawned = []
    # pid 记录指向一个真临时文件（那几个读写函数要真跑一遍），进程探活则注入：
    # 假实现里只有 9999 算活着，别的 pid 一律当死——**绝不真去 tasklist 查**。
    PIDF = os.path.join(tempfile.gettempdir(), f"botctl_selftest_{os.getpid()}.pid")
    botctl.SEARCH_PID_FILE = PIDF
    botctl.pid_alive = lambda pid: str(pid) == "9999"

    class _Alive:
        returncode = None

        def poll(self):
            return None

    class _Dead:
        returncode = 1

        def poll(self):
            return 1

    def _record(*a, **k):
        spawned.append((a, k))
        return _Alive()

    try:
        # ── 目录约定 & 开关判据（纯函数，不注入）──
        # ⚠️ 2026-10-05 改：约定从「上一级」扩成「项目内 / 上一级」**二选一**（随包携带）。
        # 这条断言以前写死 `== 上一级`，在开发机上「碰巧」是对的（上一级那份存在），
        # 到了**别人解压出来的包里**就必然失败——而自测恰恰是要在包里跑的那一份。
        # 所以这里只断言「**确实落在两个约定位置之一**」，不预设是哪一个；
        # 「两份都在时选能用的那份」由 T8 用注入的假 search_ready 精确钉住。
        bundled = os.path.join(botctl.env.BASE, "searxng")
        conv = os.path.join(os.path.dirname(botctl.env.BASE), "searxng")
        got = botctl.search_home(None)
        check("约定 = 项目内或上一级的 searxng（**不写死盘符/用户名**）",
              got in (bundled, conv), got)
        check("config 的 search.home 优先",
              botctl.search_home({"search": {"home": r"E:\tools\searxng"}}) == r"E:\tools\searxng")
        check("home 是空白串 → 回退约定（不拿空路径去找）",
              botctl.search_home({"search": {"home": "   "}}) in (bundled, conv))

        check("autostart 没写 = **开**（默认带起）", botctl.search_autostart_on({}) is True)
        check("显式 true = 开",
              botctl.search_autostart_on({"search": {"autostart": True}}) is True)
        check("显式 false = 关",
              botctl.search_autostart_on({"search": {"autostart": False}}) is False)
        check("写歪的字符串 \"false\" → **按关**（fail-safe：宁可不常驻）",
              botctl.search_autostart_on({"search": {"autostart": "false"}}) is False)
        check("写歪的 1 → 也按关",
              botctl.search_autostart_on({"search": {"autostart": 1}}) is False)

        check("端口默认跟 web_read 的 8888 一致", botctl.search_port(None) == 8888)
        check("端口跟着 search.base_url 走（别只认 8888）",
              botctl.search_port({"search": {"base_url": "http://127.0.0.1:9999"}}) == 9999)

        e = botctl.search_env(r"D:\proj\searxng")
        check("SEARXNG_SETTINGS_PATH 指向那个目录的 settings.yml",
              e["SEARXNG_SETTINGS_PATH"] == os.path.join(r"D:\proj\searxng", "settings.yml"))
        check("PYTHONPATH 挂 win_shims（少它服务在 Windows 上直接起不来）",
              e["PYTHONPATH"] == os.path.join(r"D:\proj\searxng", "win_shims"))
        check("SEARXNG_DISABLE_ETC_SETTINGS=1",
              e["SEARXNG_DISABLE_ETC_SETTINGS"] == "1")

        # 日志指到 NUL：自测**不许**在真 data/ 里留下东西
        botctl.SEARCH_LOG = os.devnull
        # 解释器的存在性也要注入：否则这份自测会**依赖本机真装了 searxng**，
        # 换台电脑（或打包目录里）就红——测试不能靠环境碰巧成立。
        botctl.search_python = lambda home=None: (
            os.path.join(GONE, "python.exe") if home == GONE else sys.executable)

        # ── search_start 的两道守卫：该拦就拦，一次 Popen 都不许发 ──
        botctl.subprocess.Popen = _record
        botctl.search_home = lambda cfg=None: HOME
        botctl.search_ready = lambda home=None: True

        botctl.search_owner = lambda *a, **k: 4242
        ok, msg = botctl.search_start(cfg=None, home=HOME)
        check("已经在跑 → 不拉第二个进程（端口会冲突）",
              ok and "本来就在跑" in msg and spawned == [], msg)

        botctl.search_owner = lambda *a, **k: None
        ok, msg = botctl.search_start(cfg=None, home=GONE)
        check("找不到后端解释器 → **不拉进程**，并给两条可照着做的路",
              (not ok) and spawned == [] and "start.bat" in msg and "search.home" in msg, msg)

        # ── 正常拉起：命令行/工作目录/环境变量 ──
        botctl.search_probe = lambda cfg=None, timeout=8: (True, "能查（探针拿到 3 条）")
        spawned.clear()
        ok, msg = botctl.search_start(cfg=None, home=HOME, wait=10)
        check("正常拉起 → 报成功，且说的是「能查」", ok and "已启动" in msg, msg)
        check("确实只拉了一个进程", len(spawned) == 1, len(spawned))
        args, kwargs = spawned[-1]
        cmd = list(args[0])
        check("命令行是 `-m searx.webapp`（**不是** python searx\\webapp.py，那样 import 不到 searx）",
              cmd[1:] == ["-m", "searx.webapp"], cmd)
        check("工作目录 = SearXNG 目录", kwargs.get("cwd") == HOME, kwargs.get("cwd"))
        cenv = kwargs.get("env") or {}
        check("三个环境变量都带上了（与 start.bat 逐条对齐）",
              cenv.get("PYTHONPATH") == os.path.join(HOME, "win_shims")
              and cenv.get("SEARXNG_SETTINGS_PATH") == os.path.join(HOME, "settings.yml")
              and cenv.get("SEARXNG_DISABLE_ETC_SETTINGS") == "1",
              {k: cenv.get(k) for k in ("PYTHONPATH", "SEARXNG_SETTINGS_PATH")})
        check("无窗口 + 脱离父进程（和 botctl.start 同一个姿势）",
              os.name != "nt" or bool(kwargs.get("creationflags", 0)
                                      & getattr(botctl.subprocess, "DETACHED_PROCESS", 0)),
              kwargs.get("creationflags"))
        check("stdin 是 DEVNULL（别让服务抢/等终端输入）",
              kwargs.get("stdin") == botctl.subprocess.DEVNULL)

        # ── 「拉起来了」≠「能用了」：这是这个功能最容易骗人的地方 ──
        botctl.search_probe = lambda cfg=None, timeout=8: (False, "连不上搜索服务")
        botctl.subprocess.Popen = lambda *a, **k: _Alive()
        ok, msg = botctl.search_start(cfg=None, home=HOME, wait=5)
        check("**拉起了进程但一直不能查 → 必须报失败**（绝不报假成功）",
              (not ok) and "没有报成功" in msg, msg)

        botctl.subprocess.Popen = lambda *a, **k: _Dead()
        ok, msg = botctl.search_start(cfg=None, home=HOME, wait=10)
        check("拉起来就退出 → 立刻失败并指向日志（不干等到超时）",
              (not ok) and "退出了" in msg and botctl.SEARCH_LOG in msg, msg)

        # ── search_stop：试运行绝不动手 ──
        calls = []
        botctl._run = lambda cmd, timeout=25: (calls.append(list(cmd)), (0, ""))[1]
        botctl.search_owner = lambda *a, **k: 777
        ok, msg = botctl.search_stop(cfg=None, dry_run=True)
        check("试运行返回成功并说清会停谁", ok and "[试运行]" in msg and "777" in msg, msg)
        check("试运行**一次 taskkill 都没发**",
              not any("taskkill" in (c[0] if c else "") for c in calls), calls)

        botctl.search_owner = lambda *a, **k: None
        ok, msg = botctl.search_stop(cfg=None)
        check("没在跑 → 幂等 ok 且如实说", ok and "本来就没在跑" in msg, msg)

        # ── ensure_search_service：bot 启动那条路的三道闸 + 绝不拦人 ──
        spawned.clear()
        botctl.subprocess.Popen = _record
        botctl.search_enabled = lambda cfg=None: False
        ok, msg = botctl.ensure_search_service({"search": {"enabled": False}}, wait=0)
        check("search.enabled 关着 → 跳过、不常驻进程",
              ok and "跳过" in msg and spawned == [], msg)

        botctl.search_enabled = lambda cfg=None: True
        ok, msg = botctl.ensure_search_service(
            {"search": {"enabled": True, "autostart": False, "home": HOME}}, wait=0)
        check("autostart=false → 跳过并指路控制台",
              ok and "跳过" in msg and "搜索服务" in msg and spawned == [], msg)

        botctl.search_ready = lambda home=None: False
        ok, msg = botctl.ensure_search_service(
            {"search": {"enabled": True, "home": GONE}}, wait=0)
        check("没装后端（换台电脑就是这样）→ **安静跳过，不当故障报**",
              ok and "跳过" in msg and spawned == [], msg)

        botctl.search_ready = lambda home=None: True
        botctl.search_owner = lambda *a, **k: None
        spawned.clear()
        ok, msg = botctl.ensure_search_service(
            {"search": {"enabled": True, "home": HOME}}, wait=0)
        check("条件都成立 → 拉起服务", ok and spawned, msg)
        check("bot 启动这条路的话术是「**启动中**」，不是「已可用」（wait=0 不等 HTTP）",
              ok and "启动中" in msg and "已可用" not in msg, msg)

        def _boom(cfg=None):
            raise RuntimeError("boom")
        botctl.search_enabled = _boom
        raised = None
        try:
            ok, msg = botctl.ensure_search_service({"search": {"enabled": True}}, wait=0)
        except Exception as e:                    # 这一条**绝不许**发生
            raised = e
            ok, msg = True, ""
        check("内部出错 → **绝不抛**（配套服务不许拦住助手启动）",
              raised is None and (not ok) and "不影响助手" in msg,
              raised or msg)

        # ── status：一屏说清，读不出来也不炸 ──
        botctl.search_home = saved["search_home"]      # 前面为守卫测试注入过，这里换回真的
        botctl.search_enabled = lambda cfg=None: True
        botctl.search_owner = lambda *a, **k: None
        botctl.search_probe = lambda cfg=None, timeout=8: (False, "连不上搜索服务（…）")
        t = botctl.search_status_text({"search": {"enabled": True, "home": HOME}})
        check("状态里有端口", "8888" in t, t)
        check("状态里说清「没有在跑」", "没有在跑" in t, t)
        check("状态里报出开关与自启两项", "search.enabled" in t and "search.autostart" in t, t)
        check("状态里给出日志路径", botctl.SEARCH_LOG in t, t)
        check("探针失败如实显示（不是一句含糊的「异常」）", "连不上搜索服务" in t, t)
        t2 = botctl.search_status_text({"search": {"home": GONE}}, probe=False)
        check("目录不存在时点出该写 search.home",
              "不存在" in t2 and "search.home" in t2, t2)

        # ── 「正在冷启动」那一整段（**真机抓出来的缺陷**：端口还没监听时判据会瞎）──
        # 现场：bot 启动时拉起 SearXNG，12 秒后状态屏说「没有在跑」——其实进程活得好好的；
        # 这时候再点一次 [1] 就会起第二个实例（Windows 的 SO_REUSEADDR 允许重复绑同一端口）。
        botctl._search_pid_clear()
        check("没记录过 → search_starting() 是 None", botctl.search_starting() is None)
        botctl._search_pid_write(9999)
        check("记下 pid 且它还活着 → 认「正在启动」", botctl.search_starting() == 9999,
              botctl.search_starting())
        check("pid 能读回（JSON 往返）", botctl._search_pid_read() == 9999)
        botctl.pid_alive = lambda pid: False
        check("进程已经没了 → 不再认为「正在启动」", botctl.search_starting() is None)
        botctl.pid_alive = lambda pid: str(pid) == "9999"

        botctl._search_pid_write(9999)
        with open(PIDF, "w", encoding="utf-8") as f:
            f.write("{坏掉的 json")
        check("pid 文件坏了 → None，不抛", botctl.search_starting() is None)
        with open(PIDF, "w", encoding="utf-8") as f:
            f.write('{"pid": 9999, "ts": 0}')
        check("陈年记录（重启后 pid 会被复用）→ 不认，免得挡住正常启动",
              botctl.search_starting() is None)

        botctl._search_pid_write(9999)
        botctl.search_owner = lambda *a, **k: None
        botctl.subprocess.Popen = _record
        spawned.clear()
        ok, msg = botctl.search_start(cfg=None, home=HOME)
        check("**端口还没监听、但我们拉起的那个还活着 → 绝不起第二个实例**",
              ok and "已经在启动了" in msg and spawned == [], msg)

        botctl.search_owner = lambda *a, **k: None
        t3 = botctl.search_status_text({"search": {"enabled": True, "home": HOME}}, probe=False)
        check("启动中 → 状态说「正在启动」，**不许说成「没有在跑」**",
              "正在启动" in t3 and "没有在跑" not in t3, t3)

        ok, msg = botctl.search_stop(cfg=None, dry_run=True)
        check("冷启动中途点「停止」也要认账（试运行说清会停谁）",
              ok and "9999" in msg, msg)
        botctl._search_pid_clear()
        t4 = botctl.search_status_text({"search": {"home": HOME}}, probe=False)
        check("端口与 pid 双双为空 → 才说「没有在跑」", "没有在跑" in t4, t4)
    finally:
        botctl._search_pid_clear()
        for n, v in saved.items():
            setattr(botctl, n, v)
        botctl.subprocess.Popen = saved_popen
        botctl._run = saved_run
        try:
            os.remove(PIDF)
        except OSError:
            pass


def t8_search_home_and_install():
    sec("T8 · 搜索后端：目录判据只有一份 + 「建 venv」不许报假成功")

    # ── ① search_home：配置优先；都留空时「能用那份优先」，否则挑存在的 ──
    # 2026-10-05 随包携带 searxng\ 之后，位置成了「项目内 / 项目上一级」二选一。
    # 开发机上两份都在（上一级那份装好了、包里那份只是源码）——**必须选装好的那份**，
    # 否则正在跑的搜索服务会突然被判成「没装」。
    saved = {n: getattr(botctl, n) for n in ("search_ready",)}
    saved_base = botctl.env.BASE
    try:
        fake_base = os.path.join(tempfile.gettempdir(), "botctl_t8_proj")
        bundled = os.path.join(fake_base, "searxng")
        sibling = os.path.join(os.path.dirname(fake_base), "searxng")
        botctl.env.BASE = fake_base

        botctl.search_ready = lambda home=None: False
        check("配置里写了 home → 就用它（不看约定位置）",
              botctl.search_home({"search": {"home": "D:/x/searxng"}}) == "D:/x/searxng")

        check("两份都没有 → 仍给「包里那份」的路径（首装建在这儿）",
              botctl.search_home({}) == bundled, botctl.search_home({}))

        botctl.search_ready = lambda home=None: os.path.normcase(str(home)) == os.path.normcase(sibling)
        check("★ 上一级那份装好了、包里那份没装 → **选装好的那份**（不许把在跑的服务判成没装）",
              botctl.search_home({}) == sibling, botctl.search_home({}))

        botctl.search_ready = lambda home=None: os.path.normcase(str(home)) == os.path.normcase(bundled)
        check("★ 包里那份装好了（新机器）→ 选它",
              botctl.search_home({}) == bundled, botctl.search_home({}))

        def _not_ready(home=None):
            return False
        botctl.search_ready = _not_ready
        just_bundled = botctl.search_home({})
        check("都装不好 → 挑**存在**的那份；都不存在也如实给一个路径（不抛）",
              just_bundled == bundled, just_bundled)
    finally:
        botctl.env.BASE = saved_base
        for n, v in saved.items():
            setattr(botctl, n, v)

    # ── ② search_install：三种失败都要**如实说**，绝不说成装好了 ──
    ok, msg = botctl.search_install(home=os.path.join(tempfile.gettempdir(), "no_such_searxng_xyz"))
    check("目录不存在 → 如实说是包不完整 / search.home 写错了",
          ok is False and "找不到" in msg, msg)

    d = tempfile.mkdtemp(prefix="botctl_t8_")
    try:
        sdir = os.path.join(d, "searxng")
        os.makedirs(sdir)
        saved2 = {n: getattr(botctl, n) for n in ("search_ready", "_base_python", "_stream")}
        try:
            botctl.search_ready = lambda home=None: False
            ok, msg = botctl.search_install(home=sdir)
            check("没有 requirements.txt → 如实说包不完整（不许去建 venv）",
                  ok is False and "requirements.txt" in msg, msg)

            open(os.path.join(sdir, "requirements.txt"), "w", encoding="utf-8").write("flask\n")
            botctl._base_python = lambda: None
            ok, msg = botctl.search_install(home=sdir)
            check("找不到能建 venv 的 Python → 如实说 + 给出怎么装 Python",
                  ok is False and "Python" in msg and "venv" in msg, msg)

            botctl._base_python = lambda: ["py", "-3.11"]
            calls = []

            def _fake_stream(args, cwd=None):
                calls.append(args)
                return 1          # 建 venv 就失败

            botctl._stream = _fake_stream
            ok, msg = botctl.search_install(home=sdir)
            check("建 venv 失败 → 不报成功",
                  ok is False and "venv" in msg and calls and "venv" in calls[0], msg)

            # pip 说成功、但 venv 里的解释器根本不在 → **仍然不算装好**
            botctl._stream = lambda args, cwd=None: 0
            ok, msg = botctl.search_install(home=sdir)
            check("★ pip 全成功但 search_ready 仍为假 → **绝不说装好了**",
                  ok is False and "跑不起来" in msg, msg)

            botctl.search_ready = lambda home=None: True
            ok, msg = botctl.search_install(home=sdir)
            check("本来就好了 → 直接说不用再装", ok and "已经装好" in msg, msg)
        finally:
            for n, v in saved2.items():
                setattr(botctl, n, v)
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
    t7_search_service()
    t8_search_home_and_install()
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
