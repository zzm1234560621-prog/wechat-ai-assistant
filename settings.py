"""运行期设置：可在微信里用命令改，持久化到 settings.json，覆盖 config.yaml。

这样不用每次去编辑文件，聊天框里发命令即可改配置，重启后仍然生效。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")


def load():
    """读取 settings.json，不存在则返回空 dict。"""
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save(data):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
