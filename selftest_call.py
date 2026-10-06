"""打电话（call 工具 / callgate / 定时 action=call）的回归。

**不碰微信、不联网、不碰真实账本**：`callgate.CALL_LOG_PATH` 全程指向临时文件，
仓库里的 `data/calls.jsonl` 一个字节都不动（和 selftest_redact_usage 对
`data/usage.jsonl` 的做法一致）。

覆盖这几条硬规矩：
  1. 能力闸**默认关**，且严格 `is True`（"true"/1/空 都当关）；
  2. 免打扰时段**跨零点判得对**；空串 = 不设；多段要能工作；
  3. 每天上限按**滑动 24 小时**算（不是自然日），坏账本行不许崩；
  4. 工具**只登记待确认**，绝不在这里拨；被闸挡下时**一条待确认都不留**；
  5. 工具返回的文本**绝不许出现"已经打了"这类假成功**；
  6. `aixed_api.call_voip` 发出去的 JSON 是 `{"wxid"}`（可选 `type`/`body`）；
     —— 2026-10-03 起契约变了：探针版要 `{wxid, self}`，实装版**不要 self**。
  7. 定时任务到点也**照样判闸**，判不过**如实报错、绝不降级成发文本**。
"""
import json
import os
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import aixed_api          # noqa: E402
import agent_tools        # noqa: E402
import callgate           # noqa: E402
import scheduler          # noqa: E402

_fails = []
_n = 0


def chk(cond, label, extra=None):
    global _n
    _n += 1
    if cond:
        print(f"  [ok] {label}")
    else:
        print(f"  [XX] {label}" + (f"  <-- {extra}" if extra is not None else ""))
        _fails.append(label)
    return bool(cond)


def sec(title):
    print(f"\n【{title}】")


_TMP = tempfile.mkdtemp(prefix="callgate_selftest_")
callgate.CALL_LOG_PATH = os.path.join(_TMP, "calls.jsonl")


def reset_ledger():
    if os.path.exists(callgate.CALL_LOG_PATH):
        os.remove(callgate.CALL_LOG_PATH)


sec("1. 能力闸：默认关，严格 is True")
chk(callgate.enabled({}) is False, "没配 -> 关")
chk(callgate.enabled({"agent": {}}) is False, "空 agent -> 关")
chk(callgate.enabled({"agent": {"call_voip": False}}) is False, "false -> 关")
chk(callgate.enabled({"agent": {"call_voip": "true"}}) is False,
    "字符串 \"true\" -> 仍然关（一个手误不能把电话打出去）")
chk(callgate.enabled({"agent": {"call_voip": 1}}) is False, "数字 1 -> 仍然关")
chk(callgate.enabled({"agent": {"call_voip": True}}) is True, "True -> 开")

ok, why = callgate.check({})
chk(ok is False and "没开启" in why, "关着时 check 直接拒绝，且说清是没开启", why)

sec("2. 免打扰：跨零点 / 空串 / 多段")
cfg_q = {"agent": {"call_voip": True, "call_quiet_hours": "23:00-07:00"}}
for hh, mm, want in ((23, 30, True), (0, 10, True), (6, 59, True),
                     (7, 0, False), (12, 0, False), (22, 59, False)):
    got, _win = callgate.in_quiet(cfg_q, datetime(2026, 10, 2, hh, mm))
    chk(got is want, f"{hh:02d}:{mm:02d} -> 免打扰={want}", got)
cfg_empty = {"agent": {"call_voip": True, "call_quiet_hours": ""}}
got, _ = callgate.in_quiet(cfg_empty, datetime(2026, 10, 2, 23, 30))
chk(got is False, "空串 = 不设免打扰（用户明确要）", got)
cfg_multi = {"agent": {"call_voip": True,
                       "call_quiet_hours": "12:00-14:00,23:00-07:00"}}
got, _ = callgate.in_quiet(cfg_multi, datetime(2026, 10, 2, 13, 0))
chk(got is True, "多段：13:00 命中", got)
got, _ = callgate.in_quiet(cfg_multi, datetime(2026, 10, 2, 15, 0))
chk(got is False, "多段：15:00 不在免打扰", got)
cfg_bad = {"agent": {"call_voip": True, "call_quiet_hours": "乱七八糟"}}
chk(callgate.quiet_windows(cfg_bad) == [],
    "时段写坏了 -> 那一段丢掉（不崩、也不整段失效）", callgate.quiet_windows(cfg_bad))

sec("3. 每天上限：滑动 24 小时")
reset_ledger()
cap_cfg = {"agent": {"call_voip": True, "call_quiet_hours": "",
                     "call_max_per_day": 2}}
now = datetime(2026, 10, 2, 12, 0)
chk(callgate.check(cap_cfg, now)[0] is True, "0 通 -> 可以打")
callgate.record("wxid_a", "A", now.timestamp())
chk(callgate.check(cap_cfg, now)[0] is True, "1 通 -> 还能打")
callgate.record("wxid_a", "A", now.timestamp())
ok, why = callgate.check(cap_cfg, now)
chk(ok is False and "上限" in why, "2 通 -> 到上限，拒绝并说明", why)
# 25 小时前的记录不算
reset_ledger()
callgate.record("wxid_a", "A", now.timestamp() - 25 * 3600)
chk(callgate.check(cap_cfg, now)[0] is True,
    "25 小时前那通不算进今天（滑动窗口，不是自然日）")
# 坏行不许崩
with open(callgate.CALL_LOG_PATH, "a", encoding="utf-8") as fh:
    fh.write("这不是 json\n")
    fh.write(json.dumps({"ts": "坏的"}) + "\n")
chk(isinstance(callgate.recent_count(now.timestamp()), int),
    "坏账本行不许崩", callgate.recent_count(now.timestamp()))
chk(callgate.cap({"agent": {"call_max_per_day": 0}}) == callgate.DEFAULT_CAP,
    "配成 0 -> 回退默认值（绝不回退成无上限）")
chk(callgate.cap({"agent": {"call_max_per_day": "abc"}}) == callgate.DEFAULT_CAP,
    "配成非数字 -> 回退默认值")

sec("4. call 工具：只登记、被挡不留痕")
reset_ledger()
CONTACTS = [{"wxid": "wxid_zhang", "name": "张三", "remark": "张三"},
            {"wxid": "wxid_li", "name": "李四", "remark": "李四"}]


class FakeClient:
    def __init__(self):
        self.sent = []

    def send_text(self, msg, wxid):
        self.sent.append((wxid, msg))

    def call_voip(self, wxid, self_wxid):
        self.calls = getattr(self, "calls", [])
        self.calls.append((wxid, self_wxid))
        return {"ret": 0}


def make_box(cfg):
    agent_tools._PENDING.clear()
    return agent_tools.ToolBox(FakeClient(), cfg, CONTACTS,
                               self_wxid="wxid_me", chat="chat1",
                               cfg_provider=lambda: cfg)


cfg_off = {"agent": {"call_voip": False, "call_quiet_hours": ""}}
box = make_box(cfg_off)
out = box.t_call({"to": "张三"})
chk("没开启" in out, "能力闸关：工具如实拒绝", out)
chk(agent_tools.list_pending("chat1") == [], "被挡下时一条待确认都不留（不白确认一次）")
chk("已经打" not in out and "已拨" not in out, "拒绝文案里没有假成功")

cfg_on = {"agent": {"call_voip": True, "call_quiet_hours": ""}}
box = make_box(cfg_on)
out = box.t_call({"to": "张三"})
items = agent_tools.list_pending("chat1")
chk(len(items) == 1 and items[0].get("kind") == "call",
    "开着时：登记一条 kind=call 的待确认", items)
chk(items and items[0].get("to_wxid") == "wxid_zhang", "待确认里是解析后的 wxid",
    items)
chk("确认" in out, "回复里明确要求用户回「确认」", out)
chk("已经打" not in out and "已拨" not in out,
    "登记这一步绝不说已经打了（还没拨！）", out)
chk(box.client.sent == [], "登记时一条消息都没发")

box = make_box({"agent": {"call_voip": True, "call_quiet_hours": "23:00-07:00"}})
# 现在多半不是免打扰时段 —— 这个断言只保证两条路都自洽
out2 = box.t_call({"to": "李四"})
chk(("确认" in out2) or ("没有打这通电话" in out2),
    "免打扰时段下要么登记、要么如实拒绝（不能沉默）", out2)

box = make_box(cfg_on)
out3 = box.t_call({"to": "查无此人"})
chk("没找到" in out3 or "重名" in out3, "对象解析不了时如实说", out3)

sec("4b. kind=call 的重启恢复（项目真踩过：字段掉了就发错东西）")
import bot  # noqa: E402  （放在这里，免得前面的用例受 bot 的副作用影响）
_state = os.path.join(_TMP, "state.json")
_old_path, _old_state = bot.STATE_PATH, bot._STATE
try:
    bot.STATE_PATH, bot._STATE = _state, None
    agent_tools._PENDING.pop("chat1", None)
    box = make_box(cfg_on)
    box.t_call({"to": "张三"})
    bot.save_pending(["chat1"], {"agent": {"confirm_ttl": 300}})
    agent_tools._PENDING.pop("chat1", None)          # 模拟重启：内存清空
    n = bot.restore_pending(["chat1"], {"agent": {"confirm_ttl": 300}})
    back = agent_tools.list_pending("chat1", 300)
    chk(n == 1 and back and back[0].get("kind") == "call",
        "重启后 kind=call 还在", back)
    chk(back and back[0].get("to_wxid") == "wxid_zhang",
        "恢复后**收件人 wxid 还在**（否则会拨给空 wxid）", back)
    chk(back and "张三" in str(back[0].get("to_name")),
        "恢复后显示名还在（用户要看得懂拨给谁）", back)
finally:
    bot.STATE_PATH, bot._STATE = _old_path, _old_state

sec("5. aixed_api.call_voip：请求形状是 {wxid}（+ 可选 type/body）")
c = aixed_api.AixedClient("http://127.0.0.1:1")
seen = {}


def fake_request(method, path, payload=None):
    seen["method"], seen["path"], seen["payload"] = method, path, payload
    return {"ret": 0}


c._request = fake_request
# 契约在 2026-10-03 变了：探针版要 {wxid, self}，而**仓库源码里实装的** /CallVoip
# **不再需要 self** —— 发送方由 WeChat 自己填，走的和发文本同一条
# send_message 路径（见 src/wx_send.cpp 的 SendVoipInvite）。
c.call_voip("wxid_zhang", "wxid_me")          # self 仍然可以传，但不进请求
chk(seen["method"] == "POST" and seen["path"] == "/CallVoip",
    "走的是 POST /CallVoip", seen)
chk(seen["payload"] == {"wxid": "wxid_zhang"},
    "载荷是 {wxid}（self 不再需要；传了也不发出去）", seen["payload"])

# 试验用的两个可选参数必须**真的进请求** —— 否则"不用重编 DLL 就能试不同
# type / 不同正文拼法"这条设计就是假的（而每重编一次都要重启微信 + 重新扫码）。
c.call_voip("wxid_zhang", msg_type=1, body="hello")
chk(seen["payload"] == {"wxid": "wxid_zhang", "type": 1, "body": "hello"},
    "msg_type / body 会进请求（真机试验靠它）", seen["payload"])

sec("6. 定时任务 action=call：照样判闸，绝不降级成发文本")
reset_ledger()
sent_text = []
notified = []
fake_calls = []


def call_ok(wxid, name):
    fake_calls.append((wxid, name))
    return None


def sched_cfg():
    """每次都给一份**全新**的配置。

    ⚠️ 不能复用同一份：`run_due` 触发后会把 `next_ts` 推到下一次（daily 就是明天），
    复用它第二次根本不会触发——那样测的是「没触发」，不是「失败时怎么报」。
    """
    return {
        "schedule": {"enabled": True, "tasks": [
            {"id": "t1", "action": "call", "to": "wxid_zhang", "to_name": "张三",
             "enabled": True, "next_ts": 1.0, "repeat": "daily", "at": "09:00"},
        ]},
    }


scheduler.run_due(sched_cfg(), datetime(2026, 10, 2, 9, 0),
                  send_text=lambda to, text: sent_text.append((to, text)),
                  notify=lambda text: notified.append(text),
                  call=call_ok)
chk(fake_calls == [("wxid_zhang", "张三")], "到点调了 call 回调", fake_calls)
chk(sent_text == [], "**没有降级成发文本**", sent_text)

sent_text.clear()
notified.clear()


def call_fail(wxid, name):
    return "现在是免打扰时段"


scheduler.run_due(sched_cfg(), datetime(2026, 10, 2, 9, 0),
                  send_text=lambda to, text: sent_text.append((to, text)),
                  notify=lambda text: notified.append(text),
                  call=call_fail)
chk(sent_text == [], "回调报错时也**一个字都没发**", sent_text)
chk(any("免打扰" in t for t in notified), "失败原因如实通知用户", notified)

sent_text.clear()
notified.clear()
scheduler.run_due(sched_cfg(), datetime(2026, 10, 2, 9, 0),
                  send_text=lambda to, text: sent_text.append((to, text)),
                  notify=lambda text: notified.append(text), call=None)
chk(sent_text == [], "没给 call 回调时也绝不发文本", sent_text)
chk(any("没打通" in t or "打不出去" in t for t in notified),
    "没给回调时如实报「打不出去」", notified)

print("\n" + "=" * 62)
if _fails:
    print(f"FAILED {len(_fails)}/{_n}: " + "；".join(_fails[:6]))
    sys.exit(1)
print(f"OK  {_n} 项全部通过")
sys.exit(0)
