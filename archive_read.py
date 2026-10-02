"""压缩包递归读（zip / 7z / rar）：把里面的成员**逐个**交给 `file_read` 那套解析。

规格：`docs/file-input-spec.md` 第九节「压缩包」。三条要点：

1. **预算共享、且跨嵌套**：整个读压缩包的过程只有一个"解压后累计字节"额度，
   取自 `file_read.unpack_cap()`（默认 200MB，**防 zip 炸弹**）。嵌套包也用同一个额度 ——
   否则"套娃压缩包"就是绕过炸弹防护的后门。
2. **不落永久文件、也不搞 zip-slip**：成员只在 `data/tmp_unpack/` 里**临时**落一份、
   用完就删；文件名一律取 basename（`../../x` 这种直接被削平），而且根本不解到用户目录。
3. **每一层都如实说**：读了几份、跳了几份、为什么跳（加密/太大/不认识/超出层数），
   绝不让用户以为"整个包都读完了"。

可选依赖（缺了只影响那一种格式，绝不影响 zip）：
  * `.7z` → `py7zr`（`pip install py7zr`）
  * `.rar` → `rarfile` + 外部解压器（`pip install rarfile`，还需要 unrar/bsdtar）
"""
import os
import shutil
import tempfile
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP_DIR = os.path.join(_HERE, "data", "tmp_unpack")

ARCHIVE_EXTS = (".zip", ".7z", ".rar")
ARCHIVE_KINDS = ("zip", "7z", "rar")


class _Budget:
    """解压后累计字节的**共享**额度（跨成员、跨嵌套包）。"""

    def __init__(self, cap):
        self.cap = cap
        self.used = 0

    def room(self):
        return max(0, self.cap - self.used)

    def spend(self, n):
        self.used += int(n)

    def check(self, n):
        """超了就抛——由上层翻成一句人话（**绝不截断后当正常内容用**）。"""
        if self.used + int(n) > self.cap:
            raise ValueError(
                f"解压后累计会超过 {self.cap / 1048576:.0f}MB（已解出 "
                f"{self.used / 1048576:.1f}MB，再加 {int(n) / 1048576:.1f}MB）")


def _cfg(cfg):
    return ((cfg or {}).get("archive") or {})


def _int_opt(value, default, lo=0, hi=10000):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def max_depth(cfg=None):
    return _int_opt(_cfg(cfg).get("max_depth"), 2, 0, 5)


def max_members(cfg=None):
    return _int_opt(_cfg(cfg).get("max_members"), 100, 1, 2000)


def _tmp_dir():
    os.makedirs(_TMP_DIR, exist_ok=True)
    return _TMP_DIR


def _sanitize(name, i=0):
    """成员名 → 安全的临时文件名（**削平目录**，防 zip-slip）。"""
    base = os.path.basename(str(name).replace("\\", "/")) or f"member{i}"
    base = base.lstrip(".") or f"member{i}"
    return f"{i:03d}_{base[:80]}"


def _zip_members(path, budget, cfg):
    """返回 `(读到的 [(名字, bytes)], 总成员数, 跳过的说明列表)`。"""
    out, skipped = [], []
    limit = max_members(cfg)
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        for info in infos:
            name = info.filename
            if info.flag_bits & 0x1:
                skipped.append(f"{name}（加密的，读不了）")
                continue
            if len(out) >= limit:
                break
            declared = info.file_size or 0
            budget.check(declared)                  # 声明值先挡一道（可能撒谎）
            room = budget.room() + 1                # +1：多读出来的那一字节就是超限证据
            try:
                with z.open(info) as f:
                    data = f.read(room)
            except (zipfile.BadZipFile, RuntimeError) as e:
                skipped.append(f"{name}（解不开：{str(e)[:60]}）")
                continue
            if len(data) > room - 1:
                raise ValueError(f"「{name}」实际解出的字节超过它自己声明的大小（判定为解压炸弹）")
            budget.spend(len(data))
            out.append((name, data))
    return out, len(infos), skipped


def _sevenz_members(path, budget, cfg):
    """读 7z 的成员。

    ⚠️ **py7zr 1.1.3 没有内存读接口了**（`read()` 被删掉，只剩 `extract`/`extractall`），
    所以这里只能"解到**我们自己的临时目录**再读回来"：
      * 先按成员**声明的大小**过一遍预算（炸弹在解压前就被挡住）；
      * 解出来的路径**必须落在那个临时目录里**（realpath 前缀核对 —— 防它自己写出去）；
      * 读完整个临时目录删掉（`finally`，出错也删）。
    """
    try:
        import py7zr
    except ImportError:
        raise RuntimeError("读 .7z 需要 py7zr，本机没装："
                           "`.venv\\Scripts\\python.exe -m pip install py7zr`")
    limit = max_members(cfg)
    out, skipped = [], []
    with py7zr.SevenZipFile(path, mode="r") as z:
        if z.needs_password():
            raise RuntimeError("这个 .7z 有密码，读不了")
        names = [n for n in z.getnames() if not n.endswith("/")]
        sizes = {}
        try:
            for info in z.list():
                if getattr(info, "filename", None) is not None:
                    sizes[info.filename] = getattr(info, "uncompressed", 0) or 0
        except Exception as e:
            print(f"⚠️ archive_read: 读不出 7z 成员清单的大小（{type(e).__name__}）；"
                  f"只能按实际解出来的字节算。", flush=True)
        picked = names[:limit]
        for n in picked:
            budget.check(int(sizes.get(n) or 0))

        os.makedirs(_TMP_DIR, exist_ok=True)
        d = tempfile.mkdtemp(prefix="7z_", dir=_TMP_DIR)
        try:
            z.extract(path=d, targets=picked)
            root = os.path.realpath(d)
            for n in picked:
                rp = os.path.realpath(os.path.join(d, *str(n).split("/")))
                if not rp.startswith(root + os.sep):
                    skipped.append(f"{n}（解出来的路径跑到临时目录外了，已跳过）")
                    continue
                if not os.path.isfile(rp):
                    skipped.append(f"{n}（没解出来）")
                    continue
                try:
                    with open(rp, "rb") as f:
                        data = f.read()
                except OSError as e:
                    skipped.append(f"{n}（读不出来：{type(e).__name__}）")
                    continue
                budget.check(len(data))
                budget.spend(len(data))
                out.append((n, data))
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return out, len(names), skipped


def _rar_members(path, budget, cfg):
    try:
        import rarfile
    except ImportError:
        raise RuntimeError("读 .rar 需要 rarfile，本机没装："
                           "`.venv\\Scripts\\python.exe -m pip install rarfile`"
                           "（另外还要一个外部解压器 unrar/bsdtar）")
    out, skipped = [], []
    with rarfile.RarFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        for info in infos:
            if info.needs_password():
                skipped.append(f"{info.filename}（加密的，读不了）")
                continue
            budget.check(info.file_size or 0)
            try:
                with z.open(info) as f:
                    data = f.read(budget.room() + 1)
            except Exception as e:
                skipped.append(f"{info.filename}（解不开：{type(e).__name__}；"
                               f"也确认一下本机有没有 unrar/bsdtar）")
                continue
            budget.check(len(data))
            budget.spend(len(data))
            out.append((info.filename, data))
            if len(out) >= max_members(cfg):
                break
    return out, len(infos), skipped


def read_archive(path, cfg=None, on_image=None, budget=None, depth=0):
    """读一个压缩包（可嵌套）。返回 `(文本, 错误)`。

    成员的解析**全交给 `file_read`**（写成临时文件再调它的 `extract`），
    这样文本/Office/图片/嵌套压缩包/音频……全自动可用，不用在这儿重写一遍分派。
    """
    import file_read
    ext = os.path.splitext(path)[1].lower()
    kind = {"zip": "zip", "7z": "7z", "rar": "rar"}.get(ext.lstrip(".")) \
        or ("zip" if file_read.sniff(path)[0] == "zip" else None)
    if kind is None:
        return None, f"不是压缩包（{ext or '无后缀'}）"

    cfg = cfg or {}
    budget = budget or _Budget(file_read.unpack_cap(cfg))
    limit = max_members(cfg)
    try:
        if kind == "zip":
            members, total, skipped = _zip_members(path, budget, cfg)
        elif kind == "7z":
            members, total, skipped = _sevenz_members(path, budget, cfg)
        else:
            members, total, skipped = _rar_members(path, budget, cfg)
    except ValueError as e:                       # 预算/炸弹
        return None, (f"这个压缩包**解压后太大，出于安全我没读**（{e}）。"
                      f"要读就把它拆小一点再发。")
    except RuntimeError as e:                     # 缺依赖/加密
        return None, str(e)
    except zipfile.BadZipFile:
        return None, "这个压缩包打不开（文件损坏，或其实是别的格式）。"
    except Exception as e:
        return None, f"读压缩包失败：{type(e).__name__}: {str(e)[:200]}"

    if not members:
        head = f"（这个压缩包里有 {total} 个成员，但一个都没能读出来）"
        return head + ("\n" + "\n".join(skipped) if skipped else ""), None

    lines = [f"—— 压缩包里的内容：读了 {len(members)} 个成员"
             + (f"（共 {total} 个）" if total > len(members) else "") + " ——"]
    d = _tmp_dir()
    for i, (name, data) in enumerate(members):
        tmp = os.path.join(d, _sanitize(name, i))
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            # 嵌套压缩包：同额度、层数 +1（**别再用一个新额度**，否则套娃就能绕过封顶）
            if file_read.sniff(tmp)[0] in ARCHIVE_KINDS and os.path.splitext(name)[1].lower() in ARCHIVE_EXTS:
                if depth + 1 > max_depth(cfg):
                    lines.append(f"〔{name}〕里面还是压缩包，但已经到 archive.max_depth="
                                 f"{max_depth(cfg)} 层，没再往下读")
                    continue
                sub, err = read_archive(tmp, cfg, on_image=on_image, budget=budget,
                                        depth=depth + 1)
                lines.append(f"〔{name}〕\n" + (sub if sub else f"（读不了：{err}）"))
                continue
            text, err = file_read.extract(tmp, cfg, on_image=on_image)
            if err:
                lines.append(f"〔{name}〕读不了：{err}")
            elif text:
                lines.append(f"〔{name}〕\n{text}")
        finally:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    if skipped:
        lines.append("（下面这些成员没读：\n  · " + "\n  · ".join(skipped) + "）")
    if len(members) < total:
        lines.append(f"（还有 {total - len(members)} 个成员没读：超过 archive.max_members="
                     f"{limit}）")
    return "\n\n".join(lines), None


def sweep_tmp(max_age=86400.0):
    """清理临时目录（**删了什么要打日志**）。正常路径每个成员用完就删，这里只是兜底。"""
    import time
    try:
        names = os.listdir(_TMP_DIR)
    except OSError:
        return []
    dead = []
    now = time.time()
    for n in names:
        p = os.path.join(_TMP_DIR, n)
        try:
            if now - os.path.getmtime(p) > max_age:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
                dead.append(n)
        except OSError:
            continue
    if dead:
        print(f"⚠️ archive_read: 清理了 {len(dead)} 个临时文件（>{max_age/3600:.0f} 小时）。",
              flush=True)
    return dead
