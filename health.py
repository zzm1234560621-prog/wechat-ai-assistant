"""健康状态模块：日志轮转 + 运行期健康快照 + 掉登录告警 + Windows 本地通知。

**这个模块自己绝不查微信库、绝不开线程去调 hook。**
它只处理「已经拿到的事实」——由 bot.py 在收消息那条线程上把事实喂进来
（`note_poll` / `note_sent` / `note_hook_error` / `note_login`），
因为 hook 不支持并发，任何在别的线程里发起的查询都可能把微信搞崩。

分工：
  - `rotate_log()`  给 `bot.setup_logging()` 在打开日志**之前**调，负责轮转 bot.log。
  - `Health`        纯内存记账 + 落一份 status.json（给状态页读）。
  - `notify()`      best-effort 的 Windows 本地通知，只用系统自带的 PowerShell。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

# 项目根目录：health.py 就放在根目录，所以取它自己所在目录即可。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 默认的 health 配置。cfg 里没有 health 段时全用这套。
DEFAULTS = {
    "login_check_interval": 300.0,   # 多久探一次登录态（秒）
    "alert_cooldown": 3600.0,        # 同一类告警的最小间隔（秒），防刷屏
    "status_file": os.path.join(PROJECT_ROOT, "data", "status.json"),
    # 「hook 可达、但库层死了」的触发阈值（2026-10-06 加；用户要求**偏保守**：
    # 宁可晚十几分钟，也不要发那种「你被叫去扫码、其实没事」的假警报）。
    "hook_stress_rounds": 10.0,      # 连续多少轮轮询不健康（慢 / 被总时限截断 / 分片错）
    "db_stale_sec": 600.0,           # 核心库多少秒没被写过
    "hook_slow_round_sec": 3.0,      # 一轮超过多少秒算「慢」（平时一轮不到 1 秒）
}

# 两次「权威探针」之间至少隔这么久：探针要碰句柄表，绝不能每轮都去问。
DB_PROBE_MIN_GAP = 300.0

# 通知子进程的超时（秒）。失败/超时就放弃，绝不阻塞 bot。
NOTIFY_TIMEOUT = 10


def _warn(msg):
    """告警统一走 stderr：bot 的 _Tee 会同时写控制台和 bot.log。

    这个模块所有异常处理都用它——**不许静默失败**，但也不许把 bot 带崩。
    """
    try:
        print(f"[health] ⚠️ {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


# 同一句告警的最小间隔（秒）。专门给「每轮都会重复失败」的地方用（见 Health._warn_throttled）：
# 2026-10-05 真机，状态盘写失败时这里**每 5 秒**刷一行，把 bot.log 灌满，
# 真正的崩溃线索（慢查询 → HTTP 500 → 10054）全被埋在里面。
WARN_THROTTLE_SEC = 60.0


def _num(value, default):
    """把配置里的数字读成 float；读不出来就用默认值（并说明一句）。"""
    if value is None or isinstance(value, bool):
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        _warn(f"配置值 {value!r} 不是数字，改用默认 {default}")
        return float(default)


def rotate_log(path, max_bytes=5 * 1024 * 1024, keep=3):
    """日志超过 max_bytes 就轮转：bot.log -> bot.log.1 -> bot.log.2 …，最多留 keep 份。

    **必须在 bot 打开日志之前调用**，所以这里绝不允许长期持有文件句柄：
    每个文件都是 with 打开、立刻关闭。

    任何失败（文件被占用、权限不足、路径不存在）都返回 False 并打印告警，
    **绝不许让 bot 起不来**。
    """
    try:
        max_bytes = int(max_bytes)
    except (TypeError, ValueError):
        _warn(f"rotate_log: max_bytes={max_bytes!r} 不是整数，按默认处理")
        max_bytes = 5 * 1024 * 1024
    try:
        keep = int(keep)
    except (TypeError, ValueError):
        _warn(f"rotate_log: keep={keep!r} 不是整数，按默认处理")
        keep = 3
    if max_bytes <= 0:
        # 0/负数 = 关闭轮转（调用方显式想要这个行为，不算出错）
        return False
    if keep < 0:
        keep = 0

    try:
        path = os.path.abspath(str(path))
    except Exception as e:
        _warn(f"rotate_log: 路径 {path!r} 不可用：{e}")
        return False

    try:
        if not os.path.exists(path):
            # 首次运行没有日志文件是正常情况，不是失败
            return False
        if not os.path.isfile(path):
            # 路径存在但不是普通文件（例如是个目录）：如实报错。
            # 不拦的话 Windows 的 os.replace 真能把目录整个搬走，
            # 那就成了「静默把目录当日志轮转」，比报错危险得多。
            _warn(f"rotate_log: 路径存在但不是普通文件，拒绝轮转：{path}")
            return False
        size = os.path.getsize(path)
        if size < max_bytes:
            return False

        if keep == 0:
            # 不留份数 = 直接清空（仍然不持有句柄）
            with open(path, "w", encoding="utf-8"):
                pass
            print(f"[health] 日志已清空（keep=0，原大小 {size} 字节）", flush=True)
            return True

        # 先找出已经存在的最大编号，避免覆盖历史（例如 bot.log.2 已存在时不能只挪 .1）
        highest = 0
        for i in range(1, keep + 2):
            if os.path.exists(f"{path}.{i}"):
                highest = i
        # 多出来的最老那份直接删掉
        oldest = f"{path}.{keep}"
        if os.path.exists(oldest):
            os.remove(oldest)
        # 从最老的往里挪：.keep-1 -> .keep … .1 -> .2，最后 bot.log -> .1
        for i in range(min(highest, keep - 1), 0, -1):
            src = f"{path}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
        print(
            f"[health] 日志已轮转：{os.path.basename(path)} {size} 字节 -> .1，最多保留 {keep} 份",
            flush=True,
        )
        return True
    except Exception as e:
        # 文件被别的进程占用（Windows 上 rotatelog 最常见的失败）就放弃轮转，
        # 让 bot 继续往原文件追加——总比起不来强。
        _warn(f"rotate_log: 轮转 {path} 失败（不轮转，继续启动）：{e}")
        return False


def notify(title, text):
    """best-effort 的 Windows 本地通知。成功返回 True，失败/超时返回 False。

    只用系统自带的 PowerShell + System.Windows.Forms.NotifyIcon 弹一个气泡：
    **不弹阻塞对话框**（绝不用 msg.exe / MessageBox，那会把 bot 卡死），
    10 秒超时，stdout/stderr 全部丢弃（bot 的 stdout 是日志文件，
    不能让子进程继承住）。
    """
    try:
        title = str(title if title is not None else "微信助手")
        text = str(text if text is not None else "")
        # 单引号是 PowerShell 字符串，内部单引号要写两遍
        t = title.replace("'", "''")
        m = text.replace("'", "''")
        # NotifyIcon 必须先加进容器并 Show()，否则气泡不显示（Windows 的已知行为）
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(8000, '{t}', '{m}', "
            "[System.Windows.Forms.ToolTipIcon]::Info); "
            "Start-Sleep -Seconds 8; "
            "$n.Visible = $false; $n.Dispose()"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        p = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        try:
            rc = p.wait(timeout=NOTIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_quietly(p)
            _warn(f"本地通知超时（{NOTIFY_TIMEOUT}s），已放弃：{title}")
            return False
        if rc != 0:
            _warn(f"本地通知返回码 {rc}：{title}")
            return False
        return True
    except Exception as e:
        _warn(f"本地通知失败（不影响 bot）：{e}")
        return False


def _kill_quietly(p):
    try:
        p.kill()
    except Exception:
        pass


def cursor_key(cursor):
    """把游标压成「只表示进度」的可比较值，用来判定**游标停滞**。

    为什么这就是「静默失效」的判据：fts 句柄掉了之后查询**不报错、只返回 0 行**，
    于是游标再也不动，而用户看到的是「它没反应」——和「一切正常」一模一样。
    游标一动就说明确实收到东西了，所以「连续 N 轮不动」是一个**确定性**的信号，
    比猜「它是不是卡了」可靠。

    ⚠️ 这里**不能**把时间戳之类的每轮都变的字段算进来：那会让「停滞」永远判不出来，
    而且失效得非常安静。本项目游标的形状是
    `{fts分片表名: rowid}` + `__time__`（**消息时间水位线，只在真有新消息时前进**）
    + `__nonttext__`（非文本补捞水位线），三个都是进度，所以整份 dict 都能比。
    真出现每轮都变的键时，要在这里把它剔掉，而不是放宽判据。
    """
    if cursor is None:
        return None
    if isinstance(cursor, dict):
        parts = []
        for k in sorted(cursor, key=str):
            v = cursor[k]
            if isinstance(v, dict):
                v = tuple(sorted((str(a), str(b)) for a, b in v.items()))
            parts.append((str(k), str(v)))
        return tuple(parts)
    return str(cursor)


class Health:
    """运行期健康记账本。所有 note_* 方法都只改内存，绝不做 IO/查库。

    典型接线（由 bot.py 在收消息那条线程上做）：

        h = health.Health(cfg, notify_fn=health.notify)
        ...
        h.note_poll(cursor, live_history.poll_errors())   # 每轮轮询完顺手记一笔
        if h.due_login_check():
            ok, detail = client.ping()                     # 探登录是 bot 决定、bot 去做
            h.note_login(ok, detail)
        h.write_status()                                   # 隔一会儿落一次盘（可选）
        h.snapshot()                                       # 喂给 status_page.start()
    """

    def __init__(self, cfg=None, notify_fn=None):
        h = {}
        if isinstance(cfg, dict):
            raw = cfg.get("health")
            if isinstance(raw, dict):
                h = raw
        self.login_check_interval = _num(
            h.get("login_check_interval", DEFAULTS["login_check_interval"]),
            DEFAULTS["login_check_interval"],
        )
        self.alert_cooldown = _num(
            h.get("alert_cooldown", DEFAULTS["alert_cooldown"]),
            DEFAULTS["alert_cooldown"],
        )
        status_file = str(h.get("status_file") or DEFAULTS["status_file"]).strip() \
            or DEFAULTS["status_file"]
        if not os.path.isabs(status_file):
            # ⚠️ 相对路径**按项目根**解析，绝不能交给 os.path.abspath 按进程 CWD 解析。
            # 2026-10-05 真机：bot 被计划任务/提权方式起时 CWD = C:\WINDOWS\System32，
            # config.yaml 里的 `./data/status.json` 于是变成
            # `C:\WINDOWS\System32\data\status.json` —— 建目录被拒（WinError 5）、每轮
            # 写一次失败一次，刷满 bot.log；而状态页读的 data/status.json 从此再没更新过。
            # 项目里其它读 config 相对路径的地方（semantic._abs / image_read.cache_path /
            # file_read.export_dir）都是这个口径，这里补齐。
            status_file = os.path.join(PROJECT_ROOT, status_file)
        self.status_file = os.path.normpath(status_file)
        # 通知函数：默认用模块级的 notify。测试里会换成记录用的假函数。
        self.notify_fn = notify_fn if callable(notify_fn) else notify
        # 告警节流状态：{键: (上次打印时刻, 被折叠的条数)}，见 _warn_throttled
        self._warn_state = {}

        self.started_at = time.time()
        self.started_at_iso = _iso(self.started_at)

        # —— 轮询事实 ——
        self.last_poll_at = None
        self.last_cursor = None
        self.poll_errors = {}
        self.poll_count = 0
        # —— 游标停滞：「静默失效」的可观察判据（见 cursor_key 的说明）——
        self.cursor_stalls = 0          # 当前连续多少轮游标没动
        self.max_cursor_stalls = 0      # 本次运行以来的最长停滞（诊断用）
        self.stall_reported = False     # 这一轮停滞是否已经汇报过（防每轮刷屏）
        self.stall_probed = False       # 这一轮停滞是否已经**问过权威探针**
                                        # ⚠️ 它和 stall_reported 必须分开：空闲时我们
                                        # 探了但不报，绝不能因此把「已恢复」也报出去
                                        # （用户会莫名其妙：我什么时候出过问题？）
        self.recovered_from_stall = False   # 一次性：停滞汇报过之后游标又动了
        self._last_cursor_key = None

        # —— 发送事实 ——
        self.last_send_ok = None
        self.last_send_at = None
        self.last_send_detail = ""
        self.send_ok_count = 0
        self.send_fail_count = 0

        # —— hook 错误事实 ——
        self.hook_errors = 0
        self.last_hook_error = None
        self.last_hook_error_at = None

        # —— 登录态 ——
        self.last_login_ok = None          # None = 还没探过 / 探针本身失败（连不上 hook）
        self.last_login_detail = ""
        self.last_login_at = None
        self.last_login_check_at = None    # 最近一次探登录的时间（不管结果是啥）
        self.login_lost_count = 0
        self.login_restored_count = 0
        self.login_probe_failed_count = 0  # 探针本身失败了几次（连不上 hook）——
                                           # **不算掉登录**，见 note_login 的注释

        # —— 告警 ——
        self.alert_count = 0
        self.last_alert_at = None
        self.last_alert_text = ""
        # {同类告警的 key: 上次发出的时间}，用来实现冷却
        self._alert_sent = {}

        # —— 「hook 可达、但库层死了」（2026-10-06 真机；见 docs/poll-reliability-notes.md §10）——
        # 触发用两条**互相独立**的事实：轮询连续不健康 + 核心库很久没被写。
        # 结论**只由权威探针给**（bot 调 live_history.db_alive_probe 后回填）。
        self.hook_stress_limit = _num(
            h.get("hook_stress_rounds", DEFAULTS["hook_stress_rounds"]),
            DEFAULTS["hook_stress_rounds"],
        )
        self.db_stale_limit = _num(
            h.get("db_stale_sec", DEFAULTS["db_stale_sec"]),
            DEFAULTS["db_stale_sec"],
        )
        self.hook_slow_round_sec = _num(
            h.get("hook_slow_round_sec", DEFAULTS["hook_slow_round_sec"]),
            DEFAULTS["hook_slow_round_sec"],
        )
        self.hook_stress_rounds = 0
        self.max_hook_stress_rounds = 0
        self.db_age_seconds = None         # 核心库最后被写距今（None = 拿不到 = 未知）
        self._db_age_missing_noted = False # 「拿不到库龄」只告警一次（静态条件，别刷屏）
        self.db_probed_at = None           # 最近一次权威探针的时刻
        self.db_probe_detail = ""
        self.db_dead = False               # 已确诊「库查不动」且还没确认恢复
        self.db_dead_count = 0
        self.db_dead_at = None
        self.db_dead_detail = ""
        self.db_recovered_count = 0

    # ------------------------------------------------------------------ 轮询

    def note_poll(self, cursor=None, errors=None):
        """记录最近一次轮询：时间 / 游标 / 分片错误。

        **每轮都会被调用，所以实现极轻量：只赋值、不做 IO、不加锁。**
        errors 的形状就是 `live_history.poll_errors()` 的 `{分片名: (错误文本, 次数)}`。
        传 None 表示这一轮没有分片错误（会清空上次的错误）。
        """
        try:
            self.last_poll_at = time.time()
            self.poll_count += 1
            if cursor is not None:
                # 游标是 dict（v4 是 {fts分片: rowid} + __time__，v3 只有 __time__），
                # 也可能是普通数字。只存下来，不做任何解释。
                self.last_cursor = cursor
            if errors:
                if isinstance(errors, dict):
                    self.poll_errors = {str(k): v for k, v in errors.items()}
                else:
                    # 形状不对也要如实记一笔，别静默丢掉
                    self.poll_errors = {"?": (f"形状异常：{errors!r}", 1)}
            else:
                self.poll_errors = {}
            # 游标停滞判定：只比「进度」，不做 IO
            key = cursor_key(self.last_cursor)
            if key is not None:
                if self._last_cursor_key is not None and key == self._last_cursor_key:
                    self.cursor_stalls += 1
                    if self.cursor_stalls > self.max_cursor_stalls:
                        self.max_cursor_stalls = self.cursor_stalls
                else:
                    if self.stall_reported:
                        # 停滞汇报过、现在又动了 = 恢复了。给 bot 一次性信号去说一声，
                        # 免得用户一直惦记「到底好没好」。
                        self.recovered_from_stall = True
                    self.cursor_stalls = 0
                    self.stall_reported = False      # 动了 -> 下次停滞可以再报一次
                    self.stall_probed = False        # 也可以再探一次
                self._last_cursor_key = key
        except Exception as e:
            _warn(f"note_poll 记账失败：{e}")

    # ------------------------------------------------------------------ 发送

    def note_sent(self, ok=True, detail=""):
        """记录一次发送结果。"""
        try:
            self.last_send_ok = bool(ok)
            self.last_send_at = time.time()
            self.last_send_detail = str(detail or "")
            if ok:
                self.send_ok_count += 1
            else:
                self.send_fail_count += 1
        except Exception as e:
            _warn(f"note_sent 记账失败：{e}")

    def note_send_failure(self, err):
        """记录一次发送失败（err 可以是异常，也可以是字符串）。"""
        self.note_sent(ok=False, detail=_err_text(err))

    # ------------------------------------------------------------------ hook
    # 「hook 可达、但库层死了」——触发、确诊、恢复（2026-10-06；详见 §10 那节）
    #
    # 现场：微信被压崩之后进程**没退**、30001 还应答、`IsLogin` 还报 1，但句柄表空
    # （`handlesAlive = 0`）、核心库自崩溃那刻起再没被写过。三态登录探针把它判成
    # 「在线」⇒ 用户拿不到任何提示，只看到「它没反应」。这里补的就是这个缺口。

    def _probe_allowed(self):
        """距上次权威探针够不够久（一次触发只问一次，探针要碰句柄表）。"""
        if self.db_probed_at is None:
            return True
        return (time.time() - self.db_probed_at) >= min(DB_PROBE_MIN_GAP, self.alert_cooldown)

    def note_round(self, unhealthy, db_age_sec=None):
        """记一轮轮询的「健不健康」+ 核心库有多久没被写。**每轮调用，纯内存、不做 IO。**

        `unhealthy` 由 bot 判：这一轮慢 / 被总时限截断 / 有分片错误。
        连续不健康会累加，**健康一轮立刻清零**（恢复不拖泥带水）。
        `db_age_sec=None`（拿不到）= 不动上一次的值，也**不会**让它变成「很旧」。
        """
        try:
            if db_age_sec is not None:
                self.db_age_seconds = float(db_age_sec)
                self._db_age_missing_noted = True
            elif not self._db_age_missing_noted:
                # 拿不到库写入时间 ⇒ **这条看护永远不会触发**。必须说一声（本项目的铁律：
                # 静默失效最贵），但只说一次（它是静态条件，刷屏没意义）。
                self._db_age_missing_noted = True
                _warn("拿不到微信库的最后写入时间（数据根找不到？）——"
                      "「hook 可达但库查不动」这条主动告警**不会生效**；"
                      "轮询与登录探针不受影响。")
            if unhealthy:
                self.hook_stress_rounds += 1
                if self.hook_stress_rounds > self.max_hook_stress_rounds:
                    self.max_hook_stress_rounds = self.hook_stress_rounds
            else:
                self.hook_stress_rounds = 0
        except Exception as e:
            _warn(f"note_round 记账失败：{e}")

    def hook_db_dead_due(self):
        """该不该去问**权威探针**「库还查得动吗」。**保守：两条独立事实同时成立。**

        ⚠️ 这里只回答「该不该探」，**绝不下结论**——结论由 `note_hook_db_probe` 给。
        为什么两条都要：
          * 只有「轮询不健康」：可能只是 hook 一时慢，库好好的 → 会误报；
          * 只有「库很久没被写」：没人用微信时本来就不写 → **必须不报**
            （2026-10-02 就是拿这种模糊信号报了假警报）。
        """
        try:
            if self.db_dead:
                return False                 # 已确诊、等恢复：不重复探
            if self.hook_stress_rounds < self.hook_stress_limit:
                return False
            if self.db_age_seconds is None or self.db_age_seconds < self.db_stale_limit:
                return False
            return self._probe_allowed()
        except Exception as e:
            _warn(f"hook_db_dead_due 判断失败：{e}")
            return False

    def hook_db_recovered_due(self):
        """确诊过之后轮询恢复正常 → 再问一次权威探针，**确认**恢复（同一个判据）。"""
        try:
            if not self.db_dead or self.hook_stress_rounds > 0:
                return False
            return self._probe_allowed()
        except Exception as e:
            _warn(f"hook_db_recovered_due 判断失败：{e}")
            return False

    def note_hook_db_probe(self, ok, detail=""):
        """权威探针结果（三态：`True` 能查 / `False` 查不动 / `None` 探针自己失败）。

        返回 `"dead"` / `"recovered"` / `""`（无变化），让 bot 决定要不要往微信里也发一条。
        告警走 `_alert_cooldown`（同类冷却），文案**必须能照着做**：做什么（重启微信+扫码）
        与不用做什么（助手不用动）。
        """
        try:
            self.db_probed_at = time.time()
            self.db_probe_detail = str(detail or "")
            if ok is None:
                return ""                    # 探不了 ≠ 坏了（连不上那条归登录探针）
            if not ok:
                self.db_dead = True
                self.db_dead_count += 1
                self.db_dead_at = self.db_probed_at
                self.db_dead_detail = self.db_probe_detail
                self._alert_cooldown(
                    "hook_db_dead", "微信这边的库查不动了",
                    "hook 还在应答，但**微信的库查不动**（这不是掉登录）。\n"
                    f"触发：连续 {self.hook_stress_rounds} 轮轮询不正常，"
                    f"而且核心库已经 {int(self.db_age_seconds or 0)} 秒没被写过。\n"
                    f"探针说：{self.db_dead_detail}\n"
                    "该做的：**完全退出微信 → 重新打开 → 扫码登录**；"
                    "助手不用动，微信回来我会自己接上。\n"
                    "（这种状态下 `IsLogin` 往往还报 1 —— 别被它骗。）")
                return "dead"
            self.db_dead = False
            if self.db_dead_count:
                self.db_recovered_count += 1
                self._alert_cooldown(
                    "hook_db_recovered", "微信的库又能查了",
                    "刚才那次「库查不动」已经过去：核心库又能查、轮询也恢复正常。\n"
                    f"探针说：{self.db_probe_detail}")
                return "recovered"
            return ""
        except Exception as e:
            _warn(f"note_hook_db_probe 记账失败：{e}")
            return ""

    def note_hook_error(self, err):
        """记录一次 hook 层错误（连不上 30001、慢查询、HTTP 500 之类）。"""
        try:
            self.hook_errors += 1
            self.last_hook_error = _err_text(err)
            self.last_hook_error_at = time.time()
        except Exception as e:
            _warn(f"note_hook_error 记账失败：{e}")

    # ------------------------------------------------------------------ 登录

    def due_login_check(self):
        """距上次探登录是否已超过 login_check_interval。到了就由 bot 去探 is_login()。"""
        now = time.time()
        if self.last_login_check_at is None:
            return True
        return (now - self.last_login_check_at) >= self.login_check_interval

    def note_login(self, ok, detail=""):
        """记录一次登录态探测结果；**掉登录要告警，恢复时再告警一次**。

        `ok` 是**三态**：True=在线 / False=明确掉登录 / **None=探针本身失败**。
        ⚠️ None 绝不能当成掉登录（2026-10-05 真机）：`_probe_login` 连不上 hook 时
        返回 None，那时微信往往好好的，只是 hook 卡住了几分钟。旧实现把它记成
        `IsLogin=0 掉登录`，告警文案还教用户去扫码 —— 把排查方向整个带偏。

        同一类告警（掉登录 / 已恢复 / 探不到）在 alert_cooldown 秒内只发一次，防刷屏。
        """
        try:
            now = time.time()
            self.last_login_check_at = now
            self.last_login_at = now
            self.last_login_ok = None if ok is None else bool(ok)
            self.last_login_detail = str(detail or "")
            if ok is None:
                self.login_probe_failed_count += 1
                self._alert_cooldown(
                    "login_probe_failed",
                    "探不到登录态（连不上 hook）",
                    "读不到微信登录态：连不上本机的 hook（30001）。"
                    "**这不等于掉登录** —— 常见原因是微信没在跑、hook 卡住、或端口不对；"
                    "先别去扫码，看一眼微信窗口和 http://127.0.0.1:30001/QueryDB/status。"
                    + (f"\n详情：{self.last_login_detail}" if self.last_login_detail else ""),
                )
                return
            if not ok:
                self.login_lost_count += 1
                self._alert_cooldown(
                    "login_lost",
                    "微信掉登录了",
                    "微信似乎回到登录界面了（IsLogin=0），bot 收不到消息。"
                    "恢复要人工扫码，请打开微信重新登录。"
                    + (f"\n详情：{self.last_login_detail}" if self.last_login_detail else ""),
                )
            else:
                # 只有「上一次明确是掉线」才算恢复，避免启动后第一次探测就报「已恢复」
                if self.login_lost_count > 0:
                    self.login_restored_count += 1
                    self._alert_cooldown(
                        "login_restored",
                        "微信登录已恢复",
                        "登录态恢复正常，bot 继续收消息。"
                        + (f"\n详情：{self.last_login_detail}" if self.last_login_detail else ""),
                    )
        except Exception as e:
            _warn(f"note_login 记账失败：{e}")

    # ------------------------------------------------------------------ 告警

    def alert(self, text, title="微信助手"):
        """先写日志（`[health] ⚠️` 前缀），再尝试本地通知。

        通知失败只记一行日志，**绝不抛异常**——告警链路本身不能成为新的故障点。
        """
        text = str(text if text is not None else "")
        title = str(title or "微信助手")
        try:
            print(f"[health] ⚠️ {title}：{text}", file=sys.stderr, flush=True)
        except Exception:
            pass
        try:
            self.alert_count += 1
            self.last_alert_at = time.time()
            self.last_alert_text = f"{title}：{text}"
        except Exception:
            pass
        try:
            fn = self.notify_fn
            if not callable(fn):
                _warn(f"notify_fn 不是可调用对象，只写日志：{text}")
                return
            # 兼容两种签名：notify(text, title) 和 notify(title, text)
            if _takes_two_args(fn):
                fn(title, text)
            else:
                fn(text, title)
        except Exception as e:
            _warn(f"本地通知调用失败（告警已写日志）：{e}")

    def _alert_cooldown(self, key, text, title="微信助手"):
        """同类告警的冷却闸：alert_cooldown 秒内同一类只真发一次。"""
        now = time.time()
        last = self._alert_sent.get(key)
        if last is not None and (now - last) < self.alert_cooldown:
            # 冷却期内不再打扰用户，但日志里留痕（不然就成了静默吞告警）
            print(
                f"[health] （冷却中，{int(self.alert_cooldown - (now - last))}s 后同类告警可再发）{text}",
                file=sys.stderr, flush=True,
            )
            return False
        self._alert_sent[key] = now
        self.alert(text, title=title)
        return True

    # ------------------------------------------------------------------ 快照

    def snapshot(self):
        """给状态页用的纯内存快照。**绝不在这里做 IO 或查库。**

        从没调用过任何 note_* 也能正常工作（全部字段给 None/0/{}）。
        """
        now = time.time()
        try:
            uptime = max(0.0, now - self.started_at)
        except Exception:
            uptime = 0.0
        return {
            "app": "微信 AI 助手",
            "started_at": self.started_at_iso,
            "now": _iso(now),
            "uptime_seconds": _round(uptime),
            "uptime_human": _human(uptime),
            "healthy": self._healthy(),
            # 轮询
            "poll_count": self.poll_count,
            "last_poll_at": _iso(self.last_poll_at),
            "last_poll_age_seconds": _age(self.last_poll_at, now),
            "last_cursor": self.last_cursor,
            "cursor_stalls": self.cursor_stalls,
            "max_cursor_stalls": self.max_cursor_stalls,
            "poll_errors": {str(k): [str(v[0]), v[1]] if _is_pair(v) else v
                            for k, v in (self.poll_errors or {}).items()},
            # 发送
            "last_send_ok": self.last_send_ok,
            "last_send_at": _iso(self.last_send_at),
            "last_send_detail": self.last_send_detail,
            "send_ok_count": self.send_ok_count,
            "send_fail_count": self.send_fail_count,
            # hook
            "hook_errors": self.hook_errors,
            "last_hook_error": self.last_hook_error,
            "last_hook_error_at": _iso(self.last_hook_error_at),
            # 登录
            "login_ok": self.last_login_ok,
            "login_detail": self.last_login_detail,
            "last_login_at": _iso(self.last_login_at),
            "last_login_check_age_seconds": _age(self.last_login_check_at, now),
            "login_lost_count": self.login_lost_count,
            "login_restored_count": self.login_restored_count,
            "login_probe_failed_count": self.login_probe_failed_count,
            # hook 可达、但库层死了（2026-10-06；判据与告警见 note_round / note_hook_db_probe）
            "hook_stress_rounds": self.hook_stress_rounds,
            "max_hook_stress_rounds": self.max_hook_stress_rounds,
            "db_age_seconds": (None if self.db_age_seconds is None
                               else _round(self.db_age_seconds)),
            "db_dead": bool(self.db_dead),
            "db_dead_count": self.db_dead_count,
            "db_dead_at": _iso(self.db_dead_at),
            "db_dead_detail": self.db_dead_detail,
            "db_recovered_count": self.db_recovered_count,
            "db_probe_detail": self.db_probe_detail,
            # 告警
            "alert_count": self.alert_count,
            "last_alert_at": _iso(self.last_alert_at),
            "last_alert_text": self.last_alert_text,
            # 配置（只放不敏感的，方便状态页对照）
            "login_check_interval": self.login_check_interval,
            "alert_cooldown": self.alert_cooldown,
            "hook_stress_limit": self.hook_stress_limit,
            "db_stale_limit": self.db_stale_limit,
            "hook_slow_round_sec": self.hook_slow_round_sec,
        }

    def _healthy(self):
        """粗判：掉登录 / 库层死了 / 有分片错误 / 很久没轮询 = 不健康。不确定时返回 None。"""
        try:
            if self.last_login_ok is False:
                return False
            if self.db_dead:
                return False
            if self.poll_errors:
                return False
            if self.last_poll_at is None:
                return None
            if (time.time() - self.last_poll_at) > max(60.0, self.login_check_interval):
                return False
            return True
        except Exception:
            return None

    def _warn_throttled(self, key, msg, interval=WARN_THROTTLE_SEC):
        """按 key 节流的 _warn：同一个失败在 interval 秒内只打第一行。

        为什么必须有：**每轮都会碰的失败**（写状态盘就是每轮一次）以前是每 5 秒一行，
        几小时就把 bot.log 灌成几 MB，把真正的线索埋掉（真机踩过）。
        绝不静默：被折叠掉的次数会在下一行里如实报出来。
        节流状态挂在实例上（不是模块级），所以新建一个 Health 就是干净的一份，
        自测之间不会互相影响。（`note_login` 那条路本来就有 alert_cooldown，不归这里管。）
        """
        now = time.monotonic()
        last, folded = self._warn_state.get(key, (0.0, 0))
        if last and (now - last) < interval:
            self._warn_state[key] = (last, folded + 1)
            return False
        tail = f"（过去 {interval:g} 秒内同类失败还有 {folded} 次，已折叠）" if folded else ""
        self._warn_state[key] = (now, 0)
        _warn(msg + tail)
        return True

    def write_status(self):
        """把 snapshot() 原子写到 status_file（临时文件 + os.replace）。失败返回 False。"""
        try:
            data = self.snapshot()
        except Exception as e:
            _warn(f"write_status: 生成快照失败：{e}")
            return False
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        except Exception as e:
            _warn(f"write_status: 序列化失败：{e}")
            return False

        tmp_path = None
        try:
            d = os.path.dirname(self.status_file) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                # os.fdopen 成功后就由它负责关闭；失败时兜底关掉 fd
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise
            os.replace(tmp_path, self.status_file)
            tmp_path = None
            return True
        except Exception as e:
            # 这里用节流版：写不进去往往是**持续**的（权限/目录不对），
            # 而这条路径每轮都会被调一次——不节流就是每 5 秒一行。
            self._warn_throttled("write_status", f"写 {self.status_file} 失败：{e}")
            return False
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


# ---------------------------------------------------------------- 内部小工具


def _err_text(err):
    """把异常/字符串统一成一行文本。"""
    try:
        if isinstance(err, BaseException):
            return f"{type(err).__name__}: {err}"
        return str(err)
    except Exception:
        return "<无法格式化的错误>"


def _is_pair(v):
    return isinstance(v, (tuple, list)) and len(v) == 2


def _iso(ts):
    """时间戳 -> 本地时间字符串；None 保持 None（状态页好显示「还没」）。"""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _age(ts, now):
    if ts is None:
        return None
    try:
        return _round(max(0.0, float(now) - float(ts)))
    except Exception:
        return None


def _round(v):
    try:
        return round(float(v), 3)
    except Exception:
        return v


def _human(seconds):
    try:
        s = int(seconds)
    except Exception:
        return ""
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}天{h}小时{m}分"
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def _takes_two_args(fn):
    """判断 notify 回调是不是 (title, text) 这种两参数签名。

    内建函数（如 print）拿不到签名，就按两参数处理（print(title, text) 正好对）。
    """
    try:
        import inspect
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    n = 0
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL):
            if p.kind == p.VAR_POSITIONAL:
                return True
            n += 1
    return n >= 2
