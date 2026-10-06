"""后台读文件：把「重活」从收消息那条线程上挪走。

规格：`docs/file-input-spec.md` 第三节。**用户要的是"什么文件都能读、不限大小"**，
而读文件本来是**同步**跑在收消息那条线程上的：一份 500MB 的 PDF、一段长视频、
一次老格式转换，都能让 bot 在那几分钟里**完全不轮询**（定时任务迟、掉登录探测停、
用户眼里就是"没反应"）。所以重活必须挪到后台。

三条铁律（姿势照抄项目里已有的那套）
------------------------------------
1. **worker 只碰磁盘和模型 HTTP**：绝不查微信库、绝不碰 hook、绝不发消息。
   （hook 不支持并发，已被两路并发查询搞崩过 6 次。）
2. **发消息一律回主线程**：worker 只把结果丢进队列，bot 在轮询空档 `drain()`
   出来自己发 —— 和 `scheduler` 挂在轮询空档里是同一个姿势。
3. **排队、超时、失败都要如实说**：不静默丢任务、不假装读完了。

超时为什么是"如实说"而不是"杀掉任务"
------------------------------------
Python 没法中断一个正卡在 `page.extract_text()` 或 OCR 子进程里的线程（硬杀线程会留下
半截状态）。所以 `read.job_timeout` 的语义是：**超过它就在结果里如实标出来**（"这次读得
比预期久：用了 N 秒"）并在日志里留一笔；任务继续跑完，结果照发。

落盘 `data/readjobs.json` 只存「还没读完的活儿」的摘要，用途只有一个：
**助手重启后如实告诉用户"上次那份没读完"**，而不是假装它读过了。
"""
import json
import os
import queue
import threading
import time
import traceback

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_PATH = os.path.join(PROJECT_DIR, "data", "readjobs.json")

_QUEUE_MAX_DEFAULT = 5
_JOB_TIMEOUT_DEFAULT = 1800          # 30 分钟

_JOBS = queue.Queue()
_RESULTS = queue.Queue()
_LOCK = threading.Lock()
_IO_LOCK = threading.Lock()          # 落盘摘要专用：主线程和 worker 线程都会写它
_THREAD = None
_CURRENT = None                      # {"chat","label","started"}
_WAITING = []                        # [{"chat","label","started"}] 排队中（落盘用）
# 「刚读完」的那份，多久之内算同一个（模型重复提交）(chat,label) → (时间, 是不是失败)
_DONE = {}
_DEDUPE_DONE_SEC = 300.0             # 5 分钟；0 = 关掉这条


def configured(cfg=None):
    """(排队上限, 单任务超时秒数) —— 都在 `read:` 段里，坏值一律退回默认。"""
    r = ((cfg or {}).get("read") or {})
    try:
        qmax = int(r.get("queue_max") or _QUEUE_MAX_DEFAULT)
    except (TypeError, ValueError):
        qmax = _QUEUE_MAX_DEFAULT
    try:
        timeout = int(r.get("job_timeout") or _JOB_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        timeout = _JOB_TIMEOUT_DEFAULT
    return max(1, qmax), max(1, timeout)


def _save_pending():
    """把「还没读完的活儿」落盘（best-effort：写不动只告警，绝不把读取搞挂）。

    **只有这个用途**：重启后能说一句「上次那份没读完」。不存内容、不存路径。

    ⚠️ 两个调用点（主线程 `submit` / worker 线程收工）会**同时**写它 —— 所以
    ① 用 `_IO_LOCK` 串行化；② 临时文件名带线程号。少了任何一条，Windows 上
    `os.replace` 会偶发 `WinError 32（另一个程序正在使用此文件）`（自测真抓到过）。
    """
    try:
        with _LOCK:
            jobs = ([dict(_CURRENT)] if _CURRENT else []) + [dict(w) for w in _WAITING]
        with _IO_LOCK:
            os.makedirs(os.path.dirname(JOBS_PATH), exist_ok=True)
            tmp = f"{JOBS_PATH}.tmp{os.getpid()}-{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"jobs": jobs, "updated": time.time()}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, JOBS_PATH)
    except Exception as e:
        print(f"⚠️ read_worker: 落盘读任务摘要失败（不影响读取）：{e}", flush=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except (OSError, UnboundLocalError):
            pass


def _clear_pending_file():
    try:
        if os.path.exists(JOBS_PATH):
            os.remove(JOBS_PATH)
    except OSError as e:
        print(f"⚠️ read_worker: 清读任务摘要失败：{e}", flush=True)


def startup_note():
    """助手启动时：上一轮有没读完的活儿就**如实说一句**，然后把摘要清掉。

    返回一句话（没有就返回空串）。这是「不假装读过」这条规矩在重启场景下的落点。
    """
    try:
        with open(JOBS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return ""
    except Exception as e:
        print(f"⚠️ read_worker: 读任务摘要读不出来（当空处理）：{e}", flush=True)
        _clear_pending_file()
        return ""
    _clear_pending_file()
    jobs = data.get("jobs") or []
    if not jobs:
        return ""
    labels = "、".join(str(j.get("label") or "（没记名字）") for j in jobs[:5])
    more = f" 等 {len(jobs)} 份" if len(jobs) > 5 else ""
    return (f"⚠️ 上次有读取没读完（助手重启过）：{labels}{more}。"
            f"要再读一次就把文件名再发我一次。")


def _worker_loop():
    """唯一的 worker 线程：串行取活儿、跑、把结果丢进结果队列。"""
    global _CURRENT
    while True:
        job = _JOBS.get()
        with _LOCK:
            for i, w in enumerate(_WAITING):
                if w["label"] == job["label"] and w["chat"] == job["chat"]:
                    _WAITING.pop(i)
                    break
            _CURRENT = {"chat": job["chat"], "label": job["label"],
                        "started": time.time()}
        _save_pending()
        t0 = time.time()
        try:
            text, err = job["fn"]()
        except Exception as e:
            text, err = None, f"{type(e).__name__}: {str(e)[:300]}"
            traceback.print_exc()
        seconds = time.time() - t0
        _RESULTS_PUT(job, text, err, seconds)
        with _LOCK:
            _CURRENT = None
            # 记下「这份刚读完」（成功/失败都记，判据在 submit 里：**失败的不拦**）
            _DONE[(job["chat"], job["label"])] = (time.time(), bool(err))
            if _DEDUPE_DONE_SEC > 0:
                now = time.time()
                for k, (ts, _e) in list(_DONE.items()):
                    if now - ts > _DEDUPE_DONE_SEC * 4:
                        _DONE.pop(k, None)
            else:
                _DONE.clear()
        _save_pending()


def _RESULTS_PUT(job, text, err, seconds):
    _RESULTS.put({"chat": job["chat"], "label": job["label"], "text": text,
                  "err": err, "seconds": seconds,
                  "slow": seconds > job.get("timeout", _JOB_TIMEOUT_DEFAULT),
                  "waited": max(0.0, time.time() - job["submitted"] - seconds)})


def _ensure_thread():
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=_worker_loop, name="read-worker", daemon=True)
    _THREAD.start()


def submit(chat, label, fn, cfg=None):
    """登记一份要读的活儿。返回 `(是否收下, 给用户看的话)`。

    `fn` **必须是纯磁盘 + 模型 HTTP**（拿不到 client/hook 是故意的：结构上就碰不到）。
    它返回 `(文本, 错误)`，和 `file_read.extract_page` 同一形状。
    """
    qmax, timeout = configured(cfg)
    with _LOCK:
        # ⚠️ **同一个文件不重复排队**：模型很容易连着提交好几次 —— 真机踩过
        # （2026-10-03）：一份 A.zip 被提交了 **4 次**，每次读完都把两万七千字
        # 原文倒进聊天，用户看到的就是「我让它找文件，它刷了我好几屏」。
        # 已经在读/在排队的直接告诉它「在读了」，**别再占队列、别再发一遍**。
        for w in _WAITING:
            if w.get("chat") == chat and w.get("label") == label:
                return True, (f"这份「{label}」**已经在排队等读了**，不用再提交一次 —— "
                              f"读完我会主动把内容发出来。**你现在手里还没有内容，别编。**")
        if _CURRENT and _CURRENT.get("chat") == chat and _CURRENT.get("label") == label:
            return True, (f"这份「{label}」**正在读**，不用再提交一次 —— "
                          f"读完我会主动把内容发出来。**你现在手里还没有内容，别编。**")
        # ⚠️ **刚读完的也别再提交一次**（2026-10-06 真机：同一份 01.mp3 的转写被整段发了**两遍**）。
        # 上面那两条只拦得住"还在排队/正在读"的重复提交，而模型很容易**等这份读完再提交一次**
        # （异步路径：它手里一直没有内容，于是又调了一次 read_file）——那会儿 `_WAITING`/`_CURRENT`
        # 都空了，于是又读一遍、又把全文倒进聊天。所以把窗口延长到"刚读完"。
        # ⚠️ **失败的绝不拦**：读完报错时，"再试一次"正是我们让用户走的那条路
        # （`bot` 的失败文案就是「要在本机再试一次就说『重新读一下 X』」）。
        if _DEDUPE_DONE_SEC > 0:
            prev = _DONE.get((chat, label))
            if prev and not prev[1] and (time.time() - prev[0]) <= _DEDUPE_DONE_SEC:
                return True, (f"这份「{label}」**刚刚已经读完、内容也发出去了**，"
                              f"不重复读一遍（避免把同一份原文刷两遍）。"
                              f"要看就往上看我发的那条；**别自己复述内容**。")
        pending = len(_WAITING) + (1 if _CURRENT else 0)
        if pending >= qmax:
            return False, (f"前面已经排了 {pending} 份在读了（上限 read.queue_max={qmax}）。"
                           f"等一份读完再来，或者先把 file.inline_bytes 调大让它同步读。")
        _WAITING.append({"chat": chat, "label": label, "started": time.time()})
    _JOBS.put({"chat": chat, "label": label, "fn": fn, "submitted": time.time(),
               "timeout": timeout})
    _ensure_thread()
    _save_pending()
    if pending:
        return True, f"已提交后台读取（前面还有 {pending} 份在排队）"
    return True, "已提交后台读取"


def drain(limit=5):
    """主线程取结果（每条含 chat/label/text/err/seconds/slow）。取不到就返回 []。"""
    out = []
    while len(out) < max(1, int(limit)):
        try:
            out.append(_RESULTS.get_nowait())
        except queue.Empty:
            break
    return out


def status(cfg=None):
    """给 `/status` 用的一行摘要（纯内存，不查库）。"""
    _qmax, timeout = configured(cfg)
    with _LOCK:
        cur = dict(_CURRENT) if _CURRENT else None
        waiting = len(_WAITING)
    if not cur and not waiting:
        return ""
    parts = []
    if cur:
        parts.append(f"正在读「{cur['label']}」（{time.time() - cur['started']:.0f} 秒）")
    if waiting:
        parts.append(f"排队 {waiting} 份")
    if cur and (time.time() - cur["started"]) > timeout:
        parts.append(f"⚠️ 已超过 read.job_timeout={timeout}s")
    return "；".join(parts)


def reset_for_test():
    """自测用：清掉队列和当前任务。

    ⚠️ **故意不重置 `_THREAD`**：worker 线程是**一条**、贯穿进程生命周期的。
    以前这里把 `_THREAD = None`，于是下一个用例会再起一条线程，而老线程还活着
    —— 两个 worker 抢同一个队列，队满判断和串行顺序全乱（自测真抓到过）。
    要"停线程"就得有哨兵消息，代价比收益大；生产里本来就只有一条。
    """
    global _CURRENT
    with _LOCK:
        _CURRENT = None
        _WAITING.clear()
        _DONE.clear()
    while not _JOBS.empty():
        try:
            _JOBS.get_nowait()
        except queue.Empty:
            break
    while not _RESULTS.empty():
        try:
            _RESULTS.get_nowait()
        except queue.Empty:
            break
    _clear_pending_file()
