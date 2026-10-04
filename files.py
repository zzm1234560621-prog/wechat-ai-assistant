"""电脑文件能力：`computer_files` 工具的内核。

**规格（权威）**：`docs/computer-files-spec.md`。

本模块是 `plugins` 契约（`docs/plugin-contract-spec.md`）的**第一个消费者** ——
它按契约注册工具，所以它落地这件事本身就在验证那份契约装不装得下真的功能。
如果契约连项目自己的文件能力都装不下，它也不配去接 MCP。

## 两条分界线（改这块之前先读）

1. **只做文件操作，不执行任何程序。** 本地执行是 `run_command`（`executor.py`）的事，
   那条链**每条命令都要用户确认**。本工具**不是它的第二条路** —— 一个 exec 参数都不加。
2. **读类复用 `file_read.extract()`**（「读文件的唯一入口」）：Office / PDF / 老 Office /
   图片 / 音频 / 压缩包 / 邮件 / SQLite 全都自动可用。本模块只换**准入策略**
   （哪些路径能碰），抽取内核一行都不重写 —— **一个内核、两套门**。

## 路径策略只能有一个所有者

本项目有三处「哪些路径可以」的判定，回答的是**三个不同的问题**：

| 所有者 | 回答的问题 |
|---|---|
| `agent.send_image_dirs` + `image_cache` | 哪些**本地图片**可以**发出去**给别人 |
| `file_read` 的 `msg/file/` 边界 | 哪些**收到的**文件可以被读 |
| **本模块的 `files.roots` / `files.deny`** | 哪些路径可以在**盘上**被读写 |

底层那个「一个路径是不是落在某个目录里」的原语**复用 `file_read._under_allowed`**
（realpath + `commonpath`，跨盘符抛 ValueError = 不通过），不造第三份实现 ——
它挡的正是字符串层面看不出来的目录联接 / 符号链接绕过。
"""
import fnmatch
import os
import shutil
import time

import file_read
import plugins


class FilesError(Exception):
    """一个文件操作**没有做成**，附一句给人看的原因。

    为什么用异常而不是「返回一句话让人去猜」：调用方必须能**可靠地**区分
    「做成了」和「没做成」—— `apply_item` 要如实把 `(条数, 错误)` 交给 bot。
    靠去猜返回文本里的关键字（「失败」「没有」「不了」）迟早会骗过自己，
    而这一步是**用户已经确认过的写 / 删**：把失败报成成功是最坏的一种结果
    （用户以为文件已经删了，其实还在；或者反过来）。
    """

# 默认挡住的系统关键目录（用户口径「系统目录可以跳过挡住」）。
# 判据在 path_ok() 里，用的是**解析后的真实路径**，所以写成什么大小写都不影响。
SYSTEM_DENY = (
    os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", ""),
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
)

# 读类动作：免确认（见规格第二节的表）。
ACTIONS_READ = ("list", "find", "info", "read")
# 写类动作：默认免确认（用户口径「读写不用」）。
ACTIONS_WRITE = ("write", "append", "mkdir", "copy", "move", "rename")
# 危险动作：**永远强制确认** + 进回收站。
ACTIONS_DANGER = ("delete",)
ACTIONS = ACTIONS_READ + ACTIONS_WRITE + ACTIONS_DANGER

# ⚠️ **不可逆的那一步不给配置留后门**：即使有人把 `delete` 从 `files.confirm`
# 里删掉，代码也把它加回来（并在启动时告警，见 `warn_forced`）。
FORCED_CONFIRM = ("delete",)

# 默认要确认的：删除（不可逆）+ 覆盖已存在文件（也不可逆）。
# `overwrite` 不是 action，是一个**情况**（见 `_needs_confirm`）。
DEFAULT_CONFIRM = ("delete", "overwrite")

# 一个「文件操作」的身份由这几个字段决定 —— 也是待确认队列的**判重字段**。
# 少了它，「删掉 A」和「删掉 B」会算出同一个判重键，第二条被判成重复而不登记，
# 用户照菜单回「确认」——**做掉的是另一件事**（规格 4.2）。
KEY_FIELDS = ("action", "path", "src", "dst", "new_name", "text")

_DEFAULT_LIST_LIMIT = 200
_DEFAULT_FIND_DEPTH = 3
_DEFAULT_FIND_DIRS = 20000


# ────────────────────────────────────────────────────────────── 配置

def _cfg(cfg):
    f = (cfg or {}).get("files")
    return f if isinstance(f, dict) else {}


def enabled(cfg):
    """`files.enabled`：默认开；**只有显式 `false` 才算关**。

    ⚠️ 和 `plugins.enabled` **故意反着来**（那个是「非布尔一律当开」）：
    这里一旦判错成「开」，模型就能碰磁盘 —— 权限开关的安全方向是「关」。
    所以非布尔值一律**当关并响亮告警**，而工具被调用时会如实说明
    「文件能力关着、在哪打开」，不是静默失效。
    """
    f = _cfg(cfg)
    v = f.get("enabled", True)
    if v is False:
        return False
    if v is True:
        return True
    return None                    # 「说不清」——调用方负责告警


def _enabled_or_note(cfg):
    v = enabled(cfg)
    if v is None:
        return False, ("`files.enabled` 写成了非布尔值（"
                       + repr(_cfg(cfg).get("enabled"))
                       + "），已按**关**处理。要开就写 enabled: true")
    if not v:
        return False, ("文件能力是关着的（`config.yaml` 的 `files.enabled: false`）。"
                       "让用户自己把它打开 —— 别改用 `run_command` 绕过。")
    return True, ""


def roots(cfg):
    """允许访问的根目录。**空列表 = 全盘**（用户 2026-10-04 的口径）。"""
    raw = _cfg(cfg).get("roots") or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for x in raw if isinstance(raw, (list, tuple)) else []:
        s = str(x or "").strip()
        if not s:
            continue
        try:
            out.append(os.path.abspath(os.path.expanduser(s)))
        except OSError:
            continue
    return out


def deny_dirs(cfg):
    """挡住的目录。缺省 = `SYSTEM_DENY`；**显式写 `[]` = 不挡**（那是用户的选择）。"""
    raw = _cfg(cfg).get("deny")
    if raw is None:
        raw = list(SYSTEM_DENY)
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for x in raw if isinstance(raw, (list, tuple)) else []:
        s = str(x or "").strip()
        if not s:
            continue
        try:
            out.append(os.path.abspath(os.path.expanduser(s.rstrip("\\/") or s)))
        except OSError:
            continue
    return out


def describe_scope(cfg):
    """一段人话，说清现在能碰哪儿（启动告警与工具回话都用它）。"""
    r = roots(cfg)
    d = deny_dirs(cfg)
    scope = "全盘" if not r else "、".join(r)
    tail = "" if not d else f"；挡着：{'、'.join(d)}"
    return f"范围：{scope}{tail}"


def path_ok(path, cfg):
    """**路径准入的唯一所有者**。返回 `(解析后的真实路径, 错误文本)`。

    两条判据，缺一不可：

    * **不在任何 `deny` 之下** —— 先判它，因为「系统目录」比「允许的根」更硬；
    * `roots` 为空 = 全盘；非空则必须落在某个根之下。

    两侧都先 `realpath` 归一化再比（`_under_allowed` 干这个），
    挡的是 `..` 与**符号链接 / 目录联接** —— 字符串层面看着在白名单里、
    打开却读到了白名单外的文件，那是这类判定最经典的一种绕过。

    登记时与执行时**各判一次**：从登记到用户回「确认」之间配置可能变、
    文件也可能被换成链接（同 `send_pending` 的 `allowed_dirs` 二次校验）。
    """
    raw = str(path or "").strip()
    if not raw:
        return "", "没给路径。"
    try:
        real = os.path.realpath(os.path.abspath(os.path.expanduser(raw)))
    except OSError as e:
        return "", f"这个路径解析不了：{e}"

    d = [os.path.realpath(x) for x in deny_dirs(cfg)]
    if d and file_read._under_allowed(real, d):
        return "", (f"「{real}」在**系统目录**里（`files.deny`），我不碰。"
                    f"它默认挡着 Windows / Program Files / ProgramData；"
                    f"确实要访问，就让用户去 config.yaml 的 `files.deny` 里"
                    f"去掉对应的那一项。**不要改用 run_command 绕过。**")

    r = [os.path.realpath(x) for x in roots(cfg)]
    if r and not file_read._under_allowed(real, r):
        return "", (f"「{real}」不在允许的目录里（`files.roots`）。"
                    f"现在允许的是：{'、'.join(r)}。"
                    f"要放开就把它加进 `files.roots`，或者把 `files.roots` 置空（= 全盘）。")
    return real, ""


# ────────────────────────────────────────────────────────────── 确认闸

def _truthy(v):
    """严格判真：**只有布尔 `True` 才算**。`"true"` / `1` / `"yes"` 一律不算。

    这里判的是「要不要先让用户确认」「要不要覆盖已有文件」—— 判错成 False 就等于
    **免确认地覆盖或删除**。同 `redact` 那条口径：写歪的配置只能变成**更严**，
    绝不能变成更松。
    """
    return v is True


def confirm_actions(cfg):
    """要确认的动作集合（**含强制项**）。`overwrite` 也在这里（它是「情况」不是 action）。"""
    raw = _cfg(cfg).get("confirm")
    if isinstance(raw, str):
        acts = [raw]
    elif isinstance(raw, (list, tuple)):
        acts = [str(x).strip() for x in raw if isinstance(x, str) and x.strip()]
    else:
        acts = list(DEFAULT_CONFIRM)       # 缺省 / 写歪了 → 用默认（更严的那份）
    return set(acts) | set(FORCED_CONFIRM)


def warn_forced(cfg):
    """启动用：用户把 `delete` 从 `files.confirm` 里删掉时，明说已**强制加回**。"""
    raw = _cfg(cfg).get("confirm")
    if not isinstance(raw, (list, tuple)):
        return ""
    declared = {str(x).strip() for x in raw if isinstance(x, str)}
    missing = [a for a in FORCED_CONFIRM if a not in declared]
    if not missing:
        return ""
    return (f"`files.confirm` 里没有 {'、'.join(missing)} —— 已**强制加回**："
            f"删除是不可逆的，这一步**不给配置留后门**。")


def _needs_confirm(action, op, cfg):
    """这个动作要不要先让用户回「确认」。

    三种情况要确认：

    1. 它在 `files.confirm` 名单里（默认含 `delete`）；
    2. 它是 `delete` —— **强制**，配置说了不算；
    3. 它是 `write` 且**要覆盖一个已存在的文件**，而 `overwrite` 在名单里
       （默认在）。注意是「目标**确实存在**」才算：对新建文件没什么可覆盖的。
    """
    acts = confirm_actions(cfg)
    if action in acts:
        return True
    if action == "write" and "overwrite" in acts and _truthy(op.get("overwrite")):
        p, err = path_ok(op.get("path"), cfg)
        if not err and p and os.path.exists(p):
            return True
    return False


def _queue_fileop(chat, op, cfg):
    """把一条文件操作登记成待确认（**一个字都不执行**），返回给模型的话。"""
    import agent_tools                      # 延迟导入：这条队列的所有者是 agent_tools
    text = _describe_fileop({"extra": op})
    idx = agent_tools.set_pending(chat, "", "", text,
                                 kind="fileop", extra=dict(op),
                                 ttl=agent_tools.confirm_ttl_of(cfg))
    if idx:
        return (f"**还没有执行。** 这条和菜单里第 {idx} 条一模一样，没有重复登记。"
                f"要执行就回「确认 {idx}」。{agent_tools.dupe_note(idx)}")
    return ("**还没有执行，一个字都没动。** 请让用户回「确认」再执行：\n"
            f"{text}\n"
            f"（这一步不可逆，所以要先确认；用户没回「确认」之前我不会碰它。）")


# ────────────────────────────────────────────────────────────── 小工具

def _limit(v, default, lo=1, hi=1000):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.0f}GB"


def _when(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OSError, ValueError, TypeError):
        return "时间未知"


def _cut(lines, limit, what):
    """裁到 limit 行，裁了**必须明说**还有多少（静默截断是本项目最忌讳的失效）。"""
    if len(lines) <= limit:
        return "\n".join(lines)
    return ("\n".join(lines[:limit])
            + f"\n…（{what}一共 {len(lines)} 项，上面只列了前 {limit} 项；"
              f"还有 {len(lines) - limit} 项没列）")


# ────────────────────────────────────────────────────────────── 读类动作

def _no_path_scope(cfg):
    """不填 `path` 时的回答：把**当前能访问的范围**如实报出来。

    为什么要有这条路：工具指导文本是**启动时定死**的，而 `files.roots` 是配置里
    随时可改的 —— 把范围写死在指导里，改了配置之后模型就会拿着**过期的范围**
    去猜路径。让它现问一次最稳（口径同「注册表是唯一真源」）。
    """
    r = roots(cfg)
    if not r:
        return ("没给 path。当前 `files.roots` 是**空的 = 全盘**（系统目录仍然挡着），"
                "所以要给一个明确的目录，比如用户的桌面、文档、下载或某个盘下的目录。"
                f"\n{describe_scope(cfg)}")
    return (f"没给 path。当前允许的目录是：{'、'.join(r)}。"
            f"要从这些目录里的哪一个开始？\n{describe_scope(cfg)}")


def _do_list(args, cfg):
    raw = str(args.get("path") or "").strip()
    if not raw:
        return _no_path_scope(cfg)
    p, err = path_ok(raw, cfg)
    if err:
        return err
    if os.path.isfile(p):
        return f"「{p}」是个文件，不是目录。要看它的内容请用 action=read。"
    if not os.path.isdir(p):
        return f"没有这个目录：{p}"
    try:
        entries = sorted(os.listdir(p), key=lambda s: s.lower())
    except OSError as e:
        return f"列不出这个目录（{e}）。"

    limit = _limit(args.get("limit"), _DEFAULT_LIST_LIMIT)
    lines = []
    for name in entries:
        full = os.path.join(p, name)
        try:
            if os.path.isdir(full):
                lines.append(f"[目录] {name}/")
            else:
                st = os.stat(full)
                lines.append(f"       {name}  ({_size(st.st_size)}, {_when(st.st_mtime)})")
        except OSError:
            lines.append(f"       {name}  (读不到属性)")
    if not lines:
        return f"这个目录是空的：{p}"
    return f"{p} 下有 {len(lines)} 项：\n" + _cut(lines, limit, "这个目录")


def _do_find(args, cfg):
    raw = str(args.get("path") or "").strip()
    if not raw:
        # ⚠️ **必须有起点**。`files.roots` 空 = 全盘，从这里开始走整个盘会把
        # 收消息那条线程占住好几分钟 —— hook 不支持并发，那就是在卡死微信。
        return _no_path_scope(cfg)
    root, err = path_ok(raw, cfg)
    if err:
        return err
    if not os.path.isdir(root):
        return f"没有这个目录：{root}"
    pat = str(args.get("name") or "").strip()
    if not pat:
        return ("find 要给 name —— 要找的文件名，可以用 * 通配，"
                "例如 `*.pdf` 或 `报告*`。")
    if not any(ch in pat for ch in "*?["):
        pat = f"*{pat}*"                  # 只给关键词就按「包含」理解，别逼用户写通配

    depth = _limit(args.get("depth"), _DEFAULT_FIND_DEPTH, 0, 20)
    limit = _limit(args.get("limit"), _DEFAULT_LIST_LIMIT)
    max_dirs = _limit(_cfg(cfg).get("find_max_dirs"), _DEFAULT_FIND_DIRS, 100, 500000)

    hits, dirs, stopped = [], 0, ""
    stack = [(root, 0)]
    while stack:
        d, dep = stack.pop()
        dirs += 1
        if dirs > max_dirs:
            stopped = (f"（扫到 {max_dirs} 个目录就停了，**没有扫完** —— "
                       f"缩小 path 或 depth 再找一次）")
            break
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if dep < depth:
                                stack.append((e.path, dep + 1))
                        elif fnmatch.fnmatch(e.name.lower(), pat.lower()):
                            # 归属**再判一次**：目录联接/符号链接能让一个「看着在根下」
                            # 的条目其实指向别处（path_ok 会 realpath）。
                            real, _e = path_ok(e.path, cfg)
                            if real:
                                hits.append(real)
                                if len(hits) >= limit:
                                    stopped = (f"（已经够 {limit} 条就停了，"
                                               f"可能还有更多）")
                                    break
                    except OSError:
                        continue
            if stopped and len(hits) >= limit:
                break
        except OSError:
            continue
    if not hits:
        return (f"在 {root} 下（depth≤{depth}）没找到匹配「{pat}」的文件"
                + (f"。{stopped}" if stopped else "。"))
    head = f"在 {root} 下（depth≤{depth}）找到 {len(hits)} 个匹配「{pat}」的文件："
    return head + "\n" + "\n".join(hits) + (f"\n{stopped}" if stopped else "")


def _do_info(args, cfg):
    p, err = path_ok(args.get("path"), cfg)
    if err:
        return err
    if not os.path.exists(p):
        return f"没有这个文件或目录：{p}"
    try:
        st = os.stat(p)
    except OSError as e:
        return f"读不到它的属性（{e}）。"
    kind = "目录" if os.path.isdir(p) else "文件"
    lines = [f"{kind}：{p}",
             f"大小：{_size(st.st_size)}（{st.st_size} 字节）",
             f"修改时间：{_when(st.st_mtime)}",
             f"创建时间：{_when(st.st_ctime)}"]
    if os.path.isfile(p):
        lines.append(f"后缀：{os.path.splitext(p)[1] or '（没有后缀）'}")
    return "\n".join(lines)


def _do_read(args, cfg):
    p, err = path_ok(args.get("path"), cfg)
    if err:
        return err
    if os.path.isdir(p):
        # 如实拒绝并**指路**：不许抛原始异常上去，也不许把目录当文件读出乱码。
        return f"「{p}」是个目录，不是文件。要列目录请用 action=list。"
    if not os.path.isfile(p):
        return f"没有这个文件：{p}"

    cursor = str(args.get("cursor") or "").strip()
    if cursor:
        text, err = file_read.extract_page(path=p, cfg=cfg, cursor=cursor)
    else:
        text, err = file_read.extract(p, cfg)
    if err:
        return err
    if not text:
        return f"「{p}」没读出内容（是空文件，或者格式读不出文字）。"
    return (f"以下是「{p}」的内容"
            f"（按 `file.max_chars` 分页；末尾有 cursor 就说明**还有下一页**，"
            f"用户说「继续」时把它原样填回来）：\n\n{text}")


# ────────────────────────────────────────────────────────────── 写类动作
#
# 这一组**失败时抛 `FilesError`**（而不是返回一句话）—— 因为它们的调用方
# `apply_item` 要如实回报「做成了几条」，靠猜文本会骗过自己。读类动作不需要：
# 它们不改变任何东西，读不出来的那句话本身就是要给用户看的答案。

def _parent_ok(p):
    """目标所在目录建得出来吗。"""
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            raise FilesError(f"建不出目标目录 {d}（{e}）。")


def _need_path(args, cfg, key="path"):
    p, err = path_ok(args.get(key), cfg)
    if err:
        raise FilesError(err)
    return p


def _do_write(args, cfg, mode="w"):
    p = _need_path(args, cfg)
    text = args.get("text")
    if not isinstance(text, str):
        raise FilesError("write 需要 text（要写进去的文本）。**本工具不写二进制。**")
    if os.path.isdir(p):
        raise FilesError(f"「{p}」是个目录，不能当文件写（要建目录请用 action=mkdir）。")
    existed = os.path.exists(p)
    if mode == "w" and existed and not _truthy(args.get("overwrite")):
        # 覆盖是不可逆的（用户口径：读写默认免确认），所以**默认只新建**。
        # 要覆盖必须显式 overwrite=true，而那一步默认进确认名单（见 _needs_confirm）。
        raise FilesError(
            f"「{p}」**已经存在**。write 默认只新建、不覆盖 —— "
            f"要覆盖就明确说一声（overwrite=true），那一步我会先让用户确认。")
    _parent_ok(p)
    try:
        with open(p, mode, encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as e:
        raise FilesError(f"写不进去（{e}）。")
    verb = "已追加" if mode == "a" else ("已覆盖" if existed else "已写入")
    return f"{verb}「{p}」（{len(text)} 字）。"


def _do_append(args, cfg):
    return _do_write(args, cfg, mode="a")


def _do_mkdir(args, cfg):
    p = _need_path(args, cfg)
    if os.path.isdir(p):
        return f"这个目录已经在了：{p}"
    if os.path.isfile(p):
        raise FilesError(f"「{p}」已经是个文件了，建不了同名目录。")
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as e:
        raise FilesError(f"建不出这个目录（{e}）。")
    return f"已建目录：{p}"


def _two_paths(args, cfg, a_key="src", b_key="dst"):
    a, err = path_ok(args.get(a_key), cfg)
    if err:
        raise FilesError(f"{a_key}：{err}")
    b, err = path_ok(args.get(b_key), cfg)
    if err:
        raise FilesError(f"{b_key}：{err}")
    return a, b


def _do_copy(args, cfg):
    src, dst = _two_paths(args, cfg)
    if not os.path.exists(src):
        raise FilesError(f"没有这个源：{src}")
    if os.path.isdir(src):
        raise FilesError("复制**整个目录**这一版还不支持（只复制单个文件）。"
                         "要搬目录请让用户用 run_command（那个每条都要确认），"
                         "或者 action=list 看清单、逐个复制。")
    if os.path.isdir(dst):
        raise FilesError(f"目标是目录（{dst}）—— 请给一个完整的**文件**路径。")
    if os.path.exists(dst):
        # 同「重名不许静默取第一个」：宁可让用户说清，也不静默盖掉一份已存在的文件。
        raise FilesError(f"目标已经存在：{dst} —— 盖掉一份已有文件是不可逆的，所以我不动。"
                         f"让用户先删掉它或换个名字。")
    _parent_ok(dst)
    try:
        shutil.copy2(src, dst)
    except (OSError, shutil.Error) as e:
        raise FilesError(f"复制失败（{e}）。")
    return f"已复制：{src} → {dst}"


def _do_move(args, cfg):
    src, dst = _two_paths(args, cfg)
    if not os.path.exists(src):
        raise FilesError(f"没有这个源：{src}")
    if os.path.isdir(dst):                 # 给的是目录 → 搬进去（保留原文件名）
        dst, err = path_ok(os.path.join(dst, os.path.basename(src)), cfg)
        if err:
            raise FilesError(err)
    if os.path.exists(dst):
        raise FilesError(f"目标已经存在：{dst} —— 盖掉一份已有文件是不可逆的，所以我不动。"
                         f"让用户先删掉它或换个名字。")
    _parent_ok(dst)
    try:
        shutil.move(src, dst)
    except (OSError, shutil.Error) as e:
        raise FilesError(f"移动失败（{e}）。")
    return f"已移动：{src} → {dst}"


def _do_rename(args, cfg):
    p = _need_path(args, cfg)
    if not os.path.exists(p):
        raise FilesError(f"没有这个文件或目录：{p}")
    new = str(args.get("new_name") or "").strip()
    if not new:
        raise FilesError("rename 需要 new_name（新名字，只给名字、不带路径）。")
    if any(ch in new for ch in ("\\", "/", ":")):
        raise FilesError("new_name 只能给**名字**、不能带路径分隔符 —— "
                         "要换目录请用 action=move。")
    dst, err = path_ok(os.path.join(os.path.dirname(p), new), cfg)
    if err:
        raise FilesError(err)
    if os.path.exists(dst):
        raise FilesError(f"已经有一个叫「{new}」的了（{dst}）—— 我不静默盖掉它。")
    try:
        os.rename(p, dst)
    except OSError as e:
        raise FilesError(f"改名失败（{e}）。")
    return f"已改名：{p} → {dst}"


# ────────────────────────────────────────────────────────────── 删除（进回收站）

def _recycle(path):
    """把一个文件/目录移进**回收站**。失败抛 `FilesError`。

    **不做永久删除**（用户 2026-10-04 拍板）。实现用 `ctypes` + `SHFileOperationW`：

    * **零依赖、不起子进程**。不用 `send2trash` / `winshell`：没装，而且新增依赖会撞
      `envsetup.requirements_specs()` 那个坑（写成正式需求行 → `启动助手.bat` 自检要求它
      → 没装的人「装完还是起不来」死循环）。也不用 PowerShell：多一个进程，
      而 `health.py` 那条 PowerShell 路子是给**本地通知**用的。
    * `FOF_ALLOWUNDO` 是**唯一**让删除可恢复的旗标，**不许删**。
    * `FOF_NOCONFIRMATION` 关掉系统弹窗：我们自己的确认闸在前面，
      再弹一个对话框会把**收消息那条线程**挡住。
    """
    import ctypes
    import ctypes.wintypes as wintypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040

    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    # pFrom 是**双 NUL 结尾**的列表（SHFileOperation 的约定）；赋字符串时
    # ctypes 会再补一个 NUL，所以这里只写到单 NUL 就够。
    op.pFrom = os.path.abspath(path) + "\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as e:                                  # pragma: no cover
        raise FilesError(f"调系统回收站接口失败（{type(e).__name__}: {e}）。**文件还在**。")
    if rc != 0:
        raise FilesError(f"删除失败（系统返回 {rc}）。**文件还在**。")
    if op.fAnyOperationsAborted:
        raise FilesError("删除被中止了。**文件还在**。")


def _do_delete(args, cfg):
    p = _need_path(args, cfg)
    if not os.path.exists(p):
        raise FilesError(f"没有这个文件或目录：{p}")
    _recycle(p)
    return f"已删到**回收站**（还能恢复）：{p}"


# 读类：失败也是「一句话」（它们不改任何东西，那句话就是要给用户看的答案）。
# 写/删：失败抛 FilesError（调用方要能可靠区分成败）。
_EXEC = {
    "list": _do_list, "find": _do_find, "info": _do_info, "read": _do_read,
    "write": _do_write, "append": _do_append, "mkdir": _do_mkdir,
    "copy": _do_copy, "move": _do_move, "rename": _do_rename,
    "delete": _do_delete,
}



# ────────────────────────────────────────────────────────────── 工具

_LABEL = {
    "list": "列目录", "find": "搜文件", "info": "看属性", "read": "读文件",
    "write": "写入", "append": "追加", "mkdir": "建目录",
    "copy": "复制", "move": "移动", "rename": "重命名", "delete": "删除",
}


def _clip(s, limit=300):
    s = str(s or "")
    if len(s) <= limit:
        return s
    return s[:limit] + f"…（路径太长已截断，完整 {len(s)} 字）"


def _describe_fileop(item):
    """给编号菜单用的一行。**磁盘路径必须原样显示**。

    为什么和 `kind="file"` 那条（只显示文件名）不一样：那边是**微信里**的文件，
    用户认的是文件名；这边是**用户自己给的盘上路径**，而同一个 basename 可以同时
    出现在十来个目录里 —— 只显示文件名，用户就是在确认一个**自己分辨不出的东西**
    （＝「重名不许静默取第一个」那条铁律在确认菜单上被违反）。
    """
    op = item.get("extra") or {}
    act = str(op.get("action") or "?")
    label = _LABEL.get(act, act)
    if act in ("copy", "move"):
        return f"{label}：「{_clip(op.get('src'))}」→「{_clip(op.get('dst'))}」"
    if act == "rename":
        return f"重命名：「{_clip(op.get('path'))}」→「{op.get('new_name')}」"
    if act in ("write", "append"):
        n = len(op.get("text") or "")
        if act == "write" and _truthy(op.get("overwrite")):
            return (f"**覆盖写入**（会把原来那份盖掉、不可逆）"
                    f"「{_clip(op.get('path'))}」（{n} 字）")
        return f"{label}「{_clip(op.get('path'))}」（{n} 字）"
    if act == "delete":
        return f"删除（**进回收站，还能恢复**）：「{_clip(op.get('path'))}」"
    return f"{label}：「{_clip(op.get('path'))}」"


def apply_item(item, ctx):
    """用户回「确认」之后**真正执行**一条文件操作。返回 `(条数, 错误)`。

    两条硬规矩：

    * **执行时把路径再判一次**。登记时判过，但从登记到用户回「确认」之间配置可能变、
      文件也可能被换成链接指向别处（同 `send_pending` 的 `allowed_dirs` 二次校验）。
      执行器内部会走 `path_ok`，所以这一条是**自动**满足的 —— 关键在于它走的是
      **当前**配置，不是登记时那份快照。
    * **失败绝不报成成功**：写/删的执行器失败时抛 `FilesError`，这里如实转成
      `(0, 原因)`。这一步是不可逆动作，报反了方向比报错更坏。
    """
    op = dict((item or {}).get("extra") or {})
    cfg = (ctx or {}).get("cfg") or {}
    act = str(op.get("action") or "")

    on, note = _enabled_or_note(cfg)
    if not on:
        return 0, note
    if act not in _EXEC:
        return 0, (f"待确认项里的 action「{act}」不认识了（配置换过代？）。**没有执行。**")
    try:
        out = _EXEC[act](op, cfg)
    except FilesError as e:
        return 0, str(e)
    except Exception as e:                                  # pragma: no cover
        return 0, f"执行出错：{type(e).__name__}: {e}"
    # 成功时：改文件的那几个**没什么可说的**（菜单里已经摆明要做什么），返回 None；
    # 读类动作（正常不会进队列，但用户可以在 files.confirm 里加上）把内容原样给出。
    return 1, (out if act in ACTIONS_READ else None)


# ────────────────────────────────────────────────────────────── 工具

def handler(args, ctx):
    """`computer_files` 的处理器。`ctx` 是契约给的只读上下文。"""
    args = args or {}
    ctx = ctx or {}
    cfg = ctx.get("cfg") or {}

    on, note = _enabled_or_note(cfg)
    if not on:
        return note

    action = str(args.get("action") or "").strip()
    if action not in ACTIONS:
        return (f"不认识的 action「{action}」。可用的有：{'、'.join(ACTIONS)}"
                f"（要执行命令请用 run_command —— 本工具**只做文件操作**，"
                f"不执行任何程序）。")

    if _needs_confirm(action, args, cfg):
        # **只登记，一个字都不执行**（同 run_command 的口径）。
        chat = str(ctx.get("chat") or "")
        if not chat:
            # 拿不到会话就登不了待确认（队列是按会话分的）→ **如实拒绝**。
            # 绝不「因为登不上就顺手执行了」—— 那是把确认闸悄悄短路。
            return (f"这一步（{action}）需要用户确认，但这一轮拿不到会话、登不了待确认，"
                    f"所以**没有执行、也没有登记**。请让用户直接在控制会话里说这件事。")
        return _queue_fileop(chat, dict(args), cfg)

    try:
        return _EXEC[action](args, cfg)
    except FilesError as e:
        return str(e)


GUIDANCE = """用户让你看、找、列、改电脑上的文件时用本工具（action 见下）。
- 只用本工具做**文件操作**；要跑命令请用 run_command（那个每条都要用户确认），
  本工具**不执行任何程序**。
- **不确定能访问哪些目录时，先 `action=list` 且不填 path** —— 它会当场告诉你
  当前允许的范围。别猜路径（可能被 `files.roots` / `files.deny` 挡住）。
- 在系统目录（Windows / Program Files / ProgramData）里的一律会被如实拒绝，
  不许改用 run_command 绕过。
- `read` 支持 Office / PDF / 图片 / 音频 / 压缩包 / 邮件 / SQLite 等，长内容会分页：
  结果末尾有 `cursor` 就说明还有下一页，用户说「继续」时把它原样填回来。
- ⚠️ **删除 / 覆盖是不可逆的，要用户回「确认」才做**：这类调用**不会立刻执行**，
  工具只登记一条待确认并把原文摆给用户看；返回里说「还没有执行」时，
  如实告诉用户「还没做、请回确认」，**绝不许说已经删了/已经覆盖了**。
- `delete` 是删到**回收站**（还能恢复），不是永久删除。
- `copy` / `move` / `rename` 的目标**已存在时会如实拒绝**，不会静默盖掉 ——
  让用户先说清是先删还是换名。
- `list` / `find` 有上限，结果里会写明**还有多少没列** —— 照实告诉用户，别说成
  「就这些」。"""


def tool_spec():
    return {
        "name": "computer_files",
        "description": (
            "看、找、列、改**这台电脑上**的文件（列目录 / 按名搜 / 看属性 / 读内容 / "
            "写文本 / 复制 / 移动 / 改名 / 删到回收站）。"
            "用户说「看看我桌面上有什么」「D 盘那个报告在哪」「读一下那个文件」"
            "「把那个文件删了」时用它。"
            "⚠️ 只做文件操作，**不执行任何程序**。⚠️ 删除与覆盖**要用户回「确认」**才做。"
            "不确定能访问哪里时先 `action=list` 且不填 path。"),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS),
                           "description": (
                               "list=列目录 / find=按名搜 / info=看属性 / read=读内容 / "
                               "write=写文本（默认只新建）/ append=追加 / mkdir=建目录 / "
                               "copy=复制文件 / move=移动 / rename=改名 / "
                               "delete=删到回收站（**每次都要用户确认**）")},
                "path": {"type": "string",
                         "description": "要操作的路径（目录或文件）。除 copy/move 外都要给。"},
                "name": {"type": "string",
                         "description": "find 用：要找的文件名，可用 * 通配；只给关键词按「包含」理解"},
                "text": {"type": "string",
                         "description": "write/append 用：要写进去的**文本**（本工具不写二进制）"},
                "overwrite": {"type": "boolean",
                              "description": ("write 覆盖一个**已存在**的文件时**必须**显式给 true；"
                                              "不给就只新建（要覆盖会先让用户确认）")},
                "src": {"type": "string", "description": "copy/move 的源路径"},
                "dst": {"type": "string", "description": "copy/move 的目标路径（可以是目录）"},
                "new_name": {"type": "string",
                             "description": "rename 用：新名字（只给名字、不带路径分隔符）"},
                "limit": {"type": "integer", "description": "最多返回几条，默认 200"},
                "depth": {"type": "integer", "description": "find 往下找几层，默认 3"},
                "cursor": {"type": "string", "description": "read 的下一页游标（上次结果末尾给的那个）"},
            },
            "required": ["action"],
        },
        "handler": handler,
        "guidance": GUIDANCE,
    }


def register(registry=None):
    """按 `plugins` 契约注册本模块的工具**与待确认种类**。导入时自注册。

    待确认种类也走契约（而不是往 `describe_pending` / `send_pending` 里再加两个
    分支）：**插件工具与核心能力共用同一条确认闸**，没有例外通道。
    `key_fields` 就是 `KEY_FIELDS` —— 少了它，「删掉 A」和「删掉 B」会算出同一个
    判重键，第二条被判成重复而不登记，用户照菜单回「确认」时**做掉的是另一件事**。
    """
    reg = registry if registry is not None else plugins.REGISTRY
    reg.register_tool(tool_spec(), source="files")
    reg.register_pending_kind("fileop", _describe_fileop, apply_item,
                              key_fields=list(KEY_FIELDS), source="files")
    return reg


register()
