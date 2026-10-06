"""只读的本地状态页：把 `Health.snapshot()` 的 dict 渲染成 HTML / JSON。

规矩（和 hook 铁律一致）：
  - **只渲染喂进来的 dict**：绝不查微信库、绝不做任何写操作、绝不开线程去碰 hook。
  - **只允许绑回环地址**：传入的 host 不是 127.0.0.1 / ::1 / localhost 就拒绝启动，
    绝不把状态暴露到局域网。
  - 端口被占用只打印告警并返回 None，**绝不让 bot 起不来**。
  - 渲染前一律 html.escape，快照里的错误文本/群名不会变成注入。

用法（由 bot.py 接线，本模块不自己起线程去查询）：

    srv = status_page.start(snapshot_fn=health_obj.snapshot, log=print)
    ...
    status_page.stop(srv)
"""
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 回环地址白名单。**只允许这三个**，别加 0.0.0.0 / 局域网 IP。
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "127.0.0.2")


def _is_loopback(host):
    h = str(host or "").strip().lower()
    return h in LOOPBACK_HOSTS


class _Handler(BaseHTTPRequestHandler):
    """只读的 GET 处理器。写请求一律 405。"""

    server_version = "WeChatAIStatus/1.0"
    # 访问日志默认走 print；start() 会用调用方给的 log 换掉它
    _log = print

    def log_message(self, fmt, *args):
        try:
            self._log(f"[status] {self.address_string()} {fmt % args}")
        except Exception:
            pass

    def _send(self, code, body, ctype):
        try:
            data = body.encode("utf-8")
        except Exception as e:
            self._send(500, f"渲染失败：{e}", "text/plain; charset=utf-8")
            return
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            # 客户端提前断开之类：如实报错，但别把服务带崩
            self._log(f"[status] ⚠️ 写响应失败：{e}")

    def _snapshot(self):
        # snapshot_fn 挂在 server 实例上（不是类属性），多实例/多测试之间不会串
        fn = getattr(self.server, "snapshot_fn", None)
        if not callable(fn):
            return {"error": "没有传入 snapshot_fn —— 无法渲染状态"}
        try:
            data = fn()
        except Exception as e:
            # 快照函数炸了要如实显示，绝不静默返回空页面
            return {"error": f"snapshot_fn() 抛异常：{type(e).__name__}: {e}"}
        if not isinstance(data, dict):
            return {"error": f"snapshot_fn() 返回的不是 dict，而是 {type(data).__name__}"}
        return data

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
        elif path == "/status.json":
            data = self._snapshot()
            try:
                body = json.dumps(data, ensure_ascii=False, indent=2, default=str)
            except Exception as e:
                self._send(500, f"JSON 序列化失败：{e}", "text/plain; charset=utf-8")
                return
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/":
            self._send(200, render_html(self._snapshot()), "text/html; charset=utf-8")
        else:
            self._send(404, "404 —— 只有 / 、/status.json 、/healthz", "text/plain; charset=utf-8")

    def do_HEAD(self):
        # 只回响应头，方便本地探活（不写 body）
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/healthz", "/status.json"):
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _reject_write(self):
        self._send(405, "405 —— 这是只读状态页", "text/plain; charset=utf-8")

    do_POST = _reject_write
    do_PUT = _reject_write
    do_DELETE = _reject_write
    do_PATCH = _reject_write


def start(host="127.0.0.1", port=39002, snapshot_fn=None, log=print, base_path=None):
    """起一个只读的本地 HTTP 状态页。

    返回 server 对象（传给 stop() 关掉）；绑定失败/非回环地址返回 None。
    base_path 目前不参与路由，保留给调用方标注（例如日志里显示来源），
    这样签名就固定下来，接线方不用猜。
    """
    if not _is_loopback(host):
        # 这是安全边界，必须拒绝，而且要说清楚为什么
        msg = (f"[status] ⚠️ 拒绝启动：host={host!r} 不是回环地址。"
               f"状态页只允许绑 127.0.0.1/::1/localhost（绝不暴露到局域网）。")
        try:
            log(msg)
        except Exception:
            print(msg)
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        msg = f"[status] ⚠️ 拒绝启动：port={port!r} 不是整数"
        try:
            log(msg)
        except Exception:
            print(msg)
        return None

    if callable(log):
        _Handler._log = log

    try:
        # ThreadingHTTPServer + daemon_threads：HTTP 线程是**纯渲染**线程，
        # 绝不碰 hook；关了 bot 也不会因为 HTTP 线程卡住而退不掉。
        # allow_reuse_address 必须**显式关掉**：socketserver 默认是 True，
        # 而 Windows 上它等于 SO_REUSEADDR——那会让第二个进程也能绑上同一个端口
        #（「端口被占用」就永远发现不了）。关掉之后重复绑定会抛 WSAEADDRINUSE，
        # 正好被下面的 OSError 分支接住，如实告警并让 bot 照常运行。
        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        srv = _Server((host, port), _Handler)
        # 快照函数挂在实例上：处理器只读它，绝不自己去查库
        srv.snapshot_fn = snapshot_fn
    except OSError as e:
        # 端口被占用是最常见的情况：绝不能因此让 bot 起不来
        msg = f"[status] ⚠️ 状态页启动失败（{host}:{port}）：{e} —— bot 照常运行，只是没有状态页"
        try:
            log(msg)
        except Exception:
            print(msg)
        return None
    except Exception as e:
        msg = f"[status] ⚠️ 状态页启动异常（{host}:{port}）：{type(e).__name__}: {e}"
        try:
            log(msg)
        except Exception:
            print(msg)
        return None

    try:
        t = threading.Thread(target=srv.serve_forever, name="status-page", daemon=True)
        t.start()
        srv._status_thread = t
        url = f"http://{host}:{srv.server_address[1]}/"
        banner = f"[status] 状态页已启动（只读）：{url}"
        if base_path:
            banner += f"  来源：{base_path}"
        try:
            log(banner)
        except Exception:
            print(banner)
        return srv
    except Exception as e:
        msg = f"[status] ⚠️ 状态页线程启动失败：{type(e).__name__}: {e}"
        try:
            log(msg)
        except Exception:
            print(msg)
        try:
            srv.server_close()
        except Exception:
            pass
        return None


def stop(server):
    """关掉 start() 返回的 server。吞掉所有异常（bot 退出路径上不能抛）。"""
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    try:
        t = getattr(server, "_status_thread", None)
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
    except Exception:
        pass


def render_html(snap):
    """把快照 dict 渲染成一整页 HTML（深色、中文、简洁）。

    渲染时会把**看起来像密钥的键**（api_key / token / secret / password …）过滤掉：
    状态页是本地页面，但快照是上层随便塞的，不该因为「塞的人忘了」就把 key 印在页面上。
    被过滤的键会如实显示一行说明——不静默。
    """
    body = _render_value(snap, 0)
    title = "微信 AI 助手 · 状态"
    skipped = _sensitive_keys(snap)
    warn = ""
    if skipped:
        warn = ('<div class="hint" style="color:#ff8a80">'
                f'⚠️ 已隐藏疑似敏感字段：{html.escape("、".join(skipped))}'
                "（/status.json 不做过滤，是同一份快照的原文）</div>")
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>"
        "body{background:#14161a;color:#e6e6e6;font-family:Consolas,'Microsoft YaHei',monospace;"
        "margin:0;padding:24px;line-height:1.6}"
        "h1{font-size:20px;margin:0 0 4px 0;color:#7ee0a0}"
        ".hint{color:#8a93a0;font-size:12px;margin-bottom:18px}"
        "table{border-collapse:collapse;margin:4px 0 14px 0;min-width:320px;max-width:100%}"
        "td,th{border:1px solid #2b3038;padding:4px 10px;vertical-align:top;font-size:13px;"
        "word-break:break-all}"
        "td.k{color:#8ab4f8;white-space:nowrap;background:#191d23}"
        "ul{margin:4px 0 4px 22px;padding:0}"
        "li{font-size:13px}"
        ".b{font-weight:bold}"
        ".ok{color:#7ee0a0}.bad{color:#ff8a80}.unk{color:#c8b273}"
        "</style></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        '<div class="hint">只读视图 · 只渲染 bot 喂进来的事实 · '
        '<a style="color:#8ab4f8" href="/status.json">/status.json</a> · '
        '<a style="color:#8ab4f8" href="/healthz">/healthz</a></div>'
        f"{warn}{body}</body></html>"
    )


# 明显是密钥的键名片段（小写包含匹配）。命中就**不渲染它的值**。
SENSITIVE_KEY_PARTS = ("api_key", "apikey", "api-key", "token", "secret",
                       "password", "passwd", "authorization", "credential",
                       "access_key", "private_key")


def _sensitive_keys(obj):
    """递归找出疑似密钥的键名（只看 dict 的键，不看值）。"""
    found = []

    def walk(v):
        if isinstance(v, dict):
            for k, sub in v.items():
                if any(p in str(k).lower() for p in SENSITIVE_KEY_PARTS):
                    found.append(str(k))
                else:
                    walk(sub)
        elif isinstance(v, (list, tuple)):
            for it in v:
                walk(it)

    try:
        walk(obj)
    except Exception:
        pass
    return found


def _render_value(v, depth):
    """递归渲染：dict -> 表格，list/tuple -> 列表，标量 -> 转义后的文本。"""
    if isinstance(v, dict):
        return _render_dict(v, depth)
    if isinstance(v, (list, tuple)):
        return _render_list(v, depth)
    return _render_scalar(v)


def _render_dict(d, depth):
    if not d:
        return '<span class="unk">（空）</span>'
    rows = []
    for k, v in d.items():
        key = html.escape(str(k))
        if _sensitive_keys({k: None}):
            # 不渲染密钥的值，但键名和「已隐藏」这件事要如实写出来
            rows.append(f'<tr><td class="k">{key}</td>'
                        '<td><span class="bad">🔒 已隐藏（疑似敏感字段）</span></td></tr>')
            continue
        rows.append(f'<tr><td class="k">{key}</td><td>{_render_value(v, depth + 1)}</td></tr>')
    return "<table>" + "".join(rows) + "</table>"


def _render_list(items, depth):
    if not items:
        return '<span class="unk">（空）</span>'
    out = ["<ul>"]
    for it in items:
        out.append(f"<li>{_render_value(it, depth + 1)}</li>")
    out.append("</ul>")
    return "".join(out)


def _render_scalar(v):
    if v is None:
        return '<span class="unk">—</span>'
    if isinstance(v, bool):
        return f'<span class="b {"ok" if v else "bad"}">{"是" if v else "否"}</span>'
    return html.escape(str(v))
