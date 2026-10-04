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
# 写类与危险动作由 T7 落地前不存在 —— **宁可工具里没有这个 action，
# 也不许接受一个参数之后再假装成功**（那是最坏的一种「有」）。
ACTIONS = ACTIONS_READ

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


_READ = {"list": _do_list, "find": _do_find, "info": _do_info, "read": _do_read}


# ────────────────────────────────────────────────────────────── 工具

def handler(args, ctx):
    """`computer_files` 的处理器。`ctx` 是契约给的只读上下文。"""
    args = args or {}
    cfg = (ctx or {}).get("cfg") or {}

    on, note = _enabled_or_note(cfg)
    if not on:
        return note

    action = str(args.get("action") or "").strip()
    if action not in ACTIONS:
        return (f"不认识的 action「{action}」。可用的有：{'、'.join(ACTIONS)}"
                f"（要执行命令请用 run_command —— 本工具**只做文件操作**，"
                f"不执行任何程序）。")
    return _READ[action](args, cfg)


GUIDANCE = """用户让你看、找、列电脑上的文件时用本工具（action 见下）。
- 只用本工具做**文件操作**；要跑命令请用 run_command（那个每条都要用户确认），
  本工具**不执行任何程序**。
- **不确定能访问哪些目录时，先 `action=list` 且不填 path** —— 它会当场告诉你
  当前允许的范围。别猜路径（可能被 `files.roots` / `files.deny` 挡住）。
- 在系统目录（Windows / Program Files / ProgramData）里的一律会被如实拒绝，
  不许改用 run_command 绕过。
- `read` 支持 Office / PDF / 图片 / 音频 / 压缩包 / 邮件 / SQLite 等，长内容会分页：
  结果末尾有 `cursor` 就说明还有下一页，用户说「继续」时把它原样填回来。
- `list` / `find` 有上限，结果里会写明**还有多少没列** —— 照实告诉用户，别说成
  「就这些」。"""


def tool_spec():
    return {
        "name": "computer_files",
        "description": (
            "看、找、列**这台电脑上**的文件（列目录 / 按名搜 / 看属性 / 读内容）。"
            "用户说「看看我桌面上有什么」「D 盘那个报告在哪」「读一下那个文件」时用它。"
            "⚠️ 只做文件操作，**不执行任何程序**。不确定能访问哪里时先 `action=list`"
            "且不填 path。"),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS),
                           "description": "list=列目录 / find=按名搜 / info=看属性 / read=读内容"},
                "path": {"type": "string",
                         "description": "要操作的路径（目录或文件）。list/find/info/read 都要。"},
                "name": {"type": "string",
                         "description": "find 用：要找的文件名，可用 * 通配；只给关键词按「包含」理解"},
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
    """按 `plugins` 契约注册本模块的工具。导入时自注册（同 `agent_tools`）。"""
    reg = registry if registry is not None else plugins.REGISTRY
    return reg.register_tool(tool_spec(), source="files")


register()
