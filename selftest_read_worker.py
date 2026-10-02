"""后台读文件（`read_worker.py`）的回归自测。

**不联网、不碰 30001、不需要微信**：全是纯磁盘 + 假活儿 + 临时目录。
用法：`.venv/Scripts/python.exe selftest_read_worker.py`

钉住的都是"静默失效"那一类：
  * 排队 / 串行（同一时刻只跑一个）；
  * 队满要**明确拒绝**，不许静默丢；
  * 活儿抛异常要**带出来**，不许当成"读出来是空的"；
  * 超时要如实标出来（`slow`），不是假装没发生；
  * 重启后残留的活儿要**如实说一句**（不假装读过）；
  * **worker 结构上碰不到 hook/微信库**（源码里不 import live_history / aixed）。
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import read_worker  # noqa: E402

TMP = tempfile.mkdtemp(prefix="selftest_read_worker_")
_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok = _ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


def _wait_results(n, timeout=10.0):
    """等 n 条结果（自测里用轮询；生产里是主循环每轮 drain 一次）。"""
    got = []
    t0 = time.time()
    while len(got) < n and time.time() - t0 < timeout:
        got += read_worker.drain()
        time.sleep(0.02)
    return got


def t0_no_wechat_dependency():
    """**结构上**保证 worker 碰不到 hook/微信库（不是靠自觉）。"""
    print("\n── 0 · worker 不许碰微信（源码级） ──")
    src = open(os.path.join(BASE, "read_worker.py"), encoding="utf-8").read()
    for bad in ("live_history", "aixed", "wcferry", "query_sql", "send_text"):
        check(f"源码里没有 {bad}", bad not in src)
    check("只 import 标准库", all(m in ("json", "os", "queue", "threading", "time",
                                       "traceback")
                                 for m in re.findall(r"^import (\w+)", src, re.M)),
          re.findall(r"^import (\w+)", src, re.M))


def t1_serial_and_result():
    print("\n── 1 · 串行执行 + 结果回到主线程 ──")
    read_worker.reset_for_test()
    order = []

    def job(tag):
        def fn():
            order.append(("start", tag, time.time()))
            time.sleep(0.15)
            order.append(("end", tag, time.time()))
            return f"{tag} 的内容", None
        return fn

    read_worker.JOBS_PATH = os.path.join(TMP, "readjobs.json")
    ok1, note1 = read_worker.submit("filehelper", "第一份.txt", job("A"))
    ok2, note2 = read_worker.submit("filehelper", "第二份.txt", job("B"))
    check("两份都收下了", ok1 and ok2, (ok1, ok2))
    check("第二份的收据里说明「前面还有 1 份在排队」", "排队" in note2, note2)

    got = _wait_results(2)
    check("两份结果都回来了", len(got) == 2, got)
    texts = {g["label"]: g.get("text") for g in got}
    check("结果带 chat / label / text",
          all(g["chat"] == "filehelper" for g in got) and
          texts.get("第一份.txt") == "A 的内容" and texts.get("第二份.txt") == "B 的内容",
          texts)
    # 串行：A 结束之后 B 才开始
    starts = [t for kind, tag, t in order if kind == "start"]
    ends = [t for kind, tag, t in order if kind == "end"]
    check("同一时刻只跑一个（A 结束 ≤ B 开始）", len(ends) == 2 and ends[0] <= starts[1],
          order)
    check("收据里没有承诺内容（内容只由结果那条发）",
          "内容" not in note1 and "读完了" not in note1, note1)


def t2_failure_is_reported():
    print("\n── 2 · 活儿失败要带出来（不许当成空内容） ──")
    read_worker.reset_for_test()

    def boom():
        raise ValueError("模拟解析炸了")

    read_worker.submit("filehelper", "坏文件.bin", boom)
    got = _wait_results(1)
    check("失败结果带 err", bool(got) and "模拟解析炸了" in (got[0].get("err") or ""), got)
    check("失败时 text 为空（不硬编一段假内容）", bool(got) and not got[0].get("text"), got)


def t3_queue_full_refuses():
    print("\n── 3 · 队满明确拒绝，不静默丢 ──")
    read_worker.reset_for_test()

    def slow():
        time.sleep(0.4)
        return "慢", None

    cfg = {"read": {"queue_max": 2}}
    r1 = read_worker.submit("c", "1", slow, cfg)
    time.sleep(0.05)
    r2 = read_worker.submit("c", "2", slow, cfg)
    r3 = read_worker.submit("c", "3", slow, cfg)
    check("前两份收下", r1[0] and r2[0], (r1, r2))
    check("第三份被**明确拒绝**并说清上限", r3[0] is False and "queue_max" in r3[1], r3)
    got = _wait_results(2)
    check("前面两份仍然正常读完（拒绝没有牵连它们）", len(got) == 2, got)


def t4_slow_flag():
    print("\n── 4 · 超时要如实标出来（不是假装没发生） ──")
    read_worker.reset_for_test()

    def slowish():
        time.sleep(0.25)
        return "慢活儿", None

    read_worker.submit("c", "慢.txt", slowish, {"read": {"job_timeout": 1}})
    got = _wait_results(1)
    check("没超时就不标 slow", bool(got) and got[0].get("slow") is False, got)

    read_worker.reset_for_test()
    read_worker.submit("c", "更慢.txt", slowish, {"read": {"job_timeout": 0}})
    got = _wait_results(1)
    check("job_timeout 配 0 也会被钳到 ≥1s（不会立刻全标慢）",
          bool(got) and got[0].get("slow") is False, got)


def t5_startup_note():
    print("\n── 5 · 重启后残留的活儿要如实说一句 ──")
    read_worker.reset_for_test()
    p = os.path.join(TMP, "readjobs.json")
    read_worker.JOBS_PATH = p
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"jobs": [{"chat": "filehelper", "label": "没读完的那份.pdf",
                             "started": time.time()}]}, f, ensure_ascii=False)
    note = read_worker.startup_note()
    check("说出是什么没读完", "没读完的那份.pdf" in note and "重启" in note, note)
    check("摘要文件被清掉（不会每次启动都念一遍）", not os.path.exists(p))
    check("没有残留时返回空串", read_worker.startup_note() == "")
    # 坏文件不许炸
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    check("坏摘要文件：当空处理、不抛异常", read_worker.startup_note() == "")


def t6_status_and_pending_file():
    print("\n── 6 · status 一行摘要 + 只存摘要不存内容 ──")
    read_worker.reset_for_test()
    p = os.path.join(TMP, "readjobs.json")
    read_worker.JOBS_PATH = p

    def slow():
        time.sleep(0.5)
        return "x", None

    read_worker.submit("filehelper", "正在读的.docx", slow, {"read": {"queue_max": 3}})
    time.sleep(0.1)
    st = read_worker.status({"read": {"queue_max": 3, "job_timeout": 1800}})
    check("/status 摘要里有「正在读」和文件名",
          "正在读" in st and "正在读的.docx" in st, st)
    if os.path.exists(p):
        raw = open(p, encoding="utf-8").read()
        check("落盘摘要里**没有内容字段**（只有 chat/label/started）",
              "内容" not in raw and "text" not in raw, raw[:120])
    _wait_results(1)
    raw = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    check("干完之后摘要里没有残留的活儿（jobs 为空）",
          raw == "" or '"jobs":[]' in raw.replace(" ", ""), raw[:120])


def main():
    print("=" * 60)
    print("read_worker 回归自测（临时目录：%s）" % TMP)
    print("=" * 60)
    read_worker.JOBS_PATH = os.path.join(TMP, "readjobs.json")
    try:
        t0_no_wechat_dependency()
        t1_serial_and_result()
        t2_failure_is_reported()
        t3_queue_full_refuses()
        t4_slow_flag()
        t5_startup_note()
        t6_status_and_pending_file()
    finally:
        read_worker.reset_for_test()
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 60)
    print("全部通过 ✅" if _ok else "有失败项 ❌")
    print("=" * 60)
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
