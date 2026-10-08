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

import admin
import agent_tools
import assets
import auto_reply
import botctl
import callgate
import executor
import file_read
import files
import groups
import hook_check
import i18n
import live_history
import plugins
import settings
import providers
import read_worker
import scheduler
import recall
import voice_mem
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
from aixed_api import AixedClient, AixedError, account_dir_wxids, resolve_self_wxid

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
    import semantic
except ImportError:                                     # pragma: no cover
    semantic = None
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


# ── 看护攒给主循环的「主动汇报」─────────────────────────────────────────
# 收消息通道（iter_aixed_messages）能查库、但**不能发消息**：发消息要走主循环那条线
# （hook 不支持并发）。所以那边把要说的话塞进这个队列，主循环每轮开头取走发出去。
# 这是「静默失效」这条主线上的一环：出问题时**它主动说**，而不是等用户发现「没反应」。
_NOTICES = []


def push_notice(text):
    if text:
        _NOTICES.append(str(text))


def drain_notices():
    out = list(_NOTICES)
    del _NOTICES[:]
    return out


def _try_selfheal(client, min_interval=None):
    """库句柄疑似掉了 → 触发一次重扫。返回一句**如实**的话。

    ⚠️ `live_history.force_rescan` 返回的是「**这次有没有真的触发重扫**」
    （它自带限流，别人刚扫过就返回 False），**不是「修好了没」**。
    所以这里绝不说「已修好」——到底修好没有，由**接下来的那一轮**告诉我们
    （游标动了 / 下一次 ping 通了，就是好了；停滞那条路另有 `Health.recovered_from_stall`
    给一次性信号）。这也是「没跑就是没跑」的同一条规矩：只报自己真做过的事。

    `min_interval` 透传给 `force_rescan`：调用方自己定「这条路值不值得为它重扫」
    （默认仍是它的 45 秒；闸门那条路要松得多，见 `_GATE_HEAL_INTERVAL`）。
    """
    try:
        triggered = live_history.force_rescan(client, min_interval=min_interval)
    except Exception as e:
        return f"试着重扫时抛了异常：{type(e).__name__}: {e}"
    if triggered:
        return ("我已经自己触发了一次重扫（force_rescan）。接下来几轮游标要是动了，"
                "就说明**数据又能读到了**；要是一直不动，再按下面做。")
    try:
        gap = int(min_interval or live_history.RESCAN_MIN_INTERVAL)
    except (TypeError, ValueError):
        gap = int(live_history.RESCAN_MIN_INTERVAL)
    return (f"刚才 {gap} 秒内已经重扫过（限流中），这次没有重复扫。")


# 启动闸门里自愈的最小间隔（秒）。**故意比 force_rescan 默认的 45 秒松得多**：
# GetAllDBName 是 700MB 进程里的全内存扫描，「调勤了会把微信拖死」（见 live_history 顶部那段），
# 而闸门是「不知道要等多久」的场景——用户扫码可能要等几分钟。首次照扫（限流比的是
# 「距上次」，初值 0），之后最多 5 分钟一次。
_GATE_HEAL_INTERVAL = 300.0
# 「连不上 hook」时**不再放弃**（2026-10-07 真机事故后退役了那条判据）。
# 旧实现：连不上就计数，到 30 次（约 5 分钟）就 `return None` → `sys.exit(1)`；
# 旧注释说「连不上 hook = 真起不来，只有这条才放弃」。那晚的事实把这句话证伪了：
#   10-06 22:03 自启拉起进程 → 当时微信没开、连不上 30001 → 22:12 放弃退出
#   → 之后**没有任何东西再拉它**（Run 键只在登录响一次）→ 用户看到的就是
#   「开机自启了，发消息却不回复」，一直持续到第二天早上人工拉起。
# 现在**一直等**：微信可能几小时后才起来（用户就是这么用的），我们得还在。
# 上界只由调用方**显式**给（`connect_aixed(give_up_fails=…)`，产品路径不传 = 不放弃）。
# 回归：selftest_bot_loop.py 的 t_gate_selfheal ⑥。
# 退避阶梯（秒）：连不上时不是每 10 秒硬敲几小时 —— 前两次仍 10 秒（用户刚开微信就能接上），
# 之后 30、60 封顶。
_GATE_BACKOFF_SEC = (10, 10, 30, 60)

# ── 「微信刚回来」时允许**绕开限流补一次重扫**（2026-10-06 真机，用户原话「恢复时间久确实搞人心态」）──
# 现场：用户重登微信后，助手先前那次重扫是在"微信还没活"时做的（必然失败），之后每 10 秒都被
# 300 秒限流挡回 ⇒ **干等约 4 分钟**，我手工 `force_rescan` **1.8 秒**修好。
# 判据用「微信自己的库现在写不写」（`hook_check.core_db_age_sec`，纯 stat、不碰 hook）：
# 库刚被写过 ⇒ 打不开句柄不是"微信死了"，而是句柄表陈旧 ⇒ 此刻扫几乎一定成功。
_GATE_HEAL_FRESH_SEC = 60.0        # 「库刚被写过」的窗口
_GATE_HEAL_BYPASS_INTERVAL = 60.0  # 绕开限流补扫之间至少隔这么久（GetAllDBName 是全内存扫描）
_GATE_HEAL_AT = [0.0]              # 上次真扫的时刻（monotonic）
_GATE_HEAL_BYPASS_AT = [0.0]       # 上次绕开限流补扫的时刻
_GATE_LOG_INTERVAL = 60.0          # 闸门日志限流：以前每 10 秒两行，一晚能把日志刷穿
_GATE_NOTIFY_AFTER = 30            # 卡这么多轮就主动弹一次本地通知（轮间隔见退避阶梯 → 约 10 分钟）
_GATE_NOTIFY_INTERVAL = 600.0


def _gate_backoff_sec(hard_fails):
    """连不上 hook 时的下一次重试间隔（秒）。10 → 30 → 60 封顶。

    为什么要有退避：现在是**一直等**，可能要等几小时（微信没开）。每 10 秒敲一次
    对已关闭的端口是瞬时失败、不心疼，但日志/通知的节奏会让人以为"它在刷屏"；
    阶梯让长时间等待安静下来，同时前 5 分钟仍保持 10 秒的敏捷（用户刚开微信就接上）。
    """
    steps = _GATE_BACKOFF_SEC
    try:
        i = min(max(0, int(hard_fails) - 1), len(steps) - 1)
    except (TypeError, ValueError):
        i = 0
    return float(steps[i])


def gate_heal_decision(last_rescan_age, rescan_interval, db_age,
                       last_bypass_age=None,
                       bypass_interval=_GATE_HEAL_BYPASS_INTERVAL,
                       fresh_sec=_GATE_HEAL_FRESH_SEC):
    """闸门这条路「现在该不该重扫」——**纯函数**（自测直接钉，不碰真微信）。

    返回 `"scan"` / `"bypass"` / `"wait"`：

    * `scan`  ：距上次重扫已超过 `rescan_interval`（常规节奏）；
    * `bypass`：限流还没到，但**微信的库又开始写了**（`db_age` 很新）⇒ 补一次
      （2026-10-06 真机：就是这条把"恢复要等 4 分钟"变成"几秒"）；
    * `wait`  ：其余情况，继续等 —— GetAllDBName 是 700MB 进程里的全内存扫描，
      **绝不为了"快点"去刷它**。

    ⚠️ 两个保守处：`db_age is None`（拿不到库龄，纯 stat 失败）**一律不当新鲜**；
    两次 bypass 之间由 `last_bypass_age` 兜住。
    """
    fresh = (db_age is not None and fresh_sec is not None
             and 0 <= float(db_age) <= float(fresh_sec))
    young = last_rescan_age is not None and rescan_interval is not None \
        and float(last_rescan_age) < float(rescan_interval)
    if not young:
        return "scan"                      # 常规到点（含"从没扫过"）
    if not fresh:
        return "wait"                      # 微信没在写库 → 扫了也白扫
    if last_bypass_age is not None and bypass_interval is not None \
            and float(last_bypass_age) < float(bypass_interval):
        return "wait"                      # 刚补过，别再补
    return "bypass"


def _gate_selfheal(client):
    """启动闸门里的自愈：常规按限流，必要时「微信刚回来」补一次。返回要打的那行话。"""
    now = time.monotonic()
    last = _GATE_HEAL_AT[0]
    bypass = _GATE_HEAL_BYPASS_AT[0]
    try:
        db_age = hook_check.core_db_age_sec()
    except Exception:
        db_age = None
    decision = gate_heal_decision(
        None if not last else now - last, _GATE_HEAL_INTERVAL, db_age,
        None if not bypass else now - bypass)
    if decision == "wait":
        # 让 `_try_selfheal` 去说「限流中」那句实话（秒数跟真正传下去的间隔一致）
        return _try_selfheal(client, min_interval=_GATE_HEAL_INTERVAL)
    note = _try_selfheal(client, min_interval=0.0 if decision == "bypass"
                         else _GATE_HEAL_INTERVAL)
    _GATE_HEAL_AT[0] = now
    if decision == "bypass":
        _GATE_HEAL_BYPASS_AT[0] = now
        note = (f"微信的库刚刚又在写了（{db_age:.0f} 秒前，说明是句柄表陈旧、"
                f"不是微信没登录）——我绕开限流补一次重扫。" + note)
    return note



def _gate_retry_step(client, hard_fails):
    """启动闸门里「这一次 ping 失败之后」要做什么。返回 `(hard_fails, 要打的一行字)`。

    ⚠️ 三种失败**必须分开处理**（2026-10-05 真机：混成一种就会让 bot 永远起不来）：
      * hook 说**已登录**、却连库都打不开 → 句柄表被重建了，**自愈一次**（重扫，2.7 秒修好）；
      * hook **连不上** → 那才是真起不来，计一次数（**但不再放弃**：退避着一直重试，
        见 `connect_aixed` 的 `give_up_fails`）；
      * hook 说**没登录**（`IsLogin: 0`）→ 等用户扫码就好，**绝不重扫**
        （扫了也白扫，还得白花一次 700MB 进程的全内存扫描）。

    抽成独立函数是为了**不用 sleep 也测得到**（和 `handle_cursor_stall` 同一个理由：
    判断本身必须能被自测钉住，不能只测零件）。
    """
    state, _why = _probe_login(client)      # 只读探针，不碰句柄表；失败原因用 ping() 那句更全的
    if state is True:
        return hard_fails, _gate_selfheal(client)
    if state is None:
        return hard_fails + 1, ""
    return hard_fails, ""


def _stall_threshold(cfg):
    """连续多少轮游标不动才算「停滞」。默认 6 轮（轮询间隔 5 秒 → 约 30 秒）。"""
    try:
        return max(2, int(((cfg or {}).get("health") or {}).get("cursor_stall_polls", 6)))
    except (TypeError, ValueError):
        return 6


CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
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
    "**可用命令**（中文命令词同样可用；也能用大白话说，助手会自己调工具）\n"
    "\n"
    "【模型 / 控制台】\n"
    "/bot             一屏看全部功能的当前状态（`/bot <功能名>` 直接控制）\n"
    "/bot menu        列出所有可控制的功能\n"
    "/provider        看服务商列表\n"
    "/provider <编号> 选服务商（自动配好协议+接口+模型）\n"
    "/api <key>       设置 API Key（自动测一次连通性）\n"
    "/api clear       清除 API Key\n"
    "/lang [zh|en]    切换界面语言（中文 / English）\n"
    "/baseurl <url>   改接口地址\n"
    "/model <id>      改模型\n"
    "/temp <0-1>      设 temperature\n"
    "/status          当前配置 + 运行健康\n"
    "/selfcheck       诊断一遍并告诉你该做什么\n"
    "/usage [天数]     看 token 用量和估算费用（默认 7 天）\n"
    "/budget [金额]    看消费闸；给金额=设上限（超了拒绝调用模型）；off=关掉\n"
    "/addchat <wxid>  加入要响应的聊天\n"
    "/delchat <wxid>  移出\n"
    "/export <某人>    把该会话的对话导出成文件\n"
    "/clear [all]     清空本会话（all=所有会话）的对话记忆\n"
    "/help            本帮助\n"
    "\n"
    "【自动回复】代你本人回某个人（也能直接说「以后张三的消息你帮我回」）\n"
    "/auto                           看开关、审核、人设、称呼和名单\n"
    "/auto on|off                    开 / 关（关掉就你自己回）\n"
    "/auto add <昵称|wxid> [self|assistant]  加入名单（顺手学一次语气和称呼）\n"
    "/auto del <昵称|wxid>           移出名单\n"
    "/auto mode <谁> self|assistant  身份：假装你本人 / 明说是助手\n"
    "/auto persona <谁> [描述|learn|清空]    单人语气（不带=看；learn=从历史学）\n"
    "/auto address <谁> [称呼|learn|清空]    你对他的称呼（同时是别名，不必在名单里）\n"
    "/auto persona 全局 [描述]       没单独设的人用的默认语气\n"
    "/auto review on|off [谁]        审核：草稿先发你，回「确认」才发\n"
    "/auto ctx <1~30>                上下文条数\n"
    "\n"
    "【定时】也能直接说「明天9点提醒我给张三发…」\n"
    "/schedule                       看列表\n"
    "/schedule add <时间> <对象> <内容>   到点发文本\n"
    "/schedule remind <时间> <内容>       到点提醒我\n"
    "/schedule ask <时间> <问题>          到点让助手答一句，答案发回本会话\n"
    "/schedule del|on|off <编号|all>      删 / 恢复 / 暂停\n"
    "时间：9:00=每天，明天9:00=一次，每周一 9:00=每周，每30分钟=每隔一段，10分钟后=一次\n"
    "\n"
    "【盯着】他发消息就通知我、不回他（和 /auto 互斥）\n"
    "/watch                          看名单\n"
    "/watch add <昵称|wxid|roomid>   加进来\n"
    "/watch del <昵称|wxid>          移出去\n"
    "/watch keyword <正则>           任何会话里命中就通知我\n"
    "/watch keyword del <正则>       看 / 删关键词\n"
    "/watch on|off                   总开关\n"
    "\n"
    "【群发】也能直接说「给张三、李四发…」\n"
    "给出原话→所有人收到同一段；只给意思→按各自语气和称呼分别写一条。\n"
    "「所有人」会先确认范围再发；说「发给同学会群里每个人」= 群成员一人一条（不在群里发）。\n"
    "人数上限见 config.yaml 的 agent.broadcast_max（默认 100）。\n"
    "\n"
    "【分组】\n"
    "/groups                         看所有分组和成员\n"
    "/groups add <组名> <人名、人名>    建组或加人\n"
    "/groups remove <组名> <人名、人名> 移人\n"
    "/groups del <组名>              删组（人不动）\n"
    "/groups labels                  看微信自带的标签和人数（只读）\n"
    "之后说「给大学同学组发…」即可。\n"
    "\n"
    "【素材暂存】在这里发一张图或表情就记下（默认最多 5 条）\n"
    "/assets                         看暂存了什么\n"
    "/assets clear                   清空暂存区\n"
    "之后说「发给张三」「把刚才那张发给李四」「发 3 次」即可。"
)


# 界面骨架里的一小段文案：`_L("中文", "English")` —— 就是 `i18n.t` 的短名字。
# 为什么用短名字：面板里一行要塞好几个标签，缩写读起来不抢戏。
_L = i18n.t


def _yn(v):
    """开关怎么显示：跟着界面语言走（开/关 ↔ on/off）。值本身一个字都不动。"""
    return _L("**开**", "**on**") if v else _L("关", "off")


# `/bot <功能名>` → 等价命令。**控制动作绝不复刻逻辑**：改写成现有命令再交给
# `handle_command`，于是「只有一处实现」——将来改 /auto 的行为，/bot 自动跟着变。
# （项目里 review/persona 撞名那次就是因为两处各有一套说法，用户必然改错东西。）
BOT_ROUTES = {
    "自动回复": "/auto", "auto": "/auto", "回复": "/auto",
    "盯着": "/watch", "watch": "/watch", "监听": "/watch",
    "定时": "/schedule", "schedule": "/schedule", "任务": "/schedule",
    "分组": "/groups", "group": "/groups",
    "预算": "/budget", "budget": "/budget",
    "用量": "/usage", "usage": "/usage", "花费": "/usage",
    "素材": "/assets", "图片": "/assets",
    "导出": "/export", "export": "/export",
    "自检": "/selfcheck", "体检": "/selfcheck",
    "状态": "/status", "status": "/status",
    "帮助": "/help", "help": "/help",
    "语言": "/lang", "lang": "/lang", "language": "/lang",
    "模型": "/provider", "provider": "/provider", "服务商": "/provider",
}

def bot_menu():
    """`/bot 功能`（以及「没认出来」时）给的那张表。

    ⚠️ 必须是**函数**而不是模块级常量：`/lang en` 在运行期切语言，
    常量在 import 时就拼死了，切完还是旧语言（这正是「看起来生效了、其实没有」那类坑）。
    """
    return "\n".join([
        _L("🤖 `/bot` 能控制这些（后面接功能名；这条只列可控制的，完整命令表发 /help）：",
           "🤖 `/bot` controls these (append a feature name; this list has only the controllable ones — send /help for the full table):"),
        "",
        _L("  模型/密钥   → `/bot 模型`　`/api <key>`　`/model <id>`",
           "  Model/key   → `/bot 模型`　`/api <key>`　`/model <id>`"),
        _L("  自动回复    → `/bot 自动回复 开|关`　`/bot 自动回复 add 张三`…",
           "  Auto-reply  → `/bot 自动回复 开|关`　`/bot 自动回复 add 张三`…"),
        _L("  盯着        → `/bot 盯着 开|关`　`/bot 盯着 关键词 <正则>`",
           "  Watch       → `/bot 盯着 开|关`　`/bot 盯着 关键词 <正则>`"),
        _L("  定时任务    → `/bot 定时`　`/bot 定时 加 9:00 张三 早`",
           "  Scheduled   → `/bot 定时`　`/bot 定时 加 9:00 张三 早`"),
        _L("  分组        → `/bot 分组`　`/bot 分组 建 同学 张三、李四`",
           "  Groups      → `/bot 分组`　`/bot 分组 建 同学 张三、李四`"),
        _L("  群发/素材   → 直接说「帮我祝所有人节日快乐」/「发给张三」",
           "  Broadcast/assets → just say it in plain words (the model picks the tool)"),
        _L("  预算        → `/bot 预算`　`/bot 预算 20`　`/bot 预算 关`",
           "  Budget      → `/bot 预算`　`/bot 预算 20`　`/bot 预算 关`"),
        _L("  用量        → `/bot 用量`",
           "  Usage       → `/bot 用量`"),
        _L("  导出对话    → `/bot 导出 张三`",
           "  Export chat → `/bot 导出 张三`"),
        _L("  诊断        → `/bot 自检`　`/bot 状态`",
           "  Diagnostics → `/bot 自检`　`/bot 状态`"),
        _L("  语言        → `/bot 语言`　`/lang zh`　`/lang en`",
           "  Language    → `/bot 语言`　`/lang zh`　`/lang en`"),
        "",
        _L("**只读、要在 config.yaml 里改的**（命令不改配置文件，那是你手写的）：",
           "**Read-only, edited in config.yaml** (commands never rewrite that file — you own it):"),
        "  " + _L("语义检索", "semantic") + " `semantic.enabled` · "
        + _L("联网搜索", "web search") + " `search.enabled` ·",
        "  " + _L("图片模式", "image mode") + " `image.mode` · "
        + _L("送云端前脱敏", "redact before cloud") + " `privacy.redact` · "
        + _L("状态页", "status page") + " `status.enabled`",
        _L("  （改完重启助手生效。语义检索还要先装可选依赖 + `semantic.py --setup/--build`。）",
           "  (Restart the assistant after changing them. Semantic search also needs the optional dependency + `semantic.py --setup/--build`.)"),
    ])


def bot_dashboard(cfg, contacts=None):
    """一屏总览所有功能的当前状态。**绝不查库、绝不起线程。**

    只读三处：① 传进来的 `cfg`（已由 `settings.effective` 合过）；
    ② 内存里的健康快照 `_h()`；③ **磁盘上的文件**（语义索引在不在）。
    理由和 health / status_page 同源：这个面板随时可能被发一次，
    **不能因为它多打一次 hook**（hook 不支持并发，崩过微信 6 次）。

    每一段都各自兜异常：某个模块坏了只让那一行显示「读不出来」，
    **绝不整条面板挂掉**（那会让用户以为整个助手坏了）。
    """
    c = cfg or {}

    def safe(fn, default=None):
        try:
            return fn()
        except Exception:
            return default

    L = [_L("🤖 **助手控制台**（`/bot <功能名>` 就能控制；`/bot 功能` 看全部）",
            "🤖 **Assistant console** (`/bot <feature>` controls it; `/bot 功能` lists all)"), ""]

    key = str(c.get("api_key") or "")
    L.append(f"{_L('模型　　', 'Model     ')}{c.get('provider') or 'anthropic'} / {c.get('model') or _L('（没设）', '(not set)')}"
             f"　key {mask(key) if key else _L('**没设**', '**not set**')}　→ `/bot 模型`")

    n_ar = len(safe(lambda: auto_reply.chats(c), []) or [])
    ar_on = safe(lambda: auto_reply.enabled(c), False)
    ar_rev = (c.get("auto_reply") or {}).get("review", True)
    L.append(f"{_L('自动回复　', 'Auto-reply')} {_yn(ar_on)}　· {n_ar} {_L('人', 'people')}　· {_L('审核', 'review')} {_yn(ar_rev)}"
             f"　→ `/bot 自动回复 开|关`")

    n_w = len(safe(lambda: watch.chat_list(c), []) or [])
    n_k = len(safe(lambda: watch.keywords(c), []) or [])
    L.append(f"{_L('盯着　　', 'Watch    ')}{_yn(safe(lambda: watch.enabled(c), False))}"
             f"　· {n_w} {_L('人', 'people')}　· {_L('关键词', 'keywords')} {n_k}　→ `/bot 盯着 开|关`")

    L.append(safe(lambda: recall.status_line(c), "撤回回显　（读不出来）"))

    L.append(f"{_L('定时　　', 'Scheduled')} {len(safe(lambda: scheduler.tasks(c), []) or [])} {_L('个任务', 'tasks')}"
             f"　→ `/bot 定时`")
    L.append(f"{_L('分组　　', 'Groups   ')} {len(safe(lambda: groups.all_groups(c), {}) or {})} {_L('个组', 'groups')}"
             f"　→ `/bot 分组`")

    if usage is None:
        L.append("预算　　**模块没装**（usage.py 不在）　→ 重装一次或看部署")
    else:
        bt = safe(lambda: usage.budget_text(c), None)
        first = (str(bt).splitlines()[0].strip() if bt else "（读不出来）")
        L.append(f"{_L('预算　　', 'Budget   ')}{first}　→ `/bot 预算 <金额>`")

    # 语义检索：**状态从磁盘读**（索引文件在不在），不查库。
    # ⚠️ `semantic is None` 必须**单独说**，不能被 `safe()` 吞成「读不出来」——
    #    那是静默降级（用户会以为是索引坏了，其实是模块没部署上来）。
    if semantic is None:
        L.append("语义检索　**模块没装**（semantic.py 不在）　→ 重装一次或看部署")
    else:
        sem = safe(lambda: semantic.cfg_of(c), {}) or {}
        sem_on = sem.get("enabled") is True
        idx = safe(lambda: semantic.load_index(c), (None, ""))
        idx_obj = idx[0] if isinstance(idx, tuple) else None
        if idx_obj is None:
            why = ""
            if isinstance(idx, tuple) and len(idx) > 1 and idx[1]:
                why = "：" + str(idx[1]).splitlines()[0][:60]
            istate = f"**没建索引**（要 `semantic.py --build`）{why}"
        else:
            istate = f"索引 {len(idx_obj.get('docs') or [])} 条可用"
        L.append(f"{_L('语义检索　', 'Semantic ')}{_yn(sem_on)}　{istate}　→ {_L('改 config.yaml 的 semantic.enabled', 'edit semantic.enabled in config.yaml')}")

    srch = c.get("search") or {}
    L.append(f"{_L('联网搜索　', 'Web search ')}{_yn(srch.get('enabled') is True)}"
             f"　→ {_L('改 config.yaml 的 search.enabled', 'edit search.enabled in config.yaml')}")
    img = c.get("image") or {}
    L.append(f"{_L('图片解读　', 'Image     ')}{img.get('mode') or 'ocr'}"
             f"　· {_L('送云端前脱敏', 'redact before cloud')} {_yn((c.get('privacy') or {}).get('redact') is True)}")

    h = _h()
    if h is not None:
        s = safe(lambda: h.snapshot(), None)
        if isinstance(s, dict):
            L.append(f"{_L('运行　　', 'Runtime  ')}{_L('轮询', 'polls')} {s.get('poll_count')} "
                     f"{_L('次　· 登录 ', '· login ')}"
                     f"{_L('正常', 'OK') if s.get('login_ok') else _L('**异常**', '**ERROR**')}"
                     f"　· {_L('分片错误', 'shard errors')} "
                     f"{len(s.get('poll_errors') or {})}　· {_L('hook 报错', 'hook errors')} "
                     f"{_err_count(s.get('hook_errors'))}　· {_L('发送失败', 'send failures')} "
                     f"{_err_count(s.get('send_failures'))}　→ `/bot 自检`")
        else:
            L.append("运行　　（读不出健康快照）　→ `/bot 自检`")
    else:
        # 拿不到 Health 实例时**要明说**，不能整行省略——用户会以为面板就是这些内容。
        L.append("运行　　**拿不到健康快照**（health 模块没装或没初始化）　→ `/bot 自检`")
    st = c.get("status") or {}
    L.append(f"{_L('状态页　', 'Status page ')}{_yn(st.get('enabled') is True)}"
             f"　({st.get('host') or '127.0.0.1'}:{st.get('port') or 39002})")

    L += ["", _L("发 `/bot 功能` 看全部可控制项；发 `/help` 看完整命令表。",
                "Send `/bot 功能` for every controllable feature; send `/help` for the full command table.")]
    return "\n".join(L)


def handle_command(text, wcf, cfg, live_ok, contacts=None, chat=None):
    """识别 / 开头的命令。返回 (回复文本, 是否改了配置)；非命令返回 (None, False)。

    `chat` 是**发这条命令的会话**（主循环传进来）。只有需要「针对当前会话」的命令
    才用它（目前是 `/clear`）—— 命令处理器原先不知道自己在哪个会话里，
    所以别的地方照旧拿 `None` 也不会出问题。
    """
    t = text.strip()
    if not t.startswith("/"):
        return None, False
    parts = t.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/help", "/帮助"):
        return HELP_TEXT, False

    if cmd in ("/clear", "/清空记忆", "/忘掉"):
        # 为什么要有：记忆里存着**模型自己上一次的失败**，它下一轮会当成既定事实
        # （真机连撞两次：复述拒绝 / 复述猜路径失败）。详见 `dialog_forget`。
        a = arg.lower()
        if a in ("all", "全部", "所有", "所有会话"):
            n = dialog_forget(None)
            return (f"已清空**所有会话**的对话记忆（{n} 个会话有记忆，已清掉）。\n"
                    "之后每轮都从零开始 —— 设置、名单、分组、定时、素材都不动。", False)
        if not chat:
            return ("这条命令得**在会话里**发（`/clear all` 可以清全部）。", False)
        n = dialog_forget(chat)
        if not n:
            return "本会话本来就没有对话记忆，什么都没变。", False
        return ("已清空**本会话**的对话记忆 —— 助手会忘掉刚才聊过什么，"
                "但**设置、名单、分组、定时、素材都不动**。\n"
                "要连别的会话一起清就发 `/clear all`。", False)

    if cmd in ("/bot", "/控制台", "/面板"):
        fresh = settings.effective(load_config())
        if not arg:
            return bot_dashboard(fresh, contacts), False
        bits = arg.split(maxsplit=1)
        key = bits[0].strip()
        rest = bits[1].strip() if len(bits) > 1 else ""
        if key.lower() in ("功能", "菜单", "menu", "help", "?", "列表"):
            return bot_menu(), False
        target = BOT_ROUTES.get(key.lower()) or BOT_ROUTES.get(key)
        if not target:
            return (f"没认出来「{key}」。\n\n" + bot_menu()), False
        sub = f"{target} {rest}".strip()
        # 防呆：路由表要是被人改成了 /bot 自己，这里就会无限递归。
        if sub.lower().startswith("/bot"):
            return "内部错误：`/bot` 的路由指向了自己（会无限递归），已拒绝。", False
        # **复用既有命令的唯一实现**，不另写一套开关逻辑。
        return handle_command(sub, wcf, fresh, live_ok, contacts=contacts)

    if cmd in ("/lang", "/语言"):
        # 界面语言：存 settings.json 的 `language`（zh / en），默认中文。
        # 认不出来的值**原地不动**并如实说 —— 绝不静默改回默认（那比报错更坏：
        # 用户写了 language: 日本語，界面却变成中文，他只会以为命令没生效）。
        if not arg or arg.lower() in ("?", "help", "用法", "状态"):
            return (_L("当前界面语言：", "Current UI language: ")
                    + f"**{i18n.LANG_NAMES[i18n.current()]}**\n"
                    + _L("用法：", "Usage: ")
                    + "`/lang zh`　`/lang en`"
                    + _L("（也可以 `/bot 语言`）", " (or `/bot 语言`)"), False)
        got, bad = i18n.set_lang(arg)
        if bad is not None:
            return (_L("认不出语言", "Unrecognised language") + f"「{bad}」"
                    + _L("——只认 `zh`（中文）/ `en`（English）。\n",
                         " — only `zh` and `en` are accepted.\n")
                    + _L("**没有改任何东西**，当前还是", "**Nothing was changed**; still ")
                    + i18n.LANG_NAMES[got] + "。", False)
        return (_L("界面语言已切成 **中文**。", "Interface language is now **English**."), False)

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

    if cmd in ("/自检", "/体检", "/selfcheck"):
        # 和 /status 的分工：/status 报**配置 + 一眼健康**，/自检报**诊断 + 该做什么**。
        # 它只读 bot 自己记到的事实，不新查库、不起线程（见 selfcheck_text 的说明）。
        fresh = settings.effective(load_config())
        try:
            return selfcheck_text(fresh), False
        except Exception as e:
            # 自检自己坏了也要如实说——不许回一句「一切正常」糊过去
            traceback.print_exc()
            return f"自检本身出错了（这也要如实说）：{type(e).__name__}: {e}", False

    if cmd in ("/导出", "/export"):
        if not arg:
            return ("用法：/export <昵称|备注|微信号>　把这个会话的对话导成一个可读文件。\n"
                    "（只导**文本**消息；文件落在导出目录里，会顺带清理旧的导出。）"), False
        fresh = settings.effective(load_config())
        try:
            return export_conversation(wcf, fresh, contacts or [], arg), False
        except Exception as e:
            # 导出失败也要如实说，并明说**没生成文件**
            traceback.print_exc()
            return f"导出出错了（**没有生成文件**）：{type(e).__name__}: {e}", False

    if cmd in ("/用量", "/usage", "/花费"):
        if usage is None:
            return "这个版本没带上 usage.py，用量统计不可用（安装不完整）。", False
        days = 7
        if arg:
            try:
                days = max(1, min(365, int(arg)))
            except (TypeError, ValueError):
                return "用法：/usage [天数]，例如 /usage 30", False
        try:
            return usage.summarize(days), False
        except Exception as e:
            # 统计坏了也得如实说，不许回一句「暂无记录」糊过去
            traceback.print_exc()
            return f"用量统计失败：{e}", False

    if cmd in ("/预算", "/budget", "/花费上限"):
        if usage is None:
            return "这个版本没带上 usage.py，消费闸不可用（安装不完整）。", False
        fresh = settings.effective(load_config())
        a = arg.strip()
        if not a:
            try:
                return usage.budget_text(fresh), False
            except Exception as e:
                traceback.print_exc()
                return f"预算状态读不出来：{e}", False
        low = a.lower()
        if low in ("关", "关闭", "off", "0", "不限", "none", "清空"):
            settings.set_value("budget", {"daily_cost": 0})
            return ("消费闸已关闭（`budget.daily_cost = 0`），模型调用不再受它限制。"
                    "\n（`/usage` 照样能看花了多少。）"), True
        try:
            v = float(a)
        except (TypeError, ValueError):
            return ("用法：`/budget` 看状态；`/budget 20` 设上限（元 / 最近 24 小时）；"
                    "`/budget off` 关闭。"), False
        if not (v > 0):                      # 含 NaN / inf / 负数
            return ("上限要是一个正数，例如 `/budget 20`；要关掉就发 `/budget off`。"), False
        settings.set_value("budget", {"daily_cost": v})
        return (f"消费闸已开：**最近 24 小时**最多花 {v:g}（`budget.daily_cost`）。\n"
                f"到上限时会**拒绝调用模型**并告诉你是哪条在拦；"
                f"不会偷偷换个便宜模型，也不会静默降级。\n"
                f"（窗口是滚动的 24 小时，不是自然日——账本里只有时间戳，"
                f"按自然日算要说清时区，容易讲错。）"), True

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

    if cmd in ("/分组", "/group", "/groups", "/组"):
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

    if cmd in ("/素材", "/asset", "/assets", "/assetbank"):
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


def now_line(now=None):
    """给模型的一行「现在几点」。**每次调用现算**，不是进程启动时算一次。

    为什么要有：模型自己不知道现在是什么时候。用户问「现在几点」「今天星期几」，
    或者让它按「今天 / 明天 / 这个月」算事情时，它只能从训练数据里编一个日期出来
    ——实测就是这样。所以每轮把真实时间拼进系统提示。
    """
    t = now or datetime.now()
    return (f"（当前时间：{t.strftime('%Y-%m-%d %H:%M:%S')} "
            f"星期{'一二三四五六日'[t.weekday()]}，本机时区）")


def with_now(system, now=None):
    """把「现在几点」拼到系统提示末尾。

    ⚠️ **必须每轮现调**：bot 是长驻进程（后台跑几天很正常），
    启动时拼一次的话，那行时间会一直骗到重启为止。所以别把它塞进
    `system` 变量本身，只在真的要发请求时才拼。
    """
    return (str(system or "").rstrip() + "\n" + now_line(now)).strip()


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


def dialog_forget(chat=None):
    """清掉对话记忆。`chat=None` = 清空**所有**会话。返回清掉的会话数。

    为什么要开这条路（2026-10-04 用户提的）：记忆里存着**模型自己上一次的失败**，
    而它下一轮会把那句话当成既定事实 —— 真机上连撞两次：
      ① 照着上一轮的「被配置限制了」继续拒绝，压根不调工具；
      ② 照着上一轮的「猜路径失败 + 我提议跑命令」继续重提命令，不用新给的路径清单。
    记忆有 ttl（`agent.dialog_ttl`，默认 900 秒）会自己过期，但**正卡在牛角尖里时等不了**，
    盘上还有 `_DIALOG_KEEP_SECONDS` 那层 7 天的地板。

    ⚠️ **只做成 `/clear` 命令，不给模型工具**：这是不可逆操作，而模型手里有
    `send_text` / `run_command` —— 一句提示词注入就能让它把你的记忆清掉。
    命令只能由**人**在微信里发出来，这道口子不留给模型。
    """
    store = _dialog_load()
    if chat is None:
        n = len(store)
        store.clear()
    else:
        n = 1 if store.pop(str(chat), None) is not None else 0
    if n:
        _dialog_save()
    return n


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
    # 时效和「确认」分支、判重窗口**同一个解析**（agent_tools.confirm_ttl_of）：
    # 几处不一致就会出现「盘上还记着、内存里已经过期」这种最难查的错。
    ttl = agent_tools.confirm_ttl_of(cfg)
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
    ttl = agent_tools.confirm_ttl_of(cfg)
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
                # `ttl` 传进去：恢复时也要判重（旧版本可能往盘上写了两条一样的）。
                # 判重命中 = 这一份**没有**恢复（队列里已经有同一条了），不能算一条。
                dupe = agent_tools.set_pending(
                    chat, it.get("to_wxid") or "", it.get("to_name") or "",
                    it.get("text") or "", kind=it.get("kind") or "agent",
                    count=it.get("count") or 1, image=it.get("image"),
                    xml=it.get("xml"), cmd=it.get("cmd"), timeout=it.get("timeout"),
                    # ⚠️ `label` / `items` / `spec` / `file` **必须一起恢复**。
                    # 少了 label，素材那条待确认项就退化成「转发一条消息」（用户认不出
                    # 是哪一条）；少了 items/spec，群发批次会变成「没有收件人」——
                    # 而它的 text 只是**给人看的预览**，真按文本分支发出去就是往空
                    # wxid 发一段预览文字（真机上是「发出去了但没人收到」这种最难查的错）。
                    # 少了 file，发文件的待确认项会退化成「发一段文字」。
                    label=it.get("label"), items=it.get("items"), spec=it.get("spec"),
                    file=it.get("file"),
                    # ⚠️ `extra` **必须整包透传**（2026-10-04 加）。
                    # 新 kind 的动作身份全在里面 —— 漏了这一句，重启后那条待确认项
                    # 会**静默退化成别的操作**：判重键算出来跟原来不一样（于是可能
                    # 重复入队），执行器也拿不到自己要的字段。
                    # 这就是上面 label/items/spec/file 那个死法的同一个形状，
                    # 所以新 kind 的字段一律走 extra、**不再加具名参数**。
                    extra=it.get("extra"), ttl=ttl)
                if dupe:
                    # 盘上有两条一模一样的（旧版本留下的）：只恢复一条，并**明说**。
                    print(f"[bot] 恢复待确认队列：第 {dupe} 条已经一模一样，"
                          f"这一份没有重复恢复")
                else:
                    n += 1
            except Exception:
                traceback.print_exc()
    return n


def run_agent(llm, system, prompt, wcf, contacts, cfg, chat, self_wxid="",
              cfg_provider=None, history=None, state=None, user_query="",
              from_self=None):
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
                              llm_factory=lambda: llm, user_query=user_query or prompt,
                              # ⚠️ **必须透传**：`ToolBox.ctx()['from_self']` 是
                              # 「这条消息是不是我自己发的」这个**事实**，工具层
                              # （`files.who_allows`）靠它决定放不放行。
                              # 2026-10-04 真机撞过一次：忘了透传 → 生产里永远是
                              # None → `computer_files` 一律拒绝，而离线自测全绿
                              # （自测自己塞了 True）。回归：`selftest_bot_loop`。
                              from_self=from_self)

    messages = list(history or []) + [{"role": "user", "content": prompt}]
    last_text = ""
    for _ in range(max_rounds):
        # 上一轮工具收下的**原图**（image.mode=inline）：附给**这一次**调用，取走即清。
        # 这样图只花一次 token；而且永远不进 messages / dialog 记忆。
        pending = box.take_images() if hasattr(box, "take_images") else []
        call_messages = attach_images(messages, pending) if pending else messages
        # 工具清单走**注册表**（内置 + 插件），不再直接读 `agent_tools.TOOLS`
        # —— 注册表是唯一真源，见 `docs/plugin-contract-spec.md` 2.2。
        # 形状一字未变（仍是 name/description/parameters，MCP 的 tool 形状）。
        result = llm.chat_with_tools(system, call_messages, plugins.REGISTRY.tools())
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
            # `on_tool`：**每次工具调用之后**（只观察，审计/日志用）。
            # 放在这里而不是 ToolBox 里，是因为 ToolBox 不知道「这一轮是哪个插件的
            # 调用」之外的东西，而事件总线要的东西它都有。
            plugins.REGISTRY.emit("on_tool", c.name, c.arguments, out,
                                  box.ctx(), cfg=cfg)
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


# 自己刚发出去的回复，用来防止「自聊模式下回复又被当成新消息」造成死循环。
#
# ⚠️ **这张表必须落盘**（2026-10-06 用户拍板；事故与取舍见 `docs/restart-catchup-notes.md`）：
# 在控制会话（文件传输助手）里，助手自己发的字和用户打的字在库里**都算「自己发的」**
# （`from_self` 分不开，见 `looks_like_own_echo_without_identity`），所以「这句话是我
# 刚发出去的」是**唯一**一道能救的闸。它以前只活在内存里，进程一重启就空 —— 于是上一轮
# 刚发的问候/失败提示被当成用户的新提问，一条条再答一遍（用户看到的「重复回复」）。
# **只落指纹、不落原文**：`state.json` 是会被打开翻看的文件，没必要把回复内容留在那儿。
_SENT_RECENT = {}
_SENT_TTL = 300.0
_SENT_KEY = "sent"
_SENT_MAX = 200                     # 最多留多少条（防止 state.json 无限长）


def _sent_fp(text):
    """「我发过这句话」的指纹（sha1 前 16 位）。为什么用指纹不用原文：见上面的注释。"""
    raw = str(text).strip().encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _sent_prune(now=None):
    """丢掉过期的（TTL）和超量的（只留最近的 `_SENT_MAX` 条）。返回清理后的那份。"""
    now = time.time() if now is None else now
    for k, t in list(_SENT_RECENT.items()):
        if now - t > _SENT_TTL:
            _SENT_RECENT.pop(k, None)
    while len(_SENT_RECENT) > _SENT_MAX:
        _SENT_RECENT.pop(min(_SENT_RECENT, key=lambda k: _SENT_RECENT[k]), None)
    return dict(_SENT_RECENT)


def load_sent_memory():
    """启动时把落盘的指纹读回内存（**重启后仍认得出自己的回显**）。返回读回几条。

    读不出来/文件坏了就当空的：状态文件不该挡住启动（和 `state.json` 那条同一个规矩）。
    只认**还在 TTL 内**的 —— 过期的恢复出来，只会让「对方恰好说了和我们旧回复一样的话」
    被静默丢掉。
    """
    n = 0
    now = time.time()
    data = state_get(_SENT_KEY)
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                t = float(v)
            except (TypeError, ValueError):
                continue
            if isinstance(k, str) and k and (now - t) <= _SENT_TTL:
                _SENT_RECENT[k] = t
                n += 1
    return n


def remember_sent(text):
    _SENT_RECENT[_sent_fp(text)] = time.time()
    try:
        state_set(_SENT_KEY, _sent_prune())
    except Exception:
        # 落盘失败只告警：**这次发送本身已经成功了**，记账出问题绝不许影响这条回复
        # （和 usage.record 同一条规矩）。
        traceback.print_exc()


def is_own_reply(text):
    """这条是不是我们自己刚发出去的回复（内存里的 + 落盘续上来的都算）。"""
    t = _SENT_RECENT.get(_sent_fp(text))
    return t is not None and (time.time() - t) < _SENT_TTL


def looks_like_own_echo_without_identity(from_self, identity_ok, text):
    """**认不出自己**时，这条消息是不是「我们刚发出去的那句话」回显（该跳过）。

    为什么要单独一个纯函数（2026-10-06 换台电脑真机）：`from_self` 那个判据要靠
    self_wxid 在消息 id 空间里对得上；一旦对不上（contact 表那条经验判据认错人，
    见 `aixed_api.resolve_self_wxid`），`is_self` 会**恒为 0** —— 于是「我刚发出的
    回复」回显回来时跟对方发来的新消息长得一模一样，而 `is_own_reply()` 那道闸
    **嵌在 from_self 分支里面**，压根不会被问到。用户看到的就是「它自己答自己、
    一遍又一遍」，而且不报错。

    `is_own_reply` 这条事实**与身份无关**（文本一字不差、就在刚才），所以这里把它
    独立出来；但只在**确认认不出自己**（`identity_ok is False`）时才兜底 ——
    正常机器一个字都不变，也不会因为「对方恰好说了和我们上一条回复一样的话」
    就静默不回。

    三个参数都是**事实**，不是推断：`from_self` 是这条消息的分类，
    `identity_ok` 是「我自己认得出来吗」（`live_history.self_identity_ok()`）。
    """
    if from_self or identity_ok:
        return False
    return is_own_reply(text)


def is_catchup(msg_ts, start_ts, now):
    """这条消息是不是「本进程启动之前就产生的」= 重启补齐的那批（只通知、不自动回复）。

    ## 判据为什么只剩「早于本进程启动」这一条（2026-10-06 用户拍的）

    以前还要求「比 `state.stale_after`（120 秒）还旧」，于是**启动前 2 分钟内**产生的
    消息被当成新消息照回。换台电脑登录正好命中：微信把最近的历史同步/重建进本机库
    （游标对不上），上一台电脑上助手自己的问候、失败提示、已经答过的课表全被当成新消息，
    于是它对着自己以前说过的话又答了一遍 —— 用户看到的「换个电脑一登录，把所有问题又回
    了一遍」。启动之前产生的东西**一律**不是「现在该处理的输入」，与它有多旧无关。

    ⚠️ 两个边界别丢：
      * 时间戳取不到（`<= 0`）→ **不算补齐**：宁可多处理一条，也不许因为读不到时间戳
        就把一条真·新消息静默丢掉；
      * 时间戳落在未来（机器时钟被往前拨 / 库里数据有问题）→ 也不算补齐，同一个理由。
    想「干脆别续旧游标、只收启动后的新消息」用 `state.resume_window: 0`（那是另一个
    旋钮，语义是 `prime()` 把当前最新那批标成已见）。
    """
    try:
        ts = float(msg_ts or 0)
        st = float(start_ts or 0)
        nw = float(now or 0)
    except (TypeError, ValueError):
        return False
    return ts > 0 and st > 0 and ts < st and ts <= nw + 300


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


def _err_count(v):
    """把 poll_errors 的条目（形状是 `[文本, 次数]`，也可能是别的）取出次数。"""
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        return v[1]
    if isinstance(v, dict):
        return v.get("count") or "?"
    return "?"


def selfcheck_text(cfg=None):
    """`/自检`：把 bot **已经记到的事实**拼成一段人话诊断 + 该做什么。

    ⚠️ **刻意不接受 client / wcf 参数** —— 这样它在结构上就不可能发查询。
    hook 不支持并发（已经崩过微信 6 次），而 `/自检` 跑在轮询线程上；
    「真机体检」那件事归 `verify_real.py`（它要求先停 bot）。两者分工不重叠：
      * `/自检` = **bot 跑着**时，看它自己记到的东西；
      * `verify_real.py` = **bot 停着**时，从外面独立验一遍。

    所以这里只做两件事：读内存快照（`health.Health.snapshot()` + `live_history.poll_errors()`），
    以及把「该做什么」按顺序列出来。**不新查一次库**、不起线程。
    """
    cfg = cfg or {}
    lines = ["🩺 自检", "（下面全是 bot 运行时**已经记到**的事实，不是刚刚新查的："
                      "hook 不支持并发，我不会为了自检再去查一次）", ""]
    todo = []
    h = _h()
    snap = None
    if h is None:
        lines.append("❌ health.py 不在：运行看护整体不可用（安装不完整）")
        todo.append("先补全安装（health.py 缺了），别的都不用看。")
    else:
        try:
            snap = h.snapshot()
        except Exception as e:
            lines.append(f"❌ 健康快照取不出来：{e}")
            todo.append("看 bot.log 里 snap 相关的报错。")

    if snap:
        lines.append(f"运行时长：{snap.get('uptime_human') or '—'}")

        # ── 轮询（判断「它还收不收得到消息」最直接的指标）──────────────
        pc = snap.get("poll_count") or 0
        age = snap.get("last_poll_age_seconds")
        try:
            interval = max(1, int(cfg.get("poll_interval", 5)))
        except (TypeError, ValueError):
            interval = 5
        if not pc or age is None:
            lines.append("❌ 还**一次都没轮询过** —— 收消息那条路没起来。")
            todo.append("看 bot.log 里有没有「连不上微信 / 连不上 30001」这类报错。")
        else:
            lines.append(f"轮询：第 {pc} 次，最近一次在 {_fmt_ago(age)} 前（间隔 {interval}s）")
            if age > interval * 3 + 1:
                lines.append(f"⚠️ 轮询**明显偏慢**（{_fmt_ago(age)} > 3×{interval}s）"
                             f"——要么正在跑长任务（读大文件 / 跑本地命令），要么卡住了。")
                todo.append("一直偏慢的话：看 bot.log 有没有「⚠️ 慢查询」，"
                            "或者是不是有个 run_command 正在跑（跑命令期间轮询会停）。")

        # ── 登录态（只能人工扫码恢复，所以优先级最高）──────────────────
        login = snap.get("login_ok")
        lage = snap.get("last_login_check_age_seconds")
        if login is False:
            lines.append("❌ **登录态掉了** —— 微信退回登录界面了。")
            todo.append("去微信里**扫码登录**。这个只能你手动做：bot 自己恢复不了，"
                        "而且它看起来和「库句柄掉了」很像（都是没反应）。")
        elif login is None:
            lines.append("❓ 登录态还没探过（bot 每 30 轮心跳探一次），再等等。")
        else:
            lines.append("登录态：正常"
                         + (f"（最后一次探在 {_fmt_ago(lage)} 前）" if lage is not None else ""))

        cur = snap.get("last_cursor")
        lines.append(f"游标：{cur if cur else '（还没有）'}")

        # ── 分片查询失败（「静默失效」的主要表现）─────────────────────
        perr = dict(snap.get("poll_errors") or {})
        try:
            for k, v in (live_history.poll_errors() or {}).items():
                perr.setdefault(str(k), v)
        except Exception:
            pass
        if perr:
            names = "、".join(f"{k}({_err_count(v)} 次)" for k, v in sorted(perr.items()))
            lines.append(f"⚠️ 分片查询失败：{names}")
            todo.append("分片失败基本就是**库句柄掉了**（不报错、只返回 0 行）。"
                        "bot 每轮会自己试 `force_rescan`；还不行就重启 bot。")

        he = snap.get("hook_errors") or 0
        if he:
            extra = ""
            if snap.get("last_hook_error"):
                extra = f"，最近一次：{str(snap.get('last_hook_error'))[:60]}"
            lines.append(f"⚠️ hook 报错累计 {he} 次{extra}")
            todo.append("hook 报错先看 bot.log 有没有「⚠️ 慢查询」："
                        "慢查询说明卡的是**微信进程本身**，那就别再往上加查询。")

        sf = snap.get("send_fail_count") or 0
        so = snap.get("send_ok_count") or 0
        if sf:
            extra = ""
            if snap.get("last_send_detail"):
                extra = f"，最近一次：{str(snap.get('last_send_detail'))[:60]}"
            lines.append(f"⚠️ 发送失败累计 {sf} 次（成功 {so} 次）{extra}")
            todo.append("发送失败**不会自动重试**（发消息不可逆、重试可能让对方收到两条）。"
                        "确认对方没收到再自己重发。")
        else:
            lines.append(f"发送：成功 {so} 次，失败 0 次")

    lines.append("")
    if not todo:
        lines.append("✅ 没发现异常：收消息、登录、发送这三条路看起来都正常。")
    else:
        lines.append("👉 按这个顺序做：")
        for i, item in enumerate(todo, 1):
            lines.append(f"  {i}. {item}")
    return "\n".join(lines)


def _fmt_ts(ts):
    """epoch 秒 → 本地时间串。读不出来就说读不出来，别显示 1970。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "（时间读不出）"


def render_conversation(rows, who, meta, names=None):
    """把历史行渲染成**给人看 / 存档**的文本。

    两条规矩：
      * 说话人一律走 `agent_tools.speaker_of`——**绝不出现 wxid**（导出文件常被
        转发/分享，把 id 写进去等于把内部标识散出去）；
      * 头部**如实**写清覆盖范围；被截断就明说「这份不完整」——
        **绝不把「前 N 条」说成「全部」**。
    """
    lines = [f"# 和 {who} 的对话导出", ""]
    span = ""
    if meta.get("oldest") and meta.get("newest"):
        span = f"　{_fmt_ts(meta['oldest'])} → {_fmt_ts(meta['newest'])}"
    lines.append(f"共 {meta.get('count', len(rows))} 条文本消息{span}")
    if meta.get("truncated"):
        lines.append("⚠️ **这份不完整**：碰到了本次导出的条数上限，更早的消息**没有导出**。")
    lines += ["", "-" * 60, ""]
    for m in rows:
        spk = agent_tools.speaker_of(m, names or {}, who)
        lines.append(f"[{_fmt_ts(m.get('time'))}] {spk}：{m.get('content')}")
    return "\n".join(lines)


def export_conversation(wcf, cfg, contacts, who_raw):
    """`/导出 <某人>` 的实现。返回给用户的文本（**落盘了就给路径**）。"""
    alias = auto_reply.address_aliases(cfg)
    cand, err = agent_tools.resolve_one(contacts, who_raw, cfg.get("self_wxid", ""),
                                       wcf, aliases=alias)
    if err:
        return err
    who = cand.get("remark") or cand.get("name") or who_raw
    f = (cfg.get("file") or {})
    try:
        page = max(20, min(500, int(f.get("export_page", 200))))
    except (TypeError, ValueError):
        page = 200
    try:
        cap = max(0, int(f.get("export_max_messages", 5000)))
    except (TypeError, ValueError):
        cap = 5000

    rows, meta = live_history.collect_contact_history(wcf, str(cand.get("wxid")),
                                                      page=page, max_items=cap)
    if not rows:
        return (f"没查到和「{who}」的**文本**历史（要么确实没有，要么库句柄掉了）。\n"
                f"可以发 `/自检` 看一眼——它会把「是不是查不到库」说清楚。")

    text = render_conversation(rows, who, meta, auto_reply.contact_names(contacts))
    try:
        d = file_read.export_dir(cfg)
        os.makedirs(d, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", str(who))[:40] or "对话"
        fp = os.path.join(d, f"对话_{safe}_{time.strftime('%Y%m%d-%H%M%S')}.txt")
        with open(fp, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as e:
        return f"导出写文件失败：{e}（**没有生成文件**）"
    try:
        # 复用 file_read 的清理（它只扫 .txt —— 所以这里故意导 .txt，
        # 保证「导出目录会自己清理」这个承诺是真的，而不是新开一处没人管的目录）。
        file_read.sweep_exports(cfg)
    except Exception:
        traceback.print_exc()

    tail = ""
    if meta.get("truncated"):
        tail = (f"\n⚠️ 这份**不完整**：到了上限 {cap} 条，更早的没有导。"
                f"要全量就把 config.yaml 的 `file.export_max_messages` 调大再导一次。")
    return (f"已导出和「{who}」的对话：**{meta['count']} 条**（翻了 {meta['pages']} 页）。\n"
            f"文件：{fp}{tail}\n"
            f"（用记事本/编辑器打开就行；这个目录会自动清理旧的导出。）")


def handle_cursor_stall(h, client, cfg):
    """游标停滞时的处置。返回**要发给用户的话**（`None` = 一个字都不说）。

    ⚠️⚠️ **游标不动 ≠ 故障**：没人发消息的时候游标本来就不动。
    所以这里只把「停滞」当触发条件，到阈值去问**权威探针**
    （`live_history.fts_alive`：fts 分片还读得到吗）：
      * 探针说好 → **空闲**，返回 None（什么都不说，只记 `stall_probed` 免得每轮白探）；
      * 探针说坏 → 那才是真失效，返回要汇报的话，并记 `stall_reported`（只报一次）。

    **为什么抽成独立函数**：这段判断原来埋在 `iter_aixed_messages` 里，自测只能覆盖到
    它的零件（阈值、通知队列、`_try_selfheal`），**覆盖不到这个判断本身**——
    于是「空闲被误报成「数据库句柄掉了」」这个 bug 一路跑到了真机上
    （2026-10-02 真机误报，用户没说话却收到两条假警报）。抽出来才测得到。
    """
    if h is None or getattr(h, "stall_probed", False):
        return None
    if h.cursor_stalls < _stall_threshold(cfg):
        return None
    h.stall_probed = True
    try:
        alive, detail = live_history.fts_alive(client)
    except Exception as e:
        # 探针自己炸了也不能当成「故障」来吓用户——如实说探不动，但不报警
        print(f"[bot] 探针 fts_alive 出错（不当成故障）：{type(e).__name__}: {e}")
        return None
    if alive:
        return None            # ← 空闲，不是故障。**这里绝不许报任何东西。**
    h.stall_reported = True
    return (f"⚠️ **收不到新消息了**：连续 {h.cursor_stalls} 轮轮询都没有新消息，"
            f"而且探针确认读不到 fts 分片——\n"
            f"  {detail}\n"
            f"（这就是「静默失效」：查询不报错、只是查不出东西，"
            f"看起来和一切正常一模一样。）\n"
            f"{_try_selfheal(client)}\n"
            f"该你做的（按顺序）：\n"
            f"  1. 先在微信里确认**没掉登录**（设置→没退回登录界面）；"
            f"掉登录只能你扫码，bot 自己恢复不了。\n"
            f"  2. 还不行就**重启 bot**。\n"
            f"  3. 想看清楚一点，发 `/自检`。")


def handle_stall_recovery(h):
    """真汇报过之后恢复了 → 给一句「过去了」；否则 `None`。

    ⚠️ **判据是 `recovered_from_stall` 本身，这里绝不能再要求 `stall_reported`。**
    原因（差点写错、自测当场抓出来）：`Health.note_poll` 在游标动的那一刻
    **同时**做三件事——置 `recovered_from_stall=True`、清 `stall_reported`、清 `stall_probed`。
    所以等这个函数跑的时候 `stall_reported` **早就是 False 了**；
    再加一句 `and h.stall_reported`，这个提示就**永远不会出现**（功能静默失效）。
    可 `recovered_from_stall` 只在「当时确实 `stall_reported` 为真」时才被置上
    （见 `Health.note_poll`），所以它自己就够可靠——**空闲时探过但没报的那种，
    这里绝不会冒出一句莫名其妙的「刚才的问题过去了」**。
    """
    if h is None or not getattr(h, "recovered_from_stall", False):
        return None
    h.recovered_from_stall = False
    return ("✅ 刚才那次「读不到 fts 分片」已经过去了：数据又能读到了"
            f"（当时连续 {h.max_cursor_stalls} 轮没动静）。")


def handle_hook_db_recovered(h, client, cfg):
    """确诊过「库查不动」之后，轮询恢复正常了 → 再问一次权威探针并**报一句恢复**。

    为什么恢复也要过探针：轮询「看起来正常」在库层半死时可以骗人（`fts` 走的是缓存、
    `session` 兜底又是另一条路）。恢复是**好消息**，同样不许报错——报了用户会白高兴一场。
    返回要发给用户的话（`None` = 什么都不说）。
    """
    if h is None or not h.hook_db_recovered_due():
        return None
    try:
        ok, detail = live_history.db_alive_probe(client)
    except Exception as e:
        print(f"[bot] 探针 db_alive_probe 出错（不结论）：{type(e).__name__}: {e}")
        return None
    if h.note_hook_db_probe(ok, detail) != "recovered":
        return None
    return (f"✅ 刚才那次「微信的库查不动」已经过去：库又能查了"
            f"（当时连续 {h.max_hook_stress_rounds} 轮不正常）。\n"
            f"探针说：{detail}")


def handle_hook_db_dead(h, client, cfg):
    """「hook 可达、但库查不动」时的处置。返回**要发给用户的话**（`None` = 什么都不说）。

    这是 2026-10-06 真机补的缺口：微信被压崩之后进程**没退**、30001 还应答、
    `IsLogin` 还报 1，但句柄表已经空、核心库再没被写过 —— 三态登录探针把它判成「在线」，
    于是助手一声不吭地对着尸体每 5 秒轮询一轮，**用户拿不到任何提示**。

    判据分两层（和 `handle_cursor_stall` 同一个姿势，别把两层并成一层）：
      * **触发**：`health` 里两条互相独立的事实同时成立——连续 N 轮轮询不健康
        **且** 核心库 M 秒没被写。单独任何一条都会误报（前者可能只是 hook 慢，
        后者在没人用微信时本来就是常态）；
      * **确诊**：`live_history.db_alive_probe` 真的去查一次库（三态：能查 / 查不动 /
        连不上）。**连不上不在这里下结论**——那条路是登录探针的活。
    """
    if h is None or not h.hook_db_dead_due():
        return None
    try:
        ok, detail = live_history.db_alive_probe(client)
    except Exception as e:
        print(f"[bot] 探针 db_alive_probe 出错（不结论）：{type(e).__name__}: {e}")
        return None
    if h.note_hook_db_probe(ok, detail) != "dead":
        return None
    return (f"⚠️ **微信这边的库查不动了**（hook 还在应答，所以不是「连不上」）：\n"
            f"  {detail}\n"
            f"触发条件：连续 {h.hook_stress_rounds} 轮轮询不正常，"
            f"而且核心库已经 {int(h.db_age_seconds or 0)} 秒没被写过"
            f"（第 {h.db_dead_count} 次）。\n"
            f"该你做的：**完全退出微信 → 重新打开 → 扫码登录**；"
            f"助手不用动，微信回来我会自己接上。\n"
            f"⚠️ 这种状态下 `IsLogin` 往往**还报 1**：判定只看「库查不查得动」，别看它。")


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


# 真机踩过（2026-10-04）：用户在控制会话连着说了两次「关闭自动回复」「关闭张三的
# 自动回复啊」，模型**没有调 auto_reply 工具**，直接回了一句「自动回复功能已经关闭。
# 如果您有其他需要帮助的地方，请告诉我。」——而 settings.json 里 `enabled` 一直是
# true，于是它**继续**替用户回对方（日志里紧接着还有 `自动回复 -> 张三: …`）。
# 用户以为关了，其实一个字都没改。和 run_command 那次**同源**：提示词是建议，不是保证。
#
# 判据（宁可少触发）：提到「自动回复」**并且**带完成态的开关说法才算；
# 出现否定/疑问/假设词一律不追加（模型在如实解释现状、或反问用户时不能被打岔）。
_AUTO_REPLY_WORDS = ("自动回复", "代回复", "代回")
_AUTO_REPLY_DONE = (
    "已关闭", "已经关闭", "关闭了", "已关掉", "已经关掉",
    "已开启", "已经开启", "开启了", "已打开", "已经打开", "打开了", "已开",
)
_AUTO_REPLY_NEG = (
    "没有", "没关", "没开", "未关闭", "未开启", "不确定",
    "是不是", "要不要", "怎么", "为什么", "想关", "要关", "需要关",
)
# 没有真改配置时固定追加的真话。**别删** —— 真机上就是它拦住「模型自己演一句已关闭」。
AUTO_REPLY_NOT_CHANGED_NOTE = (
    "\n\n（补充：我这一轮**其实没有改动自动回复的开关**，刚才那句是我自己说的、不算数。"
    "要真关：全局发 `/auto off`；只关某个人发 `/auto del 张三`。"
    "想先看当前状态发 `/auto`。）")


def looks_like_auto_reply_claim(text):
    """这句话像不像在声称「我已经把自动回复开/关了」。

    宁可少触发：光提「自动回复」不算（可能只是解释现状）；
    必须同时出现完成态的开关说法，而且整段没有否定/疑问/假设词。
    """
    t = str(text or "")
    if not t:
        return False
    if any(w in t for w in _AUTO_REPLY_NEG):
        return False
    if not any(w in t for w in _AUTO_REPLY_WORDS):
        return False
    return any(w in t for w in _AUTO_REPLY_DONE)


def with_auto_reply_truth_note(answer, cfg_changed):
    """回答里声称改了自动回复、而本轮**没有任何配置改动**时，追一句真话。

    ⚠️ `cfg_changed` 是**粗判据**（它覆盖所有改配置的工具，不只是 auto_reply）。
    这是有意的：宁可漏报（他真改过别的、我们不多嘴），
    也绝不冤枉一个真改了的 —— 和 shell 那条同一个取舍。
    """
    text = str(answer or "")
    if not text or cfg_changed:
        return text
    if looks_like_auto_reply_claim(text):
        print("[bot] 回答声称改了自动回复，但本轮 cfg_changed=False → 追加真话")
        return text.rstrip() + AUTO_REPLY_NOT_CHANGED_NOTE
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


def error_reply_text(exc):
    """把「这一轮没答上来」翻成**用户能看懂、能行动**的一句话。

    ⚠️ 换掉的是原来那句 `出错了，看终端日志。`（2026-10-07 真机）：
    助手现在是**计划任务拉起的无窗口进程**，用户**根本没有终端可看** —— 那句话
    既没说他遇上了什么，也没说该做什么，正好撞在项目最忌讳的"无用/误导文案"上。
    真机现场：模型接口那 38 分钟连不上（`ConnectionRefusedError` → `连不上
    https://api.deepseek.com`），5 次提问收到的都是那句废话。

    两条硬规矩：
      * **不转发整段 traceback**（里面有本机路径、请求细节），只取异常文本的头一段；
      * 认不出的异常也**如实报类型与原文**，并指向 `bot.log`（用户能打开那个文件），
        **绝不编一个原因**。
    """
    text = str(exc or "").strip()
    low = text.lower()
    # ① llm.py 已经分好类了（`LLMUnreachable.kind`）→ 直接照着说，**别去嗅探文本**。
    # 2026-10-07 真机（校园网 Wi-Fi 掉线 3 分钟）：分类的价值就在这里 ——
    # 「本机没网」和「只有这个接口不通」要用户做的事完全不同。
    kind = getattr(exc, "kind", None)
    target = getattr(exc, "target", "") or ""
    detail = getattr(exc, "detail", "") or text
    if kind == "net_down":
        waited = float(getattr(exc, "waited", 0.0) or 0.0)
        tail = (f"（我等了 {waited:.0f} 秒也没回来）" if waited > 0
                else "（我探了两个公共目标都不通）")
        return (f"这台电脑现在连不上外网（Wi-Fi 掉线 / 还没认证？{tail}）。"
                f"助手本身在跑，网络回来再发一次就行。")
    if kind == "dns":
        return (f"域名解析不了（{target}）：多半是本机网络刚断或刚重连。"
                f"过一会儿再发一次就行。")
    if kind == "refused":
        return (f"模型接口连不上（我探过别的公网目标是通的，所以更像接口这边/代理的问题）："
                f"{target}　过一会儿再发一次就行。")
    if kind == "host_down":
        return (f"模型接口连不上（连接超时或中断）：{target}。"
                f"过一会儿再发一次就行。")
    # ② 没分类的（老调用点、别的异常）→ 按下文文本判断，行为与以前一致。
    # 模型通道连不上（llm.py `_post` 的原话）：网络/代理问题，和助手本身无关。
    if text.startswith("连不上 ") or "urlopen error" in low:
        # 去掉异常自带的那个「连不上 」前缀，否则读起来是「连不上…：连不上…」
        detail = text[len("连不上 "):] if text.startswith("连不上 ") else text
        return ("模型接口连不上（这通常是网络或代理的问题，助手本身在跑）："
                + detail[:120] + "\n过一会儿再发一次就行。")
    # 模型接口有回应但拒绝：429 最常见，说清是限流而不是"坏了"。
    if "http 429" in low:
        return "模型接口在限流（HTTP 429）。等一两分钟再发一次就行。"
    if "http 401" in low or "http 403" in low:
        return "模型接口拒绝了这次调用（HTTP 401/403，多半是 key 失效或没额度）。" \
               "在微信里发 /api <新key> 可以换一把。"
    head = f"{type(exc).__name__}：{text[:150]}" if text else type(exc).__name__
    return f"这一轮没能答上来（{head}）。完整堆栈在 bot.log 里。"


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

    # 消费闸：自动回复也是一次真实的模型调用。到上限就别再花钱了——
    # 但**必须告诉用户**（在控制会话里说），绝不静默地不回人家。
    if usage is not None:
        blocked_text = usage.budget_block_text(cfg)
        if blocked_text:
            label0 = str(rec.get("name") or chat)
            print(f"[bot] 自动回复跳过（消费闸）：{label0}")
            try:
                send(f"⚠️ 自动回复**没有生成**（消费闸拦住了）——{label0} 这条消息"
                     f"我不会替你回。\n{blocked_text}", control_chat)
            except Exception:
                traceback.print_exc()
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
        # ⚠️ 这里**故意不判重**（`set_pending` 也对 kind="auto" 关了判重）：草稿是响应
        # **某一条消息**生成的，两条一样的草稿对应两条不同的消息，合并＝第二条没人回。
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


def connect_aixed(base_url, give_up_fails=None):
    """连 aixed/WeChat-Hook 起的本地 HTTP 服务。**一直在等**，除非调用方显式给上界。

    ⚠️ 2026-10-05 真机：微信掉登录、用户重新扫码之后，hook 自报 `IsLogin: 1`，
    可三个库的句柄**全是空的**（`get database handle which named … failed`）。
    旧实现只探不修，于是它每 10 秒刷一行「请扫码登录」，**30 次之后 sys.exit(1)**——
    用户明明已经扫码了，助手却再也起不来。现在：失败先走 `_gate_retry_step`（该自愈的自愈）。

    ⚠️ 2026-10-07 真机（**本函数的核心契约**）：旧实现还有第二条「到次数就放弃」——
    连不上 hook 时 30 次（约 5 分钟）就 `return None` → `sys.exit(1)`。那晚的后果是
    「开机自启了，却静默失联一整晚」：22:03 拉起时微信根本没开、22:12 就放弃了，
    而 Run 键是**一次性发射**，没有任何东西再拉它。**微信晚几小时起来是正常用法**，
    所以「放弃」被退役了：现在退避着**一直等**（前两次 10 秒，之后 30/60 秒封顶）。
    `give_up_fails`（连续失败多少次就放弃）**只有自测会传**，产品路径故意不传。
    见 `docs/autostart-task-notes.md`。
    """
    print(f"[bot] 正在连接 aixed HTTP 服务 {base_url} ...")
    client = AixedClient(base_url)
    hard_fails = 0
    retries = 0
    last_log = 0.0
    last_notify = 0.0
    started = time.monotonic()
    while True:
        ok, info = client.ping()
        if ok:
            print(f"[bot] 自己的 wxid = {info}")
            return client
        retries += 1
        now = time.monotonic()
        hard_fails, note = _gate_retry_step(client, hard_fails)
        # 显式上界（**只有自测会传**；产品路径不传 ⇒ 一直等，见 docstring）。
        if give_up_fails and hard_fails >= int(give_up_fails):
            print(f"[bot] 连不上 hook 已连续 {hard_fails} 次（调用方给的上界）→ 如实放弃。")
            return None
        # 连不上是"hook 没应答"；库打不开是"hook 在、句柄空"——退避按前者算。
        wait = _gate_backoff_sec(hard_fails) if hard_fails else 10.0
        # 日志**限流**（2026-10-06）：以前每 10 秒两行（"限流中"+"10 秒后重试"），一晚能把
        # 日志刷穿，而且看的人更慌。改成一分钟一行，并带上「第几次 / 等了多久 / 微信还在写库吗」
        # —— 后者正是判断"该不该等"的那条事实（`core_db_age_sec`，纯 stat）。
        if now - last_log >= _GATE_LOG_INTERVAL:
            last_log = now
            if note:
                print(f"[bot] {note}")
            try:
                db_age = hook_check.core_db_age_sec()
            except Exception:
                db_age = None
            wrote = (f"微信最近写库 {db_age:.0f} 秒前" if db_age is not None
                     else "微信写库时间读不到")
            print(f"[bot] ⏳ 还在等（第 {retries} 次 / 已等 {int(now - started)} 秒；{info}；"
                  f"{wrote}），{wait:.0f} 秒后再试 ...（助手会一直等，不会自己退出）")
        # 卡过 `_GATE_NOTIFY_AFTER` 轮就**主动弹一次本地通知**：控制台没人看、WeChat 又收不到
        # 消息的时候，这是唯一能告诉他"该去把微信开起来"的路（和 C 的告警同一个通道）。
        # 两种等法的话**不能混**（2026-10-07）：连不上 hook ≠ 已登录但句柄空，用户要做的事不一样。
        if retries >= _GATE_NOTIFY_AFTER and now - last_notify >= _GATE_NOTIFY_INTERVAL:
            last_notify = now
            body = ("连不上微信的 hook（127.0.0.1:30001 没应答）。**把微信打开就行**——"
                    "助手会一直等、自己接上，不用管它。"
                    if hard_fails else
                    "hook 还在应答、也显示已登录，但数据库句柄打不开（**不是掉登录**）。"
                    "该做的：彻底退出微信 → 重新打开 → 扫码；助手不用动，它会自己接上。")
            try:
                health.notify("微信助手：还在等微信", body)
            except Exception:
                traceback.print_exc()
        time.sleep(wait)


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
    """探一次登录态，返回 `(状态, 说明)`。**三态**：

      True  = 在线；
      False = 探到了、明确不在线（`IsLogin: 0`，微信停在登录界面）；
      None  = **探针本身失败**（连不上 hook）。⚠️ 这**不等于**掉登录 ——
              微信没在跑 / hook 卡住 / 端口不对都会长这样，说成「掉登录」会把
              排查方向带偏（2026-10-05 真机就吃了这一口：日志写「微信似乎回到
              登录界面了（IsLogin=0）」，其实微信好好的、是 hook 那几分钟不接连接）。

    只用 hook 的只读接口，**不碰数据库句柄表**（不触发那 700MB 进程里的全内存扫描）。
    """
    try:
        ok = bool(client.is_login())
    except Exception as e:
        return None, f"探不到登录态（连不上 hook）：{e}"
    if not ok:
        return False, "IsLogin: 0（微信可能停在登录界面）"
    return True, ""


def _msg_ts(msg):
    try:
        return int(getattr(msg, "create_time", 0) or 0)
    except (TypeError, ValueError):
        return 0


# 语音转写后的渲染形态：`[语音] 你好。` / `[语音 1"] 你好。`
# ⚠️ 正则**必须**只认 `[语音]` / `[语音 时长]`，不能认 `[语音条（…）]` ——
# 后者是「读不到内容」的标签，把标签后面那个时长当成识别结果就闹笑话了。
_VOICE_TAG_RE = re.compile(r"^\[语音(?:\s+[^\]]*)?\]\s*(?P<txt>.+)$", re.S)


# 后台读完的文件，**发给用户**时正文的体量闸（字）。
# 和 `executor.WECHAT_MAX_CHARS` 是同一个思路：微信消息不该有几万字。
# 超了就只发前 N 字 + 如实说"一共多少字、完整内容导出在哪儿"。
# 2026-10-03 真机：一份 A.zip 读完 27074 字**整段**发出去，聊天被刷好几屏。
_READ_BODY_MAX = 1500

# 单条**发出去**的消息上限（字）。微信会**直接拒收**过长的消息：
# 2026-10-03 真机，一条 27074 字的回复被挡下，用户那边什么都看不到，
# 只在会话里留下一条系统消息「Messge exceeds character limit. Unable to send.」。
# hook 的 `send_text` 对这种拒收**照回 ret:0**（它不知道微信内部拒了），
# 所以只能**发之前**自己截断，并把"截了"如实说出来 —— 绝不静默丢件。
_SEND_MAX_CHARS = 4000

# 「只有类型标签、没有真内容」的渲染形态：`[系统消息]`、`[表情]`、`[通话]`…
# ⚠️ 标签名长度上限 **10**：真实标签最长是 `好友申请` / `系统消息`（5 个字），
# 而 `[语音条（**读不到内容**：音频不在本机磁盘上）]` 那种带解释的**不是标签**
# —— 上限放松到 24 就会把它也吞掉（自测当场抓到），语音的失败提示就永远进不了模型。
_LABEL_ONLY_RE = re.compile(r"^\[[^\[\]]{1,10}\](\s*\d+\"?)?$")


def is_label_only(content):
    """这条内容是不是**只有一个类型标签**（没有真内容）。

    非文本消息（图片/语音/系统消息/表情…）会被渲染成 `[系统消息]` 这样一行标签，
    好让模型**知道有这么个东西**。但**标签不是用户说的话**：拿它去问模型，
    模型只能回一句「我看不到内容」—— 那就是**对着空气回话**。
    2026-10-03 真机：文件传输助手连着来了两条空 `[系统消息]`，bot 每条都郑重回一段
    解释，用户看到的就是「我没说话它却回了好几条」。
    """
    s = str(content or "").strip()
    if not s:
        return False
    return bool(_LABEL_ONLY_RE.match(s))


def voice_already_transcribed(content):
    """消息渲染里**已经带着**微信的「转文字」结果？是就返回那段文字，否则 ""。

    这条是零成本路径：`live_history._render_nontext()` 在渲染时就把
    `packed_info_data` 里的转写拼成了 `[语音] 你好。`（见 `voice_transcript`）。
    """
    m = _VOICE_TAG_RE.match(str(content or "").strip())
    if not m:
        return ""
    txt = m.group("txt").strip()
    return "" if "读不到内容" in txt else txt


def read_voice_message(client, cfg, msg):
    """语音条 → 文字。返回 `(文本, 一句说明)`；读不出来时文本为空、说明是人话。

    **三级优先，便宜的先来**：

      1. **微信自己的「转文字」**（消息渲染里已经带着）—— 用户点过一次就有，
         **零成本**、不用扫内存、不用转写；
      2. **趁热扫微信进程内存**拿明文 SILK → `pilk` 解码 → 本地 whisper 转写
         （`voice_mem.py`）。实测：手机发来的语音 7.5 秒出文字。
         前提是**趁热**：内存里同长度的语音很多，晚了就认不出是哪条
         （实测同一条语音 20 分钟后，内存里站着 12 条别人的同长度语音）；
      3. 读不出来 → **如实说**，绝不拿别的语音顶上。

    ⚠️ 第 2 条是**同步**的（实测 5~8 秒），跑在收消息那条线程上，这段时间
    轮询会停 —— 和 `run_command` / 群发同一档代价。所以有 `voice.auto_read`
    开关和 `voice.max_seconds` 上限。
    """
    tag = voice_already_transcribed(getattr(msg, "content", ""))
    if tag:
        return tag, "微信已转文字"
    vcfg = (cfg or {}).get("voice") or {}
    if not isinstance(vcfg, dict):
        vcfg = {}
    if not bool(vcfg.get("auto_read", True)):
        return "", ""
    if not voice_mem.available()[0]:
        return "", voice_mem.available()[1]

    talker = getattr(msg, "roomid", "") or getattr(msg, "sender", "") \
        or getattr(msg, "talker", "")
    lid = str(getattr(msg, "local_id", "") or "")
    if not lid:
        # fts 那条路不带 local_id；语音本来也不进 fts，所以正常轮询不会走到这里
        return "", "这条语音没带 local_id，认不出是哪一条"
    info = live_history.voice_info(client, talker, lid)
    ms = int(info.get("duration_ms") or 0)
    if ms <= 0:
        return "", "拿不到这条语音的时长（XML 里没有 voicelength），认不出是哪一段"
    try:
        cap = int(vcfg.get("max_seconds") or 60)
    except (TypeError, ValueError):
        cap = 60
    if ms / 1000.0 > cap:
        return "", (f"这条语音 {ms / 1000:.0f} 秒，超过 voice.max_seconds={cap} 秒，"
                    f"没试着转（想放宽就改 config.yaml）")
    # ⚠️ `cfg` **必须传**：不传就永远是本地 whisper-small ——
    # `audio.backend: cloud`（实测 1~2 秒）和 `audio.model: tiny` 全都不会生效，
    # 用户以为换了快的，其实还在本地跑 small。
    #
    # `target_bytes` = 消息 XML 里的 `length`：内存里那条 SILK 的真实长度和它精确对应
    # （自己发出的样本实测 8/8 差 −1）。**这是唯一能分开"同时长的两条语音"的信号** ——
    # 以前只按时长，撞上就得靠 whisper 置信度硬分、分不出来就拒答（真机多次）。
    # 解析早就有（`voice_info` 的 `length_bytes`），只是一直没往下传。
    texts, err = voice_mem.read(ms, cfg=cfg,
                                target_bytes=int(info.get("length_bytes") or 0) or None)
    if err:
        return "", err
    if len(texts) == 1:
        return texts[0], "内存里的语音 + 本地转写"
    # 多条不同的识别结果：**不替用户挑**，第一条照用，但把别的也说出来
    return texts[0], ("（另外还有相近的识别结果：" + "／".join(texts[1:4]) + "）")


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


# 「不是我该管的会话」被丢掉时的留痕（限流）。**只对拿不到会话名的那种**（`session_<N>`）
# 留痕：正常的群消息一秒好几条，全打会把 bot.log 刷穿；而 `session_<N>` 是**故障形状**
# —— 会话名映射抖动/过期时，控制会话会被认成 `session_297`，那条 `continue` 一声不吭，
# 用户看到的就是「我发消息它不理」而日志里毫无痕迹（2026-10-05 真机）。
_OFFTARGET_N = [0]
_OFFTARGET_AT = [0.0]
_OFFTARGET_INTERVAL = 60.0


def _note_offtarget_skip(sender):
    """被当成「非目标会话」丢掉的消息留一行痕。返回是否真打了。

    ⚠️ 只认 `session_<数字>`：正常群/好友消息照旧安静（那是有意丢的，不是故障）。
    """
    if not str(sender or "").startswith("session_"):
        return False
    _OFFTARGET_N[0] += 1
    now = time.monotonic()
    if now - _OFFTARGET_AT[0] < _OFFTARGET_INTERVAL:
        return False
    _OFFTARGET_AT[0] = now
    print(f"[bot] ⚠️ 有消息**拿不到会话名**（{sender}），被按「非目标会话」丢掉："
          f"累计 {_OFFTARGET_N[0]} 条。这不是「没消息」——多半是会话名映射过期/抖动，"
          f"重启助手即可恢复；这条痕迹就是为它留的。")
    return True


def _max_catchup(cfg):
    """落后多少行就**跳到头部**（`poll_max_catchup`；默认用 live_history.CURSOR_MAX_GAP；0 = 关）。

    ⚠️ 这条闸**会丢通知**（那批旧消息不再逐条通知），所以：① 它能被显式关掉（0）；
    ② 每次触发都必须在控制会话里如实说明（见 iter_aixed_messages 里那段）。
    配置读不出来时回**默认阈值**，而不是静默变成 0（那等于悄悄把闸关了）。
    """
    try:
        return int((cfg or {}).get("poll_max_catchup", live_history.CURSOR_MAX_GAP))
    except (TypeError, ValueError):
        return live_history.CURSOR_MAX_GAP


def _min_round_interval(cfg):
    """连续有消息时，两轮之间**至少**隔多少秒（`poll_min_interval`，默认 1；<=0 = 关）。

    ⚠️ 这不是「让助手变慢」：稳态（没有新消息）时它照旧等 `poll_interval`。
    这个闸只在**连续有消息**时兜底 —— 2026-10-05 真机：重启后追赶积压时，
    「有消息就不睡」让主循环连轴转（实测约 2 秒/轮；每轮 3 个 fts 分片各满页
    200 行 ≈ **300 行/秒**），而每一行 appmsg 还会再回查一次 `message_N.db`
    拿原始 XML —— 正好把持续的慢查询/500 压在刚起步的微信上，那几个窗口
    就是当天的崩溃窗口。配合 `live_history.POLL_ROWS_PER_SHARD` 一起把速度压下来。
    """
    try:
        v = float((cfg or {}).get("poll_min_interval", 1))
    except (TypeError, ValueError):
        return 1.0
    return v if v > 0 else 0.0


def _ramp_sleep(had_msgs, spent, interval, min_round, t0, cfg, now=None):
    """起步阶段把轮询间隔**渐进**放慢（2026-10-05 加，为配合 hook 崩溃治理）。

    为什么要有它（真机数据）：10-01~10-04 平均约 24 小时崩一次微信，而 10-05 一天崩了 8 次，
    且崩溃全都贴着「微信刚登录 / 刚重启」的那 1~3 分钟——那正是微信自己还在把
    MSG*.db、message_fts.db 逐个打开的窗口。助手每 `poll_interval`（默认 5 秒）一轮的查询
    正好压在这个窗口上，是已知的两个崩溃入口之一（另一个是 hook 内部主动扫句柄表）。

    所以起步阶段先静置、再逐步加压：
        [0, early_sec)                                   → 每轮睡 early_interval（默认 30 秒）
        [early_sec, +mid_sec)                            → 每轮睡 mid_interval（默认 10 秒）
        [early_sec+mid_sec, +final_sec)                  → 每轮睡 interval+5
        之后                                             → 照旧 `_round_sleep`（原有行为）
    有消息时仍然尊重 `min_round`（见 `_min_round_interval`），不会被放慢逻辑吃掉。

    `t0` = 轮询起点（第一轮开头取一次 `time.monotonic()`）。**纯函数**，便于自测。
    `poll_ramp.enabled: false` 时行为与旧版本**逐字节一致**（改慢是为了不崩，
    不能变成改不回去的默认）。
    """
    rc = (cfg or {}).get("poll_ramp") or {}
    if not rc.get("enabled", True):
        return _round_sleep(had_msgs, spent, interval, min_round)

    def _num(key, dflt):
        try:
            return float(rc.get(key, dflt))
        except (TypeError, ValueError):
            return float(dflt)

    early_sec = max(0.0, _num("early_sec", 300))
    mid_sec = max(0.0, _num("mid_sec", 600))
    final_sec = max(0.0, _num("final_sec", 600))
    early_iv = max(1.0, _num("early_interval", 30))
    mid_iv = max(1.0, _num("mid_interval", 10))

    t = (time.monotonic() if now is None else float(now)) - float(t0)
    if t < early_sec:
        slow = early_iv
    elif t < early_sec + mid_sec:
        slow = mid_iv
    elif t < early_sec + mid_sec + final_sec:
        slow = float(interval) + 5.0
    else:
        return _round_sleep(had_msgs, spent, interval, min_round)

    if had_msgs:
        # 有消息时至少占满 min_round（与 _round_sleep 同一规矩），但**不**因为放慢而更慢
        return max(slow, max(0.0, float(min_round) - float(spent)))
    return slow


def _round_sleep(had_msgs, spent, interval, min_round):
    """一轮结束后该睡多久（秒）。抽成纯函数只为了能自测（见 selftest_bot_loop）。

    没消息：照旧睡 `interval` —— 正常轮询节奏，一个字都没改。
    有消息：让这一轮至少占满 `min_round` 秒 —— 理由见 `_min_round_interval`。
    """
    if not had_msgs:
        return float(interval)
    return max(0.0, float(min_round) - float(spent))


# ── 运行期让路：hook 卡了就把轮询放慢（2026-10-06）────────────────────────
# 为什么要它：10-06 13:37 那次崩溃前，日志是一串 **1.0~3.4 秒的空探测慢查询**
# （`SELECT 1 FROM sqlite_master LIMIT 1` 打在 MSG0..MSG7 / contact 上），而
# `aixed_api.SLOW_QUERY_SEC` 只**打印**、没有任何消费者 —— 已经变慢的微信不会因此少挨查询。
# 这条把「慢」接到动作上：连续几轮不健康就放慢，恢复正常立刻回落。
#
# ⚠️ 它和 `poll_ramp`（起步冷却）是**两件事**，别合并：
#   * `poll_ramp`：**每次启动**都无差别放慢 —— 10-05 用户否掉了它
#     （「连发两条消息隔半分钟才回第一条，用起来像坏了」），本机现在是关的；
#     包里那份示例 2026-10-06 也从「25 分钟」收短成「约 3 分钟」（见 config.example.yaml）；
#   * 这条：只在**已经出问题**时让路，正常路径一个字节都不变。
POLL_BACKOFF_DFLT = {"slow_rounds": 3, "sleep_sec": 30.0, "recover_rounds": 3}


def _backoff_cfg(cfg):
    """读 `poll_backoff` 段 → `(slow_rounds, sleep_sec, recover_rounds)`。

    `sleep_sec <= 0` = **关掉让路**（和 `poll_min_interval` / `poll_max_catchup` 同一条规矩）；
    读不出来回**默认值**，**不许静默变成 0**（那等于悄悄把闸关了）。
    """
    bc = (cfg or {}).get("poll_backoff") or {}

    def _num(key):
        try:
            return float(bc.get(key, POLL_BACKOFF_DFLT[key]))
        except (TypeError, ValueError):
            return float(POLL_BACKOFF_DFLT[key])

    return max(0.0, _num("slow_rounds")), _num("sleep_sec"), max(0.0, _num("recover_rounds"))


def _slow_round_sec(cfg=None):
    """一轮超过多少秒算「慢」。阈值真源是 `health.DEFAULTS`（config 的 health 段覆盖）。

    抽出来是因为**两个消费者都要用它**：让路（bot 自己）与「库查不动」的看护（health）。
    health.py 缺失时回代码兜底，绝不让「哪个模块没部署上」变成判据消失。
    """
    dflt = 3.0
    if health is not None:
        dflt = _as_float(getattr(health, "DEFAULTS", {}).get("hook_slow_round_sec"), 3.0)
    return _as_float(((cfg or {}).get("health") or {}).get("hook_slow_round_sec"), dflt)


def round_unhealthy(errors, spent=None, slow_sec=None, tripped=False):
    """这一轮算不算「不健康」—— 让路（B）与「库查不动」看护（C）的**唯一判据**。纯函数。

    ## 只认**权威读路径**的失败

    * `message_fts*`（收消息的主路）→ 算；
    * `session.db`（fts 全掉时的兜底路）→ 算；
    * `hook`（整个连不上）→ **不算**：那条路归三态登录探针，它有自己的告警；
    * `message_N.db`（`_v4_pickup_nontext` 补捞图片/文件那条 **best-effort** 路）→ **不算**。

    ⚠️ 最后那条是 2026-10-06 抓出来的判据缺口：`message_N.db` 实测**长期解析不出句柄**
    （CLAUDE.md 记着这条，本机也真的每 30 秒熔断一次），拿它当 stress 会让让路**永远关不掉**
    ——而让路的代价是「回消息慢 6 倍」，用户当天就报上来了（另一台机器上）。
    best-effort 路的失败仍然照旧**如实记录、如实打日志**（`_POLL_ERRORS` / 心跳里看得见），
    只是不再有资格把整条收消息链路拖慢。

    另外两条与错误无关的事实：本轮被总时限截断、本轮耗时超过阈值。
    """
    if tripped:
        return True
    try:
        if spent is not None and slow_sec and float(spent) > float(slow_sec):
            return True
    except (TypeError, ValueError):
        pass
    for key in (errors or {}):
        k = str(key)
        if k.startswith("message_fts") or k == "session.db":
            return True
    return False


class StressBackoff:
    """「要不要让路」的状态机（纯逻辑、无 IO，所以自测能直接钉）。

    进：连续 `slow_rounds` 轮不健康 → 放慢到 `sleep_sec`；
    出：让路之后连续 `recover_rounds` 轮正常 → **立刻**回到原节奏。
    滞回（进 3 出 3）是故意的：只差一轮就来回抖，用户会看到间隔忽长忽短。
    """

    def __init__(self, slow_rounds=3.0, sleep_sec=30.0, recover_rounds=3.0):
        self.slow_rounds = float(slow_rounds)
        self.sleep_sec = float(sleep_sec)
        self.recover_rounds = float(recover_rounds)
        self.stress_rounds = 0          # 当前连续不健康轮数
        self.calm_rounds = 0            # 让路中连续正常轮数
        self.active = False
        self.entered = 0                # 进过几次（诊断）
        self.max_stress_rounds = 0      # 最长连续不健康（诊断）

    def enabled(self):
        """`sleep_sec <= 0` 或 `slow_rounds <= 0` = 用户关掉了让路。"""
        return self.sleep_sec > 0 and self.slow_rounds > 0

    def note_round(self, unhealthy):
        """记一轮，返回 `"enter"` / `"exit"` / `""`（状态有没有变）。"""
        if not self.enabled():
            return ""
        if unhealthy:
            self.calm_rounds = 0
            self.stress_rounds += 1
            if self.stress_rounds > self.max_stress_rounds:
                self.max_stress_rounds = self.stress_rounds
            if not self.active and self.stress_rounds >= self.slow_rounds:
                self.active = True
                self.entered += 1
                return "enter"
            return ""
        self.stress_rounds = 0
        if self.active:
            self.calm_rounds += 1
            if self.calm_rounds >= self.recover_rounds:
                self.active = False
                self.calm_rounds = 0
                return "exit"
        return ""

    def next_interval(self, base):
        """这一轮该睡多久：让路中 = `max(base, sleep_sec)`，否则**原样返回 base**。"""
        if self.active and self.enabled():
            return max(float(base), self.sleep_sec)
        return float(base)


def iter_aixed_messages(client, interval, tick=None, cfg=None):
    """aixed 没有收消息接口，只能轮询数据库拿新消息。

    传了 `cfg` 才有「重启续上」——见下面 resume_window 的说明。
    """
    global _LAST_CURSOR_SAVE
    tick = tick or (lambda: None)
    min_round = _min_round_interval(cfg)
    # 起步冷却的计时起点（见 _ramp_sleep）：这一轮轮询开始时记一次
    ramp_t0 = time.monotonic()
    st = (cfg or {}).get("state") or {}
    try:
        resume_window = max(0, int(st.get("resume_window", 1800)))
    except (TypeError, ValueError):
        resume_window = 1800

    # 运行期让路：hook 卡了就放慢轮询（见上面的 `POLL_BACKOFF_DFLT` / `StressBackoff`）。
    # 构造在这里、**跨轮存活**——它记的就是「连续几轮不正常」。
    backoff = StressBackoff(*_backoff_cfg(cfg))

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
    # 分片被重建过 → 旧游标比新表头部还大 ⇒ 这条分片**永远**读不到新行，而且静默
    # （不报错、不留痕）。对齐到头部**不会丢任何东西**：比新头部更大的 rowid 本来就不存在。
    # 下一段是**会丢通知**的那一半（落后太多就跳），用户 2026-10-05 明确要，所以带如实通知。
    try:
        cursor, _fixed = live_history.align_stale_cursors(client, cursor)
    except Exception:
        _fixed = {}
        traceback.print_exc()
    if _fixed:
        _detail = "、".join(f"{k} {old}→{head}" for k, (old, head) in _fixed.items())
        print(f"[bot] ⚠️ {len(_fixed)} 个 fts 分片的游标**超过**了它当前的最大 rowid"
              f"（分片被重建过，这条游标再也读不到新行）：{_detail}。已对齐到头部；"
              f"**不会少收任何消息**（比新头部更大的 rowid 本来就不存在）。")
        push_notice("⚠️ 检测到微信的消息索引分片被重建过，我已把一条「读不到新消息」的游标"
                    "对齐到最新。**不会少收任何消息**，只是修掉一个静默收不到的状态。")

    # 「落后太多就跳到头部」——**会丢那批旧通知**，所以①阈值可配/可关、②每次触发都如实说。
    # 为什么要它：落后十几万行时按限速要追几小时，而新消息全排在那批历史后面 →
    # 用户看到的是「发消息半天不回」。跳过去之后新消息立刻就能回。
    try:
        cursor, _jumped = live_history.skip_far_behind(client, cursor, _max_catchup(cfg))
    except Exception:
        _jumped = {}
        traceback.print_exc()
    if _jumped:
        _rows = sum(v[2] for v in _jumped.values())
        _jdetail = "、".join(f"{k} {v[0]}→{v[1]}（跳过 {v[2]} 行）"
                             for k, v in _jumped.items())
        print(f"[bot] ⚠️ fts 游标落后头部超过阈值（{_max_catchup(cfg)} 行/分片），已**跳到最新**："
              f"{_jdetail}。合计约 {_rows} 行没读 —— 按限速要追很久，而新消息会被压在后面。"
              f"⚠️ 这批旧消息**不会再逐条通知**；要看它们请直接说「翻 XX 会话」。")
        push_notice(f"⚠️ 消息索引落后太多（约 {_rows} 条），我已**跳到最新**："
                    f"这中间的旧消息**不会再逐条通知**（想看它们直接跟我说「翻 XX 会话」）。"
                    f"要改成慢慢追就把 config 的 poll_max_catchup 调大或设为 0。")
    print(f"[bot] 轮询模式：游标 = {cursor}，间隔 {interval}s")
    polls = 0
    fails = 0          # 连续轮询失败次数（成功一次就清零）
    while True:
        round_started = time.monotonic()
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

        # ── 这一轮健不健康：**事实只算一次**，喂两个消费者（2026-10-06）──
        # 判据本体在 `round_unhealthy`（纯函数，那儿写了为什么只认权威读路径）。
        # 消费者：① 让路（连续 3 轮 → 放慢到 30 秒，见 StressBackoff）；
        #         ② health 的「库查不动」看护（连续 10 轮 + 核心库 600 秒没被写 → 探一次库）。
        # **阈值与目的都不同，事实只算一次**；库龄那一路是纯 stat，不碰 hook。
        try:
            _errs_now = live_history.poll_errors()
            _spent_round = time.monotonic() - round_started
            _unhealthy = round_unhealthy(_errs_now, _spent_round, _slow_round_sec(cfg),
                                         live_history.round_tripped())
        except Exception:
            traceback.print_exc()
            _unhealthy = False
        try:
            _bo = backoff.note_round(_unhealthy)
            if _bo == "enter":
                print(f"[bot] ⚠️ hook 连续 {backoff.stress_rounds} 轮不正常 → 先把轮询放慢到 "
                      f"{backoff.sleep_sec:g} 秒一轮（恢复正常会自动回到 {interval:g} 秒；"
                      f"`poll_backoff.sleep_sec=0` 可关掉这条）")
                push_notice(f"⚠️ 微信这边有点卡（连续 {backoff.stress_rounds} 轮查询不正常），"
                            f"我先把轮询放慢到 {backoff.sleep_sec:g} 秒一轮，免得继续压它；"
                            f"恢复正常会自动回到 {interval:g} 秒。")
            elif _bo == "exit":
                print(f"[bot] ✅ hook 恢复正常 → 轮询回到原节奏（{interval:g} 秒）")
        except Exception:
            traceback.print_exc()
        if h is not None:
            try:
                h.note_round(_unhealthy, hook_check.core_db_age_sec())
            except Exception:
                traceback.print_exc()
            try:
                for _notice in (handle_hook_db_dead(h, client, cfg),
                                handle_hook_db_recovered(h, client, cfg)):
                    if _notice:
                        push_notice(_notice)
            except Exception:
                traceback.print_exc()

        # ── 游标停滞 → 主动汇报（判断本身在 handle_cursor_stall 里，那份带注释更全）──
        # 一句话：停滞只是**触发条件**，报不报由权威探针说了算——空闲时一个字都不说。
        if h is not None:
            try:
                for _notice in (handle_cursor_stall(h, client, cfg),
                                handle_stall_recovery(h)):
                    if _notice:
                        push_notice(_notice)
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
        # 歇口气：没消息按 poll_interval；**有消息也至少隔 `poll_min_interval`** ——
        # 以前「有消息就完全不睡」，追赶积压时会变成满速扫库（见 _min_round_interval）。
        # 起步阶段再经 `_ramp_sleep` 放慢（见它的 docstring：崩溃都贴着登录窗口）。
        # 最后过一道**运行期让路**：hook 已经卡了的时候才更慢（正常时 `next_interval`
        # 原样返回，一个字节都不变）。
        _base_sleep = _ramp_sleep(bool(msgs), time.monotonic() - round_started,
                                  interval, min_round, ramp_t0, cfg)
        time.sleep(backoff.next_interval(_base_sleep))


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


def _chdir_project_root():
    """把工作目录切到项目根。返回**原来的**目录（本来就在项目根 / 切不动时返回 None）。

    为什么要有这一条（2026-10-05 真机）：
    bot 被计划任务/提权方式启动时 CWD 是 `C:\\WINDOWS\\System32`，于是配置里那些
    **相对路径**（`health.status_file: ./data/status.json`）被解析到系统目录去——
    `[health] ⚠️ write_status: 写 C:\\WINDOWS\\System32\\data\\status.json 失败：[WinError 5]`
    每 5 秒一行，把 bot.log 灌满、真正的崩溃线索（慢查询 → HTTP 500 → 10054）全被埋掉；
    而状态页读的 `data/status.json` 从那一刻起再没更新过（「没反应」和「一切正常」
    又变得分不出来——这正是 health 存在的理由）。

    三个 .bat 入口本来都 `cd /d` 到项目目录，所以这不是新行为，只是把那条约定
    补成**硬保证**：不管谁（人 / 计划任务 / 提权 / 别的自动化）怎么起，行为都一样。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    try:
        cur = os.getcwd()
    except OSError:
        cur = None
    if cur and os.path.normcase(cur) == os.path.normcase(root):
        return None
    try:
        os.chdir(root)
    except OSError as e:
        print(f"[bot] ⚠️ 切到项目目录失败（{root}）：{e}；"
              f"配置里的相对路径可能被解析到别处", file=sys.stderr, flush=True)
        return None
    return cur


def _has_interactive_console():
    """这份进程有没有**可用的控制台窗口** = 「有没有人能点 UAC」的唯一判据（2026-10-07）。

    为什么要它：`admin.ensure_elevated(assume=…)` 需要知道"这一刻有没有人能点那个 UAC 弹窗"。
    错的判据会两头都坏：写死 `assume=False` → 无人值守时弹一个没人点的窗，超时后 `exit(2)`
    （pythonw 无窗口，用户什么都看不到）；写死 `assume=True` → 用户双击启动时**静默降级成
    普通权限**，语音条读不到却毫无提示（本项目最忌讳的那种失效）。

    判据用 `GetConsoleWindow()`：pythonw / 计划任务 / 无窗口自启 = 0（没人能点），
    终端 / `启动助手.bat` 里有真窗口 = 非 0（有人能点）。
    **拿不到就按"没人能点"处理**（宁可只告警继续跑，也绝不静默消失）。
    """
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return False


def gate_assume_no_uac(has_console):
    """「这一刻没人能点 UAC」→ `assume=True`（只告警、继续跑）。纯函数，便于自测钉住两个分支。

    有控制台 = 用户在看着，该弹就弹（`assume=False`）：否则静默降级成普通权限，
    语音条读不到却毫无提示。没控制台（计划任务 / pythonw / 无人值守）= 弹了也没人点，
    超时后 `exit(2)` 在无窗口进程里等于静默消失 —— 那正是 2026-10-06 22:12 那晚的死法。
    """
    return not bool(has_console)


def main():
    moved_from = _chdir_project_root()
    setup_logging()
    if moved_from:
        print(f"[bot] 工作目录已从 {moved_from} 切到项目目录 {os.getcwd()}"
              f"（配置里的相对路径都按项目目录解析）")

    # ⚠️ **必须提权**（2026-10-06 用户定的硬约束：助手永远跑在管理员上，部署到别的电脑也一样）。
    # 为什么：语音条要读微信进程内存，而跨完整性级别读不了（微信提权开着 → 助手普通权限
    # 就被 `GetLastError=5` 拒绝）。证据见 docs/voice-reliability-2026-10-03.md 第六节。
    # 位置：**在 `acquire_single_instance()` 之前** —— 提权失败时这里就退出，
    # 不会出现"没提权的那一份先抢了锁、提权那一份反而起不来"。
    #
    # `assume`：**判据是"这份进程有没有控制台窗口"**，也就是"有没有人能点 UAC"（2026-10-07 修）。
    # 旧代码写死 `assume=False`（注释却写着自启那条路是 True）→ 开机自启那一刻它会去弹 UAC，
    # 没人点就 `exit(2)`，而 pythonw 无窗口 ⇒ **一点提示都没有**，用户只看到"发消息不回复"。
    # 现在的分工：
    #   * 没控制台（计划任务 / 老 Run 键 / 无人值守）→ `assume=True`：只告警，**继续以普通权限跑**
    #     （文本仍能回，只有语音条读不到）；绝不静默消失；
    #   * 有控制台（`启动助手.bat`、终端里 `python bot.py`）→ 照旧弹一次 UAC。
    # 计划任务那条路本来就是管理员（`RunLevel=Highest`），`is_admin()` 先返回，不受这里影响。
    _assume = gate_assume_no_uac(_has_interactive_console())
    _eok, _emsg, _elaunched = admin.ensure_elevated(capture=True, assume=_assume)
    if not _eok:
        print(f"[bot] {_emsg}", file=sys.stderr, flush=True)
        sys.exit(2)
    if _elaunched:
        # 刚在**另一个**提权窗口里把 bot 拉起来了 → 这一份立刻退出（绝不能两份同时跑）
        print(f"[bot] {_emsg}", flush=True)
        return
    print(f"[bot] 权限：{_emsg}")

    if not acquire_single_instance():
        sys.exit(1)
    base_cfg = load_config()
    cfg = settings.effective(base_cfg)

    # ── hook 版本自检（2026-10-06 真机：换了新包 ≠ 微信里的 hook 换上了）─────────
    # 这个位置能拿到的证据只有**磁盘**那条（读注册表 + 比两个文件的哈希）；
    # 运行时那条（`LoginGateInfo` 有没有）要连上 hook 之后才行，在下面 connect 之后补。
    # 为什么放在起搜索服务之前：它更接近"助手能不能干活"，先报出来更值钱。
    # **绝不许拦住启动**：自检自己出问题只打一行告警（见 hook_check 的注释）。
    try:
        _hres = hook_check.check()
        _hrep = hook_check.format_report(_hres)
        print(f"[bot] {_hrep}")
    except Exception:
        traceback.print_exc()
        _hres = None

    # 配套服务：网上搜索后端（SearXNG）。它是**独立进程**，bot 只通过 HTTP 问它，
    # 所以「助手起来了、却搜不了」是一种很容易发生的残疾状态。这里 best-effort 带起它。
    # `wait=0`：拉起就返回、**不等它 HTTP 通**（冷启动十几秒，等它等于白拖慢助手启动）；
    # 所以下面这句话是「已拉起（启动中）」，**不是**「已可用」。
    # 起不来只会让搜索不可用，**绝不许拦住助手启动**——ensure 自己兜住所有异常。
    try:
        _sok, _smsg = botctl.ensure_search_service(cfg, wait=0)
        print(f"[bot] 搜索服务：{_smsg}" + ("" if _sok else "（不影响助手运行）"))
    except Exception:
        traceback.print_exc()

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

    # 连接微信；后台自启时微信**可能还没启动**——所以这里**一直等**（2026-10-07 起：
    # 不再"约 5 分钟就放弃"，那正是"自启了却不回复"那晚的死因；见 connect_aixed 的 docstring）。
    backend = cfg.get("backend", "wcferry")
    # 默认值必须跟 config.yaml / CLAUDE.md 一致（5 秒）。以前这里写 2，而
    # config.yaml 的注释明确写着「实测 2 秒间隔会把微信卡到 CPU 999 秒」——
    # 配置里一漏这一行，代码就挑了个注释亲口反对的值。
    poll_interval = cfg.get("poll_interval", 5)
    if backend == "aixed":
        # 兜底端口必须跟 config.yaml / CLAUDE.md / postman 一致（30001）。
        # 这里以前写 8080：配置里一旦漏了 aixed_base_url，就会连错端口，
        # 而报错文案却指向「微信没启动、version.dll 没加载」，排查方向全错。
        # **不传 `give_up_fails`**：一直等（微信晚几小时起来也接得上）。
        wcf = connect_aixed(cfg.get("aixed_base_url", "http://127.0.0.1:30001"))
        if wcf is None:
            # 正常路径到不了这里（不给上界就一直等）；留作兜底，绝不假装成功。
            print("[bot] 连不上 aixed 服务。请确认微信已启动、version.dll 已加载、aixed_base_url 端口正确。")
            sys.exit(1)
        # 连上了 → 这时才拿得到**运行时**那条 hook 版本证据（`LoginGateInfo` 有没有）。
        # 磁盘那条已经在启动时看过；两条一起看才能分清「文件没换」和「换了没重启微信」。
        try:
            _hres2 = hook_check.check(client=wcf)
            print(f"[bot] {hook_check.format_report(_hres2, 'hook 自检（运行时）')}")
        except Exception:
            traceback.print_exc()
        # 连上之后先静置一会儿再开始查库（见 _ramp_sleep）。为什么放在**这里**：
        # hook 的闸门是在「微信确实写了库」之后才放行的，所以 connect_aixed 一返回，
        # 就说明微信刚完成登录、正在把 MSG*.db 逐个打开 —— 正是最脆的那几十秒。
        # 配 0 或不配 `poll_ramp` 就不等（行为与旧版一致）。
        _ramp_cfg = cfg.get("poll_ramp") or {}
        if _ramp_cfg.get("enabled", True):
            try:
                _delay = max(0.0, float(_ramp_cfg.get("startup_delay", 60)))
            except (TypeError, ValueError):
                _delay = 60.0
            if _delay > 0:
                print(f"[bot] 刚连上微信：先静置 {_delay:.0f} 秒再开始查库"
                      f"（避开微信刚登录时的建库窗口，见 poll_ramp 配置）")
                time.sleep(_delay)
    else:
        wcf = connect_wcferry()
        if wcf is None:
            print("[bot] 无法连接微信。请确认微信已登录且版本与 wcferry 匹配。")
            sys.exit(1)

    live_ok = hasattr(wcf, "query_sql") or hasattr(wcf, "exec_db_query")

    # 微信 4.x 判断「哪条是我发的」需要自己的 wxid。
    # 四级来源 + **核实**：config.yaml → hook 接口 → 本机账号目录 → contact 表（要与账号目录对得上）。
    # 为什么必须核最后一级（2026-10-06 第二次换台电脑真机）：contact 表那条判据是
    # 「第一个 wxid_ 开头的行」——**行序经验，不是证明**。那台机器上它认出了别人，
    # 于是 `is_self` 恒为 0，bot 把「自己刚发出去的回复」当成对方的新消息一遍遍自己答自己
    # （用户看到的就是「重复回复」），全程不报错。解析的唯一所有者是
    # `aixed_api.resolve_self_wxid()`（`verify_real.py` 用的是同一个函数，两处不许各写一份）。
    self_wxid, self_wxid_from, _self_used, self_wxid_note = resolve_self_wxid(
        cfg, wcf, backend)
    set_self_wxid(self_wxid)
    # fts 分片探测为空时自动重扫 hook 的间隔；0 = 关闭（session.db 兜底仍在）
    set_rescan_interval((cfg.get("agent") or {}).get("fts_rescan_interval", 300))
    if self_wxid:
        print(f"[bot] 自己的 wxid = {self_wxid}（来源：{self_wxid_from}）")
        if self_wxid_note:
            print(f"      ⚠️ {self_wxid_note}")
        if "contact 表" in self_wxid_from:
            print("      建议写进 config.yaml 的 self_wxid，免得每次靠认："
                  ".venv\\Scripts\\python.exe find_self_wxid.py --apply")
    else:
        print("[bot] ⚠️ 拿不到自己的 wxid —— 历史里分不清『我』和『对方』。")
        if self_wxid_note:
            print(f"      {self_wxid_note}")
        _cands = account_dir_wxids()
        if _cands:
            print("      本机微信账号目录里有：%s" % "、".join(_cands))
        print("      修法：在项目目录跑 .venv\\Scripts\\python.exe find_self_wxid.py --apply"
              "（只改 config.yaml 的 self_wxid 那一行、先备份），然后重启助手。")
        print("      认出来之前：我认不出「我自己发的消息」，所以只会靠"
              "「这句话我刚发过」兜住，**不会去回答自己刚发出的回复**。")

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

    def system_now():
        """每轮现算的系统提示（末尾带真实时间）。

        为什么是函数：`system` 只在启动和 reload_cfg 时更新，而 bot 会连跑好几天
        ——把那行时间缓存进 `system` 就等于给它一个会过期的假时间。

        插件/契约注册的工具**自带的 `guidance`** 在这里拼进去 —— 拼在**时间之前**：
        时间必须留在末尾（既有回归钉着这一点），而指导是静态的、放前面即可。
        `inject_guidance` 在没有自带指导时**逐字返回原文**，所以没有插件时
        这段提示和以前完全一样。
        """
        return with_now(plugins.REGISTRY.inject_guidance(system))

    # 自动回复：代替我本人回这些会话
    auto_on = auto_reply.enabled(cfg)
    auto_recs = auto_reply.chats(cfg)
    # 盯着：只通知我，不回对方
    watch_on = watch.enabled(cfg)
    watch_recs = watch.chats(cfg)
    # 撤回原文回显：把最近见过的消息留在内存里，收到「撤回」系统提示时回显原文。
    # 为什么不靠 hook 的防撤回字节补丁 —— 见 recall.py 的文件头注释。
    recall_on = recall.enabled(cfg)
    recall_ring = recall.Ring(recall.buffer_seconds(cfg), recall.buffer_max(cfg))
    # 审核模式把草稿往哪儿发：第一个控制会话（默认文件传输助手）
    control_chat = (list(cfg.get("target_chats") or []) or ["filehelper"])[0]
    # 待确认项总是登记在**控制会话**上（工具层的 self.chat / 审核草稿都发这儿）。
    # 控制会话可能不在 target_chats 里（例如一个都没配、默认文件传输助手），
    # 所以落盘/恢复的会话集合要把两边并起来，否则那条队列永远存不下来。
    pending_chats = sorted(set(targets) | {control_chat})

    # 「这句话是我刚发出去的」那张表也要捡回来：在控制会话（文件传输助手）里它是**唯一**
    # 能区分「自己的回显」和「用户新提问」的判据（`from_self` 两边都是真），而进程一重启
    # 内存就空 —— 上一轮刚发的问候会被当成新消息再答一遍。理由见
    # `docs/restart-catchup-notes.md`。
    try:
        n_sent = load_sent_memory()
        if n_sent:
            print(f"[bot] 认下 {n_sent} 条「我最近发过的话」（重启后不再自己答自己）")
    except Exception:
        traceback.print_exc()

    # 把盘上的待确认队列捡回来：不然重启之后用户照着刚才看到的提示回「确认」，
    # 什么都不会发生（以前就是这样，白等一场）。
    try:
        n_back = restore_pending(pending_chats, cfg)
        if n_back:
            print(f"[bot] 从盘上恢复了 {n_back} 条待确认动作（还在时效内）")
            try:
                items = []
                ttl_back = agent_tools.confirm_ttl_of(cfg)
                for c in pending_chats:
                    items += agent_tools.list_pending(c, ttl_back)
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

    def plug_ctx(chat, from_self=None, query=""):
        """插件用的只读上下文（形状的唯一所有者是 `plugins.make_ctx`）。

        ⚠️ **事实由有事实的那一层传进来**：`from_self` 是「这条消息是不是我自己
        发的」，只有主循环手里有。**绝不在这里猜** —— 「不知道」（`None`）和
        「是我自己发的」（`True`）是两回事，权限类判断（文件能力的 `files.who`）
        靠它放行，把未知当 True 就是静默放宽权限。
        """
        return plugins.make_ctx(chat=chat, self_wxid=self_wxid, cfg=cfg,
                                from_self=from_self, user_query=query)

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
            # 发之前先截断（原因见 _SEND_MAX_CHARS 的注释：微信会**直接拒收**过长的
            # 消息，而 hook 对这种拒收照回 ret:0 —— 只能自己先挡。）
            _t = str(text or "")
            if len(_t) > _SEND_MAX_CHARS:
                text = (_t[:_SEND_MAX_CHARS] +
                        f"\n\n…（这条回复一共 {len(_t)} 字，一条微信消息装不下，"
                        f"上面只发了前 {_SEND_MAX_CHARS} 字）")
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
        # `after_reply`：**一条消息真的发出去之后**才触发（只观察，不能改内容）。
        #
        # 为什么它比 `before_reply` 覆盖得宽（凡出站都算，包括确认菜单和群发预览）：
        # 观察类事件没有「改坏原样直发」的风险，而且一个记录出站消息的插件
        # 本来就该看到全部。`before_reply` 能改文本，所以只许挂在**模型答复**那两处
        # —— 菜单/预览是用户用来回「确认」的依据，改写它等于把确认闸做废。
        plugins.REGISTRY.emit("after_reply", text, plug_ctx(to), cfg=cfg)
        return True

    print(f"[bot] 后端：{backend}  |  监听中，控制会话：{targets}  |  只回目标：{reply_only}")
    if auto_recs:
        who = "、".join(f"{r.get('name') or w}({w})" for w, r in auto_recs.items())
        print(f"[bot] 自动回复：{'开启' if auto_on else '关闭'}  |  {who}")
    else:
        print("[bot] 自动回复：未配置（在微信里发 /auto add <昵称> 添加）")
    print("[bot] 在微信里发 /help 查看可用的配置命令。Ctrl+C 退出。")
    print(f"[bot] {scheduler.summary_line(cfg)}  |  {watch.summary_line(cfg)}"
          f"  |  {recall.summary_line(cfg)}")

    # 插件（`docs/plugin-contract-spec.md`）：扫 `plugins/` 目录。
    # **必须在进轮询循环之前** —— 插件可能在 `setup` 里注册工具/事件，
    # 循环一开就晚了。而且**加载失败绝不拦住启动**：一个写坏的插件不该让整台
    # 助手起不来（同 `health` / `status_page` / 坏掉的 `state.json` 那条规矩）。
    # `load_dir` 自己逐条 catch + 整份回滚；这里的 try 是**第三条保险**：
    # 万一加载器本身有 bug，也绝不能把助手拦在启动阶段。
    try:
        plugins.load_dir(cfg=cfg, log=print)
    except Exception:
        traceback.print_exc()
    # `on_start`：加载完就通知（插件可能在 setup 里注册了工具/事件，这里告诉它
    # 「环境就绪了」，比如去连 MCP server 拉 tools/list）。异常由 emit 兜住。
    plugins.REGISTRY.emit("on_start", cfg, cfg=cfg)

    # 文件能力开机自报（`docs/computer-files-spec.md` 第三节）。默认值是**宽**的
    # （用户口径：默认全盘、读写免确认），所以必须让用户看见宽在哪 ——
    # 本项目的既有规矩是「边界可以宽，但用户必须知道它宽在哪」，**静默地宽最坏**。
    try:
        for _note in files.startup_notes(cfg):
            print(f"[files] {_note}")
    except Exception:
        traceback.print_exc()

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
        nonlocal recall_on, recall_ring
        cfg = settings.effective(base_cfg)
        llm = make_llm(cfg)
        targets = set(cfg.get("target_chats", []))
        system = cfg.get("system_prompt", "")
        auto_on = auto_reply.enabled(cfg)
        auto_recs = auto_reply.chats(cfg)
        watch_on = watch.enabled(cfg)
        watch_recs = watch.chats(cfg)
        # 撤回回显：**就地改**缓冲配置，不重建 Ring —— 重建会把刚攒的原文丢光，
        # 于是「改完配置之后那几条撤回」捞不到原文（只在改配置后出现，最难查）。
        recall_on = recall.enabled(cfg)
        recall_ring.configure(recall.buffer_seconds(cfg), recall.buffer_max(cfg))
        control_chat = (list(cfg.get("target_chats") or []) or ["filehelper"])[0]

    _tick_no = [0]

    def run_scheduled():
        """跑一遍到点的定时任务。

        **只在收消息那条线程上调用**（见 _Ticker）：定时任务里要发消息、
        还可能跑一整轮 agent，而 hook 不支持并发——另起线程会直接把微信搞崩。
        传入的是内存里的 cfg（不是重新读盘）：scheduler 会就地更新任务的
        next_ts，这样同一个任务不会在下一 tick 又触发一遍。命令改过配置后
        主循环会 reload_cfg()，新任务自然生效。
        """
        # `on_tick`：轮询空档（定时任务本来就在这个空档里跑，节流 ≥1 秒）。
        # **必须在同一线程**（规格第三节），所以挂在这里而不是另起定时器。
        _tick_no[0] += 1
        plugins.REGISTRY.emit("on_tick", _tick_no[0], cfg=cfg)

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
                    llm, system_now(), prompt, wcf, contacts, cfg, control_chat, self_wxid,
                    cfg_provider=lambda: settings.effective(base_cfg),
                    history=history, state=run_state, user_query=query,
                    # 定时任务是**用户自己**在控制会话里建的（`/定时`），那句话是
                    # 用户的指令、不是别人发来的消息 → 算「我自己发的」。
                    # 不给这个事实的话，控制类能力（文件）会把定时任务判成
                    # 「不是我的消息」而一律拒绝。
                    from_self=True)
                # 定时的「提问」走的也是这条路：模型说「已提交命令等你确认」而
                # 本轮其实没登记时，同样要追一句真话（否则用户回「确认」白等）。
                answer = with_shell_truth_note(answer, run_state.get("shell_queued", False))
                # 同上：声称改了自动回复但本轮没改配置 → 追一句真话。
                answer = with_auto_reply_truth_note(answer, changed)
                answer = with_image_notes(answer, run_state.get("image_notes"))
                # 群发预览**原样**带上（模型转述十条例文必走样）。
                answer = with_broadcast_preview(
                    answer, run_state.get("broadcast_preview", ""))
            else:
                answer = llm.chat(system_now(),
                                  history + [{"role": "user", "content": prompt}])
                changed = False
            # `before_reply`：定时的「提问」也是一次**模型答复**，同样要过一遍。
            # 挂在「模型答复」这一层而不是挂在 send()，理由见主循环那处
            # —— 确认菜单和群发预览必须原样直发。
            #
            # `from_self=True`：定时任务是**用户自己**在控制会话里建的（`/定时`），
            # 那句话是用户的指令、不是别人发来的消息，所以它算「我自己发的」。
            # 不给这个事实的话，控制类能力（文件）会把定时任务判成「不是我的消息」而拒绝。
            answer = plugins.REGISTRY.before_reply(
                answer, plug_ctx(control_chat, from_self=True, query=question), cfg=cfg)
            dialog_append(control_chat, "user", question, cfg)
            dialog_append(control_chat, "assistant", answer, cfg)
            if changed:
                # 任务状态在 scheduler 里是**先落盘再执行**的，所以这里重建 cfg
                # 不会把刚写进去的 next_ts 冲掉。
                reload_cfg()
            return answer

        def call_task(wxid, name):
            """定时打电话（`scheduler.run_due` 的 call 回调）。

            **定时任务不能绕过用户设的闸**：到点了也照样先判能力闸 / 免打扰 /
            每天上限——否则用户设的「23:00-07:00 别打」会被一个定时任务绕过去，
            而那正是他最不想要电话的时候。判不过就**返回失败原因**，
            `run_due` 会如实报给控制会话，**绝不降级成发文本**（那是在骗人）。

            返回 None = 成功；返回字符串 = 失败原因（scheduler 的约定）。
            """
            ok, why = callgate.check(cfg)
            if not ok:
                return why
            fn = getattr(wcf, "call_voip", None)
            if fn is None:
                return ("这套 hook 没有 `/CallVoip` 端点（通话能力只在探针版里），"
                        "所以打不出去。")
            try:
                res = fn(wxid, self_wxid)
            except Exception as e:
                return f"拨号请求发不出去：{e}"
            if isinstance(res, dict) and res.get("error"):
                return f"hook 拒绝了这次拨号：{res['error']}"
            # **真发出去了才记账**（提前记会把额度白吃掉）。
            callgate.record(wxid, name)
            return None

        fired = scheduler.run_due(
            cfg, datetime.now(),
            send_text=lambda to, text: send(text, to),
            notify=lambda text: send(text, control_chat),
            ask=ask_task,
            call=call_task,
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
                    full = str(r.get("text") or "（读出来是空的）")
                    body = full
                    if len(full) > _READ_BODY_MAX:
                        # ⚠️ **绝不能把几万字原文倒进微信**（2026-10-03 真机踩过：
                        # 一份 A.zip 读完 27074 字整段发出去，聊天被刷好几屏，
                        # 而且同样内容被重复发了 4 次）。裁了要**明说裁了**，
                        # 并把「全文在哪」告诉用户 ——
                        # 和 `executor.WECHAT_MAX_CHARS` 是同一个「微信消息体量闸」思路。
                        m = re.search(r"全文也已导出到本机：(\S+)", full)
                        where = f"；**完整内容已导出到** `{m.group(1)}`" if m else ""
                        body = (full[:_READ_BODY_MAX] +
                                f"\n\n…（这份一共 {len(full)} 字，上面只发了前 "
                                f"{_READ_BODY_MAX} 字{where}。要看后面就说「继续」，"
                                f"或者说清要看哪一段）")
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

                # 看护攒下的主动汇报（游标停滞 / 自愈 / 恢复）——在这里发出去。
                # 必须放在「非文本就 continue」**之前**：否则一条图片/表情消息
                # 就会把汇报卡在队列里，用户还是看不到「它没反应」的原因。
                for _notice in drain_notices():
                    try:
                        send(_notice, control_chat)
                    except Exception:
                        traceback.print_exc()

                if getattr(msg, "type", 0) != 1:  # 1 = 文本
                    continue

                sender = msg.roomid or msg.sender
                # `on_message`：**尚未路由**时触发（只观察）。
                # 放在这里而不是路由之后，是为了让插件看到「这条消息到了」，
                # 而不是「这条消息被采纳了」—— 采纳与否是**路由**的判断，
                # 插件不许插手（规格第三节：路由只能有一个所有者）。
                plugins.REGISTRY.emit(
                    "on_message",
                    plug_ctx(sender, from_self=msg.from_self(),
                             query=(msg.content or "")), cfg=cfg)
                in_targets = sender in targets
                rec = auto_recs.get(sender) if auto_on else None
                watched = watch_recs.get(sender) if watch_on else None

                # 关键词监听：它盯的是**内容**，不是某个会话，所以必须在下面那条
                # 「不是我该管的会话就跳过」**之前**判断——否则用户加了关键词也永远不触发。
                # 只对**文本**消息生效（上面已经把非文本 continue 掉了），且跳过控制会话
                # （不然自己发一句 `/预算 20` 都会被自己的关键词命中，纯噪音）。
                if watch_on and sender != control_chat:
                    try:
                        _kws = watch.keywords(cfg)
                        _hits = watch.match_keywords(msg.content or "", _kws)
                    except Exception:
                        traceback.print_exc()
                        _hits = []
                    for _kw in _hits:
                        try:
                            _who = _msg_speaker(msg, auto_reply.contact_names(contacts))
                        except Exception:
                            _who = sender
                        try:
                            send(watch.format_keyword_hit(_kw, _who, msg.content or ()),
                                 control_chat)
                            print(f"[bot] 关键词命中 {_kw.get('raw')!r} ← {_who}")
                        except Exception:
                            traceback.print_exc()

                if not in_targets and rec is None and watched is None and reply_only:
                    # 有意丢掉非目标会话的消息 —— 但**拿不到会话名**的那种必须留痕，
                    # 否则「映射抖动 → 控制会话被认成 session_N → 消息被丢」会一声不吭
                    # （2026-10-05 真机就是这么变成「我发消息它不理」的）。
                    _note_offtarget_skip(sender)
                    continue

                query = (msg.content or "").strip()
                if not query:
                    continue

                # ⚠️ 「只有一个类型标签」的检查**必须排在语音处理之后**（2026-10-03 真机踩到）：
                # 语音条的渲染形态就是「一个标签 + 一个时长」，而**标签文字是跟着微信界面
                # 语言走的** —— 中文界面是 `[语音条（…）]`，英文界面是**字面的 `[Audio] 8"`**
                # （`SessionTable.summary` 原样拼进来）。`_LABEL_ONLY_RE` 只认「1~10 字的标签
                # + 可选时长」，于是 `[Audio] 8"` **命中**，语音在走到下面那段转写之前
                # 就被 `continue` 掉了 —— 用户看到的是「发了语音，bot 完全没反应」，
                # 而且**连一句失败提示都没有**（静默丢弃，正是本文件最忌讳的那种失效）。
                # 所以判据不能只看渲染出来的文字（会随界面语言变），要让**结构性的
                # `local_type == 34`** 先说话。下面语音那段处理完会自己 `continue`。

                # 「重启补齐」判定：**早于本进程启动**的消息 = 停机期间/上一台电脑上产生的
                # 那批，只通知、不自动回复。理由与两个边界都在 `is_catchup` 的 docstring 里
                # （2026-10-06 改：以前那条「比 120 秒还旧」的宽限正是重复回复的成因）。
                try:
                    _msg_ts = float(getattr(msg, "create_time", 0) or 0)
                except (TypeError, ValueError):
                    _msg_ts = 0.0
                catchup = is_catchup(_msg_ts, _START_TS, time.time())

                # 图片消息：**自己刚发出去的那张会作为「我发的新消息」回显回来**
                # （图片不在 fts 里，是靠 live_history 的非文本补漏捞回来的，见那边
                # 的 docstring），不能当成新消息再答一遍。文本有 is_own_reply 兜着，
                # 图片没有，所以这里用「会话 + 时间窗」认（agent_tools 那组簿记）。
                if getattr(msg, "local_type", 1) != 1:
                    if agent_tools.is_own_image(sender, _msg_ts):
                        print(f"[bot] 跳过（这是自己刚发出的图片）: {sender}")
                        continue
                    # 文件同理，而且这一条是 2026-10-03 真机踩出来的：把文件发出去之后，
                    # 那条文件消息会被当成「用户让我发这个文件」再处理一轮 → 又登记一条
                    # 待确认 → 用户每回一次「确认」就多收一条（「为什么会重复发」）。
                    # 判据还是「会话 + 时间窗」，见 agent_tools.is_own_file。
                    if agent_tools.is_own_file(sender, _msg_ts):
                        print(f"[bot] 跳过（这是自己刚发出的文件）: {sender}")
                        continue

                # 「这句是我刚发出去的」——**与「我是谁」无关**的一道闸（2026-10-06 换台电脑真机）。
                #
                # 为什么必须与身份分开：下面那条 `if msg.from_self()` 靠 `is_self`，而 `is_self`
                # 要靠 self_wxid 在消息 id 空间里对得上。换台电脑一旦对不上（contact 表那条
                # 经验判据认错人，见 `aixed_api.resolve_self_wxid`），`is_self` 会**恒为 0**：
                # 「我刚发出的回复」回显回来时，长得跟对方发来的新消息一模一样 —— 而
                # `is_own_reply()` 那道闸**嵌在 from_self 分支里面**，压根不会被问到。
                # 用户看到的就是「它自己答自己、一遍又一遍」（真机上就是这么重复回复的），
                # 而且不报错。所以这里补一道**不依赖身份**的（判据本体在
                # `looks_like_own_echo_without_identity`，那儿有完整理由），
                # 只在**确认认不出自己**时兜底，正常机器一个字都不变。
                # 图片/文件的同类闸门在更上面（`is_own_image` / `is_own_file`，会话+时间窗）。
                if looks_like_own_echo_without_identity(msg.from_self(),
                                                        live_history.self_identity_ok(),
                                                        query):
                    print(f"[bot] 跳过（这台机器认不出自己的 wxid，而这句话是我们刚发出去的）: "
                          f"{query[:30]}")
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

                # 语音条：**自动转成文字**，转出来就当作用户说的那句话。
                # 为什么在这里同步做、而不是丢给 read_worker：转写出来的文字必须
                # 成为**这一轮的 query** —— 后面的命令解析、待确认队列、agent 全都按
                # 「用户说了这句话」处理；丢到后台再回注，等于把那一整套逻辑复制一遍
                # （并可能走样）。代价是同步 5~8 秒，见 read_voice_message 的注释。
                if not catchup and (getattr(msg, "local_type", 1) & 0xFFFFFFFF) == 34:
                    try:
                        _vtxt, _vwhy = read_voice_message(wcf, cfg, msg)
                    except Exception:
                        traceback.print_exc()
                        _vtxt, _vwhy = "", "语音识别时出错（见 bot.log）"
                    if _vtxt:
                        query = _vtxt
                        print(f"[bot] 语音 → 文字（{_vwhy or '转写'}）：{_vtxt[:40]}")
                    elif _vwhy:
                        print(f"[bot] 语音没读出来：{_vwhy}")
                        send(f"🎤 收到一条语音，但没能读出来：{_vwhy}", control_chat)
                        continue

                # 「只有一个类型标签」的消息（空 `[系统消息]`、`[表情]`…）**不进模型**：
                # 标签**不是用户说的话**，拿它去问模型 = 对着空气回话。真机踩过
                # （2026-10-03）：filehelper 连着两条空 `[系统消息]`，bot 每条都郑重
                # 回一段解释，用户看到的就是「我没说话它却回了好几条」。
                # ⚠️ 位置：**必须在上面语音那段之后**（理由见上面 `query = …` 处的长注释）——
                # 语音的标签形态（尤其英文界面的 `[Audio] 8"`）本身就命中这个判据。
                if is_label_only(query):
                    print(f"[bot] 跳过（只有类型标签、没有内容）: {query[:30]}")
                    continue

                # 撤回原文回显（见 recall.py）。**必须排在「盯着」之前**：
                # 否则同一条撤回提示会先生成一条「👀 xx：[系统消息]」的噪音通知。
                # 判据是**结构**（local_type=10000 系统消息）**加**文本里带「撤回」，
                # 不是只看文本 —— 否则你正常说一句「他刚撤回了什么」就会被当成
                # 系统提示，然后我们拿一条不相干的原文回显，那是编。
                if recall_on and not catchup:
                    _lt = getattr(msg, "local_type", 1)
                    if recall.is_recall(_lt, query):
                        try:
                            _who = _msg_speaker(msg, auto_reply.contact_names(contacts))
                        except Exception:
                            _who = sender
                        _orig = recall_ring.find(sender, _msg_ts)
                        send(recall.format_echo(_who, _orig[1] if _orig else ""),
                             control_chat)
                        print(f"[bot] 撤回回显 ← {_who}"
                              f"{'（有原文）' if _orig else '（没留住原文）'}")
                        continue
                    if (_lt & 0xFFFFFFFF) == 10000 and recall.note_system(query):
                        # 没被当成撤回的**系统消息**：同一种形状只打一次日志。
                        # 判据的第二道（文本里带「撤回」）还没被真机样本验证过，
                        # 把没见过的形状打出来，「功能没反应」时才查得下去。
                        print(f"[bot] 系统消息（未按撤回处理）: {query[:60]!r}")
                    recall_ring.add(sender, query, _msg_ts, local_type=_lt)

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

                # 启动之前产生的消息（= 重启补齐那批）：除了上面「盯着」的通知，**一律不处理**。
                # 自动回复尤其不能补——那是在替用户本人说话；而且换台电脑时同步进来的历史里
                # 本来就有助手自己以前说过的话，补了就是「把所有问题又回一遍」。
                if catchup:
                    _catchup_total += 1
                    if not _catchup_announced:
                        _catchup_announced = True
                        send("⚠️ 重启补齐：启动之前还有消息没处理（停机期间、或上一台电脑上"
                             "产生的）。这些**只通知、不自动回复**——它们不是「现在该处理的"
                             "输入」；要我处理哪条，重发一遍或直接说。",
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
                    reply, changed = handle_command(query, wcf, cfg, live_ok, contacts,
                                                    chat=sender)
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
                    # 「确认」的有效期：和判重窗口、save/restore 用**同一个解析**
                    # （agent_tools.confirm_ttl_of），几处不一致会让「这条还在不在」
                    # 在不同地方给出不同答案。
                    ttl = agent_tools.confirm_ttl_of(cfg)
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
                    if item and item.get("kind") == "fileop":
                        # 文件操作（`computer_files`，见 docs/computer-files-spec.md）：
                        # 用户回「确认」才真做。
                        #
                        # **它必须有自己的分支**：这个待确认项**没有收件人**，
                        # 落到下面那条通用的发送分支就会报「已发送给 」
                        # 这种胡说八道。
                        #
                        # 返回的第二个值是「要说给用户听的一句话」：删除/覆盖这类
                        # 改文件的动作成功时是 None，失败时是原因，读类动作是内容。
                        # 所以这里一律**有话就说、没话就报完成**。
                        n, msg = agent_tools.send_pending(wcf, item, 0.0, cfg=cfg)
                        desc = agent_tools.describe_pending(item)
                        if msg:
                            print(f"[bot] 文件操作（{n}）：{msg}")
                            send(f"{msg}", sender)
                        else:
                            print(f"[bot] 文件操作完成：{desc}")
                            send(f"已完成：{desc}", sender)
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
                    if item and item.get("kind") == "call":
                        # 打电话：**确认时再判一次闸**。登记时判过一次，但从登记到
                        # 用户回「确认」之间隔着时间——配置可能被改回去了，也可能
                        # 正好跨过了免打扰边界（23:59 登记、00:01 确认）。
                        ok, why = callgate.check(cfg)
                        if not ok:
                            send(f"这通电话**没有拨**：{why}", sender)
                            continue
                        fn = getattr(wcf, "call_voip", None)
                        if fn is None:
                            send("这套 hook 没有 `/CallVoip` 端点"
                                 "（通话能力目前只在探针版 DLL 里），**没有拨出去**。",
                                 sender)
                            continue
                        send(f"好的，正在给 {item.get('to_name')} 拨过去…", sender)
                        try:
                            res = fn(item.get("to_wxid"), self_wxid)
                        except Exception as e:
                            # ⚠️ 发送失败**绝不自动重试**：第一次可能已经拨通了，
                            # 重试会让对方接到两通电话（和 bot.send 同一条铁律）。
                            send(f"拨号请求发不出去：{e}\n"
                                 f"**不确定到底拨没拨出去**，这里不重试"
                                 f"（重试可能让对方接到两通）。", sender)
                            remember_executed(item, cfg)
                            continue
                        if isinstance(res, dict) and res.get("error"):
                            send(f"拨号被 hook 拒绝了：{res['error']}\n"
                                 f"**没有拨出去**。", sender)
                            remember_executed(item, cfg)
                            continue
                        callgate.record(item.get("to_wxid"), item.get("to_name"))
                        # 只说「邀请已发出」——本地无法确认对方接没接（hook 成功
                        # 也回不了「对方收到了」，和发图那条同一个道理）。
                        send(f"通话邀请已经发给 {item.get('to_name')}。"
                             f"（我只能确认**邀请发出去了**，接没接本地看不到。）",
                             sender)
                        remember_executed(item, cfg)
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
                                                          allowed_dirs=dirs_now,
                                                          cfg=cfg)
                        is_text = (not item.get("image") and not item.get("xml")
                                   and not item.get("file"))
                        if is_text:
                            # 记一下，免得发给自己时又被当成新消息回一遍
                            remember_sent(item["text"])
                        what = ("转发" if item.get("xml")
                                else "文件" if item.get("file") else "图片")
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

                # 消费闸（/预算）：到上限就**不调模型**，并说清为什么。
                # 放在这里是因为**两条路都要过**——带工具的 agent 和纯 chat 都是一次
                # 真实的模型调用。算不准的情况（没价目表 / 账本读不出来）它自己会说，
                # 不会假装拦住了。
                if usage is not None:
                    blocked_text = usage.budget_block_text(cfg)
                    if blocked_text:
                        print(f"[bot] 消费闸拦住一次调用（{sender}）")
                        send(blocked_text, sender)
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
                            llm, system_now(), prompt, wcf, contacts, cfg, sender, self_wxid,
                            cfg_provider=lambda: settings.effective(base_cfg),
                            history=history, state=run_state,
                            # **事实在这儿**：这条消息是不是我自己发的，只有主循环
                            # 手里有。工具层的权限判断（`files.who_allows`）靠它。
                            from_self=msg.from_self())
                        # 确定性兜底：模型没调 run_command 却自己说「已提交/等你确认」时，
                        # 固定追一句真话。**别删**——真机上就是这么骗到用户的。
                        answer = with_shell_truth_note(
                            answer, run_state.get("shell_queued", False))
                        # 同一条规矩：模型说「自动回复已关闭/已开启」而本轮一个配置都
                        # 没改时，追一句真话（真机踩过：它说关了、其实还开着，继续回别人）。
                        answer = with_auto_reply_truth_note(answer, cfg_changed)
                        # 图片那边的如实说明（比如"这一轮已经给了 3 张，这张没给"）
                        answer = with_image_notes(answer, run_state.get("image_notes"))
                        # 群发预览**原样**带上（模型转述十条例文必走样）。
                        answer = with_broadcast_preview(
                            answer, run_state.get("broadcast_preview", ""))
                    else:
                        answer = llm.chat(system_now(),
                                          history + [{"role": "user", "content": prompt}])
                        cfg_changed = False
                    # `before_reply`：**只对模型答复**能改文本。
                    # 挂在 send() 里会连确认菜单、群发预览一起改 —— 而那是用户
                    # 用来回「确认」的依据（bot 原样直发），改写它等于把确认闸做废。
                    answer = plugins.REGISTRY.before_reply(
                        answer, plug_ctx(sender, from_self=msg.from_self(),
                                         query=query), cfg=cfg)
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
                except Exception as e:
                    traceback.print_exc()
                    try:
                        # 说清「这一轮为什么没答上来」+ 该怎么办；**绝不再说"看终端日志"**
                        # （无窗口后台任务，用户没有终端；见 error_reply_text 的说明）。
                        send(error_reply_text(e), sender)
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
