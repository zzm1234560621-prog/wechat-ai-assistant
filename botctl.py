"""控制 bot 进程：谁在跑 / 启动 / 停止 / 重启 / 看日志 / 看健康。

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
import subprocess
import sys
import time

import envsetup as env

LOCK_PORT = 39001
STATUS_JSON = os.path.join(env.BASE, "data", "status.json")
LOG_PATH = os.path.join(env.BASE, "bot.log")


# ── 纯函数（可测，不碰真实进程）──────────────────────────────────────────

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


# ── 真实系统（薄壳）────────────────────────────────────────────────────

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


def tail(n=40):
    """bot.log 末尾 n 行。读不出来就如实说。"""
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"读不到日志（{e}）：{LOG_PATH}"
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


def main():
    arg = (sys.argv[1].lower() if len(sys.argv) > 1 else "status")
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
    else:
        print("用法：python botctl.py status|health|log [行数]|follow|start|stop|restart")
        sys.exit(2)


if __name__ == "__main__":
    main()
