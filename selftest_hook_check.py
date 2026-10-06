"""`hook_check.py`（启动自检：微信里装的那份 hook 是不是包里这一份）的回归自测。

**只读、不碰微信、不连 hook**：把「微信目录里那份」和「包里那份」换成桩来验。

为什么必须有这个模块的自测（2026-10-06 真机）：用户换了**新包两遍**，
`IsLogin` 仍是 0、bot 一直刷「数据库打不开（微信没登录？）」——根因是
**微信目录里的 hook 还是旧的**，而没有任何地方对比过这两份文件。
这条自测钉的就是「必须能看出来、并且说清该跑哪个命令」。

用法：`.venv/Scripts/python.exe selftest_hook_check.py`
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import hook_check  # noqa: E402

_PASS = 0
_OK = True


def chk(cond, label, extra=""):
    global _PASS, _OK
    cond = bool(cond)
    _PASS += 1 if cond else 0
    _OK = _OK and cond
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {extra}" if extra and not cond else ""))
    return cond


NEW = ("C:\\PF\\Weixin\\version.dll", 527360, "a" * 64)
OLD = ("C:\\PF\\Weixin\\version.dll", 519168, "b" * 64)
BUNDLE = ("D:\\pkg\\installers\\wechat-4.1.10.27\\version.dll", 527360, "a" * 64)


def t1_same():
    print("\n[1] 装的就是包内那一份 → 正常，不报问题")
    saved = (hook_check.installed_dll, hook_check.bundle_dll)
    try:
        hook_check.installed_dll = lambda: NEW
        hook_check.bundle_dll = lambda: BUNDLE
        r = hook_check.check()
        chk(r["ok"] is True, "ok=True", r)
        chk(r["file_same"] is True, "file_same=True", r)
        chk(r["problems"] == [], "没有问题项", r["problems"])
        rep = hook_check.format_report(r)
        chk("✅ 正常" in rep, "报告里是「正常」", rep)
        chk("527360" in rep, "报告里带上了字节数（可核对）", rep)
    finally:
        hook_check.installed_dll, hook_check.bundle_dll = saved


def t2_old_installed():
    print("\n[2] 微信里是旧的（真机这一档）→ 必须报出来 + 给出该跑的命令")
    saved = (hook_check.installed_dll, hook_check.bundle_dll)
    try:
        hook_check.installed_dll = lambda: OLD
        hook_check.bundle_dll = lambda: BUNDLE
        r = hook_check.check()
        chk(r["ok"] is False, "ok=False（有问题）", r)
        chk(r["file_same"] is False, "file_same=False", r)
        p = " ".join(r["problems"])
        chk("旧" in p, "说清是「旧的」", p)
        chk("519168" in p and "527360" in p, "两个字节数都报出来（一眼可比）", p)
        chk("[7]" in p and "装 hook" in p, "给出该跑的命令（菜单路径）", p)
        chk("重启微信" in p, "提醒必须重启微信才会加载", p)
        rep = hook_check.format_report(r)
        chk("❌ 需要处理" in rep, "报告标题是「需要处理」", rep)
        chk("519168" in rep and "527360" in rep, "报告里两份都在", rep)
    finally:
        hook_check.installed_dll, hook_check.bundle_dll = saved


def t3_no_hook():
    print("\n[3] 微信目录里根本没有 version.dll → 报「还没装」")
    saved = (hook_check.installed_dll, hook_check.bundle_dll)
    try:
        hook_check.installed_dll = lambda: None
        hook_check.bundle_dll = lambda: BUNDLE
        r = hook_check.check()
        chk(r["ok"] is False, "ok=False", r)
        chk(any("没有" in p and "hook" in p for p in r["problems"]), "如实说没装", r["problems"])
    finally:
        hook_check.installed_dll, hook_check.bundle_dll = saved


def t4_runtime_evidence():
    print("\n[4] 运行时那条证据：没有 LoginGateInfo = 进程里是旧 hook")
    saved = (hook_check.installed_dll, hook_check.bundle_dll)
    try:
        hook_check.installed_dll = lambda: NEW       # 文件是新的（用户以为换好了）
        hook_check.bundle_dll = lambda: BUNDLE

        class _NoField:
            def db_status(self):
                return {"IsLogin": 0, "LoginGate": "", "hWeixin": 123}

        r = hook_check.check(client=_NoField())
        chk(r["runtime_gate_info"] is False, "runtime_gate_info=False", r)
        chk(r["ok"] is False, "ok=False（文件对、进程里还是旧的）", r)
        p = " ".join(r["problems"])
        chk("LoginGateInfo" in p, "指出缺的是哪个字段", p)
        chk("重启" in p, "给出「重启微信」这条路", p)

        class _New:
            def db_status(self):
                return {"IsLogin": 1, "LoginGate": "ok", "LoginGateInfo": {"cycles": 1}}

        r2 = hook_check.check(client=_New())
        chk(r2["ok"] is True and r2["runtime_gate_info"] is True,
            "有字段 → 运行时就正常", r2)
        chk(r2["is_login"] is True, "顺带报出 IsLogin", r2)

        class _Boom:
            def db_status(self):
                raise RuntimeError("连不上")

        r3 = hook_check.check(client=_Boom())
        chk(r3["ok"] is True and r3["runtime_gate_info"] is None,
            "连不上 hook → 那条证据判不了，**不误报**成「旧 hook」", r3)
        chk(any("探不到" in n for n in r3["notes"]), "……并在 notes 里如实说", r3["notes"])
    finally:
        hook_check.installed_dll, hook_check.bundle_dll = saved


def t5_bundle_missing():
    print("\n[5] 包内那份读不到（开发仓/半份包）→ 只跳过对比，不误报")
    saved = (hook_check.installed_dll, hook_check.bundle_dll)
    try:
        hook_check.installed_dll = lambda: NEW
        hook_check.bundle_dll = lambda: None
        r = hook_check.check()
        chk(r["ok"] is True, "ok=True（判不了就不说有问题）", r)
        chk(r["file_same"] is None, "file_same=None（明确是「判不了」）", r)
        chk(any("读不到" in n for n in r["notes"]), "notes 里说明为什么", r["notes"])
    finally:
        hook_check.installed_dll, hook_check.bundle_dll = saved


def t6_known_fingerprints():
    print("\n[6] 已知构建指纹（换包时这条会提醒你同步文档）")
    chk(527360 in hook_check.KNOWN and "新" in hook_check.KNOWN[527360],
        "527360 标成「新」", hook_check.KNOWN.get(527360))
    chk(519168 in hook_check.KNOWN and "旧" in hook_check.KNOWN[519168],
        "519168 标成「旧」", hook_check.KNOWN.get(519168))
    # 包里那份真文件必须能读、且落在已知指纹里（跑在仓库里时）
    b = hook_check.bundle_dll()
    if b:
        chk(b[1] in hook_check.KNOWN,
            f"仓库里那份 hook（{b[1]} 字节）在已知指纹表里", b)
    else:
        print("     （仓库里读不到 installers 下那份，跳过）")


def t7_db_activity():
    """「微信还在不在写库」的判据（2026-10-06 加的；给「hook 可达但库死了」当触发条件）。

    它在**每一轮轮询**里被调用，所以必须：只 stat 一批缓存下来的文件、不递归整个
    数据根、**不碰 hook**；而且**拿不到就返回 None**（未知 ≠ 死了）。
    """
    import shutil
    import tempfile
    import time
    print("\n[7] 「微信还在不在写库」（纯文件判据，不碰 hook）")
    root = tempfile.mkdtemp(prefix="hookcheck_db_")
    saved = dict(hook_check._DB_WATCH)
    try:
        db = os.path.join(root, "wxid_x_1", "db_storage", "message")
        os.makedirs(db)
        os.makedirs(os.path.join(root, "wxid_x_1", "db_storage", "session"))
        os.makedirs(os.path.join(root, "wxid_x_1", "msg", "file", "2026-10"))
        now = time.time()
        fresh = os.path.join(db, "message_fts.db")
        old = os.path.join(db, "message_0.db")
        wal = os.path.join(db, "message_fts.db-wal")
        shm = os.path.join(db, "message_fts.db-shm")
        decoy = os.path.join(root, "wxid_x_1", "msg", "file", "2026-10", "a.zip")
        for p in (fresh, old, wal, shm, decoy):
            with open(p, "w", encoding="utf-8") as f:
                f.write("x")
        os.utime(fresh, (now - 5000, now - 5000))
        os.utime(old, (now - 5000, now - 5000))
        os.utime(wal, (now - 120, now - 120))
        os.utime(shm, (now - 1, now - 1))
        os.utime(decoy, (now - 1, now - 1))      # msg/file 里再新也不算「在写库」
        hook_check.reset_db_watch_cache()
        paths = hook_check._db_watch_paths(root=root)
        chk(all("db_storage" in p for p in paths), "只看 db_storage 下的文件", paths)
        chk(not any(p.lower().endswith("-shm") for p in paths),
            "★ 不把 -shm 当判据（只读访问也会动它）", paths)
        chk(any(p.endswith("-wal") for p in paths), "把 -wal 算进来（事务按它更新）", paths)
        age = hook_check.core_db_age_sec(root=root, now=now)
        chk(100 <= age <= 200, "库龄取「最新一次写入」（比 -wal 的 120 秒稍大）", age)
        os.utime(wal, (now - 30, now - 30))
        hook_check.reset_db_watch_cache()
        chk(hook_check.core_db_age_sec(root=root, now=now) <= 60,
            "有新的写入 → 库龄立刻回落", hook_check.core_db_age_sec(root=root, now=now))
        chk(hook_check.core_db_age_sec(root=os.path.join(root, "不存在"), now=now) is None,
            "★ 找不到库 → 返回 None（未知 ≠ 死了）")
    finally:
        hook_check._DB_WATCH.clear()
        hook_check._DB_WATCH.update(saved)
        shutil.rmtree(root, ignore_errors=True)


def main():
    print("=" * 60)
    print("hook_check 自测（只读 / 不碰微信 / 不连 hook）")
    print("=" * 60)
    t1_same()
    t2_old_installed()
    t3_no_hook()
    t4_runtime_evidence()
    t5_bundle_missing()
    t6_known_fingerprints()
    t7_db_activity()
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
