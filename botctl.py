"""控制 bot 进程：谁在跑 / 启动 / 停止 / 重启 / 看日志 / 看健康。
另外还管**配套服务**（网上搜索后端 SearXNG）的启 / 停 / 看 / 随助手起
——这是同一类动作（认端口、拉进程、杀进程），所以必须有**唯一**实现，见下面 SEARCH_* 那一节。

## 为什么需要它

`助手.bat` 的菜单以前只有「前台启动」（独占一个黑窗口），**没有停止、没有重启、没有看日志**；
而后台无窗口运行时（开机自启就是这么起的）更是只能靠任务管理器。
这个模块就是那套动作的唯一实现，`console.py` 只负责显示菜单、调这里。

## 「谁在跑」的权威判据是 **39001 单实例锁**，不是扫命令行

`bot.py` 的 `main()` 干的第一件事就是 `acquire_single_instance()`：绑住回环端口 39001，
抢不到就 `sys.exit(1)`（并如实说明「同时跑两个会把微信搞崩」）。
所以**谁持有 39001，谁就是那个 bot** —— 这是代码自己保证的，比
「扫进程命令行里有没有 bot.py」可靠得多：后者会误伤编辑器、调试进程，
而且本项目 venv 的 `python.exe` 在实测里**只是个转发器**，真解释器是它的子进程，
于是「一个 bot」在进程表里会出现**两个**条目（真踩过：我一度以为起了两个实例）。

## 停的时候要连转发器一起停

正因为有那层转发器，停掉持锁那个之后可能剩一个空壳。所以这里会**沿父进程链往上走**，
把「可执行文件在本项目 venv 里」的那些祖先一起停掉——但**不会**越过 venv 边界
（避免误杀 cmd/终端/别的程序）。

## 这个模块**不查微信库、不碰 hook**

它只做进程/文件层面的事（netstat、taskkill、读 bot.log / data/status.json）。
健康信息一律读 `data/status.json`——那是 bot 自己写的快照，**不是**这里去问微信。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import admin
import envsetup as env

LOCK_PORT = 39001
STATUS_JSON = os.path.join(env.BASE, "data", "status.json")
LOG_PATH = os.path.join(env.BASE, "bot.log")

# 它和 bot 是**两个进程**：bot 只通过 HTTP 问它（web_read.py），从不 import 它。
# 「助手起来了、搜索却用不了」因此是一种很容易发生的残疾状态——这一节就是为它准备的：
# 启 / 停 / 看 / 随助手起 的唯一实现在这里，`console.py` 只显示菜单，`bot.py` 启动时也调这里。
#
# **为什么不另开一个模块**：本机进程控制的原语（netstat 认端口、taskkill、venv 路径）
# 全在本文件，抄第二份就会出现**两个「谁在跑」的判据**——项目已经吃过「两处同名不同义」的亏。
#
# ⚠️ 三条不许破的规矩：
#   1. **起不来绝不许拦住 bot 启动**：服务只是让搜索可用，不是助手能跑的前提；
#   2. **「拉起了进程」不等于「能查了」**：SearXNG 冷启动十几秒，手动启动那条路必须等到
#      真能查才算成功，**绝不报假成功**；bot 启动那条路不等（会白拖慢启动），
#      所以它的话术是「已拉起（启动中）」，不是「已可用」；
#   3. **`search.enabled` 关着就不起它**：没开搜索，没必要为它常驻一个进程。
SEARCH_PORT = 8888
SEARCH_LOG = os.path.join(env.BASE, "data", "searxng.log")
# 我们拉起的那个搜索服务的 pid（JSON）。**只为盖住「还在冷启动」那十几秒**，见 search_starting()。
SEARCH_PID_FILE = os.path.join(env.BASE, "data", "searxng.pid")
# 这份记录的可信窗口：超过它就不认（重启后 pid 会被复用，陈年记录会挡住正常启动）。
SEARCH_PID_TTL = 600
# 探针查询词：只为证明「服务真能按 bot 那条路返回 JSON」，不关心结果内容。
_SEARCH_PROBE_Q = "ping"



def parse_netstat(text, port=LOCK_PORT):
    """从 `netstat -ano` 的输出里找出**监听** `port` 的进程号。找不到返回 None。

    只看 LISTENING 行：ESTABLISHED 那半边是同一条连接的反向记录，认了会挑错进程。
    """
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        # 形如：TCP  127.0.0.1:39001  0.0.0.0:0  LISTENING  55044
        if not any(p.endswith(":" + str(port)) for p in parts[:3]):
            continue
        if "LISTENING" not in [p.upper() for p in parts]:
            continue
        tail = parts[-1]
        if tail.isdigit():
            return int(tail)
    return None


def chain_root(table, pid, venv_dir):
    """沿父进程链往上走，返回**最顶层那个仍在本项目 venv 里**的进程号。

    `table` 是 `{pid: (ppid, exe_path)}`。走到第一层「父进程不在 venv 里」就停——
    这样既能把转发器一起带走，又绝不越过 venv 边界去动 cmd/终端/别的程序。
    """
    venv = os.path.normcase(os.path.abspath(venv_dir))
    cur = int(pid)
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        row = table.get(cur)
        if not row:
            break
        ppid, _exe = row
        parent = table.get(int(ppid or 0))
        if not parent:
            break
        pexe = os.path.normcase(os.path.abspath(str(parent[1] or "")))
        if not pexe.startswith(venv):
            break
        cur = int(ppid)
    return cur


def fmt_health(snap, extra=None):
    """`data/status.json` → 给人看的一屏。**读不出来就说读不出来**，不编。"""
    if not isinstance(snap, dict):
        return "读不到健康快照（data/status.json 不存在或坏了）。"
    lines = ["运行健康（bot 自己写的快照）："]
    age = snap.get("last_poll_age_seconds")
    if age is None:
        lines.append("  轮询：还没有记录")
    else:
        try:
            lines.append(f"  轮询：{snap.get('poll_count')} 次，最近一次 {float(age):.1f} 秒前")
        except (TypeError, ValueError):
            lines.append(f"  轮询：{snap.get('poll_count')} 次")
    lines.append(f"  登录：{'正常' if snap.get('login_ok') else '**异常/未知**'}")
    errs = snap.get("poll_errors") or {}
    lines.append(f"  分片错误：{'无' if not errs else f'{len(errs)} 项 ' + str(list(errs)[:3])}")
    hk = snap.get("hook_errors")
    lines.append(f"  hook 报错：{0 if hk in (None, 0) else hk}")
    sf = snap.get("send_failures")
    lines.append(f"  发送失败：{0 if sf in (None, 0) else sf}")
    if snap.get("cursor_stalls") is not None:
        lines.append(f"  游标停滞：当前 {snap.get('cursor_stalls')} 轮，"
                     f"本次运行最长 {snap.get('max_cursor_stalls')} 轮"
                     f"（**停滞不等于故障**，空闲时也会涨）")
    if extra:
        lines.append("  " + str(extra))
    return "\n".join(lines)



def _run(cmd, timeout=25):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, text=True, encoding="utf-8",
                           errors="replace")
        return p.returncode, p.stdout or ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, f"{type(e).__name__}: {e}"


def owner(port=LOCK_PORT):
    """持锁进程号；没在跑返回 None。"""
    _rc, out = _run(["netstat", "-ano", "-p", "TCP"])
    return parse_netstat(out, port)


def is_running():
    return owner() is not None


def proc_table():
    """`{pid: (ppid, exe_path)}`。用系统自带 PowerShell 取（不引第三方依赖）。"""
    cmd = ("Get-CimInstance Win32_Process | "
           "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.ExecutablePath)\" }")
    _rc, out = _run(["powershell", "-NoProfile", "-Command", cmd], timeout=30)
    table = {}
    for line in out.splitlines():
        bits = line.rstrip("\r").split("\t")
        if len(bits) < 2 or not bits[0].strip().isdigit():
            continue
        exe = bits[2] if len(bits) > 2 else ""
        table[int(bits[0])] = (int(bits[1]) if bits[1].strip().isdigit() else 0, exe)
    return table


def status_text():
    pid = owner()
    if pid is None:
        return (f"bot：**没有在跑**（回环端口 {LOCK_PORT} 没人占）。\n"
                f"  启动：助手.bat 菜单，或 python botctl.py start")
    lines = [f"bot：**在跑**（pid {pid} 占着回环端口 {LOCK_PORT}）",
             f"  日志：{LOG_PATH}"]
    if os.path.isfile(STATUS_JSON):
        try:
            with open(STATUS_JSON, encoding="utf-8") as f:
                snap = json.load(f)
            lines.append("  " + fmt_health(snap).splitlines()[1].strip())
        except (OSError, ValueError):
            lines.append("  （健康快照读不出来）")
    return "\n".join(lines)


def start(background=True, wait=45):
    """启动。返回 `(ok, 一句人话)`。

    `background=True` 用 venv 的 **pythonw**（无窗口），和开机自启同一套；
    `False` 则在前台跑（看日志用，会阻塞）。

    ⚠️ 启动后**要确认它真的拿到锁**才算成功——「拉起了进程」不等于「跑起来了」
    （装坏的 venv、端口冲突都会让它立刻退出）。所以这里轮询到超时为止，
    超时就如实说「没起来，去看日志」，**绝不报成功**。
    """
    if owner() is not None:
        return False, (f"已经有一个在跑了（端口 {LOCK_PORT} 被占）。"
                       f"要重启就先 stop，或直接用 restart。")
    if not env.venv_ready():
        return False, ("虚拟环境未就绪或依赖缺失。先跑 install.bat（或菜单里的「安装依赖」）。")
    py = env.VENV_PYW if (background and os.path.exists(env.VENV_PYW)) else env.VENV_PY
    bot = os.path.join(env.BASE, "bot.py")

    # ⚠️ **提权在真正拉起进程之前**（2026-10-06 用户定的硬约束：助手永远跑在管理员上）。
    # 为什么必须在这里而不能只靠 bot.py 自己那一道：`py` 可能是 **pythonw**
    # （无窗口），而提权只能靠"再拉起一个进程"完成——那样会**多出一个控制台窗口**，
    # 开机自启就成了"弹个黑窗"。所以由调用方指定 exe，提权后仍是无窗口那个 pythonw。
    _aok, _amsg, _launched = admin.ensure_elevated(argv=[bot], exe=py, cwd=env.BASE,
                                                  capture=True)
    if not _aok:
        return False, _amsg
    if _launched:
        # 刚在**另一个**提权窗口里把 bot 拉起来了 → 这一份必须什么都不做，
        # 否则就是两个助手同时轮询 hook（会把微信搞崩，见 acquire_single_instance）。
        return True, _amsg

    if not background:
        return True, "前台启动中（Ctrl+C 停止）…"
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([py, bot], cwd=env.BASE, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except OSError as e:
        return False, f"启动失败：{type(e).__name__}: {e}"
    deadline = time.time() + max(5, int(wait))
    while time.time() < deadline:
        time.sleep(1.5)
        pid = owner()
        if pid is not None:
            return True, f"已启动（pid {pid}，无窗口）。日志：{LOG_PATH}"
    return False, (f"进程拉起来了，但 **{wait} 秒内没拿到 {LOCK_PORT} 锁**——"
                   f"它多半启动失败退出了。看日志：{LOG_PATH}")


def stop(dry_run=False, wait=20):
    """停止。返回 `(ok, 一句人话)`。没在跑也算 ok（幂等），但会如实说。"""
    root_pid = owner()
    if root_pid is None:
        return True, "本来就没在跑。"
    # ⚠️ **试运行也要算真正的目标**——试运行的意义就是「告诉你它会动谁」。
    # （第一版在这里写成 `proc_table() if not dry_run else {}`，于是试运行永远报
    #  「目标 = 持锁 pid」，看不出转发器那一层，等于白试。自测抓出来的。）
    table = proc_table()
    target = chain_root(table, root_pid, os.path.join(env.BASE, ".venv")) if table else root_pid
    note = f"（连转发器一起，根 pid {target}）" if target != root_pid else ""
    if dry_run:
        return True, f"[试运行] 会停止 pid {target}{note}；当前持锁 pid {root_pid}。"
    rc, out = _run(["taskkill", "/T", "/F", "/PID", str(target)], timeout=30)
    if rc != 0:
        return False, f"停止失败（rc={rc}）：{out.strip()[:160]}"
    deadline = time.time() + max(3, int(wait))
    while time.time() < deadline:
        if owner() is None:
            return True, f"已停止 pid {target}{note}。"
        time.sleep(1)
    return False, (f"发了停止命令但端口 {LOCK_PORT} 还占着——"
                   f"可能还有别的进程持有它。查：netstat -ano | findstr {LOCK_PORT}")


def restart(wait=45):
    ok, msg = stop()
    if not ok:
        return False, f"重启中止（停不掉）：{msg}"
    time.sleep(2)
    ok2, msg2 = start(wait=wait)
    return ok2, f"{msg}\n{msg2}"


def read_health():
    """`data/status.json` 的 dict；读不出来返回 None。"""
    try:
        with open(STATUS_JSON, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def tail(n=40, path=None):
    """日志末尾 n 行（默认 bot.log；`path` 给配套服务日志这类别的文件用）。读不出来就如实说。"""
    p = path or LOG_PATH
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"读不到日志（{e}）：{p}"
    return "".join(lines[-max(1, int(n)):])


def follow():
    """实时跟日志（Ctrl+C 停）。"""
    print(f"[跟随 {LOG_PATH}，Ctrl+C 停止]")
    pos = os.path.getsize(LOG_PATH) if os.path.isfile(LOG_PATH) else 0
    while True:
        try:
            if os.path.getsize(LOG_PATH) < pos:      # 轮转过 → 从头读
                pos = 0
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
            else:
                time.sleep(1)
        except KeyboardInterrupt:
            return
        except OSError:
            time.sleep(1)


# 判据**只走 web_read**（enabled / base_url / build_url / parse_json）——它才是「能不能搜」
# 的权威；这里只补它没有的东西（进程、目录、日志、启停）。懒导入是**必须的**：
# `console.py` 拿系统 python 跑、顶层只许导入标准库 + envsetup，而本模块是被它顶层导入的。

def _search_mod():
    """懒导入 web_read（它本身只依赖标准库，所以什么时候导都安全）。"""
    import web_read
    return web_read


def _search_sec(cfg):
    """config 的 `search:` 段（不是字典就当空）。

    只做这一层防御；**判据本身不许在这儿重写一份**（enabled / base_url 一律走 web_read）。
    """
    sec = ((cfg or {}).get("search") or {})
    return dict(sec) if isinstance(sec, dict) else {}


def load_cfg():
    """读 config.yaml（读不出来/没有/yaml 不在，都返回 `{}`，绝不抛）。

    CLI 与 `console.py` 共用这一份；yaml 延迟导入的理由同上（本模块要被系统 python 导入）。
    """
    try:
        import yaml
        with open(os.path.join(env.BASE, "config.yaml"), encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def search_enabled(cfg=None):
    """`search.enabled`。判据只有 web_read 那一份。"""
    return _search_mod().enabled(cfg)


def search_url(cfg=None):
    """bot 问搜索服务的地址（`search.base_url`）。"""
    return _search_mod().base_url(cfg)


def search_port(cfg=None):
    """搜索服务的端口：**从 search.base_url 里取**，取不到才退回顾约定的 8888。

    别另写一个「只认 8888」的判据——用户把 `base_url` 改到别的端口时，两处会走偏：
    HTTP 探的是新端口、启停却盯着 8888。
    """
    try:
        u = urllib.parse.urlsplit(search_url(cfg))
        if u.port:
            return int(u.port)
    except Exception:
        pass
    return SEARCH_PORT


def search_home(cfg=None):
    """SearXNG 目录。**判据只有这一份**——`web_read._searxng_hint()` 也调它，别再抄第二份
    （抄了就会「报错指向 A、启动去找 B」）。

    配置的 `search.home` 优先；留空时按顺序试两个约定位置：

      1. `<项目>\\searxng\\`      —— **2026-10-05 起随包携带的那份**（新机器上只有它）；
      2. `<项目上一级>\\searxng\\` —— 老约定（开发机上是它，且装着单独的 .venv）。

    ⚠️ **两份都在时「能用那份优先」**（有 `.venv\\Scripts\\python.exe`）：否则开发机上
    （上一级那份装好了、包里这份只是源码）会突然被判成「没装」，正在跑的搜索服务白挂。
    **绝不写死盘符/用户名。**
    """
    raw = str(_search_sec(cfg).get("home") or "").strip()
    if raw:
        return raw
    bundled = os.path.join(env.BASE, "searxng")
    sibling = os.path.join(os.path.dirname(env.BASE), "searxng")
    for d in (bundled, sibling):
        if search_ready(d):          # 装好的优先（只看解释器在不在，不实跑）
            return d
    for d in (bundled, sibling):
        if os.path.isdir(d):         # 都没装好 → 挑存在的那份（首装会在这份里建 venv）
            return d
    return bundled


def search_python(home=None):
    return os.path.join(home or search_home(), ".venv", "Scripts", "python.exe")


def search_ready(home=None):
    """搜索后端的解释器在不在。只看文件在不在，**不实跑**（起服务本身就会说话）。"""
    return os.path.exists(search_python(home))


def search_autostart_on(cfg=None):
    """`search.autostart`：**没写 = 开**；写了就必须是 `true` 才算开。

    写歪的值（`"false"` / `0` / 手滑）一律按**关**处理——这个开关决定要不要多一个常驻进程，
    方向要朝「宁可不常驻」倒（和 `image.mode` 写歪就按 `off` 同一条 fail-safe 规矩）。
    """
    sec = _search_sec(cfg)
    if "autostart" not in sec:
        return True
    return sec.get("autostart") is True


def search_env(home=None):
    """Windows 上跑原生 SearXNG 必须的三个环境变量——**逐条对应 `searxng\\start.bat`**。

    改这里就必须同步改那个 .bat（反之亦然）：`PYTHONPATH` 挂的是 `win_shims\\pwd.py`
    兼容层，少了它服务直接起不来（见 docs/web-search-notes.md §2.1）。
    """
    home = home or search_home()
    return {
        "SEARXNG_SETTINGS_PATH": os.path.join(home, "settings.yml"),
        "SEARXNG_DISABLE_ETC_SETTINGS": "1",
        "PYTHONPATH": os.path.join(home, "win_shims"),
    }


def search_owner(port=None):
    """听着搜索端口的进程号；没在跑返回 None。**这是「在跑」的权威判据**。"""
    _rc, out = _run(["netstat", "-ano", "-p", "TCP"])
    return parse_netstat(out, int(port or SEARCH_PORT))


# ── 「还在冷启动」那一段：端口权威判据在这里会瞎 ──────────────────────────
# SearXNG 从拉起到开始监听要十几秒（bot 启动时它正忙着重活，实测能超过 12 秒）。
# 这段窗口里 `search_owner()` 是 None —— 于是**「正在启动」和「根本没在跑」长得一模一样**：
#   * 控制台会把「正在启动」显示成「没有在跑」（少说了一半事实）；
#   * 再点一次 [1] 就会**起第二个实例**（Windows 上 SO_REUSEADDR 允许重复绑同一端口，
#     本项目为此栽过不止一次）。
# 所以这里补一份「我们拉起的那个 pid」的记录，**只作辅助**：端口一旦监听，就以端口为准。

def pid_alive(pid):
    """这个 pid 还活着吗。用系统自带的 `tasklist`。

    ⚠️ **绝不用 `os.kill(pid, 0)`**：Windows 上 Python 的 `os.kill` 只认
    `CTRL_C_EVENT` / `CTRL_BREAK_EVENT`，**其它值一律 TerminateProcess** ——
    那句「探活」会**真的把服务杀掉**，是拿判据当凶器。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    rc, out = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=15)
    if rc != 0:
        return False
    return str(pid) in str(out or "")


def _search_pid_read():
    """读我们上次拉起的 pid；没有 / 坏了 / 太旧 → None（**绝不抛**）。"""
    try:
        with open(SEARCH_PID_FILE, encoding="utf-8") as f:
            d = json.load(f)
        pid = int(d.get("pid") or 0)
        ts = float(d.get("ts") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if pid <= 0:
        return None
    if SEARCH_PID_TTL and (time.time() - ts) > SEARCH_PID_TTL:
        return None                 # 陈年记录：pid 早被复用了，认它反而会挡住启动
    return pid


def _search_pid_write(pid):
    """记下我们拉起的 pid。**记不下来也只算了**——它只是防重复的辅助判据，不是权威。"""
    try:
        os.makedirs(os.path.dirname(SEARCH_PID_FILE), exist_ok=True)
        with open(SEARCH_PID_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "ts": time.time()}, f)
    except OSError:
        pass


def _search_pid_clear():
    try:
        os.remove(SEARCH_PID_FILE)
    except OSError:
        pass


def search_starting():
    """我们拉起的那个进程**还活着、但端口还没开始监听**（= 正在冷启动）。返回 pid 或 None。"""
    pid = _search_pid_read()
    if pid and pid_alive(pid):
        return pid
    return None


def search_probe(cfg=None, timeout=8):
    """按 **bot 走的那条路**真查一次（`/search?...&format=json`），返回 `(能不能查, 一句人话)`。

    ⚠️ 这一步会**真的花一次搜索**（SearXNG 那边会去问引擎）。换来的是「能查」这个断言有证据
    ——只探「端口开着」是不够的：`settings.yml` 里没开 json 时端口照样开着，一搜就返回网页。
    解析/判据全部复用 `web_read`，不在这儿另写一套。
    """
    w = _search_mod()
    url = w.build_url(cfg, _SEARCH_PROBE_Q)
    base = w.base_url(cfg)
    req = urllib.request.Request(url, headers={
        "User-Agent": "wechat-ai-assistant/botctl",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=max(2, int(timeout))) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return False, f"搜索服务返回 HTTP {e.code}（{base}）"
    except Exception as e:
        return False, f"连不上搜索服务（{base}）：{type(e).__name__}"
    results, err = w.parse_json(body)
    if err:
        return False, err
    return True, f"能查（探针拿到 {len(results)} 条，{base}）"


def search_start(cfg=None, home=None, wait=40):
    """起搜索服务（无窗口、detached），返回 `(ok, 一句人话)`。

    和 bot 自己的 `start()` 同一个姿势：拉进程 → **等它真能用** → 才算成功。
    差别在「能用」的判据：bot 是「拿到 39001 锁」，这里是「HTTP 真能查」（见 search_probe）。
    `wait=0` = 拉起就返回，不等（给 bot 启动用，见 ensure_search_service）。
    """
    home = home or search_home(cfg)
    port = search_port(cfg)
    pid = search_owner(port)
    if pid is not None:
        _search_pid_clear()          # 端口已经监听了，那份 pid 记录没用了
        return True, f"搜索服务本来就在跑（pid {pid}，端口 {port}）。"
    boot = search_starting()
    if boot is not None:
        # 端口还没监听、但我们拉起的那个还活着 —— **绝不能再起第二个**。
        return True, (f"搜索服务**已经在启动了**（pid {boot}，端口 {port} 还没开始监听）——"
                      f"冷启动十几秒是正常的，别起第二个。看状态：助手.bat → [8] 更多 → [9] → [3]。")
    py = search_python(home)
    if not os.path.exists(py):
        return False, (
            f"找不到搜索后端的解释器：{py}\n"
            f"  SearXNG 要单独装：源码放在本项目**上一级**、跑那个目录里的 start.bat 装依赖；\n"
            f"  装在别处就在 config.yaml 里写 `search.home` 指过去。")
    child_env = dict(os.environ)
    child_env.update(search_env(home))
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | \
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        os.makedirs(os.path.dirname(SEARCH_LOG), exist_ok=True)
    except OSError:
        pass
    try:
        log = open(SEARCH_LOG, "a", encoding="utf-8", errors="replace")
    except OSError:
        log = None
    try:
        proc = subprocess.Popen([py, "-m", "searx.webapp"], cwd=home, env=child_env,
                                creationflags=flags, stdin=subprocess.DEVNULL,
                                stdout=(log if log is not None else subprocess.DEVNULL),
                                stderr=subprocess.STDOUT, close_fds=True)
    except OSError as e:
        return False, f"启动失败：{type(e).__name__}: {e}"
    finally:
        if log is not None:
            log.close()
    # 记下「这是我们拉起的那个」——只为盖住下面这段「端口还没监听」的冷启动窗口。
    _search_pid_write(getattr(proc, "pid", 0) or 0)
    if int(wait) <= 0:
        return True, f"已拉起搜索服务（**启动中**，十几秒后能查）。日志：{SEARCH_LOG}"
    deadline = time.time() + max(5, int(wait))
    while time.time() < deadline:
        time.sleep(2)
        try:
            if proc.poll() is not None:      # 我们拉起来的那个已经退出了 → 别再干等
                _search_pid_clear()
                return False, (f"搜索服务启动后就退出了（exit={proc.returncode}）。"
                               f"原因看日志 {SEARCH_LOG} 的末尾：\n{tail(8, SEARCH_LOG)}")
        except Exception:
            pass
        ok, detail = search_probe(cfg)
        if ok:
            _search_pid_clear()
            return True, f"搜索服务已启动，{detail}。日志：{SEARCH_LOG}"
    return False, (f"进程拉起来了，但 {wait} 秒内还不能查——**没有报成功**。\n"
                   f"  看日志：{SEARCH_LOG}\n{tail(8, SEARCH_LOG)}")


def _stream(args, cwd=None):
    """跑一条**要边跑边看**的命令（pip 装依赖得几分钟），原样把输出交给用户。

    和 `_run` 的区别：`_run` 是「拿输出回来解析」（netstat/tasklist），这里是「让它说话」——
    装依赖失败时那几行 pip 输出就是**唯一**的线索，捕获了再转述只会丢信息。
    """
    try:
        return subprocess.run(args, cwd=cwd).returncode
    except OSError as e:
        print(f"[搜索服务] 起不来：{type(e).__name__}: {e}")
        return 1


def _base_python():
    """找一个**能用来建 venv** 的解释器。返回 argv 列表；找不到返回 None。

    优先当前这个解释器（`sys.executable`），但**绝不在我们自己的 `.venv` 里再套一层**
    ——那种 venv 的 base 是同一个 Python，能建，只是容易让人看糊。
    退路按 `env.PREFERRED_PY` 试 `py -3.11` 这些（和 install.bat 挑 Python 的顺序同一份
    常量，别另写一串版本号）；再退到 PATH 上的 `python`。
    """
    exe = sys.executable
    our_venv = os.path.normcase(os.path.join(env.BASE, ".venv") + os.sep)
    if exe and not os.path.normcase(os.path.abspath(exe)).startswith(our_venv):
        return [exe]
    py = shutil.which("py")
    if py:
        for v in env.PREFERRED_PY:
            if _run([py, "-" + v, "-c", "import sys"], timeout=30)[0] == 0:
                return [py, "-" + v]
    p = shutil.which("python")
    return [p] if p else None


def search_install(cfg=None, home=None):
    """给搜索后端**建 venv + 装依赖**（第一次装 / 换机器时跑）。返回 `(ok, 一句人话)`。

    两条不明显的规矩：

      * **单独一份 venv**：SearXNG 的依赖（flask / lxml / curl_cffi / valkey…）和 bot 的
        不是一套，塞进 bot 的 `.venv` 会互相顶版本；
      * **它的 `.venv` 绝不进包**：venv 里记的是绝对路径，跨机器拷必坏——和 bot 自己
        `.venv` 同一条规矩。所以随包只带源码，venv 到这台上现建。

    「装完了没」的判据是 `search_ready()`（解释器真在），**不是 pip 的退出码**——
    所以这里绝不因为 pip 说成功就报成功。
    """
    home = home or search_home(cfg)
    if not os.path.isdir(home):
        return False, (f"找不到 SearXNG 目录：{home}\n"
                       f"  包里本该自带（项目根的 `searxng\\`）；没有就是包不完整。\n"
                       f"  装在别处就在 config.yaml 写 `search.home` 指过去。")
    if search_ready(home):
        return True, f"搜索后端的依赖已经装好了（{search_python(home)}），不用再装。"
    req = os.path.join(home, "requirements.txt")
    if not os.path.isfile(req):
        return False, f"SearXNG 目录里没有 requirements.txt（{req}）——包可能不完整。"

    py = _base_python()
    if not py:
        return False, ("找不到能建 venv 的 Python。先装 64 位 Python 3.11"
                       "（winget install -e --id Python.Python.3.11），再跑一次。")

    venv_dir = os.path.join(home, ".venv")
    print(f"[搜索服务] 建 venv：{venv_dir}（用 {' '.join(py)}）")
    if _stream(py + ["-m", "venv", venv_dir], cwd=home) != 0:
        return False, ("建 venv 失败。上面那几行有原因；常见是没有这个 Python 版本，"
                       "或者目录没有写权限。")

    pyv = search_python(home)
    print("[搜索服务] 装依赖（要联网下载，第一次几分钟）…")
    if _stream([pyv, "-m", "pip", "install", "--upgrade", "pip"], cwd=home) != 0:
        print("[搜索服务] ⚠️ 升级 pip 失败（不致命），继续直接装依赖。")
    if _stream([pyv, "-m", "pip", "install", "-r", req], cwd=home) != 0:
        return False, (f"装依赖失败（pip 退出码非 0）。上面那段 pip 输出里是真正的原因；"
                       f"SearXNG 的依赖里有 lxml / curl_cffi 这类轮子，装不上多半是网络。")

    if not search_ready(home):
        return False, "pip 说装完了，但 venv 里的解释器跑不起来——**不能算装好**。"
    return True, (f"装好了：{pyv}\n"
                  f"  起服务：助手.bat → [8] 更多 → [9] 搜索服务 → [1]，或那个目录里的 start.bat")


def search_stop(cfg=None, dry_run=False, wait=20):
    """停搜索服务。没在跑也算 ok（幂等），但会如实说。"""
    port = search_port(cfg)
    pid = search_owner(port)
    if pid is None:
        # 端口权威判据在这里会瞎：冷启动中途还没监听。顺手也把那个停掉，
        # 否则用户点了「停止」却被回一句「本来就没在跑」，而进程其实正在起。
        pid = search_starting()
    if pid is None:
        return True, f"搜索服务本来就没在跑（端口 {port} 没人占）。"
    if dry_run:
        return True, f"[试运行] 会停止搜索服务 pid {pid}（端口 {port}）。"
    rc, out = _run(["taskkill", "/T", "/F", "/PID", str(pid)], timeout=30)
    if rc != 0:
        return False, f"停止失败（rc={rc}）：{out.strip()[:160]}"
    _search_pid_clear()
    deadline = time.time() + max(3, int(wait))
    while time.time() < deadline:
        if search_owner(port) is None:
            return True, (f"已停止搜索服务 pid {pid}。"
                          f"（如果它是从 start.bat 的窗口起的，那个窗口会显示「已退出」，"
                          f"按任意键关掉即可。）")
        time.sleep(1)
    return False, (f"发了停止命令但端口 {port} 还占着——可能还有别的进程持有它。"
                   f"查：netstat -ano | findstr {port}")


def search_status_text(cfg=None, probe=True):
    """搜索服务的一屏：进程 / 能不能查 / 开关 / 自启 / 目录 / 日志。**读不出来就说读不出来。**"""
    port = search_port(cfg)
    url = search_url(cfg)
    home = search_home(cfg)
    pid = search_owner(port)
    boot = None if pid else search_starting()
    lines = [f"搜索服务（网上搜索后端 SearXNG）：{url}"]
    if pid:
        lines.append(f"  进程：**在跑**（pid {pid}，端口 {port}）")
    elif boot:
        # 端口权威判据在这段窗口里是瞎的：**「正在启动」不许被说成「没有在跑」**。
        lines.append(f"  进程：**正在启动**（pid {boot}，端口 {port} 还没开始监听）"
                     f"——冷启动十几秒是正常的，**别再点一次启动**。")
    else:
        lines.append(f"  进程：**没有在跑**（端口 {port} 没人占）")
    if probe:
        ok, detail = search_probe(cfg)
        lines.append(f"  能不能查：{'✅ ' if ok else '❌ '}{detail}")
    try:
        on = search_enabled(cfg)
        lines.append(f"  开关：config.yaml 的 search.enabled = {str(on).lower()}"
                     + ("" if on else "（**关着**，助手不会用搜索；要开就写 true）"))
    except Exception as e:
        lines.append(f"  开关：**读不出来**（{type(e).__name__}）")
    lines.append("  随助手自启：" + ("开（search.autostart）" if search_autostart_on(cfg)
                                  else "**关**（search.autostart）"))
    exists = os.path.isdir(home)
    lines.append(f"  目录：{home}（{'存在' if exists else '**不存在**'}）")
    if exists and not search_ready(home):
        lines.append("  ⚠️ 目录里没有 .venv\\Scripts\\python.exe：依赖还没装"
                     "（见 README「网上搜索」）。")
    if not exists:
        lines.append("     按约定它该和本项目**平级**；装在别处就在 config.yaml 写 search.home。")
    lines.append(f"  日志：{SEARCH_LOG}")
    return "\n".join(lines)


def ensure_search_service(cfg=None, wait=0):
    """bot 启动时带起配套搜索服务 —— **best-effort，绝不抛、绝不拦住 bot**。

    只在三件事都成立时才动手：
      * `search.enabled` 为真（没开搜索就没必要常驻一个进程）；
      * `search.autostart` 没被显式关掉；
      * 解释器真在（换台电脑/没装后端时本来就该安静跳过，那不是故障）。

    返回 `(ok, 一句人话)`；跳过时也是 ok=True + 一句「跳过」，让 bot.log 不出现假警报。
    """
    try:
        if not search_enabled(cfg):
            return True, "网上搜索没开启（search.enabled），跳过——没开就不为它常驻进程。"
        if not search_autostart_on(cfg):
            return True, "search.autostart 关着，跳过（要起：助手.bat → 更多 → 搜索服务）。"
        home = search_home(cfg)
        if not search_ready(home):
            return True, (f"没找到 SearXNG（{home}），跳过——没装后端时不吵。"
                          f"装在别处就在 config.yaml 写 search.home。")
        return search_start(cfg=cfg, home=home, wait=wait)
    except Exception as e:
        # 这一条是**故意的**：配套服务起不来只该让搜索不可用，绝不该让微信助手起不来。
        return False, f"带起搜索服务时出错（已忽略，不影响助手）：{type(e).__name__}: {e}"


def main():
    arg = (sys.argv[1].lower() if len(sys.argv) > 1 else "status")
    # ⚠️ 命令式入口在这里就提权（交互式 `botctl.py start/stop/...`）：提权后**等它跑完并沿用
    # 它的退出码**，用户在本窗口里能直接看到结果；不提权的话父进程会立刻退出、看起来像
    # "什么都没发生"。
    # 为什么连 `stop` 也要提权：助手是**管理员**跑的（硬约束），普通权限的 `taskkill`
    # 杀不掉高完整性进程 —— 提权少了这一条，「停不掉」就会变成新的坑。
    # 只读命令（status/health/log/follow/search-status）**不提权**：看一眼状态不该弹 UAC。
    if arg in ("start", "stop", "restart",
               "search-start", "search-stop", "search-install"):
        _ok, _msg, _launched = admin.ensure_elevated(capture=False, wait=True)
        if not _ok:
            print(f"[!] {_msg}")
            sys.exit(1)
        if _launched:
            return
    if arg == "status":
        print(status_text())
    elif arg == "health":
        print(fmt_health(read_health()))
    elif arg == "log":
        n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 40
        print(tail(n))
    elif arg == "follow":
        follow()
    elif arg == "start":
        ok, msg = start()
        print(("[√] " if ok else "[!] ") + msg)
        sys.exit(0 if ok else 1)
    elif arg == "stop":
        ok, msg = stop(dry_run="--dry-run" in sys.argv)
        print(("[√] " if ok else "[!] ") + msg)
        sys.exit(0 if ok else 1)
    elif arg == "restart":
        ok, msg = restart()
        print(("[√] " if ok else "[!] ") + msg)
        sys.exit(0 if ok else 1)
    elif arg in ("search-status", "search"):
        print(search_status_text(load_cfg()))
    elif arg == "search-start":
        ok, msg = search_start(cfg=load_cfg())
        print(("[√] " if ok else "[!] ") + msg)
        sys.exit(0 if ok else 1)
    elif arg == "search-stop":
        ok, msg = search_stop(cfg=load_cfg(), dry_run="--dry-run" in sys.argv)
        print(("[√] " if ok else "[!] ") + msg)
        sys.exit(0 if ok else 1)
    elif arg == "search-install":
        ok, msg = search_install(cfg=load_cfg())
        print(("[√] " if ok else "[!] ") + msg)
        sys.exit(0 if ok else 1)
    else:
        print("用法：python botctl.py status|health|log [行数]|follow|start|stop|restart"
              "|search-status|search-start|search-stop|search-install")
        sys.exit(2)


if __name__ == "__main__":
    main()
