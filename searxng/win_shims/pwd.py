"""Windows 兼容层：给 SearXNG 补一个 `pwd` 模块。

**为什么需要它**：SearXNG master 的 `searx/valkeydb.py` 第 22 行是 `import pwd`——
`pwd` 是 Unix 专有模块，Windows 上没有，于是**整个 SearXNG 起不来**
（`ModuleNotFoundError: No module named 'pwd'`）。SearXNG 官方本来就没打算原生跑 Windows
（他们走 Docker/WSL），但本机 Docker 官方源不通、WSL 也拿不到权限，所以我们自己补一层。

**它有多小**：`pwd` 在整个 SearXNG 里只被用了一次——
`valkeydb.initialize()` 的**异常分支**里 `pwd.getpwuid(os.getuid()).pw_name`，
只是拿用户名打一条「连不上 valkey」的日志。而我们**根本没配 valkey**
（`valkey.url` 为空时函数在第 44 行就 return 了），所以这段代码**永远不会执行**。
这里仍然把两个函数老实实现，免得哪天真配了 valkey 时异常分支再炸一次。

**用法**：不用安装，只要让 Python 能找到它——`start.bat` 里已经设了
`set PYTHONPATH=%~dp0win_shims`。**别把本文件拷进 .venv\Lib\site-packages**，
那样重建 venv 就丢了、也看不出来源。

**没实现的东西**：`pwd.getpwall` / `getpwnam` 之类**一概没有**——不假装支持。
真用到它们时报的 `AttributeError` 就是它该有的样子。
"""
import getpass
import os


class struct_passwd:
    """和 Unix 那个 `pwd.struct_passwd` 同名同字段（Windows 上填不出真实值的地方给空串）。"""

    def __init__(self, pw_name="", pw_uid=0, pw_gid=0, pw_dir="", pw_shell=""):
        self.pw_name = pw_name
        self.pw_uid = pw_uid
        self.pw_gid = pw_gid
        self.pw_dir = pw_dir
        self.pw_shell = pw_shell

    def __repr__(self):
        return (f"struct_passwd(pw_name={self.pw_name!r}, pw_uid={self.pw_uid}, "
                f"pw_gid={self.pw_gid}, pw_dir={self.pw_dir!r}, pw_shell={self.pw_shell!r})")


def _current_name():
    try:
        return getpass.getuser()
    except Exception:                     # 拿不到就如实给空串，别编一个
        return ""


def getuid():
    """Windows 没有 uid 这个概念，老实返回 0（不是「假装是 root」）。"""
    return 0


def geteuid():
    return 0


def getgid():
    return 0


def getpwuid(uid):
    """只给「当前用户」这一件事；uid 不同也返回同一个（Windows 上无从区分）。"""
    return struct_passwd(pw_name=_current_name(), pw_uid=uid or 0,
                         pw_dir=os.path.expanduser("~"))


def getpwnam(name):
    if name and name == _current_name():
        return getpwuid(0)
    raise KeyError(f"getpwnam(): name not found: {name!r}（Windows 兼容层只有当前用户）")
