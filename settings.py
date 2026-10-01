"""运行期设置：可在微信里用命令改，持久化到 settings.json，覆盖 config.yaml。

这样不用每次去编辑文件，聊天框里发命令即可改配置，重启后仍然生效。
"""
import json
import os
import shutil
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

_BOM = b"\xef\xbb\xbf"      # UTF-8 BOM


def load():
    """读取 settings.json，不存在则返回空 dict。

    **坏文件不许静默当空**：原来 `except: return {}` 让调用方看到的是「配置被清空」，
    api_key / target_chats / 名单全没了却一句告警都没有（CLAUDE.md：静默失效是最大的坑）。
    现在改成：先把坏文件备份成 `settings.json.bad-<时间戳>`，打印明确告警（含备份路径），
    **仍然返回 `{}`** —— 可用性优先，不能因为一个坏文件就把 bot 拦死起不来。

    **BOM 单独处理（真机实测踩到）**：本机这份 `settings.json` 头三个字节就是
    `EF BB BF`（Windows 记事本 / 某些编辑器保存时会加）。`json.load` 遇到 BOM 会直接抛
    `Unexpected UTF-8 BOM`，于是**旧代码每次启动都静默返回 `{}`**，api_key、自动回复名单、
    定时任务全部不生效——而且一点告警都没有。这不是坏文件，是编码声明，必须读出来；
    这种文件第一次 `save()` 之后 BOM 就没了（我们写的是无 BOM 的 UTF-8），
    所以那句提示只会出现一次。
    """
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        # 先把原始字节读全：包在 try 里，这样连「读不动」也能走同一套告警
        with open(SETTINGS_PATH, "rb") as f:
            raw = f.read()
    except OSError as e:
        return _give_up(f"文件读不动（{type(e).__name__}: {e}）")

    text = raw[3:].decode("utf-8", "replace") if raw.startswith(_BOM) else None
    if text is not None:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            return _give_up(f"去掉 UTF-8 BOM 之后还不是合法 JSON（{e}）")
        print("⚠️ settings.json 带 UTF-8 BOM（记事本/某些编辑器保存时会加）。"
              "旧代码会因为这一点把它整个当成坏文件、静默按空配置跑。"
              "这里已经读出来了；下次任何一次 save() 都会把它重写成不带 BOM 的格式。",
              flush=True)
        if not isinstance(data, dict):
            return _give_up(f"顶层不是对象，是 {type(data).__name__}")
        return data

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        return _give_up(f"{type(e).__name__}: {e}")
    if not isinstance(data, dict):
        return _give_up(f"顶层不是对象，是 {type(data).__name__}")
    return data


def _give_up(why):
    """坏配置：备份 + 明确告警 + 返回空 dict（可用性优先，但不静默）。"""
    backup = f"{SETTINGS_PATH}.bad-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        shutil.copy2(SETTINGS_PATH, backup)
        where = f"已备份到 {backup}"
    except OSError as be:
        where = f"备份也失败了（{be}），原文件先别动，请手工检查 {SETTINGS_PATH}"
    print(f"⚠️ settings.json 读不出来（{why}）。{where}。"
          f"本次按**空配置**继续运行，所以 /api、/auto 名单、定时任务等设置看起来"
          f"都像是没了——它们还在那个文件（和备份）里，修好后重启即可恢复。", flush=True)
    return {}


def save(data):
    """原子写：先写同目录临时文件，再 os.replace 顶上去。

    原来的 `open(w)` 直接覆写有两个真实后果：
      * 写到一半断电/被杀 → 留下半截 JSON，下次 load() 直接判定「配置坏了」；
      * 覆盖瞬间进程被杀 → 原文件已被截断，**旧配置也没了**。
    `os.replace` 在同一分区上是原子的（`bot.py` 写对话记忆就是这么做的），
    所以要么是完整的旧文件、要么是完整的新文件，不存在第三种。
    """
    tmp = f"{SETTINGS_PATH}.tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            # 让数据真正落到盘上再替换，否则断电时 os.replace 之后文件仍可能是空的
            os.fsync(f.fileno())
        os.replace(tmp, SETTINGS_PATH)
    finally:
        # 失败时（如磁盘满）不许把临时文件留在目录里冒充配置
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def set_value(key, value):
    """设置某个键（value=None 视为删除该覆盖项）。"""
    data = load()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    save(data)
    return data


def effective(base_cfg):
    """合并 config.yaml 与 settings.json，settings 优先（None 项忽略）。

    dict 值做**一层深合并**：像 auto_reply / agent 这种分段配置，settings.json
    里只存被命令改过的键，config.yaml 里的 persona_* 之类默认值仍然生效。
    整段替换的话，用户在 settings.json 里存过一次，之后改 config.yaml 就再也
    不生效了（会被那份旧副本盖住）。
    """
    merged = dict(base_cfg)
    for k, v in load().items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            sub = dict(merged[k])
            sub.update({sk: sv for sk, sv in v.items() if sv is not None})
            merged[k] = sub
        else:
            merged[k] = v
    return merged
