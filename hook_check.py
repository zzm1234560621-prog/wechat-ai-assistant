"""助手启动自检：**微信目录里装的那份 hook（version.dll）是不是包里这一份**。

## 为什么要这个模块（2026-10-06 真机踩到的静默失效）

「换了新包」≠「微信里的 hook 换上了」。包里有两份 `version.dll`：

* `installers\\wechat-4.1.10.27\\version.dll` —— **用来安装的那份**（新包解压下来就是它）；
* `C:\\Program Files\\Tencent\\Weixin\\version.dll` —— **微信正在加载的那份**（装过之后一直躺在那）。

解压一个新包**不会**动第二份。于是出现过这个形状：包是最新的、日志全绿、
**功能却还是旧的**——那台机器上 `IsLogin` 恒 0、bot 每 10 秒刷「数据库打不开（微信没登录？）」，
而用户已经把包换过两遍。**没有任何地方对比过这两份文件**，正是本项目最忌讳的静默失效。

所以启动时对比一次，不一致就**明说是哪一份旧了、该跑哪个命令**。

## 两条独立证据（都要，缺一条就分不清故障）

1. **磁盘**：微信目录里那份的 SHA256 ≠ 包里那份 ⇒ 装的是旧 hook（该重装）。
2. **运行时**：`/QueryDB/status` 返回里**没有 `LoginGateInfo`** ⇒ 微信进程里跑的是旧 hook。
   ⚠️ 它只在连上 hook 之后才拿得到，所以 `is_login` 闸门卡住时这条证据**拿不到**——
   磁盘那条才是启动时唯一能用的。

只读：读注册表 + 读两个文件 + 一次 HTTP GET。**不碰微信进程、不查库、不改任何东西。**
"""
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 包内那份「用来安装」的 hook（相对本模块所在目录）
BUNDLE_REL = os.path.join("installers", "wechat-4.1.10.27", "version.dll")

# 已知构建的指纹（真源在 docs/hook-login-gate-notes.md，改包时同步这里）
KNOWN = {
    527360: "527360/868BFF8F（新：读保存位置 ini、25 秒窗口、零主动扫描）",
    519168: "519168/3877BA84（旧：要求「核心库连续一直在写」，安静账号上闸门永不开）",
}


def _short_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_weixin_dir():
    """微信 4.x 安装目录（有 `Weixin.exe` 的那个）。找不到返回 None。

    判据与 `installers/wechat-4.1.10.27/_common.ps1` 的 `Find-Weixin` **必须一致**：
    HKCU/HKLM 的 `SOFTWARE\\Tencent\\Weixin` 下的 `InstallPath`，再退到
    `%ProgramFiles%\\Tencent\\Weixin`。两处各写一份就是下一次分叉（这个项目踩过）。
    """
    cands = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key in (r"SOFTWARE\Tencent\Weixin",):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for name in ("InstallPath", "InstallDir"):
                            try:
                                val, _ = winreg.QueryValueEx(k, name)
                            except OSError:
                                continue
                            if val:
                                cands.append(str(val).rstrip("\\/"))
                except OSError:
                    continue
    except ImportError:            # 非 Windows：这个模块本来也只是 Windows 用得上
        pass
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env)
        if root:
            cands += [os.path.join(root, "Tencent", "Weixin"),
                      os.path.join(root, "Tencent", "WeChat")]
    for d in cands:
        if d and os.path.isfile(os.path.join(d, "Weixin.exe")):
            return d
    return None


def installed_dll():
    """微信目录里现役那份 `version.dll`：`(路径, 字节数, sha256) `，找不到返回 None。"""
    d = find_weixin_dir()
    if not d:
        return None
    p = os.path.join(d, "version.dll")
    if not os.path.isfile(p):
        return None
    try:
        return p, os.path.getsize(p), _short_hash(p)
    except OSError:
        return None


def bundle_dll():
    """包里那份用来安装的 `version.dll`：`(路径, 字节数, sha256)`，没有则 None。

    ⚠️ 开发仓里这个文件**就是**同名的那份（`installers/.../version.dll`），
    所以开发机上两者必然一致——这条自检主要对「拿包的人」有意义。
    """
    p = os.path.join(HERE, BUNDLE_REL)
    if not os.path.isfile(p):
        return None
    try:
        return p, os.path.getsize(p), _short_hash(p)
    except OSError:
        return None


# ── 「微信还在不在写库」：文件侧判据（唯一所有者）────────────────────────────
#
# 为什么需要它（2026-10-06 真机）：微信被 hook 压崩之后进程**没有退出** ——
# 30001 还应答、`IsLogin` 还报 1、`LoginGateInfo.cycles` 还在涨，但句柄表已经拿不到
# 任何库（`handlesAlive = 0`），`db_storage` 里的库自崩溃那一刻起**再没被写过**。
# 用户看到的是「它没反应」，而当时的告警一句都不发（三态登录探针把它判成「在线」）。
# 这里给出**不碰 hook、不查库**的那一半事实：微信最后一次写库是多久以前。
#
# ⚠️ 「库很久没被写」**单独不构成故障**（没人用微信时本来就不写）——它必须和
# 「轮询连续不健康」一起用；判定与告警在 `health.Health` / `bot.handle_hook_db_dead`，
# 完整理由见 `docs/poll-reliability-notes.md` 的第 10 节。
_DB_WATCH = {"paths": None, "built": 0.0}
DB_WATCH_TTL = 600.0            # 候选清单缓存多久（账号/库很少变，避免每轮递归遍历）
DB_FILE_SUFFIXES = (".db", "-wal")


def _db_watch_paths(root=None, ttl=DB_WATCH_TTL):
    """要看「还在不在写」的那批文件：每个账号 `db_storage` 下的 `*.db` / `*-wal`。

    为什么只挑这两种：它们按事务更新，代表**真的写了**；`-shm` 是只读访问也会动的
    共享内存索引，拿它当判据会把「只是被读过」当成「在写」。
    为什么只扫 `db_storage`（不是整个 `xwechat_files`）：后者下面还有 `msg/file` 那种
    上万文件的目录，走过它纯属浪费（这是每轮都要用的判据）。
    """
    now = time.time()
    cached = _DB_WATCH.get("paths")
    if cached is not None and root is None and (now - _DB_WATCH.get("built", 0.0)) < ttl:
        return cached
    if root is None:
        try:
            import image_cache            # 「数据根在哪」的唯一所有者
            root = image_cache.data_root()
        except Exception:
            root = None
    out = []
    if root and os.path.isdir(root):
        try:
            accounts = os.listdir(root)
        except OSError:
            accounts = []
        for acc in accounts:
            db = os.path.join(root, acc, "db_storage")
            if not os.path.isdir(db):
                continue
            for dirpath, _dirs, files in os.walk(db):
                for fn in files:
                    if fn.lower().endswith(DB_FILE_SUFFIXES):
                        out.append(os.path.join(dirpath, fn))
    out.sort()
    _DB_WATCH["paths"] = out
    _DB_WATCH["built"] = now
    return out


def reset_db_watch_cache():
    """清掉候选清单缓存（自测换一个临时数据根时用）。"""
    _DB_WATCH["paths"] = None
    _DB_WATCH["built"] = 0.0


def core_db_age_sec(root=None, now=None):
    """微信最后一次写库距今多少秒；**拿不到就返回 None**（未知 ≠ 死了）。

    只 `stat` 缓存下来的那批文件（几十个），不做递归、不碰 hook —— 所以它能被
    **每一轮轮询**调用。
    """
    now = time.time() if now is None else now
    newest = None
    for p in _db_watch_paths(root):
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    if newest is None:
        return None
    return max(0.0, now - newest)


def check(client=None):
    """跑一次自检。返回 dict：

    `ok`       —— True = 磁盘与运行时都不像「装了旧 hook」
    `file_same`—— 磁盘那份与包里那份哈希是否相同（None = 有一边读不到，判不了）
    `runtime_gate_info` —— 运行时那份有没有 `LoginGateInfo`（None = 没连上 hook）
    `problems` —— 需要用户处理的条目（每项一句人话）
    `notes`    —— 只是诊断信息，不用处理
    """
    res = {"ok": True, "file_same": None, "runtime_gate_info": None,
           "problems": [], "notes": []}

    inst = installed_dll()
    bund = bundle_dll()
    if inst and bund:
        res["installed"] = {"path": inst[0], "size": inst[1], "sha256": inst[2][:16]}
        res["bundle"] = {"path": bund[0], "size": bund[1], "sha256": bund[2][:16]}
        res["file_same"] = (inst[2] == bund[2])
        if not res["file_same"]:
            res["ok"] = False
            res["problems"].append(
                f"微信目录里装的是**旧的** hook：{inst[1]} 字节 / {inst[2][:16]}"
                f"（包里那份是 {bund[1]} 字节 / {bund[2][:16]}）。"
                f"解压新包**不会**替换它——要重装一次 hook："
                f"`助手.bat` → [8] 更多 → [7] Hook → [1] 装 hook"
                f"（会弹 UAC；装完**必须重启微信**才会加载）。"
                f"旧的已知症状：`IsLogin` 恒 0、bot 一直刷「数据库打不开（微信没登录？）」。")
        else:
            res["notes"].append(
                f"hook 与包内一致（{inst[1]} 字节 / {inst[2][:16]}）。")
        if inst[1] in KNOWN:
            res["notes"].append(f"微信目录里那份：{KNOWN[inst[1]]}")
    elif not inst:
        res["problems"].append(
            "微信目录里没有 `version.dll` —— hook 还没装（或微信装在别处）。"
            "装法：`助手.bat` → [8] 更多 → [7] Hook → [1] 装 hook。")
        res["ok"] = False
    else:
        res["notes"].append("包内那份 hook 读不到（多半在开发仓里跑），跳过文件对比。")

    # ── 运行时证据（只在拿到 client 时验）────────────────────────────────
    if client is not None:
        try:
            st = client.db_status() or {}
        except Exception as e:
            res["notes"].append(f"探不到 hook 运行版本（{type(e).__name__}）：{e}")
            return res
        res["runtime_gate_info"] = ("LoginGateInfo" in st)
        if not res["runtime_gate_info"]:
            res["ok"] = False
            res["problems"].append(
                "**微信进程里跑的是旧 hook**：`/QueryDB/status` 里没有 `LoginGateInfo` 字段"
                "（新构建才带）。常见原因：文件换了但**微信没重启**（DLL 只在微信启动时加载）。"
                "处理：完全退出微信（右下角也退）→ 重新打开 → 重新扫码登录。")
        try:
            res["is_login"] = int((st or {}).get("IsLogin", 0)) == 1
        except (TypeError, ValueError):
            res["is_login"] = False
        res["login_gate"] = str((st or {}).get("LoginGate") or "")
    return res


def format_report(res, title="hook 自检"):
    """把 `check()` 的结果变成几行给人看的字（终端的宽度，别太长）。"""
    lines = [f"{title}：{'✅ 正常' if res.get('ok') else '❌ 需要处理'}"]
    if res.get("installed"):
        lines.append(f"  微信目录：{res['installed']['size']} 字节 / "
                     f"{res['installed']['sha256']}")
    if res.get("bundle"):
        lines.append(f"  包内那份：{res['bundle']['size']} 字节 / "
                     f"{res['bundle']['sha256']}")
    for p in res.get("problems", []):
        lines.append(f"  ⚠️ {p}")
    return "\n".join(lines)


if __name__ == "__main__":       # 命令行直接跑一次（只读）
    import json
    print(json.dumps(check(), ensure_ascii=False, indent=2))
