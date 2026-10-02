"""health.py / status_page.py 的自测：不联网、不碰 30001、不需要微信。

覆盖：
  1. rotate_log：轮转 / keep 上限 / 文件不存在 / 失败路径不抛异常
  2. Health：掉登录告警 + 冷却 + 恢复告警、日志里有明确标记
  3. snapshot()：一次 note_* 都没调用也能用
  4. note_poll 的形状与 live_history.poll_errors() 一致
  5. status_page：/ 渲染 200、/status.json 合法且等于快照、非回环 host 被拒、HTML 转义生效

**测试期间绝不真发系统通知**：Health 的 notify_fn 一律换成记录用的假函数。
所有临时文件都写 tempfile 目录，不碰仓库的 data/。

用法：python selftest_health.py
"""
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout

try:
    import health
    import status_page
    import live_history
except ModuleNotFoundError as e:
    print(f"❌ 缺模块：{e}（请在项目根目录跑，且 health.py / status_page.py 已就位）")
    sys.exit(1)


def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{('  ' + str(extra)) if extra and not cond else ''}")
    return bool(cond)


def _tmpdir():
    return tempfile.mkdtemp(prefix="health_selftest_")


class FakeNotifier:
    """记录调用的假通知函数——测试里绝不允许真弹窗。"""

    def __init__(self):
        self.calls = []

    def __call__(self, title, text):
        self.calls.append((title, text))
        return True


def _capture(fn):
    """跑 fn，返回 (stderr 文本, stdout 文本)。"""
    err, out = io.StringIO(), io.StringIO()
    with redirect_stderr(err), redirect_stdout(out):
        fn()
    return err.getvalue(), out.getvalue()


def _http_get(url, timeout=5):
    """返回 (状态码, 文本)。"""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- rotate_log


def test_rotate_log():
    print("\n── rotate_log：轮转 / keep / 不存在的文件 / 失败不抛异常 ──")
    ok = True
    d = _tmpdir()
    p = os.path.join(d, "bot.log")

    # 不存在的文件：不报错、返回 False
    try:
        rc = health.rotate_log(p, max_bytes=10, keep=3)
        ok &= check("文件不存在时返回 False 且不抛异常", rc is False, rc)
    except Exception as e:
        ok &= check("文件不存在时返回 False 且不抛异常", False, e)

    # 没超阈值：不动
    with open(p, "w", encoding="utf-8") as f:
        f.write("small")
    rc = health.rotate_log(p, max_bytes=1024, keep=3)
    ok &= check("未超阈值不轮转", rc is False and not os.path.exists(p + ".1"), rc)

    # 超阈值：bot.log -> bot.log.1，新文件重新开始
    for i in range(20):
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"line {i}\n")
    size = os.path.getsize(p)
    rc = health.rotate_log(p, max_bytes=64, keep=3)
    ok &= check("超阈值时轮转出 .1", rc is True and os.path.exists(p + ".1"), rc)
    ok &= check(".1 的内容就是原来的日志",
                os.path.getsize(p + ".1") == size, os.path.getsize(p + ".1"))
    # 轮转后原文件不在了是**正常**的：bot.setup_logging() 紧接着 open(path,"a") 会重建它。
    # 这里必须确认「不在了」，否则说明轮转根本没搬走。
    ok &= check("原日志已被搬走（bot 随后会重新 open 建空文件）",
                not os.path.exists(p) or os.path.getsize(p) == 0,
                os.path.exists(p) and os.path.getsize(p))

    # keep 生效：多轮之后最多只有 .1/.2/.3，没有 .4
    for round_no in range(5):
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"round {round_no} " + "x" * 100)
        health.rotate_log(p, max_bytes=64, keep=3)
    have = [i for i in range(1, 6) if os.path.exists(f"{p}.{i}")]
    ok &= check("keep=3 只留 3 份", have == [1, 2, 3], have)
    ok &= check("最老的 .4 不存在", not os.path.exists(p + ".4"))

    # .2/.3 的内容确实是历史（顺序没串）：.1 是最新的那一轮
    with open(p + ".1", encoding="utf-8") as f:
        c1 = f.read()
    ok &= check(".1 是最新一轮的内容", "round 4" in c1, c1[:40])

    # 失败路径一：路径是个目录 -> 必须返回 False，不许抛，且**要告警**（不静默）
    err_buf = io.StringIO()
    try:
        with redirect_stderr(err_buf):
            rc = health.rotate_log(d, max_bytes=1, keep=3)
        ok &= check("路径是目录时返回 False 不抛异常", rc is False, rc)
        ok &= check("拒绝轮转时有告警（不静默）",
                    "[health] ⚠️" in err_buf.getvalue() and "不是普通文件" in err_buf.getvalue(),
                    repr(err_buf.getvalue()))
    except Exception as e:
        ok &= check("路径是目录时返回 False 不抛异常", False, e)
    # 失败路径二：日志被**独占占用**（最真实的失败场景：bot 已经开着这个文件）
    # 搬运必须失败，而且只许告警，绝不许抛——**绝不因为轮转失败让 bot 起不来**
    locked = os.path.join(d, "locked.log")
    with open(locked, "a", encoding="utf-8") as lf:
        lf.write("y" * 200)
        lf.flush()
        os.fsync(lf.fileno())          # 先冲盘，再让 Python 独占打开它
        with open(locked, "r+", encoding="utf-8"):
            err_buf2 = io.StringIO()
            try:
                with redirect_stderr(err_buf2), redirect_stdout(io.StringIO()):
                    rc = health.rotate_log(locked, max_bytes=16, keep=3)
                ok &= check("日志被占用时返回 False 不抛异常", rc is False, rc)
                ok &= check("被占用时有告警（不静默、含路径）",
                            "[health] ⚠️" in err_buf2.getvalue() and "locked.log" in err_buf2.getvalue(),
                            repr(err_buf2.getvalue()))
                ok &= check("被占用时原日志内容完好无损",
                            os.path.getsize(locked) == 200, os.path.getsize(locked))
            except Exception as e:
                ok &= check("日志被占用时不抛异常", False, e)
    ok &= check("目录没被搬走（拒绝轮转是安全的）", os.path.isdir(d), d)
    # 失败路径：max_bytes 是垃圾值 -> 也不许抛
    try:
        rc = health.rotate_log(p, max_bytes="abc", keep="xyz")
        ok &= check("参数是垃圾值时返回 False 不抛异常", rc is False, rc)
    except Exception as e:
        ok &= check("参数是垃圾值时返回 False 不抛异常", False, e)
    return ok


# ---------------------------------------------------------------- Health


def test_health_login_alerts():
    print("\n── Health：掉登录告警 / 冷却 / 恢复告警（且日志里有明确标记）──")
    ok = True
    d = _tmpdir()
    fake = FakeNotifier()
    h = health.Health(
        {"health": {"login_check_interval": 300, "alert_cooldown": 3600,
                    "status_file": os.path.join(d, "status.json")}},
        notify_fn=fake,
    )

    # 掉登录：一次告警 + stderr 里有 [health] ⚠️ 标记
    err, _ = _capture(lambda: h.note_login(False, "IsLogin=0"))
    ok &= check("掉登录触发一次本地通知", len(fake.calls) == 1, fake.calls)
    ok &= check("告警日志带 [health] ⚠️ 前缀", "[health] ⚠️" in err, err.strip())
    ok &= check("告警文案含「掉登录」", "掉登录" in err, err.strip())
    ok &= check("告警计数 +1", h.snapshot()["alert_count"] == 1, h.snapshot()["alert_count"])

    # 连续多次：冷却生效，不再重复通知
    fake.calls.clear()
    for _ in range(5):
        err2, _ = _capture(lambda: h.note_login(False, "IsLogin=0"))
    ok &= check("冷却期内不重复通知（0 次）", len(fake.calls) == 0, fake.calls)
    ok &= check("冷却期内日志仍留痕（不静默吞）", "冷却中" in err2, err2.strip())

    # 冷却过期后可以再发（把冷却调成 0 模拟）
    h.alert_cooldown = 0.0
    fake.calls.clear()
    h.note_login(False, "still down")
    ok &= check("冷却过后同类告警可再发", len(fake.calls) == 1, fake.calls)

    # 恢复：再告警一次「已恢复」
    fake.calls.clear()
    err3, _ = _capture(lambda: h.note_login(True, "昵称=我自己"))
    ok &= check("恢复时再告警一次", len(fake.calls) == 1, fake.calls)
    ok &= check("恢复告警文案含「恢复」", "恢复" in err3 + fake.calls[0][1],
                fake.calls)
    snap = h.snapshot()
    ok &= check("登录态记为 True", snap["login_ok"] is True, snap["login_ok"])
    ok &= check("掉登录/恢复计数各对", snap["login_lost_count"] == 7 and
                snap["login_restored_count"] == 1,
                (snap["login_lost_count"], snap["login_restored_count"]))

    # 启动后第一次就探到登录正常：不该报「已恢复」（没有掉线哪来恢复）
    fake2 = FakeNotifier()
    h2 = health.Health({"health": {"status_file": os.path.join(d, "s2.json")}},
                       notify_fn=fake2)
    h2.note_login(True, "正常")
    ok &= check("从未掉线时不报「已恢复」", len(fake2.calls) == 0, fake2.calls)

    # due_login_check：刚探过 -> 不该到期
    ok &= check("刚探过登录时 due_login_check 为 False", h2.due_login_check() is False)
    h2.last_login_check_at = time.time() - 9999
    ok &= check("超过间隔后 due_login_check 为 True", h2.due_login_check() is True)
    return ok


def test_snapshot_minimal():
    print("\n── snapshot()：一次 note_* 都没调用也能用 ──")
    ok = True
    fake = FakeNotifier()
    h = health.Health({"health": {"status_file": os.path.join(_tmpdir(), "status.json")}},
                      notify_fn=fake)
    try:
        snap = h.snapshot()
    except Exception as e:
        return check("全新 Health.snapshot() 不抛异常", False, e)

    ok &= check("返回 dict", isinstance(snap, dict), type(snap).__name__)
    ok &= check("last_poll_at 为 None", snap["last_poll_at"] is None, snap["last_poll_at"])
    ok &= check("poll_errors 为空 dict", snap["poll_errors"] == {}, snap["poll_errors"])
    ok &= check("login_ok 为 None（还没探过）", snap["login_ok"] is None, snap["login_ok"])
    ok &= check("alert_count 为 0", snap["alert_count"] == 0, snap["alert_count"])
    ok &= check("uptime_seconds 是数字", isinstance(snap["uptime_seconds"], (int, float)),
                snap["uptime_seconds"])
    ok &= check("alert_count 不影响快照（没发过通知）", len(fake.calls) == 0)
    # 快照必须可 JSON 序列化（状态页要直接 dumps）
    try:
        json.dumps(snap, ensure_ascii=False)
        ok &= check("snapshot() 可 JSON 序列化", True)
    except Exception as e:
        ok &= check("snapshot() 可 JSON 序列化", False, e)
    return ok


def test_note_poll_shape():
    print("\n── note_poll：形状与 live_history.poll_errors() 一致 ──")
    ok = True
    # 真实来源的形状（live_history 里就是 tab -> (str(e), 连续次数)）
    real = live_history.poll_errors()
    ok &= check("live_history.poll_errors() 返回 dict", isinstance(real, dict),
                type(real).__name__)

    h = health.Health({"health": {"status_file": os.path.join(_tmpdir(), "status.json")}},
                      notify_fn=FakeNotifier())
    fed = {"fts_0": ("HTTP 500", 3), "fts_1": ("timeout", 1)}
    t0 = time.time()
    h.note_poll(cursor={"fts_0": 120, "__time__": 1_700_000_000}, errors=fed)
    cost = time.time() - t0
    snap = h.snapshot()
    ok &= check("没调用过也能用（此时 last_poll_at 不为 None）", snap["last_poll_at"] is not None)
    ok &= check("轮询距今 0 秒左右", snap["last_poll_age_seconds"] is not None
                and snap["last_poll_age_seconds"] < 5, snap["last_poll_age_seconds"])
    ok &= check("游标原样保留", snap["last_cursor"] == {"fts_0": 120, "__time__": 1_700_000_000},
                snap["last_cursor"])
    ok &= check("错误项数与喂进去的一致", len(snap["poll_errors"]) == 2, snap["poll_errors"])
    # 每个值都是 [错误文本, 次数]（元组经 JSON 形状归一成两元列表）
    good_shape = all(
        isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], str)
        and isinstance(v[1], int)
        for v in snap["poll_errors"].values()
    )
    ok &= check("每个值都是 (错误文本, 次数) 两元组", good_shape, snap["poll_errors"])
    ok &= check("错误文本没丢", snap["poll_errors"]["fts_0"][0] == "HTTP 500",
                snap["poll_errors"])
    ok &= check("连续次数没丢", snap["poll_errors"]["fts_1"][1] == 1, snap["poll_errors"])
    ok &= check("note_poll 足够轻量（<50ms）", cost < 0.05, f"{cost:.4f}s")

    # 下一轮没错误 -> 必须清空（不能留着旧错误假装还有问题）
    h.note_poll(cursor={"fts_0": 130}, errors=None)
    ok &= check("新一轮无错误时清空上次错误", h.snapshot()["poll_errors"] == {},
                h.snapshot()["poll_errors"])
    ok &= check("轮询计数累计", h.snapshot()["poll_count"] == 2, h.snapshot()["poll_count"])

    # 形状喂错也要如实记账，不许静默丢
    h.note_poll(errors="不是 dict")
    ok &= check("形状不对时如实记一笔（不静默丢）",
                bool(h.snapshot()["poll_errors"]), h.snapshot()["poll_errors"])
    return ok


def test_notes_and_status_file():
    print("\n── note_sent / note_hook_error / write_status ──")
    ok = True
    d = _tmpdir()
    sf = os.path.join(d, "sub", "status.json")   # 顺带验证会自己建目录
    h = health.Health({"health": {"status_file": sf}}, notify_fn=FakeNotifier())

    h.note_sent(ok=True, detail="发给张三")
    ok &= check("note_sent 成功计数", h.snapshot()["send_ok_count"] == 1)
    h.note_send_failure(RuntimeError("发送超时"))
    snap = h.snapshot()
    ok &= check("note_send_failure 记失败", snap["send_fail_count"] == 1 and
                snap["last_send_ok"] is False, (snap["send_fail_count"], snap["last_send_ok"]))
    ok &= check("失败详情带异常类型与文本",
                "RuntimeError" in snap["last_send_detail"] and "发送超时" in snap["last_send_detail"],
                snap["last_send_detail"])
    h.note_hook_error("连不上 30001")
    snap = h.snapshot()
    ok &= check("note_hook_error 计数", snap["hook_errors"] == 1 and
                "30001" in snap["last_hook_error"], snap["hook_errors"])

    rc = h.write_status()
    ok &= check("write_status 返回 True", rc is True, rc)
    ok &= check("status 文件真的写出来了", os.path.exists(sf))
    with open(sf, encoding="utf-8") as f:
        saved = json.load(f)
    now_snap = h.snapshot()
    # 不能直接 saved == h.snapshot()：uptime / *age* 字段随调用时刻变化，
    # 那是设计如此。要断言的是「同一份记账数据除了时间字段外完全一致」。
    volatile = {"uptime_seconds", "uptime_human", "now", "last_poll_age_seconds",
                "last_login_check_age_seconds"}
    diff_keys = [k for k in set(saved) | set(now_snap)
                 if k not in volatile and saved.get(k) != now_snap.get(k)]
    ok &= check("落盘的 JSON 与 snapshot() 一致（时间类字段除外）",
                saved.get("app") == "微信 AI 助手" and not diff_keys,
                diff_keys)
    ok &= check("落盘的 poll_errors / 计数都在",
                saved.get("send_ok_count") == 1 and saved.get("hook_errors") == 1,
                {k: saved.get(k) for k in ("send_ok_count", "hook_errors")})
    ok &= check("没有留下临时文件",
                [n for n in os.listdir(os.path.dirname(sf)) if n.startswith(".status-")] == [],
                os.listdir(os.path.dirname(sf)))

    # 失败路径：status_file 指向一个目录 -> 返回 False、不抛异常、有告警
    h2 = health.Health({"health": {"status_file": d}}, notify_fn=FakeNotifier())
    try:
        rc2 = None
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            rc2 = h2.write_status()
        ok &= check("写不进去时返回 False", rc2 is False, rc2)
        ok &= check("写不进去时有告警（不静默）", "[health] ⚠️" in err_buf.getvalue(),
                    err_buf.getvalue())
    except Exception as e:
        ok &= check("写不进去时不抛异常", False, e)

    # alert() 的通知函数自己炸了：只记日志，绝不抛
    def bad_notify(title, text):
        raise RuntimeError("通知组件炸了")

    h3 = health.Health({"health": {"status_file": os.path.join(d, "s3.json")}},
                       notify_fn=bad_notify)
    err_buf = io.StringIO()
    try:
        with redirect_stderr(err_buf):
            h3.alert("测试告警")
        ok &= check("通知函数抛异常时 alert 不抛", True)
        ok &= check("且日志里有痕迹", "[health] ⚠️" in err_buf.getvalue(), err_buf.getvalue())
    except Exception as e:
        ok &= check("通知函数抛异常时 alert 不抛", False, e)
    return ok


# ---------------------------------------------------------------- status_page


# status_page 测试用的假快照：**故意**塞了 XSS 载荷和一个敏感键。
# 放在模块级（不是函数里），因为下面的 snapshot_fn 闭包要读它。
SNAP = {
    "app": "微信 AI 助手",
    "healthy": True,
    "uptime_seconds": 12.5,
    "login_ok": False,
    "last_cursor": {"fts_0": 99},
    "poll_errors": {"fts_0": ["<script>alert('xss')</script>", 2]},
    "群名": "<img src=x onerror=alert(1)>",
    "api_key": "sk-should-never-be-here",
    "notes": ["a", "b"],
    "nothing": None,
}


def test_status_page():
    print("\n── status_page：只读渲染 / JSON / 非回环拒绝 / HTML 转义 ──")
    ok = True

    calls = {"n": 0}

    def snapshot_fn():
        calls["n"] += 1
        return SNAP

    logs = []
    srv = status_page.start(host="127.0.0.1", port=0, snapshot_fn=snapshot_fn,
                            log=logs.append)
    ok &= check("port=0 能起服务（返回 server 对象）", srv is not None)
    if srv is None:
        return False
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        code, body = _http_get(base + "/")
        ok &= check("GET / 返回 200", code == 200, code)
        ok &= check("/ 是 HTML", "<html" in body.lower() and "微信 AI 助手" in body)
        ok &= check("HTML 里出现快照的值", "12.5" in body and "fts_0" in body)
        ok &= check("HTML 转义生效：<script> 被转义",
                    "<script>alert" not in body and "&lt;script&gt;" in body,
                    body[:200])
        ok &= check("HTML 转义生效：<img onerror> 被转义",
                    "<img src=x" not in body and "&lt;img" in body)
        # 状态页是本地页面，但快照是上层随便塞的：密钥类字段的值不该被渲染出来
        ok &= check("疑似密钥的字段值不落页面", "sk-should-never-be-here" not in body)
        ok &= check("隐藏密钥这件事在页面上如实说明", "已隐藏" in body)

        code2, body2 = _http_get(base + "/status.json")
        ok &= check("GET /status.json 返回 200", code2 == 200, code2)
        try:
            parsed = json.loads(body2)
            ok &= check("/status.json 是合法 JSON", True)
        except Exception as e:
            parsed = None
            ok &= check("/status.json 是合法 JSON", False, e)
        ok &= check("/status.json 等于 snapshot_fn() 的结果", parsed == SNAP, parsed)
        ok &= check("snapshot_fn 被调用过", calls["n"] >= 2, calls["n"])

        code3, body3 = _http_get(base + "/healthz")
        ok &= check("/healthz 返回 ok", code3 == 200 and body3.strip() == "ok", body3)

        try:
            _http_get(base + "/nope")
            ok &= check("未知路径 404", False, "居然 200")
        except urllib.error.HTTPError as e:
            ok &= check("未知路径 404", e.code == 404, e.code)

        # 只读：POST 必须被拒绝
        try:
            req = urllib.request.Request(base + "/", data=b"x", method="POST")
            urllib.request.urlopen(req, timeout=5)
            ok &= check("POST 被拒绝（只读）", False, "居然成功")
        except urllib.error.HTTPError as e:
            ok &= check("POST 被拒绝（只读，405）", e.code == 405, e.code)
        except Exception as e:
            ok &= check("POST 被拒绝（只读）", True, e)
    finally:
        status_page.stop(srv)
    ok &= check("stop() 后端口已释放", True)

    # 非回环 host：必须拒绝启动
    for bad in ("0.0.0.0", "192.168.1.5", "::", "", " 8.8.8.8"):
        r = status_page.start(host=bad, port=0, snapshot_fn=snapshot_fn,
                              log=logs.append)
        ok &= check(f"host={bad!r} 被拒绝启动", r is None, r)
    ok &= check("拒绝时有清晰告警", any("拒绝启动" in str(x) for x in logs),
                logs[-3:])
    ok &= check("拒绝的是安全边界（文案提到回环）",
                any("回环" in str(x) for x in logs))

    # 端口被占用：返回 None + 告警，不许抛
    srv_a = status_page.start(host="127.0.0.1", port=0, snapshot_fn=snapshot_fn,
                              log=logs.append)
    if srv_a is not None:
        used = srv_a.server_address[1]
        logs.clear()
        srv_b = status_page.start(host="127.0.0.1", port=used, snapshot_fn=snapshot_fn,
                                  log=logs.append)
        ok &= check("端口被占用时返回 None", srv_b is None, srv_b)
        ok &= check("端口被占用时有清晰告警",
                    any("启动失败" in str(x) for x in logs), logs)
        status_page.stop(srv_a)

    # snapshot_fn 抛异常：页面要如实报错，不许白屏/静默
    def boom():
        raise RuntimeError("快照炸了")

    srv_c = status_page.start(host="127.0.0.1", port=0, snapshot_fn=boom,
                              log=logs.append)
    if srv_c is not None:
        try:
            code4, body4 = _http_get(f"http://127.0.0.1:{srv_c.server_address[1]}/status.json")
            ok &= check("snapshot_fn 抛异常时仍回 200 且如实报错",
                        code4 == 200 and "快照炸了" in body4, body4[:120])
        finally:
            status_page.stop(srv_c)

    # 没传 snapshot_fn 也要能起（页面说明原因）
    srv_d = status_page.start(host="127.0.0.1", port=0, log=logs.append)
    if srv_d is not None:
        try:
            code5, body5 = _http_get(f"http://127.0.0.1:{srv_d.server_address[1]}/status.json")
            ok &= check("没传 snapshot_fn 时如实报错",
                        code5 == 200 and "snapshot_fn" in body5, body5[:120])
        finally:
            status_page.stop(srv_d)

    # 用真 Health 的快照起一次页面：端到端串一下（顺带验证快照里没有敏感字段）
    h = health.Health({"health": {"status_file": os.path.join(_tmpdir(), "status.json")}},
                      notify_fn=FakeNotifier())
    h.note_poll(cursor={"__time__": 1}, errors=None)
    srv_e = status_page.start(host="127.0.0.1", port=0, snapshot_fn=h.snapshot,
                              log=logs.append)
    if srv_e is not None:
        try:
            _, b = _http_get(f"http://127.0.0.1:{srv_e.server_address[1]}/status.json")
            d = json.loads(b)
            ok &= check("端到端：Health.snapshot() 能直接喂给状态页",
                        d.get("app") == "微信 AI 助手" and d.get("poll_count") == 1, d.get("app"))
            ok &= check("快照里不含 api_key / token 之类的键",
                        not any("key" in k.lower() or "token" in k.lower() or
                                "secret" in k.lower() for k in d), list(d))
        finally:
            status_page.stop(srv_e)
    return ok


def test_cursor_stall():
    print("\n── 游标停滞：「静默失效」的确定性判据 ──")
    ok = True

    # cursor_key：只比「进度」，且与 dict 键顺序无关（顺序敏感会让判据时灵时不灵）
    ok &= check("None → None", health.cursor_key(None) is None)
    a = health.cursor_key({"fts_0": 1, "__time__": 100})
    b = health.cursor_key({"__time__": 100, "fts_0": 1})
    ok &= check("与 dict 键顺序无关", a == b, f"{a} vs {b}")
    ok &= check("分片 rowid 变了就不同", a != health.cursor_key({"fts_0": 2, "__time__": 100}))
    ok &= check("__time__ 前进也算变（它是消息时间水位线，只在真有新消息时前进）",
                a != health.cursor_key({"fts_0": 1, "__time__": 101}))
    n1 = health.cursor_key({"__nonttext__": {"a": 1, "b": 2}})
    n2 = health.cursor_key({"__nonttext__": {"b": 2, "a": 1}})
    n3 = health.cursor_key({"__nonttext__": {"a": 1, "b": 3}})
    ok &= check("嵌套水位线（__nonttext__）与键顺序无关", n1 == n2, f"{n1} vs {n2}")
    ok &= check("嵌套水位线内容变了就不同", n1 != n3)

    h = health.Health({"health": {"status_file": os.path.join(_tmpdir(), "stall1.json")}},
                      notify_fn=FakeNotifier())
    cur = {"fts_0": 10, "__time__": 1000}
    h.note_poll(cursor=cur)
    ok &= check("第一轮没有前值可比较 → 不算停滞",
                h.snapshot()["cursor_stalls"] == 0, h.snapshot()["cursor_stalls"])
    for _ in range(3):
        h.note_poll(cursor=dict(cur))
    ok &= check("游标不动 → 逐轮累计", h.snapshot()["cursor_stalls"] == 3,
                h.snapshot()["cursor_stalls"])
    ok &= check("最长停滞也记下来（诊断用）", h.snapshot()["max_cursor_stalls"] == 3,
                h.snapshot()["max_cursor_stalls"])
    ok &= check("快照里带了这两个字段", "cursor_stalls" in h.snapshot()
                and "max_cursor_stalls" in h.snapshot())

    h.stall_reported = True                  # 假装已经汇报过
    h.note_poll(cursor={"fts_0": 11, "__time__": 1000})
    ok &= check("游标一动 → 停滞清零", h.snapshot()["cursor_stalls"] == 0)
    ok &= check("汇报过之后又动了 → 给出一次性「已恢复」信号",
                h.recovered_from_stall is True)
    ok &= check("汇报标记同时清掉（下次停滞还能再报一次）", h.stall_reported is False)

    h2 = health.Health({"health": {"status_file": os.path.join(_tmpdir(), "stall2.json")}},
                       notify_fn=FakeNotifier())
    h2.note_poll(cursor={"fts_0": 1})
    h2.note_poll(cursor={"fts_0": 2})
    ok &= check("没汇报过就不该冒「已恢复」（否则用户莫名其妙）",
                h2.recovered_from_stall is False)

    h3 = health.Health({"health": {"status_file": os.path.join(_tmpdir(), "stall3.json")}},
                       notify_fn=FakeNotifier())
    for _ in range(5):
        h3.note_poll(cursor=None)
    ok &= check("游标还是 None（还没拿到游标）时不许乱判停滞",
                h3.snapshot()["cursor_stalls"] == 0, h3.snapshot()["cursor_stalls"])
    return ok


def main():
    ok = True
    print("=" * 50)
    print("health.py / status_page.py 自测（不联网、不碰 30001）")
    print("=" * 50)
    ok &= test_rotate_log()
    ok &= test_health_login_alerts()
    ok &= test_snapshot_minimal()
    ok &= test_note_poll_shape()
    ok &= test_cursor_stall()
    ok &= test_notes_and_status_file()
    ok &= test_status_page()
    print("\n" + "=" * 50)
    print("全部通过 ✅" if ok else "有失败项 ❌")
    print("=" * 50)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
