"""主程序：连接微信，在指定聊天里用 AI 回复（实时读历史 + 聊天框配置）。

流程：
  1. 连上微信，拿到自己的 wxid
  2. 开启消息接收
  3. 收到命令（/ 开头）-> 改配置并持久化
     收到问题 -> 实时查微信数据库历史 -> 调大模型回复

依赖 wcferry（必须匹配微信版本）。用法：python bot.py，Ctrl+C 退出。
"""
import json
import os
import re
import socket
import sys
import time
import traceback
from datetime import datetime

import yaml

import agent_tools
import auto_reply
import live_history
import settings
import providers
import scheduler
import watch
from llm import ChatLLM
from history import HistoryStore
from live_history import (
    all_contacts,
    extract_keywords,
    query_contact_history,
    recent_messages,
    search_history,
    set_rescan_interval,
    set_self_wxid,
)
from aixed_api import AixedClient, AixedError

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

# 单实例锁占用的回环端口。换成别的数就行（别撞 hook 的 30001/29999）。
INSTANCE_PORT = 39001
_instance_lock = None


class _Tee:
    """把 stdout/stderr 同时写到控制台和 bot.log，方便后台无窗口运行时排查。

    每次写入都 flush：文件对象默认是块缓冲的，后台跑时日志会一直不落盘。
    """
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:
                pass
        self.flush()

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


def setup_logging():
    try:
        logf = open(LOG_PATH, "a", encoding="utf-8")
    except OSError:
        return
    sys.stdout = _Tee(sys.stdout, logf)
    sys.stderr = _Tee(sys.stderr, logf)


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def mask(key):
    if not key:
        return ""
    key = str(key)
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "****" + key[-4:]


def make_llm(cfg):
    """按当前配置重建 LLM；没配 key 时返回 None。"""
    key = cfg.get("api_key") or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    return ChatLLM(
        model=cfg.get("model"),
        api_key=key,
        base_url=cfg.get("base_url"),
        max_tokens=cfg.get("max_tokens", 2000),
        temperature=cfg.get("temperature", 0.7),
        provider=cfg.get("provider", "anthropic"),
    )


HELP_TEXT = (
    "可用命令（发到这个聊天即可）：\n"
    "/provider       列出可选服务商（DeepSeek / Claude / 通义 / Kimi / GLM …）\n"
    "/provider <编号> 选一个服务商，自动配好协议+接口+模型\n"
    "/api <key>      设置 API Key（会自动测一次连通性）\n"
    "/api clear      清除 API Key\n"
    "/baseurl <url>  单独改接口地址\n"
    "/model <id>     单独改模型\n"
    "/temp <0-1>     设置 temperature，如 /temp 0.7\n"
    "/addchat <wxid> 添加要响应的聊天\n"
    "/delchat <wxid> 移除聊天\n"
    "/status         查看当前配置\n"
    "/help           显示本帮助\n"
    "\n"
    "—— 自动回复（让 AI 代替我本人回某个人）——\n"
    "下面这些也能直接用大白话说，助手会自己调用工具：\n"
    "   「以后张三的消息你帮我回」 「别自动回李四了」 「发之前先给我看一眼」\n"
    "/auto                    看开关、审核和名单\n"
    "/auto on | off           开 / 关（关掉就你自己回）\n"
    "/auto add <昵称|wxid|roomid> [self|assistant]  加入名单\n"
    "/auto del <昵称|wxid>    移出名单\n"
    "/auto mode <谁> self|assistant   改人设\n"
    "/auto review on|off [谁] 开审核（草稿先发你，回「确认」才发）\n"
    "/auto ctx <1~30>         上下文条数\n"
    "\n"
    "—— 定时任务（到点自动给对方发消息）——\n"
    "也可以直接说「明天9点提醒我给张三发…」。\n"
    "/定时 —— 看列表\n"
    "/定时 加 <时间> <对象> <内容> —— 加一个发文本的\n"
    "/定时 加提问 <时间> <问题> —— 到点让助手答这个问题，答案发回本会话\n"
    "/定时 加通话 <时间> <对象> —— 加一个打电话的（该功能还没打通）\n"
    "/定时 删|开|关 <编号|all> —— 删 / 恢复 / 暂停\n"
    "时间写法：9:00=每天，明天9:00=只一次，每周一 9:00=每周，每30分钟=每隔一段\n"
    "\n"
    "—— 盯着某人（他发消息就通知我，不回他）——\n"
    "也可以直接说「张三发消息告诉我一声」。\n"
    "/盯着 —— 看名单\n"
    "/盯着 加 <昵称|wxid|roomid> —— 加进来\n"
    "/盯着 删 <昵称|wxid> —— 移出去\n"
    "/盯着 开|关 —— 总开关\n"
    "（和 /auto 互斥：那个是代你回对方，这个是只告诉你不回）"
)


def handle_command(text, wcf, cfg, live_ok, contacts=None):
    """识别 / 开头的命令。返回 (回复文本, 是否改了配置)；非命令返回 (None, False)。"""
    t = text.strip()
    if not t.startswith("/"):
        return None, False
    parts = t.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/帮助"):
        return HELP_TEXT, False

    if cmd == "/api":
        if not arg or arg.lower() in ("clear", "清空", "清除"):
            settings.set_value("api_key", None)
            return "API Key 已清除。", True
        # 容错：key 本身就以 sk- 开头，用户手打一遍再粘贴就会变成 sk-sk-...
        key = arg.strip()
        while key.startswith("sk-sk-"):
            key = key[3:]
        settings.set_value("api_key", key)

        head = (f"API Key 已设置（{mask(key)}）。\n"
                f"当前：{cfg.get('provider', 'anthropic')} | {cfg.get('model')}")
        # 立刻测一次，省得等到提问才发现配错
        try:
            probe_cfg = dict(cfg)
            probe_cfg["api_key"] = key
            probe = make_llm(probe_cfg)
            out = probe.chat("你是测试助手。", [{"role": "user", "content": "只回两字：成功"}])
            return head + f"\n✅ 连通性测试通过（模型回了「{out.strip()[:12]}」）\n现在可以直接发消息提问了。", True
        except Exception as e:
            return head + f"\n❌ 连通性测试失败：{str(e)[:180]}\n检查 key / 接口地址 / 模型名。", True

    if cmd in ("/provider", "/协议", "/服务商"):
        if not arg:
            lines = ["选一个服务商，然后发 /provider <编号>：", ""]
            lines += providers.menu_lines("  ")
            lines += ["", "选完再发 /api <你的key> 设置密钥即可。",
                      "（也可以直接 /provider anthropic 或 /provider openai 只切协议）"]
            return "\n".join(lines), False

        preset = providers.by_index(arg)
        if preset is not None:
            data = settings.load()
            data.update({
                "provider": preset["provider"],
                "base_url": preset["base_url"],
                "model": preset["model"],
            })
            settings.save(data)
            return (f"已切到 {preset['short']}：\n"
                    f"  协议 {preset['provider']}\n"
                    f"  接口 {preset['base_url']}\n"
                    f"  模型 {preset['model']}\n\n"
                    f"下一步：发 /api <你的key> 设置密钥。"), True

        v = arg.lower()
        if v in ("anthropic", "openai"):
            settings.set_value("provider", v)
            return (f"协议已改为 {v}。\n"
                    f"再用 /baseurl <url> 和 /model <模型名> 补齐另外两项。"), True
        return f"没有编号 {arg} 的服务商。发 /provider 看列表。", False

    if cmd == "/model":
        if not arg:
            return f"当前模型：{cfg.get('model')}", False
        settings.set_value("model", arg)
        return f"模型已改为 {arg}。", True

    if cmd in ("/provider", "/协议"):
        if not arg:
            return f"当前协议：{cfg.get('provider', 'anthropic')}", False
        v = arg.lower()
        if v not in ("anthropic", "openai"):
            return "只支持 anthropic 或 openai。", False
        settings.set_value("provider", v)
        tip = ""
        if v == "openai" and not cfg.get("base_url"):
            tip = "\n注意：openai 协议还需要接口地址，发 /baseurl <url> 设置。"
        return f"协议已改为 {v}。{tip}", True

    if cmd in ("/baseurl", "/接口"):
        if not arg:
            return f"当前接口地址：{cfg.get('base_url') or '（默认）'}", False
        settings.set_value("base_url", arg)
        return f"接口地址已改为 {arg}。", True

    if cmd in ("/temp", "/温度"):
        try:
            v = float(arg)
        except ValueError:
            return "temperature 要填数字，如 /temp 0.7", False
        if not (0.0 <= v <= 1.0):
            return "temperature 范围是 0~1", False
        settings.set_value("temperature", v)
        return f"temperature 已设为 {v}。", True

    if cmd in ("/addchat", "/加聊天"):
        if not arg:
            return "用法：/addchat <wxid或roomid>", False
        chats = list(cfg.get("target_chats", []))
        if arg in chats:
            return f"{arg} 已在目标列表里。", False
        chats.append(arg)
        settings.set_value("target_chats", chats)
        return f"已添加目标聊天 {arg}。", True

    if cmd in ("/delchat", "/删聊天"):
        if not arg:
            return "用法：/delchat <wxid或roomid>", False
        chats = list(cfg.get("target_chats", []))
        if arg not in chats:
            return f"{arg} 不在目标列表里。", False
        chats.remove(arg)
        settings.set_value("target_chats", chats)
        return f"已移除 {arg}。", True

    if cmd == "/status":
        key = cfg.get("api_key") or os.getenv("ANTHROPIC_API_KEY")
        try:
            who = wcf.get_self_wxid() or ""
        except Exception:
            who = ""
        # aixed 后端的 GetSelfProfile 可能取不到（实测返回空），退回配置里的值
        who = who or cfg.get("self_wxid") or "（未知，请在 config.yaml 里设 self_wxid）"
        lines = [
            f"你的 wxid: {who}",
            f"后端: {cfg.get('provider', 'anthropic')}  |  模型: {cfg.get('model')}",
            f"temperature: {cfg.get('temperature')}",
            f"API Key: {mask(key) if key else '未设置'}",
            f"目标聊天: {cfg.get('target_chats')}",
            f"实时查库: {'开启' if live_ok else '关闭（静态导出模式）'}",
        ]
        return "\n".join(lines), False

    if cmd in ("/auto", "/自动回复"):
        # 重新读一次配置再处理：连着发几条 /auto 时，传进来的 cfg 还是上一条
        # 命令之前的快照，直接用它会在「读-改-写」里丢掉上一次的改动。
        return auto_reply.handle_command(arg, settings.effective(load_config()), wcf,
                                         can_lookup=live_ok)

    if cmd in ("/定时", "/schedule", "/提醒"):
        # 同 /auto：重新读一次，避免连着发命令时丢掉上一条的改动。
        fresh = settings.effective(load_config())

        def resolve(who):
            # 走和 agent 工具**同一套**解析：重名不静默取第一个
            return agent_tools.resolve_one(contacts, who,
                                           fresh.get("self_wxid", ""), wcf)

        return scheduler.handle_command(arg, fresh, resolve, can_lookup=live_ok)

    if cmd in ("/盯着", "/watch", "/盯"):
        # 同 /auto：重新读一次，避免连着发命令时丢掉上一条的改动。
        fresh = settings.effective(load_config())

        def resolve_watch(who):
            return agent_tools.resolve_one(contacts, who,
                                           fresh.get("self_wxid", ""), wcf)

        return watch.handle_command(arg, fresh, resolve_watch)

    return f"未知命令 {cmd}。发 /help 查看帮助。", False


def find_mentions(query, contacts):
    """在问题里找出提到的好友（昵称/备注/微信号，长度>=2 的才匹配）。

    按命中名字的长度排序：名字越长越具体，优先用「张三丰」而不是「张三」。
    """
    hits = []
    for c in contacts:
        best = 0
        for key in (c.get("remark"), c.get("name"), c.get("alias")):
            if key and len(str(key)) >= 2 and str(key) in query:
                best = max(best, len(str(key)))
        if best:
            hits.append((best, c))
    hits.sort(key=lambda x: -x[0])
    return [c for _, c in hits]


def _msg_speaker(m, names):
    """一条消息的说话人**显示名**。

    以前这里直接用 talker，也就是 wxid / roomid 原样塞给模型——模型看到的
    是 `wxid_h8i9j0k1l2m3n4: 到小区了`，答出来自然也是一串 id。
    群聊标成「群名/发言人」，单聊就是对方的名字。
    """
    talker = str(m.get("talker") or "")
    tname = names.get(talker) or talker
    group = auto_reply.is_group(talker)
    who = agent_tools.speaker_of(m, names, tname, group)
    return f"{tname}/{who}" if group and who != tname else who


def build_user_prompt(query, wcf, contacts, cfg, static_history, live_ok):
    """优先实时查库，查不到或接口缺失时退回静态导出。

    注意：**检索历史时要排除目标聊天本身**。文件传输助手既是提问的地方、
    又被当成可检索的历史，会把它自己过往的回答也喂回去（自聊会话里
    所有消息的 is_self 都是 1），导致模型自我指涉、把「我和助手的对话」
    误当成「我的聊天记录」。
    """
    parts = []
    targets = {str(t) for t in (cfg.get("target_chats") or [])}
    names = auto_reply.contact_names(contacts)

    def keep(talker):
        return str(talker) not in targets

    if live_ok:
        mentions = find_mentions(query, contacts)
        if mentions:
            parts.append("【你提到的联系人，最近的历史对话】")
            for c in mentions[:3]:
                name = c.get("remark") or c.get("name") or c.get("wxid")
                msgs = query_contact_history(
                    wcf, c["wxid"], limit=cfg.get("recent_messages", 30)
                )
                parts.append(f"--- 与 {name}（{c['wxid']}）---")
                for m in msgs:
                    who = "我" if m["is_self"] else name
                    parts.append(f"[{m['time']}] {who}: {m['content']}")

        terms = extract_keywords(query)
        if terms:
            seen = set()
            for term in terms[:4]:
                for m in search_history(wcf, term, limit=8):
                    if not keep(m.get("talker")):
                        continue
                    key = (m["time"], m["content"])
                    if key in seen:
                        continue
                    seen.add(key)
                    who = _msg_speaker(m, names)
                    parts.append(f"[{m['time']}] {who}: {m['content']}")
        else:
            # 问句里没有可检索的关键词（例如「最近聊了什么」），直接给最近的聊天
            parts.append("【最近的聊天记录】")
            for m in recent_messages(wcf, limit=cfg.get("recent_messages", 30)):
                if not keep(m.get("talker")):
                    continue
                who = _msg_speaker(m, names)
                parts.append(f"[{m['time']}] {who}: {m['content']}")
    else:
        hits = static_history.search(query, cfg.get("search_topk", 8))
        parts.append("【相关历史片段（静态导出）】")
        for m in hits:
            parts.append(f"[{m.get('time','?')}] {m.get('sender','?')}: {HistoryStore.text(m)}")

    if not parts:
        parts.append("（历史记录里没查到相关内容）")

    parts.append("")
    parts.append(f"【用户的问题】{query}")
    return "\n".join(parts)


# ============================================================
#  Agent：带工具调用的问答循环
# ============================================================

# 用户回哪些词算「确认发送」
_CONFIRM_WORDS = {"确认", "确定", "确认发送", "可以发", "发吧", "发送", "ok", "yes", "y"}

# 自动回复草稿只认这几个明确的词：草稿是要发给**别人**的，在控制会话里
# 随口一句「ok」「发送」就发出去太危险，所以比 agent 的确认词严格得多。
_STRICT_CONFIRM = {"确认", "确定", "确认发送"}

_CANCEL_WORDS = {"不发", "取消", "算了", "别发", "不发送"}


def agent_enabled(cfg):
    return bool((cfg.get("agent") or {}).get("enabled", False))


def _norm_word(text):
    return str(text).strip().lower().rstrip("。！!~ ")


def is_confirm(text):
    return _norm_word(text) in _CONFIRM_WORDS


def is_strict_confirm(text):
    return _norm_word(text) in _STRICT_CONFIRM


def is_cancel(text):
    return _norm_word(text) in _CANCEL_WORDS


# ============================================================
#  短期对话记忆：同一个控制会话里记住最近几轮
# ============================================================

# {chat: {"ts": 最后活动时间, "turns": [{"role","content"}, ...]}}
# 只记**原始提问**和**最终答复**。绝不记 build_user_prompt 里那段检索结果——
# 那段每轮都要重算，记下来等于每轮把整块历史重发一遍，token 直接爆。
#
# 落盘到 data/dialog.json（data/ 在 .gitignore 里）：以前只在内存里，
# 重启一次「刚才那个」就接不上了。延迟加载，第一次用到才读盘。
_DIALOG = None
DIALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dialog.json")
# 盘上的硬上限：超过这么久没动静的会话直接扔掉。真正的过期判断用
# agent.dialog_ttl（默认 900 秒），这里只是个防无限增长的地板。
_DIALOG_KEEP_SECONDS = 7 * 24 * 3600
_DIALOG_MAX_CHATS = 50


def _dialog_load():
    """读一次盘，之后走内存。文件坏了就当空的——记忆文件不该挡住启动。"""
    global _DIALOG
    if _DIALOG is not None:
        return _DIALOG
    _DIALOG = {}
    try:
        with open(DIALOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _DIALOG
    if not isinstance(data, dict):
        return _DIALOG
    cutoff = time.time() - _DIALOG_KEEP_SECONDS
    for k, v in data.items():
        if not isinstance(v, dict) or not isinstance(v.get("turns"), list):
            continue
        try:
            ts = float(v.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff or not v["turns"]:
            continue
        _DIALOG[str(k)] = {"ts": ts, "turns": v["turns"]}
    # 会话太多就留最近用过的那些
    if len(_DIALOG) > _DIALOG_MAX_CHATS:
        keep = sorted(_DIALOG.items(), key=lambda kv: -kv[1]["ts"])[:_DIALOG_MAX_CHATS]
        _DIALOG.clear()
        _DIALOG.update(keep)
    return _DIALOG


def _dialog_save():
    """先写临时文件再替换：直接覆盖时半截崩了，整份记忆就没了。"""
    if _DIALOG is None:
        return
    try:
        os.makedirs(os.path.dirname(DIALOG_PATH), exist_ok=True)
        tmp = DIALOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_DIALOG, f, ensure_ascii=False)
        os.replace(tmp, DIALOG_PATH)
    except OSError:
        # 记忆写不进去不是致命问题，但得让人看得见
        traceback.print_exc()


def _dialog_cfg(cfg):
    a = cfg.get("agent") or {}
    try:
        turns = int(a.get("dialog_turns", 6))
    except (TypeError, ValueError):
        turns = 6
    try:
        ttl = float(a.get("dialog_ttl", 900))
    except (TypeError, ValueError):
        ttl = 900.0
    return max(0, turns), max(0.0, ttl)


def dialog_history(chat, cfg):
    """该会话之前的对话（中立格式），没有就返回 []。

    过期的上下文比没有更糟——模型会把几天前的话当成「刚才说的」。
    """
    turns, ttl = _dialog_cfg(cfg)
    store = _dialog_load()
    rec = store.get(str(chat))
    if not rec or turns <= 0:
        return []
    if ttl and (time.time() - rec["ts"]) > ttl:
        store.pop(str(chat), None)
        _dialog_save()
        return []
    return list(rec["turns"])


def dialog_append(chat, role, content, cfg):
    """把一轮问答续进记忆，超过上限就从头丢。"""
    turns, _ = _dialog_cfg(cfg)
    if turns <= 0:
        return
    text = str(content or "").strip()
    if not text:
        return
    store = _dialog_load()
    rec = store.setdefault(str(chat), {"ts": 0.0, "turns": []})
    rec["turns"].append({"role": role, "content": text})
    rec["ts"] = time.time()
    keep = turns * 2            # 一轮 = 一问一答
    if len(rec["turns"]) > keep:
        del rec["turns"][:-keep]
    _dialog_save()


def dialog_forget(chat):
    store = _dialog_load()
    if store.pop(str(chat), None) is not None:
        _dialog_save()


def run_agent(llm, system, prompt, wcf, contacts, cfg, chat, self_wxid="",
              cfg_provider=None, history=None):
    """带工具的问答循环。返回 (最终要回复的文本, 工具是否改动了配置)。

    hook 不能并发查询，所以工具是**串行**执行的；查询次数由
    agent_tools.ToolBox 内部的预算控制（agent.max_queries）。

    第二个返回值是给主循环用的：auto_reply 工具改了配置后，主循环要重建
    自己的 auto_on/auto_recs，否则「以后张三的消息你帮我回」要重启才生效。

    history 是该会话之前的几轮对话（见 dialog_history），拼在本轮提问前面，
    这样「刚才那个」「再帮我问他一句」才接得上。
    """
    agent_cfg = cfg.get("agent") or {}
    max_rounds = max(1, int(agent_cfg.get("max_rounds", 3)))
    box = agent_tools.ToolBox(wcf, cfg, contacts, self_wxid, chat, cfg_provider)

    messages = list(history or []) + [{"role": "user", "content": prompt}]
    last_text = ""
    for _ in range(max_rounds):
        result = llm.chat_with_tools(system, messages, agent_tools.TOOLS)
        last_text = result.text or last_text
        if not result.tool_calls:
            return result.text or "（模型没有返回内容）", box.cfg_changed

        messages.append({
            "role": "assistant",
            "content": result.text,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments}
                           for c in result.tool_calls],
        })
        for c in result.tool_calls:
            out = box.run(c.name, c.arguments)
            print(f"[bot] 工具 {c.name} {json.dumps(c.arguments, ensure_ascii=False)[:70]} -> {out[:70]}")
            messages.append({"role": "tool", "tool_call_id": c.id,
                             "name": c.name, "content": out})

    # 轮次用完：再要一次纯文本答复
    messages.append({"role": "user",
                     "content": "（工具调用轮次已用完，请直接给出最终答复，不要再调用工具。）"})
    try:
        final = llm.chat_with_tools(system, messages, agent_tools.TOOLS)
        return (final.text or last_text or "（工具调用次数用完了，没能给出答复）",
                box.cfg_changed)
    except Exception:
        return last_text or "（工具调用次数用完了，没能给出答复）", box.cfg_changed


# 自己刚发出去的回复，用来防止「自聊模式下回复又被当成新消息」造成死循环
_SENT_RECENT = {}
_SENT_TTL = 300.0


def remember_sent(text):
    now = time.time()
    _SENT_RECENT[str(text).strip()] = now
    for k, t in list(_SENT_RECENT.items()):
        if now - t > _SENT_TTL:
            _SENT_RECENT.pop(k, None)


def is_own_reply(text):
    """这条是不是我们自己刚发出去的回复。"""
    t = _SENT_RECENT.get(str(text).strip())
    return t is not None and (time.time() - t) < _SENT_TTL


# ============================================================
#  自动回复：代替我本人在指定会话里回消息
# ============================================================

# 每个自动回复会话上次自动回复的时间，用来做 min_gap 冷却
_LAST_AUTO = {}


def auto_due(chat, min_gap):
    """距上次自动回复该会话是否已超过 min_gap 秒。

    min_gap 兜底按 1 秒算：设成 0 会让「自己发出的回复又被当成新消息」成环。
    """
    try:
        gap = max(1.0, float(min_gap))
    except (TypeError, ValueError):
        gap = 6.0
    return (time.time() - _LAST_AUTO.get(chat, 0.0)) >= gap


def do_auto_reply(wcf, llm, cfg, chat, rec, contacts, control_chat, send):
    """读上下文 → 生成 → 直接发给对方，或转成待审核草稿。返回是否产生了动作。"""
    if llm is None:
        print("[bot] 自动回复跳过：还没设置 API Key。")
        return False

    ar = cfg.get("auto_reply") or {}
    group = auto_reply.is_group(chat)
    label = str(rec.get("name") or chat)

    msgs = query_contact_history(wcf, chat, limit=ar.get("context_messages", 20))
    if not msgs:
        print(f"[bot] 自动回复跳过：{label} 没查到上下文。")
        return False

    reply = auto_reply.make_reply(llm, rec, msgs,
                                  auto_reply.contact_names(contacts), cfg, group)
    if not reply:
        print(f"[bot] 自动回复静默（{'群聊判定不该接' if group else '清洗后为空'}）: {label}")
        return False

    _LAST_AUTO[chat] = time.time()
    if auto_reply.review_on(rec, cfg):
        agent_tools.set_pending(control_chat, chat, label, reply, kind="auto")
        send(f"【自动回复待确认】给 {label}：\n{reply}\n\n"
             f"回「确认」发出，回「不发」取消。", control_chat)
        print(f"[bot] 自动回复 -> 待审核 {label}: {reply[:50]}")
        return True

    wcf.send_text(reply, chat)
    remember_sent(reply)
    print(f"[bot] 自动回复 -> {label}: {reply[:50]}")
    return True


def connect_wcferry():
    """连 wcferry（注入模式）。微信没启动时重试等待，最多约 5 分钟。失败返回 None。"""
    from wcferry import Wcf

    print("[bot] 正在连接微信（wcferry 注入模式）...")
    for _ in range(1, 31):
        try:
            wcf = Wcf()
            print(f"[bot] 自己的 wxid = {wcf.get_self_wxid()}")
            if wcf.enable_receiving_msg():
                return wcf
            print("[bot] 开启消息接收失败，10 秒后重试 ...")
        except Exception as e:
            print(f"[bot] 连接失败：{e}，10 秒后重试 ...")
        time.sleep(10)
    return None


def connect_aixed(base_url):
    """连 aixed/WeChat-Hook 起的本地 HTTP 服务。失败返回 None。"""
    print(f"[bot] 正在连接 aixed HTTP 服务 {base_url} ...")
    client = AixedClient(base_url)
    for _ in range(1, 31):
        ok, info = client.ping()
        if ok:
            print(f"[bot] 自己的 wxid = {info}")
            return client
        print(f"[bot] {info}，10 秒后重试 ...")
        time.sleep(10)
    return None


class _Ticker:
    """把定时任务的 tick 节流到最多 min_gap 秒一次。

    两条收消息通路（轮询 / wcferry 阻塞读）都在里面调 tick：这样定时任务
    永远跑在**同一条线程**上，绝不会和收消息并发碰 hook。
    """

    def __init__(self, fn, min_gap=1.0):
        self.fn = fn
        self.min_gap = min_gap
        self.last = 0.0

    def __call__(self):
        if self.fn is None:
            return
        now = time.monotonic()
        if now - self.last < self.min_gap:
            return
        self.last = now
        try:
            self.fn()
        except Exception:
            # 定时任务炸了绝不能把收消息的主循环带下去
            traceback.print_exc()


def iter_wcferry_messages(wcf, tick=None):
    """wcferry 的 get_msg 是阻塞的，直接 yield。"""
    tick = tick or (lambda: None)
    while True:
        tick()
        try:
            msg = wcf.get_msg()
        except Exception:
            msg = None
        if msg is None:
            time.sleep(0.05)
            continue
        yield msg


def iter_aixed_messages(client, interval, tick=None):
    """aixed 没有收消息接口，只能轮询数据库拿新消息。"""
    tick = tick or (lambda: None)
    cursor, seen = client.prime()
    print(f"[bot] 轮询模式：游标 = {cursor}，间隔 {interval}s")
    polls = 0
    while True:
        # 每一轮轮询之前先跑一次定时任务：空闲时这个循环每 interval 秒转一圈，
        # 所以定时精度就是 poll_interval（默认 5 秒）。
        tick()
        msgs = []
        try:
            msgs, cursor, seen = client.poll_messages(since=cursor, seen=seen)
        except AixedError as e:
            print(f"[bot] 轮询出错：{e}")
        except Exception:
            # 以前这里只捕 AixedError，别的异常会被静默吞掉，排查时很难受
            traceback.print_exc()
        polls += 1
        if polls % 30 == 0:
            errs = live_history.poll_errors()
            extra = ""
            if errs:
                names = "、".join(f"{k}({v[1]}次)" for k, v in errs.items())
                extra = f"  ⚠️ 分片查询失败：{names}"
            print(f"[bot] 轮询心跳 #{polls}，游标={cursor}{extra}")
        for m in msgs:
            yield m
        if not msgs:
            time.sleep(interval)


def acquire_single_instance():
    """占住一个回环端口当单实例锁；已经有实例在跑就返回 False。

    项目有三条入口都会起 bot.py（助手.bat 的菜单、启动助手.bat、开机自启注册表），
    同时跑两个会并发打 hook —— 实测会把微信搞崩（NULL 解引用，报 Weixin.dll）。

    用端口而不是锁文件：进程崩溃或被强杀时操作系统自动释放，不会留下要手动清的残留。
    注意**不能**设 SO_REUSEADDR —— Windows 下它反而允许第二个 socket 绑同一地址，锁就形同虚设。
    """
    global _instance_lock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        s.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        # 只 bind 不 listen 的话 netstat 看不到这个端口，下面那句排查提示就成了废话
        s.listen(1)
    except OSError as e:
        s.close()
        print(f"[bot] 已经有一个助手在跑了（回环端口 {INSTANCE_PORT} 被占用：{e}）。")
        print("[bot] 同时跑两个会并发查数据库，实测会把微信搞崩，已拒绝启动。")
        print(f"[bot] 看是谁占着：netstat -ano | findstr {INSTANCE_PORT}")
        return False
    _instance_lock = s  # 存成全局，别让 GC 把 socket 回收了
    return True


def main():
    setup_logging()
    if not acquire_single_instance():
        sys.exit(1)
    base_cfg = load_config()
    cfg = settings.effective(base_cfg)

    llm = make_llm(cfg)
    history_file = cfg.get("history_file", "data/history.jsonl")
    if not os.path.isabs(history_file):
        history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), history_file)
    static_history = HistoryStore(history_file)

    # 连接微信；后台自启时微信可能还没启动，两种后端都会重试等待（最多约 5 分钟）
    backend = cfg.get("backend", "wcferry")
    poll_interval = cfg.get("poll_interval", 2)
    if backend == "aixed":
        wcf = connect_aixed(cfg.get("aixed_base_url", "http://127.0.0.1:8080"))
        if wcf is None:
            print("[bot] 连不上 aixed 服务。请确认微信已启动、version.dll 已加载、aixed_base_url 端口正确。")
            sys.exit(1)
    else:
        wcf = connect_wcferry()
        if wcf is None:
            print("[bot] 无法连接微信。请确认微信已登录且版本与 wcferry 匹配。")
            sys.exit(1)

    live_ok = hasattr(wcf, "query_sql") or hasattr(wcf, "exec_db_query")

    # 微信 4.x 判断「哪条是我发的」需要自己的 wxid
    self_wxid = str(cfg.get("self_wxid") or "")
    if not self_wxid:
        try:
            self_wxid = wcf.get_self_wxid() or ""
        except Exception:
            self_wxid = ""
    set_self_wxid(self_wxid)
    # fts 分片探测为空时自动重扫 hook 的间隔；0 = 关闭（session.db 兜底仍在）
    set_rescan_interval((cfg.get("agent") or {}).get("fts_rescan_interval", 300))
    if self_wxid:
        print(f"[bot] 自己的 wxid = {self_wxid}")
    else:
        print("[bot] 警告：拿不到自己的 wxid，历史里将无法区分『我』和『对方』。")
        print("      请在 config.yaml 里设置 self_wxid。")

    contacts = []
    if live_ok:
        try:
            contacts = all_contacts(wcf)
            print(f"[bot] 实时历史查询已启用，联系人 {len(contacts)} 个。")
        except Exception as e:
            live_ok = False
            print(f"[bot] 实时查询不可用（{e}），将退回静态导出模式。")
    else:
        print("[bot] 当前 wcferry 版本没有 query_sql，退回静态导出模式。")

    targets = set(cfg.get("target_chats", []))
    reply_only = cfg.get("reply_only_targets", True)
    system = cfg.get("system_prompt", "")
    respond_to_self = bool(cfg.get("respond_to_self", False))

    # 自动回复：代替我本人回这些会话
    auto_on = auto_reply.enabled(cfg)
    auto_recs = auto_reply.chats(cfg)
    # 盯着：只通知我，不回对方
    watch_on = watch.enabled(cfg)
    watch_recs = watch.chats(cfg)
    # 审核模式把草稿往哪儿发：第一个控制会话（默认文件传输助手）
    control_chat = (list(cfg.get("target_chats") or []) or ["filehelper"])[0]

    # 同一个会话不能既是控制会话又是自动回复对象，否则对方发来的消息会被当命令解析。
    # /auto add 时已经拦了，这里是兜底（比如用户手改了 config.yaml）。
    for chat in sorted(set(auto_recs) & targets):
        print(f"[bot] ⚠️ {chat} 同时在 target_chats 和 auto_reply.chats 里，"
              f"自动回复对它不生效，请二选一。")
        auto_recs.pop(chat, None)

    def send(text, to):
        wcf.send_text(text, to)
        remember_sent(text)

    print(f"[bot] 后端：{backend}  |  监听中，控制会话：{targets}  |  只回目标：{reply_only}")
    if auto_recs:
        who = "、".join(f"{r.get('name') or w}({w})" for w, r in auto_recs.items())
        print(f"[bot] 自动回复：{'开启' if auto_on else '关闭'}  |  {who}")
    else:
        print("[bot] 自动回复：未配置（在微信里发 /auto add <昵称> 添加）")
    print("[bot] 在微信里发 /help 查看可用的配置命令。Ctrl+C 退出。")
    print(f"[bot] {scheduler.summary_line(cfg)}  |  {watch.summary_line(cfg)}")

    def reload_cfg():
        """配置被改后重建主循环的状态。

        / 命令改了要走这里；agent 的工具（auto_reply）改了也要走这里——
        它是从另一边写 settings.json 的，主循环不重建就还按旧状态跑。
        """
        nonlocal cfg, llm, targets, system, auto_on, auto_recs, control_chat
        nonlocal watch_on, watch_recs
        cfg = settings.effective(base_cfg)
        llm = make_llm(cfg)
        targets = set(cfg.get("target_chats", []))
        system = cfg.get("system_prompt", "")
        auto_on = auto_reply.enabled(cfg)
        auto_recs = auto_reply.chats(cfg)
        watch_on = watch.enabled(cfg)
        watch_recs = watch.chats(cfg)
        control_chat = (list(cfg.get("target_chats") or []) or ["filehelper"])[0]

    def run_scheduled():
        """跑一遍到点的定时任务。

        **只在收消息那条线程上调用**（见 _Ticker）：定时任务里要发消息、
        还可能跑一整轮 agent，而 hook 不支持并发——另起线程会直接把微信搞崩。
        传入的是内存里的 cfg（不是重新读盘）：scheduler 会就地更新任务的
        next_ts，这样同一个任务不会在下一 tick 又触发一遍。命令改过配置后
        主循环会 reload_cfg()，新任务自然生效。
        """

        def ask_task(question):
            """定时的「提问」：把这句话当普通提问跑一遍，走的和主循环完全同一条路。

            「每天早8点给我一份谁还没回我的整理」就是这么实现的——不用为摘要
            另写一套查询，工具、历史检索、对话记忆全都自动可用。
            """
            if llm is None:
                raise RuntimeError("还没设置 API Key")
            prompt = build_user_prompt(question, wcf, contacts, cfg,
                                       static_history, live_ok)
            history = dialog_history(control_chat, cfg)
            if agent_enabled(cfg):
                answer, changed = run_agent(
                    llm, system, prompt, wcf, contacts, cfg, control_chat, self_wxid,
                    cfg_provider=lambda: settings.effective(base_cfg),
                    history=history)
            else:
                answer = llm.chat(system, history + [{"role": "user", "content": prompt}])
                changed = False
            dialog_append(control_chat, "user", question, cfg)
            dialog_append(control_chat, "assistant", answer, cfg)
            if changed:
                # 任务状态在 scheduler 里是**先落盘再执行**的，所以这里重建 cfg
                # 不会把刚写进去的 next_ts 冲掉。
                reload_cfg()
            return answer

        fired = scheduler.run_due(
            cfg, datetime.now(),
            send_text=lambda to, text: send(text, to),
            notify=lambda text: send(text, control_chat),
            ask=ask_task,
        )
        if fired:
            print(f"[bot] 定时任务已触发：{'、'.join(fired)}")

    source = (iter_aixed_messages(wcf, poll_interval, tick=_Ticker(run_scheduled))
              if backend == "aixed"
              else iter_wcferry_messages(wcf, tick=_Ticker(run_scheduled)))

    try:
        while True:
            msg = next(source)

            if getattr(msg, "type", 0) != 1:  # 1 = 文本
                continue

            sender = msg.roomid or msg.sender
            in_targets = sender in targets
            rec = auto_recs.get(sender) if auto_on else None
            watched = watch_recs.get(sender) if watch_on else None

            if not in_targets and rec is None and watched is None and reply_only:
                continue

            query = (msg.content or "").strip()
            if not query:
                continue

            if msg.from_self():
                # 自己发的消息默认忽略（否则会回复自己）。
                # 但「文件传输助手」这类自聊场景需要响应自己——
                # 这时用 is_own_reply 排除掉刚发出去的回复，避免自己回自己无限循环。
                own_reply = is_own_reply(query)
                if own_reply:
                    print(f"[bot] 跳过（这是自己刚发出的回复）: {query[:30]}")
                    continue
                if rec is not None:
                    # 自动回复只回对方。少了这一条，我们自己刚发出去的那句回复
                    # 会以 from_self 回来、被当成新问题再答一遍 —— 死循环。
                    continue
                if not respond_to_self:
                    print(f"[bot] 跳过（自己发的消息，respond_to_self=false）: {query[:30]}")
                    continue
                print(f"[bot] 自聊模式，处理自己的消息: {query[:30]}")

            # 盯着：他发消息就通知我，**一个字都不回他**。
            # 和自动回复一样，这条路不解析命令——对方随口发个「/help」不该触发命令表。
            if watched is not None and not msg.from_self():
                send(watch.format_hit(watched, query), control_chat)
                print(f"[bot] 盯着命中 {watched.get('name') or sender}: {query[:40]}")
                continue

            # 自动回复：代我回对方。这条路**不解析命令**——
            # 对方随口发个「/help」不该触发助手的命令表。
            if rec is not None:
                min_gap = (cfg.get("auto_reply") or {}).get("min_gap", 6)
                if auto_due(sender, min_gap):
                    try:
                        do_auto_reply(wcf, llm, cfg, sender, rec, contacts,
                                      control_chat, send)
                    except Exception:
                        traceback.print_exc()  # 自动路径抛异常绝不能杀掉主循环
                else:
                    print(f"[bot] 自动回复跳过（冷却中）: {rec.get('name') or sender}")
                continue

            # 1) 命令优先
            reply, changed = handle_command(query, wcf, cfg, live_ok, contacts)
            if reply is not None:
                send(reply, sender)
                if changed:
                    reload_cfg()
                print(f"[bot] 命令回复: {reply[:60]}")
                continue

            # 1.5) 待确认的发送动作：用户回「确认」才真发
            #      不再限定 agent_enabled：审核模式下的自动回复草稿也要走这里，
            #      而 pop_pending 没内容时返回 None，本就不会误触发。
            if is_confirm(query) or is_cancel(query):
                ttl = int((cfg.get("agent") or {}).get("confirm_ttl", 300))
                head = agent_tools.peek_pending(sender, ttl)
                # 自动回复草稿是要发给**别人**的，只认明确的中文确认词，
                # 免得在控制会话里随口一句「ok」就把草稿发出去。
                if head and head.get("kind") == "auto" \
                        and not is_strict_confirm(query) and not is_cancel(query):
                    send("这条是自动回复草稿。要发请回「确认」，不发请回「不发」。", sender)
                    continue
                if is_cancel(query):
                    n = agent_tools.discard_pending(sender)
                    if n:
                        send(f"已取消 {n} 条待确认的发送。", sender)
                        continue
                item = agent_tools.pop_pending(sender, ttl)
                if item:
                    agent_cfg = cfg.get("agent") or {}
                    count = max(1, int(item.get("count") or 1))
                    interval = max(0.0, float(agent_cfg.get("send_interval", 1.5)))
                    # 连发是同步做的：中途轮询会暂停几秒（消息在库里排着，回来照收）。
                    # 故意不开线程——并发碰 hook 会把微信搞崩。
                    # 图片和转发只发一次，count/连发对它们没意义（见 send_pending）。
                    n, err = agent_tools.send_pending(wcf, item, interval)
                    is_text = not item.get("image") and not item.get("xml")
                    if is_text:
                        # 记一下，免得发给自己时又被当成新消息回一遍
                        remember_sent(item["text"])
                    what = "转发" if item.get("xml") else "图片"
                    if err is not None:
                        print(f"[bot] 确认发送失败（已发 {n}/{count}）: {err}")
                        send(f"发给 {item['to_name']} 失败：{err}", sender)
                    elif is_text:
                        print(f"[bot] 确认发送 -> {item['to_name']}: {item['text'][:40]} ×{n}")
                        send(f"已发送给 {item['to_name']}。" if n == 1
                             else f"已给 {item['to_name']} 连发 {n} 条。", sender)
                    else:
                        print(f"[bot] 确认发送 -> {item['to_name']}: {what} ×{n}")
                        send(f"已把 {n} 张{what}发给 {item['to_name']}。" if n > 1
                             else f"已把{what}发给 {item['to_name']}。", sender)
                    continue

            # 2) 否则走 AI 问答（开了 agent 就带工具）
            print(f"[bot] 收到 {sender}: {query}")
            if llm is None:
                send("还没设置 API Key。发 /api sk-ant-xxx 设置（或告诉我接本地模型）。", sender)
                continue

            try:
                prompt = build_user_prompt(query, wcf, contacts, cfg, static_history, live_ok)
                history = dialog_history(sender, cfg)
                if history:
                    print(f"[bot] 带上 {len(history)} 条对话记忆")
                if agent_enabled(cfg):
                    # cfg_provider：工具要读「当前最新配置」而不是这一轮开始时的快照，
                    # 否则连着改两次（比如加了人再开开关）第二次会基于旧快照覆盖前一次。
                    answer, cfg_changed = run_agent(
                        llm, system, prompt, wcf, contacts, cfg, sender, self_wxid,
                        cfg_provider=lambda: settings.effective(base_cfg),
                        history=history)
                else:
                    answer = llm.chat(system,
                                      history + [{"role": "user", "content": prompt}])
                    cfg_changed = False
                send(answer, sender)
                if cfg_changed:
                    reload_cfg()
                # 只记原始提问和最终答复。命令、审核确认那些路径在上面就 continue 了，
                # 压根走不到这儿——它们是操作，不是对话。
                dialog_append(sender, "user", query, cfg)
                dialog_append(sender, "assistant", answer, cfg)
                print(f"[bot] 已回复: {answer[:60]}...")
            except Exception:
                traceback.print_exc()
                try:
                    send("出错了，看终端日志。", sender)
                except Exception:
                    pass
    except KeyboardInterrupt:
        print("\n[bot] 已退出。")


if __name__ == "__main__":
    main()
