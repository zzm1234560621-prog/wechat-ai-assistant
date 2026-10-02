"""主程序：连接微信，在指定聊天里用 AI 回复（实时读历史 + 聊天框配置）。

流程：
  1. 连上微信，拿到自己的 wxid
  2. 开启消息接收
  3. 收到命令（/ 开头）-> 改配置并持久化
     收到问题 -> 实时查微信数据库历史 -> 调大模型回复

依赖 wcferry（必须匹配微信版本）。用法：python bot.py，Ctrl+C 退出。
"""
import base64
import hashlib
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
import assets
import auto_reply
import executor
import groups
import live_history
import settings
import providers
import read_worker
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

# 运维 / 隐私侧的后加模块。**故意写成可缺省导入**：万一哪个没跟着部署上来，
# 不许把 bot 直接拦死在启动阶段（那就等于整台助手全废），但用到处会明确告警，
# 绝不静默当它不存在。
try:
    import health
except ImportError:                                     # pragma: no cover
    health = None
try:
    import usage
except ImportError:                                     # pragma: no cover
    usage = None
try:
    import redact
except ImportError:                                     # pragma: no cover
    redact = None
try:
    import status_page
except ImportError:                                     # pragma: no cover
    status_page = None

# health.Health 的实例，main() 里建。
# **故意不叫 health**：`health` 这个名字留给模块本身（模块级的 rotate_log 等函数
# 要用它），实例单独放这里，避免「调了实例上不存在的方法、被 try/except 静默吞掉」。
_HEALTH = None


def _h():
    """当前健康看护实例；health.py 缺失或还没初始化时返回 None。"""
    return _HEALTH


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
    # 先轮转再打开：日志是 `open(..., "a")` 无限追加的，后台长期跑会一直涨。
    # 轮转失败不许挡住启动（拿不到日志也总比没有 bot 强），但会如实打出来。
    if health is not None:
        try:
            health.rotate_log(LOG_PATH)
        except Exception:
            traceback.print_exc()
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
    "/status         查看当前配置 + 运行健康（轮询 / 分片错误 / 登录态）\n"
    "/用量 [天数]     看 token 用量和估算费用（默认最近 7 天）\n"
    "/help           显示本帮助\n"
    "\n"
    "—— 自动回复（让 AI 代替我本人回某个人）——\n"
    "下面这些也能直接用大白话说，助手会自己调用工具：\n"
    "   「以后张三的消息你帮我回」 「别自动回李四了」 「发之前先给我看一眼」\n"
    "   「以后跟张三说话随便点」 「对李四别那么客气」 「回他的时候叫他老张」\n"
    "/auto                    看开关、审核、人设、称呼和名单\n"
    "/auto on | off           开 / 关（关掉就你自己回）\n"
    "/auto add <昵称|wxid|roomid> [self|assistant]  加入名单（会顺手学一次语气和称呼）\n"
    "/auto del <昵称|wxid>    移出名单\n"
    "/auto mode <谁> self|assistant   改身份（假装你本人 / 明说是助手）\n"
    "/auto persona <谁> [描述]  给这个人单独一套语气；不带描述=看，清空=恢复默认\n"
    "/auto persona <谁> 学习    从你和他的历史对话里学语气+称呼（会覆盖已有的）\n"
    "/auto address <谁> [称呼]  你平时怎么叫他；不带=看，清空=不套称呼\n"
    "/auto address <谁> 学习    只学称呼，不动人设\n"
    "/auto persona 全局 [描述]  没单独设的人用的默认语气\n"
    "（学到的称呼也是别名：「给老张发消息」能认出来）\n"
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
    "（和 /auto 互斥：那个是代你回对方，这个是只告诉你不回）\n"
    "\n"
    "—— 群发（一次给多个人发，各按自己的语气和称呼）——\n"
    "也可以直接说「给张三、李四发…」或「帮我祝所有人节日快乐」。\n"
    "· 你给出**要发的那句话本身** -> 所有人收到同一段，一字不改；\n"
    "· 你只给出**意思** -> 按每个人的语气和称呼分别写一条。\n"
    "「所有人」= 你的所有好友，会**先确认范围**（那步一个字都不发），\n"
    "确认后才生成内容：免确认名单里的人直接收到，其余的人等你看过再发。\n"
    "单次人数上限见 config.yaml 的 agent.broadcast_max（默认 100）。\n"
    "\n"
    "—— 分组（把联系人分好组，群发直接按组发）——\n"
    "/分组                      看所有分组和成员\n"
    "/分组 建 <组名> <人名、人名>  建一个组（组名不能带空格）\n"
    "/分组 加 <组名> <人名、人名>  往组里加人（组不存在就建）\n"
    "/分组 移 <组名> <人名、人名>  从组里移人\n"
    "/分组 删 <组名>             删掉整个组（人本身不动）\n"
    "/分组 标签                  看**微信自带**的标签和人数（只读，不算分组）\n"
    "之后说「给大学同学组发…」就行，不用点名。\n"
    "微信里已经建好的标签也能直接发：说「给亲人发…」即可（标签在微信那边改）。\n"
    "\n"
    "—— 素材暂存（发一次图/表情，之后说「发给谁」就能再发）——\n"
    "在这里发一张图或一个表情，我就记下来（默认最多 5 条，新的顶掉最老的）。\n"
    "之后直接说「发给张三」「把刚才那张发给李四」「发 3 次」就行。\n"
    "/素材 —— 看暂存了什么\n"
    "/素材 清空 —— 清空暂存区"
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
        # 运行健康（纯内存快照，不查库）。以前 /status 只报配置，
        # 而用户最需要知道的恰恰是「它现在到底还收不收得到消息」。
        h = _h()
        if h is not None:
            try:
                snap = h.snapshot()
                up = int(snap.get("uptime_seconds") or 0)
                lines.append(f"运行时长: {up // 3600} 小时 {(up % 3600) // 60} 分")
                if snap.get("last_poll_text"):
                    lines.append(f"最近轮询: {snap.get('last_poll_text')}")
                if snap.get("login_ok") is False:
                    lines.append("⚠️ 登录态: 已掉登录（要在微信里重新扫码）")
                sendf = snap.get("send_failures") or 0
                if sendf:
                    lines.append(f"⚠️ 发送失败累计: {sendf} 次（看 bot.log）")
            except Exception:
                lines.append("（运行健康快照取不出来，看 bot.log）")
        else:
            lines.append("⚠️ health.py 不在：健康看护/状态页不可用（安装不完整）")
        try:
            errs = live_history.poll_errors()
        except Exception:
            errs = {}
        if errs:
            names = "、".join(f"{k}({v[1]}次)" for k, v in errs.items())
            lines.append(f"⚠️ 分片查询失败: {names}")
        return "\n".join(lines), False

    if cmd in ("/用量", "/usage", "/花费"):
        if usage is None:
            return "这个版本没带上 usage.py，用量统计不可用（安装不完整）。", False
        days = 7
        if arg:
            try:
                days = max(1, min(365, int(arg)))
            except (TypeError, ValueError):
                return "用法：/用量 [天数]，例如 /用量 30", False
        try:
            return usage.summarize(days), False
        except Exception as e:
            # 统计坏了也得如实说，不许回一句「暂无记录」糊过去
            traceback.print_exc()
            return f"用量统计失败：{e}", False

    if cmd in ("/auto", "/自动回复"):
        # 重新读一次配置再处理：连着发几条 /auto 时，传进来的 cfg 还是上一条
        # 命令之前的快照，直接用它会在「读-改-写」里丢掉上一次的改动。
        fresh = settings.effective(load_config())
        # llm_factory 是**懒调用**的：只有 `/auto add` 或 `/auto persona 谁 学习`
        # 真要去学语气时才建模型客户端，普通 /auto 命令一分钱不花。
        return auto_reply.handle_command(arg, fresh, wcf, can_lookup=live_ok,
                                         llm_factory=lambda: make_llm(fresh))

    if cmd in ("/定时", "/schedule", "/提醒"):
        # 同 /auto：重新读一次，避免连着发命令时丢掉上一条的改动。
        fresh = settings.effective(load_config())
        # 学到的「称呼」也当名字：用户说「给老张发消息」时能认出来。
        # 只有一份真源（auto_reply.chats[].address），这里只是取出来传给统一解析。
        alias = auto_reply.address_aliases(fresh)

        def resolve(who):
            # 走和 agent 工具**同一套**解析：重名不静默取第一个
            return agent_tools.resolve_one(contacts, who,
                                           fresh.get("self_wxid", ""), wcf,
                                           aliases=alias)

        return scheduler.handle_command(arg, fresh, resolve, can_lookup=live_ok)

    if cmd in ("/盯着", "/watch", "/盯"):
        # 同 /auto：重新读一次，避免连着发命令时丢掉上一条的改动。
        fresh = settings.effective(load_config())
        alias = auto_reply.address_aliases(fresh)      # 同上：称呼也当名字用

        def resolve_watch(who):
            return agent_tools.resolve_one(contacts, who,
                                           fresh.get("self_wxid", ""), wcf,
                                           aliases=alias)

        return watch.handle_command(arg, fresh, resolve_watch)

    if cmd in ("/分组", "/group", "/组"):
        # 分组只用来决定「群发发给谁」，不发消息。同 /盯着：重新读配置 + 同一套解析
        # （重名不静默取第一个；学到的称呼也当名字用）。
        fresh = settings.effective(load_config())
        alias = auto_reply.address_aliases(fresh)

        def resolve_group(who):
            return agent_tools.resolve_one(contacts, who,
                                           fresh.get("self_wxid", ""), wcf,
                                           aliases=alias)

        # `client` 只有「/分组 标签」那一支要用（看微信自带的标签）。
        return groups.handle_command(arg, fresh, resolve_group, client=wcf)

    if cmd in ("/素材", "/asset", "/assetbank"):
        # 容量的钳制在 _assets_cap 里（配置写歪了也只告警钳制，不静默放大）。
        if arg.strip().lower() in ("清空", "clear", "清除", "删", "全清"):
            n = assets.clear()
            return (f"素材暂存区已清空（{n} 条）。" if n
                    else "素材暂存区本来就是空的。"), False
        return "\n".join(assets.list_lines(assets.load())), False

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
    是 `wxid_xxxxxxxxxxxx: 到小区了`，答出来自然也是一串 id。
    群聊标成「群名/发言人」，单聊就是对方的名字。
    """
    talker = str(m.get("talker") or "")
    # ⚠️ 查不到显示名时**不要把 talker 当会话名**：talker 是 wxid / roomid
    # （无 fts 兜底那条路甚至可能是 `Msg_<md5>` 表名），拿它当名字就等于把原始 id
    # 交给模型（CLAUDE.md 明令禁止）。查不到就给空串，由 speaker_of 退回「对方/群成员」。
    tname = names.get(talker) or ""
    group = auto_reply.is_group(talker)
    who = agent_tools.speaker_of(m, names, tname, group)
    if group and who != tname:
        return f"{tname or '群'}/{who}"
    return who


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
        win = cfg.get("recent_messages", 30)
        mentions = find_mentions(query, contacts)
        if mentions:
            # **这段是按条数截的窗口，不是时间范围。** 标题必须自己说清这一点：
            # 2026-10-01 真机踩过——用户问「我跟张三最近 10 天说了什么」，
            # 模型看到这里只有 9/30 起的 30 条，就把它答成
            # 「最近 10 天（9/30–10/1）」；而那条会话从 2026-01 起有 6486 条，
            # 30 条其实只覆盖 1.4 天。范围是它自己编的，因为没人告诉它窗口有多小。
            parts.append(
                f"【你提到的联系人，最近的历史对话】（**每个会话只有最近 {win} 条**"
                f"——这是按**条数**取的窗口，不是按时间范围取的，**不是全部历史**）")
            for c in mentions[:3]:
                name = c.get("remark") or c.get("name") or c.get("wxid")
                msgs = query_contact_history(wcf, c["wxid"], limit=win)
                cover = f"以下 {len(msgs)} 条"
                span = agent_tools.span_of(msgs)
                if span:
                    cover += f"，实际覆盖 {span}"
                if len(msgs) >= win:
                    cover += (f"；⚠️ 取满了 {win} 条上限——**更早的没有取**，"
                              f"所以这段不能当成全部历史")
                parts.append(f"--- 与 {name}（{c['wxid']}）--- {cover}")
                for m in msgs:
                    who = "我" if m["is_self"] else name
                    parts.append(f"[{m['time']}] {who}: {m['content']}")

        terms = extract_keywords(query)
        if terms:
            seen = set()
            snips = []
            for term in terms[:4]:
                for m in search_history(wcf, term, limit=8):
                    if not keep(m.get("talker")):
                        continue
                    key = (m["time"], m["content"])
                    if key in seen:
                        continue
                    seen.add(key)
                    who = _msg_speaker(m, names)
                    snips.append(f"[{m['time']}] {who}: {m['content']}")
            if snips:
                parts.append("【按关键词搜到的历史片段】"
                             "（每个关键词最多 8 条，**只是片段、不是全部历史**）")
                parts.extend(snips)
        else:
            # 问句里没有可检索的关键词（例如「最近聊了什么」），直接给最近的聊天
            parts.append("【最近的聊天记录】"
                         "（**每个会话只取最后一条**，而且只覆盖最近活跃的那些"
                         "——**不是全部历史**，也不是某个时间范围）")
            for m in recent_messages(wcf, limit=win):
                if not keep(m.get("talker")):
                    continue
                who = _msg_speaker(m, names)
                parts.append(f"[{m['time']}] {who}: {m['content']}")
    else:
        hits = static_history.search(query, cfg.get("search_topk", 8))
        parts.append("【相关历史片段（静态导出）】（**是片段，不是全部历史**）")
        for m in hits:
            parts.append(f"[{m.get('time','?')}] {m.get('sender','?')}: {HistoryStore.text(m)}")

    if not parts:
        parts.append("（历史记录里没查到相关内容）")

    parts.append("")
    # 上面那些全都是**按条数**取的窗口。用户问时间范围时必须去查库，
    # 不许把窗口的跨度当成用户问的范围（2026-10-01 真机踩过，见上面 mentions 那段）。
    parts.append(
        "⚠️ 上面这段历史是**按条数**取的窗口，**不等于用户问的时间范围**。"
        "用户问「最近 N 天 / 上周 / 某天 / 这一个月」时，必须调用 read_history 工具"
        "并把 days 填上（N 天就填 N）重新查；回答时**照实说这次覆盖到什么时候**。"
        "窗口里没有**不等于**那段时间没有记录——只能说「我这边只取到 X 以来」，"
        "绝不许把上面这段的跨度当成用户问的范围。")
    parts.append(f"【用户的问题】{query}")
    text = "\n".join(parts)
    # 送云端前脱敏：**默认关闭**（config.yaml 的 privacy.redact）。
    # 只作用于「送出去的那一份」，本地原文一个字都不动。命中数要打日志，
    # 让用户知道这次真打了码——不许悄悄改内容还装作没发生。
    if redact is not None:
        try:
            text, hits = redact.redact(text, cfg)
            if hits:
                print(f"[bot] 已对送出的历史脱敏，命中 {hits} 处（privacy.redact）")
        except Exception:
            traceback.print_exc()
    return text


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


# 「确认 2」「第2条」「执行 2」这类**整句就是选号**的写法。
# 句子里夹带数字（例如「确认下 2 点的会」「2 是谁」）一律**不认**——
# 宁可多问一次，也绝不猜他要确认哪一条。
_PENDING_SEL_RE = re.compile(
    r"^(?:确认|确定|确认发送|执行|ok|yes|y)?\s*第?\s*(\d{1,3})\s*条?$", re.I)


def pending_index_of(text):
    """这句里显式点了第几条待确认项。没有就返回 None（1 起）。"""
    m = _PENDING_SEL_RE.match(_norm_word(text))
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


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


# ============================================================
#  落盘运行状态：轮询游标 + 待确认队列
# ============================================================
#
# 为什么必须有：
#   * 游标以前只活在内存里，重启就是 `prime()`「把当前最新那批标成已见」——
#     停机期间来的消息**直接被丢掉**，用户看到的是「重启后漏了一段」。
#   * 待确认队列以前只在内存里（`agent_tools._PENDING`）：重启之后用户照着
#     刚才看到的提示回「确认」，什么都不会发生，白等一场。
#
# 落盘策略与对话记忆一致：先写临时文件再 `os.replace`，半截崩了不会把整份弄没。

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "state.json")
_STATE = None
# 游标落盘的节流时间戳（没必要每 5 秒就写一次盘）
_LAST_CURSOR_SAVE = 0.0


def _state_load():
    """读一次盘，之后走内存。文件坏了就当空的——状态文件不该挡住启动。"""
    global _STATE
    if _STATE is not None:
        return _STATE
    _STATE = {}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _STATE
    if isinstance(data, dict):
        _STATE = data
    return _STATE


def _state_save():
    if _STATE is None:
        return
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except OSError:
        traceback.print_exc()


def state_get(key, default=None):
    return _state_load().get(key, default)


def state_set(key, value):
    _state_load()[key] = value
    _state_save()


def save_pending(chats, cfg=None):
    """把待确认队列落盘。**队列一变就调**——不能等节流窗口过去，

    否则「用户已确认并执行/已发送」和「盘上还记着这条」之间就有个窗口，
    崩溃重启后那条会被恢复出来，可能被再执行一次。
    """
    try:
        ttl = int(((cfg or {}).get("agent") or {}).get("confirm_ttl", 300))
    except (TypeError, ValueError):
        ttl = 300
    snap = {}
    for c in list(chats or []):
        try:
            items = agent_tools.list_pending(c, ttl)
        except Exception:
            traceback.print_exc()
            continue
        if items:
            snap[str(c)] = items
    try:
        state_set("pending", snap)
    except Exception:
        traceback.print_exc()


def restore_pending(chats, cfg):
    """把盘上的待确认队列恢复到内存。返回恢复了几条。

    只恢复**还在 `confirm_ttl` 时效内**的：过期的恢复出来只会让用户
    对着一句早就没意义的提示回「确认」。
    """
    data = state_get("pending")
    if not isinstance(data, dict):
        return 0
    try:
        ttl = int((cfg.get("agent") or {}).get("confirm_ttl", 300))
    except (TypeError, ValueError):
        ttl = 300
    allowed = {str(c) for c in (chats or [])}
    now = time.time()
    n = 0
    for chat, items in data.items():
        if str(chat) not in allowed or not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                if now - float(it.get("ts") or 0) > ttl:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                agent_tools.set_pending(
                    chat, it.get("to_wxid") or "", it.get("to_name") or "",
                    it.get("text") or "", kind=it.get("kind") or "agent",
                    count=it.get("count") or 1, image=it.get("image"),
                    xml=it.get("xml"), cmd=it.get("cmd"), timeout=it.get("timeout"),
                    # ⚠️ `label` / `items` / `spec` **必须一起恢复**。
                    # 少了 label，素材那条待确认项就退化成「转发一条消息」（用户认不出
                    # 是哪一条）；少了 items/spec，群发批次会变成「没有收件人」——
                    # 而它的 text 只是**给人看的预览**，真按文本分支发出去就是往空
                    # wxid 发一段预览文字（真机上是「发出去了但没人收到」这种最难查的错）。
                    label=it.get("label"), items=it.get("items"), spec=it.get("spec"))
                n += 1
            except Exception:
                traceback.print_exc()
    return n


def run_agent(llm, system, prompt, wcf, contacts, cfg, chat, self_wxid="",
              cfg_provider=None, history=None, state=None):
    """带工具的问答循环。返回 (最终要回复的文本, 工具是否改动了配置)。

    hook 不能并发查询，所以工具是**串行**执行的；查询次数由
    agent_tools.ToolBox 内部的预算控制（agent.max_queries）。

    第二个返回值是给主循环用的：auto_reply 工具改了配置后，主循环要重建
    自己的 auto_on/auto_recs，否则「以后张三的消息你帮我回」要重启才生效。

    history 是该会话之前的几轮对话（见 dialog_history），拼在本轮提问前面，
    这样「刚才那个」「再帮我问他一句」才接得上。

    state：可选的 dict。传了就写入本轮的工具事实（目前只有 shell_queued：
    本轮**是否真的登记过**一条待确认的本地命令），给主循环做确定性兜底用
    （见 with_shell_truth_note）。返回值形状**故意不动**，免得破坏现有解包。
    """
    agent_cfg = cfg.get("agent") or {}
    # 夹到 [1, 10]：轮次直接线性放大对 hook 的调用次数，而 CLAUDE.md 记着
    # 「hook 不能并发、已被并发查询搞崩 6 次」。光靠注释劝人「别放开」不算闸门。
    max_rounds = max(1, min(10, int(agent_cfg.get("max_rounds", 3))))
    box = agent_tools.ToolBox(wcf, cfg, contacts, self_wxid, chat, cfg_provider,
                              llm_factory=lambda: llm)

    messages = list(history or []) + [{"role": "user", "content": prompt}]
    last_text = ""
    for _ in range(max_rounds):
        # 上一轮工具收下的**原图**（image.mode=inline）：附给**这一次**调用，取走即清。
        # 这样图只花一次 token；而且永远不进 messages / dialog 记忆。
        pending = box.take_images() if hasattr(box, "take_images") else []
        call_messages = attach_images(messages, pending) if pending else messages
        result = llm.chat_with_tools(system, call_messages, agent_tools.TOOLS)
        last_text = result.text or last_text
        if not result.tool_calls:
            if state is not None:
                state["shell_queued"] = box.shell_queued
                state["broadcast_preview"] = box.broadcast_preview
                state["image_notes"] = list(box.image_notes)
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
    if state is not None:
        state["shell_queued"] = box.shell_queued
        state["broadcast_preview"] = box.broadcast_preview
        state["image_notes"] = list(box.image_notes)
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


# ── 已执行指纹：同一条待确认项绝不执行两次（落盘，扛得住重启） ──────────────
#
# 为什么需要它：待确认队列是**落盘**的（`data/state.json` 的 `pending`），崩溃重启
# 之后会被恢复出来。`save_pending()` 已经在「用户确认后」立刻落盘，把窗口压到最小，
# 但仍有缝：**发送成功** 与 **落盘移除这条** 之间进程死掉，重启后那条会被恢复出来，
# 而它其实已经发出去了——再执行一次就是**给别人重复发消息**（不可逆）。
#
# 所以执行过的待确认项要留一个指纹。关键设计：**指纹里包含这条自己的 `ts`**
# （见 `set_pending`，每条登记时带一个创建时间）。
#   * 同一个 `ts` = **同一条**待确认项（就是崩溃恢复出来的那一份）→ 拦住；
#   * 用户重新说一遍「再给张三发一次」会生成**新的 ts** → 指纹不同 → 照常放行。
# 这样它只拦「同一条执行两次」，**不拦「同样内容的第二条」**——后者是用户的正当需求，
# 拦下来就是坏功能。
_EXECUTED_KEY = "executed"
_EXECUTED_TTL_DEFAULT = 1800.0


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def executed_ttl(cfg=None):
    """指纹保留多久。默认 1800 秒——和 `state.resume_window` 同量级：
    比这更久的恢复项本来也不会被当成「本次重启要补的」。"""
    try:
        v = ((cfg or {}).get("state") or {}).get("executed_ttl", _EXECUTED_TTL_DEFAULT)
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return _EXECUTED_TTL_DEFAULT


def item_fingerprint(item):
    """给一条待确认项算稳定指纹。**`ts` 一定要算进去**（理由见上面那段注释）。"""
    if not isinstance(item, dict):
        return ""
    try:
        items = json.dumps(item.get("items"), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        items = ""
    parts = [str(item.get("kind") or ""),
             str(item.get("to_wxid") or ""),
             str(item.get("to_name") or ""),
             str(item.get("cmd") or ""),
             str(item.get("text") or ""),
             str(item.get("xml") or ""),
             str(item.get("image") or ""),
             str(item.get("count") or 1),
             repr(item.get("ts")),
             items]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def describe_executed(item):
    """给用户看的一句话「这条是什么」。别把 wxid 摆出来。"""
    if not isinstance(item, dict):
        return "这一条"
    kind = item.get("kind")
    who = item.get("to_name") or item.get("to_wxid") or ""
    if kind == "shell":
        cmd = str(item.get("cmd") or "")
        return f"本地命令「{cmd[:40]}{'…' if len(cmd) > 40 else ''}」"
    if kind == "broadcast_scope":
        return f"群发（{len(item.get('items') or [])} 人）的范围确认"
    if kind == "broadcast":
        return f"群发给 {len(item.get('items') or [])} 人的那一批"
    if kind == "auto":
        return f"给 {who} 的自动回复草稿"
    if item.get("xml"):
        return f"转发给 {who} 的那条消息"
    if item.get("image"):
        return f"发给 {who} 的那张图"
    return f"发给 {who} 的那条消息"


def _fmt_ago(seconds):
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s // 3600} 小时 {(s % 3600) // 60} 分"


def already_executed(item, cfg=None):
    """这条待确认项是不是**已经执行过**了。返回 `(bool, 距上次多少秒)`。

    两头都有代价：**漏拦** = 给别人重复发消息（不可逆）；**误拦** = 用户被要求
    重说一遍。所以判据取严：只要**指纹认得出来**且没过期，就按「已执行」拦住；
    只有在指纹根本算不出（item 结构坏掉）或账面数据读不出来时才放行。
    """
    fp = item_fingerprint(item)
    if not fp:
        return False, None
    book = state_get(_EXECUTED_KEY) or {}
    if not isinstance(book, dict):
        return False, None
    rec = book.get(fp)
    if not isinstance(rec, dict):
        return False, None
    try:
        ago = time.time() - float(rec.get("ts") or 0)
    except (TypeError, ValueError):
        return False, None
    if ago > executed_ttl(cfg):
        return False, None
    return True, ago


def remember_executed(item, cfg=None):
    """记下「这条待确认项真的执行过了」。

    调用点必须在**真正尝试执行之后**：早于执行就落指纹的话，一次「还没来得及发
    就崩了」会让恢复出来的那条被拦住，用户以为发了其实没发（比重复发更糟——
    这正是项目一贯的「没跑就是没跑」）。
    """
    fp = item_fingerprint(item)
    if not fp:
        return
    try:
        ttl = executed_ttl(cfg)
        now = time.time()
        book = state_get(_EXECUTED_KEY) or {}
        if not isinstance(book, dict):
            book = {}
        book[fp] = {"ts": now, "what": describe_executed(item)}
        # 顺手清过期的，免得 state.json 无限长
        book = {k: v for k, v in book.items()
                if isinstance(v, dict) and (now - _as_float(v.get("ts"), now)) <= ttl}
        state_set(_EXECUTED_KEY, book)
    except Exception:
        # 记账失败只告警，绝不影响这次执行本身
        traceback.print_exc()


def _as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def shell_command_text(item, cfg):
    """执行一条已确认的本地命令，返回要回给用户的文本。

    只在用户**真的回了「确认」**之后才会走到这里——命令原文早在
    agent_tools.t_run_command 里登记过、也给用户看过了。

    同步阻塞、故意不开线程：hook 不支持并发，主循环是单线程的，跑命令期间
    就停下轮询（消息在库里排着，回来照收）。超时 / 非零退出 / 输出被截断
    一律由 executor.format_result 如实写在文本里，绝不假装成功。
    """
    cmd = str(item.get("cmd") or "")
    # 模型给的 timeout 优先；没给就让 executor 自己去读 shell.timeout。
    kw = {}
    try:
        if item.get("timeout") is not None:
            kw["timeout"] = int(item["timeout"])
    except (TypeError, ValueError):
        pass

    print(f"[bot] 已确认，执行命令: {cmd}")
    try:
        # cfg 一并传进去：executor 自己读 shell.cwd / timeout / max_output，
        # 边界（夹到 [1,600] 等）也由它管，这里不重复实现一套。
        res = executor.run_command(cmd, cfg=cfg, **kw)
    except Exception as e:
        # 连跑都没跑起来（executor 自己抛了）——如实说，不编输出
        return f"执行「{cmd}」时出错：{e}"
    try:
        # 不自己截长度：format_result 的默认额度就是**按微信消息体量**定的，
        # 而且它带命令原文 + 目录 + 状态 + 截断说明，比自己拼的文本可信。
        text = executor.format_result(res)
    except Exception as e:
        return f"命令跑完了，但结果渲染不出来：{e}"
    print(f"[bot] 命令结束 ok={getattr(res, 'ok', None)} "
          f"exit={getattr(res, 'exit_code', None)}")
    return text


# ============================================================
#  本地执行的**确定性兜底**：说的和做的必须对得上
# ============================================================

# 真机踩过（2026-10-01）：用户说「在电脑上跑一条命令看看：dir /b」，模型**没有**
# 调用 run_command 工具，直接自己演了一段「好的，我来提交这条命令，但它不会立刻
# 执行——需要你回『确认』后才真跑。命令原文：dir /b」。
# 日志里没有 `工具 run_command` 行、也没有任何待确认项——用户以为登记好了，回
# 「确认」时什么都不会发生。这直接踩中项目硬规矩「没跑就是没跑」。
#
# 光靠 system_prompt 叮嘱不管用（提示词是建议，不是保证）。所以这里做一层
# **确定性**兜底：answer 里出现了「已经提交 / 已登记 / 等你确认」这类话，而本轮
# ToolBox 的 shell_queued 为假（= 一个字都没登记），就固定追加一句真话。
#
# 判据宁可少触发，分两档（**不按长度卡**：长度不是"有没有声称执行"的判据，
# 话术才是，而且按长度早退会把长一点的谎报正好放过去——真机那条原话就不短）：
#   ① 明确的**完成态**说法（带「已/了」）：单独出现就算；
#   ② 「提交/登记/排队/待确认」这类**动作词**，必须和确认词**出现在同一行**才算。
#      —— 真机原话是「好的，我来提交这条命令，但它不会立刻执行——需要你回
#      「确认」后才真跑」，正好落在这一档。
#   ③ 出现**否定 / 反问 / 假设**语义一律不追加：模型在如实说"我这边什么都没有"
#     或反问用户"是不是指跑命令"时，不能再被追加一句真话，那是自相矛盾的啰嗦。
#      真实反例：「目前挂着的只有一条**命令待确认**：dir /b」——用了「待确认」，
#      但那是**说明现状**，不是声称自己刚登记，所以必须被这一档拦下。
#      按 lead 的取舍：**简单可预期的子串判断优先于聪明**（宁可少触发）。
#   ④ 只在同一句里出现「命令」「执行」这类词**不算**——否则正常聊天老被追加。
_PENDING_CLAIM_DONE = (
    "已提交", "已经提交", "提交了", "已登记", "已经登记", "登记了",
    "已排队", "已经排队", "排队了", "已待确认", "已加入待确认", "已生成待确认",
)
_PENDING_CLAIM_ACT = ("提交", "登记", "排队", "待确认")
_PENDING_CLAIM_CONFIRM = ("确认", "等你", "待你", "等您", "待您")
# 否定 / 反问 / 假设（整段里出现任意一个 → 不追加）
_PENDING_CLAIM_NEG = (
    "没有", "没什么", "没看到", "没查", "没提交", "没登记", "没排队",
    "没待确认", "未提交", "未登记", "未排队", "未找到",
    "不是", "不确定", "是不是", "如果你要", "如果你", "请明确", "要不要",
    "只有一条", "挂着", "没有任何",
)

# 没有真登记时固定追加的真话。**别删**——这是真机上唯一能拦住
# 「模型自己演一段已登记」的东西（见上）。
SHELL_NOT_QUEUED_NOTE = ("\n\n（补充：我这边其实**还没有登记任何本地命令**。"
                         "要真在电脑上跑，我得先调用工具把命令原文提交上来、"
                         "你再回「确认」——刚才那条如果你要执行，请回「确认」或再说一次。）")


def looks_like_pending_claim(text):
    """这句话像不像在声称「我已经把命令提交上去、等你确认了」。

    两档判据 + 一道否定闸，见上面注释。**宁可少触发**：只说「命令/执行」
    不算；拿不准的一律当"不是声称"（少追一句补充，好过把实话再包一层）。
    """
    t = str(text or "")
    if not t:
        return False
    # ③ 否定 / 反问 / 假设闸（门在最前面，简单子串，宁可少触发）
    if any(w in t for w in _PENDING_CLAIM_NEG):
        return False
    # ① 完成态
    if any(w in t for w in _PENDING_CLAIM_DONE):
        return True
    # ② 动作词 + 确认词同一行（真机原话就是这一档）
    for line in t.splitlines():
        if any(a in line for a in _PENDING_CLAIM_ACT) \
                and any(c in line for c in _PENDING_CLAIM_CONFIRM):
            return True
    return False


def with_shell_truth_note(answer, shell_queued):
    """回答里声称「已提交命令」但本轮根本没登记时，追加一句真话。

    shell_queued 来自本轮 ToolBox（真的 set_pending 过才为真）。
    不追加的三种情况：文本为空（shell.auto_ok 那轮工具直接执行、模型可能没回话）、
    本轮真登记过、以及判据没命中（没声称、或者说的是"我这边没有待确认"）。
    """
    text = str(answer or "")
    if not text or shell_queued:
        return text
    if looks_like_pending_claim(text):
        # 记一笔日志：这类"说的和做的不一致"值得回头统计（模型行为问题）
        print("[bot] 回答声称已提交命令，但本轮没有登记任何 shell 项 → 追加真话")
        return text + SHELL_NOT_QUEUED_NOTE
    return text


def with_image_notes(answer, notes):
    """把「这一轮的图片没给成」之类的**如实说明**追加在答复后面。

    和 `with_shell_truth_note` / `with_broadcast_preview` 同一个姿势：
    事实由工具层记下来，bot 原样补上——绝不让模型自己转述
    （它多半会漏掉"有一张没给模型看"，用户就以为模型看过全部图了）。
    """
    rows = [str(n).strip() for n in (notes or []) if str(n).strip()]
    text = str(answer or "")
    if not rows:
        return text
    return (text.rstrip() + "\n\n" + "\n".join(rows)).strip()


def attach_images(messages, items):
    """把「要交给模型看的原图」附成**这一次调用**的一条 user 消息。

    两条硬规矩（见 docs/file-input-spec.md 第八节）：
      * 只附**这一次**：图片不进 `messages` 列表，后续轮次不会重发（token 会翻倍）；
      * **绝不进 `bot.dialog_*`**：那份记忆每轮都重发，图进去＝反复计费。
    """
    if not items:
        return messages
    blocks = [{"type": "text", "text": "（下面是刚读到的图片原图，请直接看图回答）"}]
    for path, label in items:
        try:
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
        except OSError as e:
            print(f"[bot] ⚠️ 这张图附不上去（{e}）：{path}")
            continue
        ext = "png" if str(path).lower().endswith(".png") else "jpeg"
        blocks.append({"type": "text", "text": f"【{label}】"})
        blocks.append({"type": "image_url",
                       "image_url": {"url": f"data:image/{ext};base64,{data}"}})
    if len(blocks) == 1:
        return messages
    return list(messages) + [{"role": "user", "content": blocks}]


def with_broadcast_preview(answer, preview):
    """群发的预览（人数 + 逐条正文）**由 bot 原样补在答复后面**。

    为什么不靠模型转述：用户是照着这段回「确认」的，而里面有**人数**和**每条正文**——
    模型转述十条正文必然走样（漏一条、改一个字，用户就在没看清的情况下把消息
    发给了一群人）。所以这里和 `with_shell_truth_note` 一个姿势：拿 ToolBox 记下的
    **事实**（`box.broadcast_preview`）直接给用户看。

    放在答复**之后**：模型常常会写一句「已经准备好了，回确认即可」，那句在前、
    权威的那段在后，用户看到的是能照着确认的东西。
    """
    prev = str(preview or "").strip()
    text = str(answer or "")
    if not prev:
        return text
    return (text.rstrip() + "\n\n" + prev).strip()


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


def _probe_login(client):
    """探一次登录态，返回 (是否在线, 说明)。

    只用 hook 的只读接口，**不碰数据库句柄表**（不触发那 700MB 进程里的全内存扫描）。
    探测本身失败时按「不在线」处理并如实记下原因——分诊要保守，宁可多报一次。
    """
    try:
        ok = bool(client.is_login())
    except Exception as e:
        return False, f"探登录态失败：{e}"
    if not ok:
        return False, "IsLogin: 0（微信可能停在登录界面）"
    return True, ""


def _msg_ts(msg):
    try:
        return int(getattr(msg, "create_time", 0) or 0)
    except (TypeError, ValueError):
        return 0


def stash_control_media(wcf, cfg, talker, msg, send):
    """控制会话来了图片/表情/视频 -> 暂存进素材区并回执。返回 True = 已处理。

    **为什么不把这件事交给模型**：素材是「刚才那张」。模型看不到图片内容、也拿不到
    local_id（图片/表情根本不在 fts 里，见 live_history 的非文本补漏），让它去认
    只会来回问用户。收到就存 + 回一句回执，用户才知道「它记住了」——这是确定性
    动作，不该看模型心情。

    **只收用户自己发出去的**（`msg.from_self()`）：控制会话是「和自己说话」，别人
    发进来的图不该被悄悄收进暂存区——那会让「发给谁」把对方刚发来的东西又发出去。
    `is_self` 取不到时（None）**不当素材**：宁可漏判，也不误存。

    取不到那条消息的原始 XML 时**如实回一句「暂存失败」**并返回 True（这轮到此为止），
    绝不退化成「先存个空壳」——空壳存下去，「发给谁」时会变成一个发不出去的东西。
    """
    if not (cfg.get("assets") or {}).get("enabled", True):
        return False
    kind = live_history.media_kind(getattr(msg, "local_type", 1))
    if not kind or not msg.from_self():
        return False

    ts = _msg_ts(msg)
    try:
        rows = live_history.latest_media(wcf, talker, limit=3)
    except Exception:
        traceback.print_exc()
        rows = []
    mine = [r for r in rows if r.get("is_self") == 1]
    target = next((r for r in mine if _msg_ts_key(r) == ts), None)
    if target is None and len(mine) == 1 and abs(_msg_ts_key(mine[0]) - ts) <= 120:
        # 库里 create_time 与轮询拿到的时间戳可能有秒级差异：只有**唯一候选**
        # 且差得很小时才认。认错一条就是把别的消息存进素材区，宁可让用户重发一次。
        target = mine[0]
    if target is None:
        print(f"[bot] 素材暂存失败：找不到对应的消息"
              f"（local_type={int(getattr(msg, 'local_type', 0))} ts={ts}）")
        send(f"{assets.this_label(kind)}我取不到原始内容，暂存失败"
             f"（转发它需要那条原始消息）。", talker)
        return True

    cap = assets.cap_of(cfg)          # 唯一一处钳制逻辑（assets.cap_of）
    # 明文来源按「质量/可靠性」排序，取到就用，取不到就退一步：
    #   ① 微信发图时暂存的**明文原图**（`temp\RWTemp`，实测比消息行早 2 秒）——原图质量，
    #      而且这是「自己在微信里发的图」唯一能拿到的明文；它会被清理，所以立刻复制走；
    #   ② 微信缓存的**明文缩略图**（有的话，质量差些）；
    #   ③ 都没有就只留一条**消息引用**（发不出去，回执里会明说）。
    # 为什么要这么麻烦：hook 的 XML 转发在 4.1.10.27 上会把微信搞崩、已在源码里禁用
    # （见 wx_send_xml.cpp），所以**只有明文发得出去**。
    pl = str(target.get("image") or "")
    item = None
    note = ""
    if kind == "图片":
        try:
            item, note = agent_tools.capture_sent_plaintext(
                ts, kind=kind, talker=talker, local_id=target.get("local_id"))
        except Exception:
            traceback.print_exc()
            item, note = None, ""
    if item is None and pl and os.path.isfile(pl):
        item = assets.entry_from_file(pl, kind=kind, talker=talker,
                                     local_id=target.get("local_id"))
        note = "明文缩略图"
    try:
        if item is None:
            try:
                xml = live_history.message_xml(wcf, talker, target["local_id"])
            except Exception:
                traceback.print_exc()
                xml = ""
            if not xml:
                print(f"[bot] 素材暂存：local_id={target.get('local_id')} "
                      f"既没有明文也取不到原始 XML")
                send(f"{assets.this_label(kind)}我既拿不到明文、也取不到原始内容，"
                     f"暂存失败——要发它请**以「文件」方式**再发一次。", talker)
                return True
            item = assets.entry_from_media(target, xml)
            note = "消息引用（发不出去）"
        items, added, dropped = assets.stash(item, cap)
    except Exception as e:
        traceback.print_exc()
        send(f"暂存{assets.this_label(kind)}时出错：{e}", talker)
        return True

    print(f"[bot] 素材暂存 -> {talker}: {kind} local_id={target.get('local_id')} "
          f"共 {len(items)}/{cap} 条" + (f"，顶掉最老的 {dropped} 条" if dropped else ""))
    # 刚收到的这条**永远是第 1 条**（stash 把它挪到末尾），也就是说「发给谁」
    # 默认发的就是它——这正是用户要的「发一次，然后说发给谁」。
    if added:
        head = f"已暂存{assets.this_label(kind)}。"
    else:
        head = f"{assets.this_label(kind)}刚才已经存过了，还是用它。"
    if dropped:
        extra = f"（最多 {cap} 条，顶掉了最老的 {dropped} 条）"
    elif len(items) > 1:
        extra = f"（暂存区共 {len(items)} 条，第 1 条是最近那张）"
    else:
        extra = ""
    # 回执必须说清**这张到底发不发得出去**：hook 的 XML 转发在 4.1.10.27 上会把
    # 微信搞崩、已在 hook 源码里禁用（见 wx_send_xml.cpp），所以只有拿到明文
    # （微信缓存的缩略图 / 用户以「文件」方式发来的原图）才发得出去。
    # 不告诉用户的话，他会以为「已暂存 = 随时能发」，然后撞一句"发不了"。
    if assets.plaintext_of(items[-1]):
        tail = (f"说「发给谁」我就发（存的是{note or '明文图'}）；"
                f"想连发就说「发 3 次」。")
    else:
        tail = ("⚠️ 但这张**只有消息引用、发不出去**（微信只留加密原图，"
                "hook 的转发接口会把微信搞崩、已禁用）——要发它，"
                "请把这张图**以「文件」方式**再发一次，那样就有明文了。")
    send(head + tail + extra, talker)
    return True


def _msg_ts_key(row):
    try:
        return int((row or {}).get("_ts") or 0)
    except (TypeError, ValueError):
        return 0


def iter_aixed_messages(client, interval, tick=None, cfg=None):
    """aixed 没有收消息接口，只能轮询数据库拿新消息。

    传了 `cfg` 才有「重启续上」——见下面 resume_window 的说明。
    """
    global _LAST_CURSOR_SAVE
    tick = tick or (lambda: None)
    st = (cfg or {}).get("state") or {}
    try:
        resume_window = max(0, int(st.get("resume_window", 1800)))
    except (TypeError, ValueError):
        resume_window = 1800

    # 重启后从**上次的游标**接着收，而不是一刀切到「最新」。
    # 以前 `prime()` 把当前最新那批直接标成已见，停机期间来的消息就永远丢了，
    # 用户看到的是「重启后漏了一段」。窗口外的（比如关机一整晚）不续——
    # 续了等于把一大段历史当新消息重放。
    saved = state_get("cursor")
    cursor, seen = None, {}
    if resume_window > 0 and isinstance(saved, dict) and saved.get("cursor"):
        try:
            age = time.time() - float(saved.get("ts") or 0)
        except (TypeError, ValueError):
            age = None
        if age is not None and age <= resume_window:
            cursor = saved["cursor"]
            print(f"[bot] 从上次的游标继续（{int(age)} 秒前的位置）："
                  f"停机期间的旧消息只通知、不自动回复")
    if cursor is None:
        cursor, seen = client.prime()
    print(f"[bot] 轮询模式：游标 = {cursor}，间隔 {interval}s")
    polls = 0
    fails = 0          # 连续轮询失败次数（成功一次就清零）
    while True:
        # 每一轮轮询之前先跑一次定时任务：空闲时这个循环每 interval 秒转一圈，
        # 所以定时精度就是 poll_interval（默认 5 秒）。
        tick()
        msgs = []
        try:
            msgs, cursor, seen = client.poll_messages(since=cursor, seen=seen)
        except AixedError as e:
            # 常见情况（hook 没起来/微信没登录）。**不许每次都刷屏**：
            # 持续失败时它会每 poll_interval 打一行，一晚上能把日志轮转刷穿。
            # 按 1/10/50 次打印，之后每 50 次提醒一次——和 live_history._note_poll_error 同一路子。
            fails += 1
            if fails in (1, 10, 50) or fails % 50 == 0:
                print(f"[bot] ⚠️ 轮询第 {fails} 次失败：{e}")
        except Exception:
            # 同理：完整 traceback 更不能每轮都刷。
            # （以前这里只捕 AixedError、别的异常被静默吞掉；现在既不静默、也不刷屏。）
            fails += 1
            if fails in (1, 10, 50) or fails % 50 == 0:
                print(f"[bot] ⚠️ 轮询第 {fails} 次失败（未预期异常）：")
                traceback.print_exc()
        else:
            fails = 0
        # 游标落盘（节流 10 秒一次）
        if time.time() - _LAST_CURSOR_SAVE >= 10:
            _LAST_CURSOR_SAVE = time.time()
            try:
                state_set("cursor", {"cursor": cursor, "ts": time.time()})
            except Exception:
                traceback.print_exc()
        polls += 1
        h = _h()
        if h is not None:
            # 每轮都喂一次（实现是纯内存的，不做 IO）；心跳那行再带上分片错误
            try:
                h.note_poll(cursor=cursor)
            except Exception:
                traceback.print_exc()
        if polls % 30 == 0:
            errs = live_history.poll_errors()
            extra = ""
            if errs:
                names = "、".join(f"{k}({v[1]}次)" for k, v in errs.items())
                extra = f"  ⚠️ 分片查询失败：{names}"
            print(f"[bot] 轮询心跳 #{polls}，游标={cursor}{extra}")
            if h is not None:
                # 定期分诊「是不是掉登录了」。CLAUDE.md 记着：微信会自己重启到
                # 登录界面，表现和 fts 静默失效几乎一样，而恢复只能人工扫码——
                # 所以必须主动探测并告警，不能等用户自己发现「bot 没反应」。
                try:
                    h.note_poll(cursor=cursor, errors=errs)
                    if h.due_login_check():
                        h.note_login(*_probe_login(client))
                    h.write_status()
                except Exception:
                    traceback.print_exc()
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

    # 本进程的启动时刻。「重启补齐」判定靠它：**早于它**的消息只可能来自
    # 落盘游标续上来的那批（正常运行时起点就是「最新」，不会造出更早的消息）。
    _START_TS = time.time()

    # 健康看护：只收「已经发生的事实」，自己绝不查库 —— 不碰 hook 并发那条铁律。
    global _HEALTH
    if health is not None:
        try:
            _HEALTH = health.Health(cfg, notify_fn=health.notify)
        except Exception:
            traceback.print_exc()
            _HEALTH = None
    else:
        print("[bot] ⚠️ 没找到 health.py：健康看护 / 日志轮转 / 状态页都不可用。")

    llm = make_llm(cfg)
    history_file = cfg.get("history_file", "data/history.jsonl")
    if not os.path.isabs(history_file):
        history_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), history_file)
    static_history = HistoryStore(history_file)

    # 连接微信；后台自启时微信可能还没启动，两种后端都会重试等待（最多约 5 分钟）
    backend = cfg.get("backend", "wcferry")
    # 默认值必须跟 config.yaml / CLAUDE.md 一致（5 秒）。以前这里写 2，而
    # config.yaml 的注释明确写着「实测 2 秒间隔会把微信卡到 CPU 999 秒」——
    # 配置里一漏这一行，代码就挑了个注释亲口反对的值。
    poll_interval = cfg.get("poll_interval", 5)
    if backend == "aixed":
        # 兜底端口必须跟 config.yaml / CLAUDE.md / postman 一致（30001）。
        # 这里以前写 8080：配置里一旦漏了 aixed_base_url，就会连错端口，
        # 而报错文案却指向「微信没启动、version.dll 没加载」，排查方向全错。
        wcf = connect_aixed(cfg.get("aixed_base_url", "http://127.0.0.1:30001"))
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
    # 待确认项总是登记在**控制会话**上（工具层的 self.chat / 审核草稿都发这儿）。
    # 控制会话可能不在 target_chats 里（例如一个都没配、默认文件传输助手），
    # 所以落盘/恢复的会话集合要把两边并起来，否则那条队列永远存不下来。
    pending_chats = sorted(set(targets) | {control_chat})

    # 把盘上的待确认队列捡回来：不然重启之后用户照着刚才看到的提示回「确认」，
    # 什么都不会发生（以前就是这样，白等一场）。
    try:
        n_back = restore_pending(pending_chats, cfg)
        if n_back:
            print(f"[bot] 从盘上恢复了 {n_back} 条待确认动作（还在时效内）")
            try:
                items = []
                for c in pending_chats:
                    items += agent_tools.list_pending(
                        c, int((cfg.get("agent") or {}).get("confirm_ttl", 300)))
                lines = [f"重启后还有 {n_back} 条待确认的动作（还在时效内）："]
                for i, it in enumerate(items, 1):
                    lines.append(f"{i}) {agent_tools.describe_pending(it)}")
                lines.append("要执行/发送请回「确认 <编号>」，不要了回「不发」。")
                send("\n".join(lines), control_chat)
            except Exception:
                traceback.print_exc()
                send(f"重启后还有 {n_back} 条待确认的动作。回「确认」可执行第一条，"
                     f"回「不发」全部取消。", control_chat)
    except Exception:
        traceback.print_exc()

    # 同一个会话不能既是控制会话又是自动回复对象，否则对方发来的消息会被当命令解析。
    # /auto add 时已经拦了，这里是兜底（比如用户手改了 config.yaml）。
    for chat in sorted(set(auto_recs) & targets):
        print(f"[bot] ⚠️ {chat} 同时在 target_chats 和 auto_reply.chats 里，"
              f"自动回复对它不生效，请二选一。")
        auto_recs.pop(chat, None)

    def send(text, to):
        """发消息。**绝不抛异常**，返回是否真的发出去了。

        以前这里是裸调用：hook 一抖、`send_text` 抛 AixedError，异常就顺着主循环
        冒泡把整个进程带走（三条入口都是后台无窗口起的，没人会把它拉起来）。
        收消息那条路本来有 try 兜着，命令 / 盯着 / 确认这几条没有——坏在一处就全下线。
        现在统一在这里兜住，并且把失败**如实报出来**。

        **故意不自动重试**：发消息不可逆，超时/HTTP 500 的情况下第一次到底发没发出去
        无法确认，重试就可能给对方发两条。宁可如实说「没发出去」。
        """
        try:
            wcf.send_text(text, to)
        except Exception as e:
            print(f"[bot] ⚠️ 发送失败（未重试）→ {to}：{e}")
            h = _h()
            if h is not None:
                try:
                    h.note_send_failure(e)
                except Exception:
                    pass
            # 尽量让用户知道。这条用裸调用并吞异常：失败告警自己再失败不许绕成递归。
            if to != control_chat:
                try:
                    wcf.send_text(f"⚠️ 有一条消息没能发出去（对象 {to}）：{e}", control_chat)
                except Exception:
                    pass
            return False
        remember_sent(text)
        h = _h()
        if h is not None:
            try:
                h.note_sent()
            except Exception:
                pass
        return True

    print(f"[bot] 后端：{backend}  |  监听中，控制会话：{targets}  |  只回目标：{reply_only}")
    if auto_recs:
        who = "、".join(f"{r.get('name') or w}({w})" for w, r in auto_recs.items())
        print(f"[bot] 自动回复：{'开启' if auto_on else '关闭'}  |  {who}")
    else:
        print("[bot] 自动回复：未配置（在微信里发 /auto add <昵称> 添加）")
    print("[bot] 在微信里发 /help 查看可用的配置命令。Ctrl+C 退出。")
    print(f"[bot] {scheduler.summary_line(cfg)}  |  {watch.summary_line(cfg)}")

    # 只读状态页（默认关闭，见 config.yaml 的 status 段）。
    # **只渲染内存快照、绝不查库**，所以它不违反「hook 不支持并发」那条铁律。
    # 只允许绑回环地址；status_page 自己会拒绝其它地址。
    status_srv = None
    _st_cfg = cfg.get("status") or {}
    if status_page is not None and _st_cfg.get("enabled") is True:
        def _status_snapshot():
            h = _h()
            return h.snapshot() if h is not None else {}

        try:
            status_srv = status_page.start(
                host=str(_st_cfg.get("host") or "127.0.0.1"),
                port=int(_st_cfg.get("port") or 39002),
                snapshot_fn=_status_snapshot,
                log=print,
            )
        except Exception:
            traceback.print_exc()
    elif status_page is None:
        print("[bot] ⚠️ 没找到 status_page.py：只读状态页不可用。")

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
                run_state = {}
                answer, changed = run_agent(
                    llm, system, prompt, wcf, contacts, cfg, control_chat, self_wxid,
                    cfg_provider=lambda: settings.effective(base_cfg),
                    history=history, state=run_state)
                # 定时的「提问」走的也是这条路：模型说「已提交命令等你确认」而
                # 本轮其实没登记时，同样要追一句真话（否则用户回「确认」白等）。
                answer = with_shell_truth_note(answer, run_state.get("shell_queued", False))
                answer = with_image_notes(answer, run_state.get("image_notes"))
                # 群发预览**原样**带上（模型转述十条例文必走样）。
                answer = with_broadcast_preview(
                    answer, run_state.get("broadcast_preview", ""))
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

        # 后台读文件的结果：**由主线程发**（worker 只碰磁盘和模型 HTTP，绝不碰 hook）。
        # 挂在轮询空档里，和定时任务是同一个姿势 —— 不新增线程碰微信。
        try:
            for r in read_worker.drain():
                label = str(r.get("label") or "那份文件")
                if r.get("err"):
                    body = (f"读不了：{r['err']}\n"
                            f"（要在本机再试一次就说「重新读一下 {label}」）")
                    head = f"⚠️ 刚才那份「{label}」没读成。"
                else:
                    body = (r.get("text") or "（读出来是空的）")
                    head = f"📄 刚才那份「{label}」读完了："
                tail = ""
                if r.get("slow"):
                    tail = (f"\n\n（这次读得比预期久：用了 {r.get('seconds', 0):.0f} 秒；"
                            f"如果这已经超过 read.job_timeout，可以在 config.yaml 调大）")
                send(head + "\n" + body + tail, r["chat"])
        except Exception:
            traceback.print_exc()

    def make_source():
        """建收消息的迭代器。

        它是生成器：内部一旦抛出异常，这个生成器就死了、再也取不到消息，
        所以必须包成函数，好在它死掉之后重建一个——微信自己重启、hook 掉了都会遇上。
        """
        return (iter_aixed_messages(wcf, poll_interval, tick=_Ticker(run_scheduled),
                                    cfg=cfg)
                if backend == "aixed"
                else iter_wcferry_messages(wcf, tick=_Ticker(run_scheduled)))

    # 「重启补齐」：只提示一次，别每条都发
    _catchup_announced = False
    _catchup_total = 0

    # 上次有读取没读完（助手重启过）→ **如实说一句**，别假装读过
    try:
        _left = read_worker.startup_note()
        if _left:
            send(_left, control_chat)
    except Exception:
        traceback.print_exc()

    # 三处临时目录的兜底清理（正常路径用完就删；这里防上次被强杀留下的残渣）。
    # 每次都只 listdir + 按时间删，**删了什么各自会打日志**（静默丢弃不允许）。
    for _mod_name in ("image_read", "video_read", "archive_read", "mail_read"):
        try:
            _mod = __import__(_mod_name)
            _mod.sweep_tmp()
        except Exception as e:
            print(f"[bot] ⚠️ {_mod_name}.sweep_tmp 失败（不影响启动）：{e}")

    source = make_source()

    # 外层守护：**任何未预期的异常都不许让进程退出。**
    # 这是后台无窗口进程（三条入口都会起它）：一旦退出就没人收消息、也没人把它拉起来，
    # 用户只会觉得「bot 没反应」——正是 CLAUDE.md 里最怕的那种静默失效。
    # 所以这里兜一层：把栈打出来、等 5 秒、接着跑。
    while True:
        try:
            while True:
                try:
                    msg = next(source)
                except StopIteration:
                    print("[bot] ⚠️ 收消息通道结束了，重建一个（微信可能重启过）")
                    time.sleep(5)
                    source = make_source()
                    continue

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

                # 「重启补齐」判定：比 stale_after 秒还旧、**且早于本进程启动**的消息，
                # 只可能是从落盘游标续上来的那一批。按年龄判、不按「第几轮」判，
                # 所以停机期间积压多少条都不会漏判、也不会把正常消息误判成补齐。
                try:
                    catchup_after = int((cfg.get("state") or {}).get("stale_after", 120))
                except (TypeError, ValueError):
                    catchup_after = 120
                try:
                    _msg_ts = float(getattr(msg, "create_time", 0) or 0)
                except (TypeError, ValueError):
                    _msg_ts = 0.0
                # 上界：机器时钟被往前拨、或 DB 时间戳落在未来时，
                # 「早于本进程启动」和「够旧」可能同时成立，把**新**消息误判成补齐 → 静默丢掉。
                # 所以再要求它不在未来（留 5 分钟余量给时钟漂移）。
                _now = time.time()
                catchup = (bool(catchup_after) and _msg_ts > 0
                           and _msg_ts <= _now + 300
                           and _msg_ts < _START_TS
                           and (_now - _msg_ts) > catchup_after)

                # 图片消息：**自己刚发出去的那张会作为「我发的新消息」回显回来**
                # （图片不在 fts 里，是靠 live_history 的非文本补漏捞回来的，见那边
                # 的 docstring），不能当成新消息再答一遍。文本有 is_own_reply 兜着，
                # 图片没有，所以这里用「会话 + 时间窗」认（agent_tools 那组簿记）。
                if getattr(msg, "local_type", 1) != 1 \
                        and agent_tools.is_own_image(sender, _msg_ts):
                    print(f"[bot] 跳过（这是自己刚发出的图片）: {sender}")
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
                    try:
                        hit_text = watch.format_hit(watched, query)
                    except Exception:
                        # 渲染失败也必须把「他发消息了」告诉用户，别整条吞掉
                        traceback.print_exc()
                        hit_text = (f"【盯着】{watched.get('name') or sender} "
                                    f"发了一条消息（内容渲染失败）")
                    send(hit_text + ("（重启补齐）" if catchup else ""), control_chat)
                    print(f"[bot] 盯着命中 {watched.get('name') or sender}: {query[:40]}")
                    continue

                # 补齐期的旧消息：除了上面「盯着」的通知，**一律不处理**。
                # 自动回复尤其不能补——那是在替用户本人说话，几小时前的话现在代回
                # 比漏掉更糟；命令和提问也不补（用户当时的意图早就过去了）。
                if catchup:
                    _catchup_total += 1
                    if not _catchup_announced:
                        _catchup_announced = True
                        send("⚠️ 重启补齐：停机期间还有消息没处理。这些**只通知、"
                             "不自动回复**（几小时前的话现在代你回，比漏掉更糟）。",
                             control_chat)
                    print(f"[bot] 补齐跳过（{int(time.time() - _msg_ts)} 秒前的消息）: "
                          f"{query[:30]}")
                    continue

                # 1.1) 素材暂存：控制会话里发来的图片/表情/视频，收到就记下来
                #      （之后说「发给谁」就能再发，见 assets.py）。
                #      插在**补齐判定之后**：停机期间的旧图不补存——那个「刚才那张」
                #      的语境早就过去了，存进来只会让「发给谁」发错东西。
                if in_targets and stash_control_media(wcf, cfg, sender, msg, send):
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
                        # 审核模式会在上面登记一条草稿 → 立刻落盘
                        save_pending(pending_chats, cfg)
                    else:
                        print(f"[bot] 自动回复跳过（冷却中）: {rec.get('name') or sender}")
                    continue

                # 1) 命令优先
                #    命令处理器会读盘（settings.effective(load_config())），config.yaml
                #    写坏了会抛 YAML 错误——那属于「这一条消息没处理成功」，
                #    不该让整个 bot 下线。
                try:
                    reply, changed = handle_command(query, wcf, cfg, live_ok, contacts)
                except Exception as e:
                    traceback.print_exc()
                    send(f"这条命令没处理成功：{e}", sender)
                    continue
                if reply is not None:
                    send(reply, sender)
                    if changed:
                        try:
                            reload_cfg()
                        except Exception as e:
                            traceback.print_exc()
                            send(f"命令生效了，但重新加载配置失败：{e}", sender)
                    print(f"[bot] 命令回复: {reply[:60]}")
                    continue

                # 1.5) 待确认的动作：用户回「确认」才真发 / 真跑
                #      不再限定 agent_enabled：审核模式下的自动回复草稿也要走这里。
                pending_sel = pending_index_of(query)
                if is_confirm(query) or is_cancel(query) or pending_sel is not None:
                    ttl = int((cfg.get("agent") or {}).get("confirm_ttl", 300))
                    item = None

                    # 「不发」= 取消该会话全部待确认项
                    if is_cancel(query):
                        n = agent_tools.discard_pending(sender)
                        if n:
                            save_pending(pending_chats, cfg)      # 队列已变，立刻落盘
                            send(f"已取消 {n} 条待确认的动作。", sender)
                            continue

                    pending = agent_tools.list_pending(sender, ttl)
                    if not pending:
                        # 没有待确认项：这句话不是给队列的，落到下面走普通问答
                        pending_sel = None
                    else:
                        want = pending_sel if len(pending) > 1 else None
                        if len(pending) > 1 and want is None:
                            # **队列压着多条时绝不猜。** 以前只对队头判严格词、再 pop
                            # 出同一个队头，于是「心里想确认 A、实际执行 B」是可达的
                            # （项目自己的复核报告 R5-3 就是这个）。先让用户点编号。
                            lines = [f"待确认的有 {len(pending)} 条，"
                                     f"回「确认 <编号>」指明是哪一条："]
                            for i, it in enumerate(pending, 1):
                                lines.append(f"{i}) {agent_tools.describe_pending(it)}")
                            send("\n".join(lines), sender)
                            continue
                        if want is not None and not (1 <= want <= len(pending)):
                            send(f"只有 {len(pending)} 条待确认项，没有第 {want} 条。", sender)
                            continue
                        head = pending[0] if want is None else pending[want - 1]
                        # 自动回复草稿是要发给**别人**的，只认明确的中文确认词，
                        # 免得在控制会话里随口一句「ok」就把草稿发出去。
                        if head.get("kind") == "auto" and not is_strict_confirm(query):
                            send("这条是自动回复草稿。要发请回「确认」，不发请回「不发」。",
                                 sender)
                            continue
                        # 本地执行比发消息更不可逆（发错了能解释，命令跑了就跑了），
                        # 所以比 agent 发送**更严**：只有「确认/确定/确认发送」才算数，
                        # 随口一句 ok / y / 发送 一律不算。判定方式和 kind="auto" 一致，
                        # 但理由不同：那边是防手滑发错人，这边是防手滑在本机真跑一条命令。
                        if head.get("kind") == "shell" and not is_strict_confirm(query):
                            send(f"这条是本地命令，要真在电脑上跑它。确认请回「确认」"
                                 f"（**不是**「ok」，本地执行只认「确认/确定/确认发送」），"
                                 f"不跑请回「不发」。\n命令原文：{head.get('cmd') or ''}", sender)
                            continue
                        # 群发**两道确认**：范围那一步和内容那一步都只认明确的中文
                        # 确认词。理由同类：群发一口气发给 N 个人、内容还不是用户写的，
                        # 在控制会话里随口一句「ok」就发出去太危险（真机踩过手滑）。
                        if head.get("kind") in ("broadcast_scope", "broadcast") \
                                and not is_strict_confirm(query):
                            send("这条是群发确认。要继续请回「确认」（**不是**「ok」），"
                                 "不发了请回「不发」。", sender)
                            continue
                        # 既没点号、也不是确认词（例如一句带数字的闲聊）：不当成确认
                        if want is None and not is_confirm(query):
                            pending_sel = None
                        else:
                            item = agent_tools.pop_pending(sender, ttl, index=want)
                            # 队列已经变了就**立刻**落盘：否则「这条已经执行了」
                            # 和「盘上还记着它」之间有窗口，崩溃重启会把它恢复出来。
                            save_pending(pending_chats, cfg)
                    if item:
                        # ⚠️ 同一条待确认项绝不执行两次（已执行指纹落盘，扛得住重启）。
                        # 典型场景：发送成功后、`save_pending` 落盘之前进程死掉，
                        # 重启后这条被恢复出来——它其实已经发出去了，再执行一次就是
                        # **给别人重复发消息**。指纹里带这条自己的 `ts`，所以只拦
                        # 「同一条」，用户重说一遍生成的新条目照常放行。
                        dup, ago = already_executed(item, cfg)
                        if dup:
                            print(f"[bot] 拒绝重复执行（{_fmt_ago(ago)} 前执行过）: "
                                  f"{describe_executed(item)}")
                            send(f"这一条**没有重复执行**：{describe_executed(item)}"
                                 f"在 {_fmt_ago(ago)} 前已经执行过了。\n"
                                 f"（同一条待确认项只执行一次。你要是确实想再来一次，"
                                 f"重新说一遍就行——那会是一条新的。）", sender)
                            continue
                    if item and item.get("kind") == "broadcast_scope":
                        # 第一道确认（范围）已过 -> 现在才**生成内容**并分流。
                        # 这一段不过模型：范围是用户亲自确认的，剩下只是照做。
                        report, berr = agent_tools.finish_broadcast(
                            wcf, sender, item, llm, cfg)
                        if berr:
                            send(f"群发没有进行：{berr}", sender)
                            continue
                        send(report, sender)
                        print(f"[bot] 群发范围已确认 -> {len(item.get('items') or [])} 人")
                        remember_executed(item, cfg)     # 范围这步也算「执行过」了
                        continue
                    if item and item.get("kind") == "broadcast":
                        # 第二道确认（内容）已过 -> 逐条发出。**逐字发预览里那一条**。
                        batch = list(item.get("items") or [])
                        try:
                            interval = max(0.0, float(
                                (cfg.get("agent") or {}).get("send_interval", 1.5)))
                        except (TypeError, ValueError):
                            interval = 1.5
                        n, err = agent_tools.send_pending(wcf, item, interval)
                        if err is not None:
                            print(f"[bot] 群发失败（已发 {n}/{len(batch)}）: {err}")
                            send(f"群发只发出了 {n}/{len(batch)} 条。{err}", sender)
                        else:
                            print(f"[bot] 群发完成 {n} 条")
                            send(f"群发 {n} 条已发出（逐条按上面那段原文发的）。", sender)
                        # 失败也记：已经发出去的那 n 条收不回来，重来一遍会重复发
                        remember_executed(item, cfg)
                        continue
                    if item and item.get("kind") == "shell":
                        # 本地执行：用户回「确认」才真跑。shell 没有收件人——绝不走
                        # send_pending、也绝不发给 item["to_wxid"]（它是空的），
                        # 只把结果发回**发起确认的那个会话**（sender）。
                        if not executor.enabled(cfg):
                            # 登记之后用户把开关关了：如实说没跑，不偷偷执行
                            send(f"本地执行已经关掉了（config.yaml 的 shell.enabled），"
                                 f"这条命令**没有执行**。", sender)
                            continue
                        secs = (cfg.get("shell") or {}).get("timeout") or executor.DEFAULT_TIMEOUT
                        send(f"好的，开始执行（超过 {secs} 秒就算超时）：\n"
                             f"{item.get('cmd') or ''}", sender)
                        send(shell_command_text(item, cfg), sender)
                        remember_executed(item, cfg)     # 真跑过了才记
                        continue
                    if item:
                        agent_cfg = cfg.get("agent") or {}
                        count = max(1, int(item.get("count") or 1))
                        interval = max(0.0, float(agent_cfg.get("send_interval", 1.5)))
                        # 连发是同步做的：中途轮询会暂停几秒（消息在库里排着，回来照收）。
                        # 故意不开线程——并发碰 hook 会把微信搞崩。
                        # 图片按路径串发（一条路径一次）；转发认 count 的**只有素材
                        # 暂存区那条路**（见 send_xml_repeated），forward_message 仍只发一次。
                        # 发送时**再校验一次**目录归属：登记时校验过，但登记之后
                        # 用户可能改了配置、路径本身也可能是个软链——发出去就收不回。
                        try:
                            dirs_now = agent_tools.allowed_image_dirs(cfg)
                        except Exception:
                            traceback.print_exc()
                            dirs_now = None
                        n, err = agent_tools.send_pending(wcf, item, interval,
                                                          allowed_dirs=dirs_now)
                        is_text = not item.get("image") and not item.get("xml")
                        if is_text:
                            # 记一下，免得发给自己时又被当成新消息回一遍
                            remember_sent(item["text"])
                        what = "转发" if item.get("xml") else "图片"
                        label = item.get("label")
                        if err is not None:
                            print(f"[bot] 确认发送失败（已发 {n}/{count}）: {err}")
                            send(f"发给 {item['to_name']} 失败：{err}", sender)
                        elif is_text:
                            print(f"[bot] 确认发送 -> {item['to_name']}: {item['text'][:40]} ×{n}")
                            send(f"已发送给 {item['to_name']}。" if n == 1
                                 else f"已给 {item['to_name']} 连发 {n} 条。", sender)
                        elif label:
                            # 素材暂存区那条路：说人话（「那张图」/「第 2 个表情」）。
                            # 只说「已发出」不说「对方收到了」——hook 成功也回 ret:0，
                            # 收没收到本地无法确认（见 assets.py 顶部）。
                            print(f"[bot] 确认发送 -> {item['to_name']}: {label} ×{n}")
                            send(f"已把{label}发给 {item['to_name']}"
                                 + (f"（连发 {n} 次）。" if n > 1 else "。"), sender)
                        else:
                            print(f"[bot] 确认发送 -> {item['to_name']}: {what} ×{n}")
                            send(f"已把 {n} 张{what}发给 {item['to_name']}。" if n > 1
                                 else f"已把{what}发给 {item['to_name']}。", sender)
                        # 同上：部分失败也记，避免恢复出来的同一条把已发出的再发一遍
                        remember_executed(item, cfg)
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
                        run_state = {}
                        answer, cfg_changed = run_agent(
                            llm, system, prompt, wcf, contacts, cfg, sender, self_wxid,
                            cfg_provider=lambda: settings.effective(base_cfg),
                            history=history, state=run_state)
                        # 确定性兜底：模型没调 run_command 却自己说「已提交/等你确认」时，
                        # 固定追一句真话。**别删**——真机上就是这么骗到用户的。
                        answer = with_shell_truth_note(
                            answer, run_state.get("shell_queued", False))
                        # 图片那边的如实说明（比如"这一轮已经给了 3 张，这张没给"）
                        answer = with_image_notes(answer, run_state.get("image_notes"))
                        # 群发预览**原样**带上（模型转述十条例文必走样）。
                        answer = with_broadcast_preview(
                            answer, run_state.get("broadcast_preview", ""))
                    else:
                        answer = llm.chat(system,
                                          history + [{"role": "user", "content": prompt}])
                        cfg_changed = False
                    send(answer, sender)
                    # 工具可能刚登记了待确认动作（发消息/发图/跑命令）→ 立刻落盘
                    save_pending(pending_chats, cfg)
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
            break
        except Exception:
            traceback.print_exc()
            print("[bot] ⚠️ 主循环抛了一个未预期的异常：已记录，5 秒后继续（进程不退出）。")
            time.sleep(5)

    if status_page is not None and status_srv is not None:
        try:
            status_page.stop(status_srv)
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    main()
