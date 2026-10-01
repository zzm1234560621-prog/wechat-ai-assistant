"""scheduler / auto_reply 的回归自测（T1~T6）。

跑： .venv/Scripts/python.exe selftest_sched_auto.py

规矩（和 selftest_aixed.py 一致）：
  * **不联网、不碰 30001、不需要微信、不启动 bot.py**——全是纯逻辑 + 假客户端；
  * 用临时文件当 settings.json（只改 `settings.SETTINGS_PATH` 这个变量，
    **不动真的配置文件**），测完删掉；
  * 出错如实报错并 sys.exit(1)，不静默跳过。
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import auto_reply          # noqa: E402
import scheduler           # noqa: E402
import settings            # noqa: E402
import watch               # noqa: E402

_PASS = 0


def ok(msg):
    global _PASS
    _PASS += 1
    print(f"  ✅ {msg}")


def chk(cond, msg):
    if cond:
        ok(msg)
    else:
        print(f"  ❌ {msg}")
        raise SystemExit(1)


# ---------------- 假环境 ----------------

class TempSettings:
    """把 settings.SETTINGS_PATH 指到临时文件。

    **为什么不 mock settings.load / set_value**：T2 要验的正是「真落盘的 JSON
    会不会被旧列表盖掉」。mock 掉读写就等于只测了自己的 mock，测不出真问题。
    所以这里只用真实现 + 临时路径，真的序列化一次 JSON。
    """

    def __init__(self, initial):
        self.dir = tempfile.mkdtemp(prefix="sched_auto_selftest_")
        self.path = os.path.join(self.dir, "settings.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(initial, f, ensure_ascii=False, indent=2)

    def __enter__(self):
        self._real = settings.SETTINGS_PATH
        settings.SETTINGS_PATH = self.path
        return self

    def __exit__(self, *exc):
        settings.SETTINGS_PATH = self._real
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class FakeClient:
    """假客户端：**只**回答「联系人查询」，别的一律抛错。

    这是 aixed hook 的最小替身：不 socket、不 HTTP、不碰 30001。
    auto_reply 的 /auto add 要走 live_history.resolve_contact，所以「联系人表」
    这条路必须真答；别的库（消息/会话/fts）一律不许被碰——碰了就说明这条链
    在自测里做了多余的事，直接抛错暴露出来。
    """

    def __init__(self, contacts):
        self.contacts = contacts

    @staticmethod
    def _rows(contacts):
        """(username, nick_name, remark, alias) 四列，和 _v4_contact_rows 的取列一致。"""
        return [(c["wxid"], c.get("name"), c.get("remark"), c.get("alias"))
                for c in contacts]

    def query_sql(self, db, sql):
        low = sql.lower()
        if db == "contact.db" and "from contact" in low:
            # 自测只认 v4 schema（微信 4.1.x）；这是「探 schema」，一条就够
            if low.strip().startswith("select 1 "):
                return [(1,)]
            rows = self._rows(self.contacts)
            # 名字是参数化的 LIKE，这里做同样的「包含」匹配，好让重名/找不到的
            # 判定跟真机一致（重名时 resolve_contact 会返回多条，命令层要拦）
            import re as _re
            m = _re.search(r"nick_name like '%(.*?)%'", low)
            if m:
                key = m.group(1)
                rows = [r for r in rows if key in str(r[1] or "")]
            return rows
        raise AssertionError(f"自测不该查这个库：{db} / {sql}")


_CONTACTS = [
    {"wxid": "wxid_z", "name": "张三", "remark": "张三"},
    {"wxid": "wxid_l", "name": "李四", "remark": "李四"},
    {"wxid": "room_x", "name": "老同学群", "remark": "老同学群"},
]


def _resolve_one(who):
    hits = [c for c in _CONTACTS if who in (c["wxid"], c["name"], c["remark"])]
    if len(hits) == 1:
        return hits[0], None
    return None, f"没找到「{who}」。"


def _task(tid, **kw):
    base = {"id": tid, "action": "text", "to": "wxid_z", "to_name": "张三",
            "text": "内容", "repeat": "daily", "at": "09:00", "enabled": True,
            "next_ts": 0.0, "last_ts": None}
    base.update(kw)
    return base


# ---------------- T1：总开关关着时加任务 ----------------

def t1_add_keeps_master_switch():
    print("T1. 总开关关着时 add：保留关状态，且不动别的任务的 next_ts")
    # **now 必须取真实当前时间往前推**：run_due 用的是传进去的 now，而任务里
    # 的 next_ts 是 epoch 秒。写死一个未来的绝对时间（比如 2026-10-01）会让
    # 「过去」和「未来」的判定反过来，测出来的是假的绿灯。
    now = datetime.now().replace(microsecond=0)
    stale = now.timestamp() - 86400       # 昨天的触发点，暂停期间故意留着的
    frozen = {t["id"]: t.get("next_ts") for t in
              [_task("t1", next_ts=stale), _task("t2", at="23:00", next_ts=stale + 1)]}
    init = {"schedule": {"enabled": False, "tasks": [
        _task("t1", next_ts=stale),
        _task("t2", at="23:00", next_ts=stale + 1, enabled=False),
    ]}}
    with TempSettings(init) as tmp:
        cfg = settings.effective({"schedule": {"enabled": False, "tasks": [
            _task("t1", next_ts=stale),
            _task("t2", at="23:00", next_ts=stale + 1, enabled=False),
        ]}})
        out, changed = scheduler.handle_command("加 明天9:00 李四 记得带伞", cfg,
                                                _resolve_one)
        chk(changed and "已加定时任务" in out, "加任务本身成功")
        disk = tmp.read()["schedule"]
        chk(disk.get("enabled") is False, "落盘后总开关**仍是 false**（以前硬写 True）")
        chk("关着" in out and "不会触发" in out, f"文案和落盘一致（实际：{out.splitlines()[-1]}）")
        new = disk["tasks"][-1]
        tomorrow = (now + timedelta(days=1)).date().isoformat()
        chk(new["to"] == "wxid_l" and new["date"] == tomorrow,
            f"新任务字段正确（明天={tomorrow}）")
        after = {t["id"]: t.get("next_ts") for t in disk["tasks"]}
        chk(after["t1"] == frozen["t1"] and after["t2"] == frozen["t2"],
            "被暂停任务（含过期的 next_ts）**没被动过**，不会补跑")

        # 兜底：就算真轮到 run_due，总开关关着也一条都不许触发
        sent = []
        fired = scheduler.run_due(cfg, now, lambda to, tx: sent.append((to, tx)),
                                  notify=lambda s: None)
        chk(fired == [] and sent == [],
            f"总开关关着时 run_due 一条都不跑（实际 fired={fired} sent={sent}）")


# ---------------- T2：结算写盘不能复活/丢失动作里的增删 ----------------

def t2_merge_save_keeps_action_changes():
    print("T2. 一 tick 内动作增删任务后，结算写盘不复活已删、不丢失新增")
    now = datetime.now().replace(microsecond=0)
    past = now.timestamp() - 10
    t1 = _task("t1", next_ts=past)
    # t2 下次在**将来**（今天 23:00），所以本轮它不该触发，参数都留着——
    # 动作里把它删掉，然后看本轮结算会不会把它写回来（复活）。
    t2 = _task("t2", action="text", at="23:00", next_ts=now.timestamp() + 3600)
    init = {"schedule": {"enabled": True, "tasks": [t1, t2]}}
    with TempSettings(init) as tmp:
        cfg = settings.effective({"schedule": {"enabled": True, "tasks": [t1, t2]}})
        seen = {}

        def ask(_q):
            """模拟动作里「跑一整轮 agent」：agent 同时删掉 t2、新增 t9。

            这里直接调 scheduler.handle_command——和生产里 agent 工具走的是
            同一条实现；"新 cfg" 由 settings.effective 重新读盘拿到，
            对应 bot.py 里命令改完配置后的 reload_cfg()。
            """
            live = settings.effective({})
            scheduler.handle_command("删 t2", live, _resolve_one)
            live = settings.effective({})
            out, _ = scheduler.handle_command("加 每天23:30 李四 打卡", live, _resolve_one)
            chk("已加定时任务" in out, "动作里新增任务成功")
            seen["disk_mid"] = tmp.read()["schedule"]["tasks"]

        due = _task("t1", next_ts=past, action="ask", to="", to_name="",
                    text="整理谁还没回我")
        cfg["schedule"]["tasks"] = [due, t2]
        fired = scheduler.run_due(cfg, now, lambda to, tx: None,
                                  notify=lambda s: None, ask=ask)
        chk(fired == ["t1"], f"触发了 t1（实际 {fired}）")
        disk = tmp.read()["schedule"]["tasks"]
        ids = [t["id"] for t in disk]
        chk("t2" not in ids, f"动作里删掉的 t2 **没有复活**（实际 {ids}）")
        chk(sum(1 for i in ids if i != "t1") == 1,
            f"动作里新增的任务**没丢**（实际 {ids}）")
        new = [t for t in disk if t["id"] not in ("t1", "t2")][0]
        chk(new["text"] == "打卡", "新增任务的字段完整")
        chk(new["id"] != "t2",
            f"同一 tick 里删完再加，新任务**不复用刚删掉的编号**（实际 {new['id']}）")
        t1d = [t for t in disk if t["id"] == "t1"][0]
        chk(t1d["next_ts"] > now.timestamp() and t1d["last_ts"] == now.timestamp(),
            "本轮触发任务的 next_ts/last_ts 照样并回盘上（不会下个 tick 重发）")
        chk(cfg["schedule"]["tasks"][0]["next_ts"] > now.timestamp(),
            "内存里那份 cfg 也被就地更新（主循环不 reload 也不会重复触发）")


# ---------------- T3：一条坏 at 不许停摆整轮 ----------------

def t3_bad_at_only_skips_itself():
    print("T3. 一条 at 坏掉：只跳过它自己，其余照常，且 notify 收到告警（有节流）")
    now = datetime.now().replace(microsecond=0)
    past = now.timestamp() - 10
    good = _task("t1", next_ts=past)
    bad = _task("t2", at="25:00", next_ts=past)     # parse_when 不会让这种值进来，
    #                                                  手工编辑 settings.json 才会
    init = {"schedule": {"enabled": True, "tasks": [good, bad]}}
    with TempSettings(init) as tmp:
        cfg = settings.effective({"schedule": {"enabled": True,
                                               "tasks": [good, bad]}})
        sent, notes = [], []
        fired = scheduler.run_due(cfg, now, lambda to, tx: sent.append((to, tx)),
                                  notify=notes.append)
        chk(fired == ["t1"], f"好任务照常触发，坏任务不触发（实际 {fired}）")
        chk(sent == [("wxid_z", "内容")], "好任务真的发出去了")
        chk(any("t2" in n and ("数据有问题" in n or "时间数据" in n) for n in notes),
            f"坏任务通过 notify **如实告警**（实际 {notes}）")
        chk(len(notes) == 2, f"本轮共 2 条通知=1 告警 + 1 发送回执（实际 {len(notes)}）")
        chk(bad.get("next_ts") == past, "坏任务的 next_ts 没被改成 None（数据留在原地，修好即用）")

        print("   节流：同一 tick 再来几次不该每 tick 刷屏")
        for _ in range(3):
            fired2 = scheduler.run_due(cfg, now, lambda to, tx: None,
                                       notify=notes.append)
        chk(fired2 == [], "第二次 tick 好任务的 next_ts 已推到未来，不再触发")
        warn = [n for n in notes if "t2" in n]
        chk(len(warn) == 1, f"坏任务告警只发了 1 条，没每 tick 刷屏（实际 {len(warn)}）")

        print("   坏的是 next_ts（不是 at）时同样只跳过它自己")
        bad2 = _task("t3", next_ts="明天")
        cfg2 = settings.effective({})
        cfg2["schedule"]["tasks"] = [_task("t9", next_ts=now.timestamp() - 5), bad2]
        notes2 = []
        fired3 = scheduler.run_due(cfg2, now, lambda to, tx: None,
                                   notify=notes2.append)
        chk(fired3 == ["t9"] and any("t3" in n for n in notes2),
            f"next_ts 坏掉也只跳过它自己（fired={fired3}）")


# ---------------- T4：auto_reply 与 watch 双向互斥 ----------------

def t4_mutual_exclusion():
    print("T4. /auto add 把已在「盯着」名单里的人加进来会被拒")
    init = {"watch": {"enabled": True,
                      "chats": [{"wxid": "wxid_l", "name": "李四"}]},
            "auto_reply": {"enabled": True, "chats": []}}
    with TempSettings(init) as tmp:
        cfg = settings.effective({})
        client = FakeClient(_CONTACTS)
        out, changed = auto_reply.handle_command("add 李四", cfg, client)
        chk(not changed, "拒了，没改配置")
        chk("盯着" in out and "互斥" in out and "/盯着 删" in out,
            f"错误文案说清「二选一」并给了怎么解（实际：{out}）")
        chk(tmp.read()["auto_reply"]["chats"] == [], "名单没被写进去")

        # 反过来：不在 watch 名单里的人正常加
        out2, changed2 = auto_reply.handle_command("add 张三", cfg, client)
        chk(changed2 and "已加入自动回复" in out2, "不在盯着名单里的人照常能加")

        # watch 那一侧的老行为不能坏（这次没改 watch.py，顺手回归一下）
        cfg2 = settings.effective({})
        out3, changed3 = watch.handle_command("加 张三", cfg2, _resolve_one)
        chk(not changed3 and "自动回复" in out3,
            f"反向仍然拦得住（实际：{out3}）")


# ---------------- T5：群聊回复解析 ----------------

def t5_group_reply_parsing():
    print("T5. 群聊回复：只有真出现 reply 字段才按 JSON 解析")
    ex = auto_reply._extract_group_reply

    r1 = ex("价格是 {100} 元，回头聊")
    chk(r1 is not None and "100" in r1,
        f"「价格是 {{100}} 元，回头聊」不再静默（实际 {r1!r}）")
    chk(auto_reply.sanitize(r1) == "价格是 {100} 元，回头聊", "清洗后就是原话")

    r2 = ex('看这个 {"a":1} 的例子')
    chk(r2 is not None and "例子" in r2,
        f"「看这个 {{\"a\":1}} 的例子」不再静默（实际 {r2!r}）")

    chk(ex('{"reply": null}') is None, "reply 为 null = 明确静默（保持原设计）")
    chk(ex('{"reply": ""}') is None, "reply 空串 = 静默，不发空消息")
    chk(ex('{"reply":"没空格也认"}') == "没空格也认", "冒号后没空格的 JSON 也认")
    chk(ex('  {"reply": "带空白的"}  ') == "带空白的", "整段就是 JSON（前后空白）也认")
    chk(ex("{'reply': '好'}") == "好",
        "模型吐 Python 单引号字面量时照样取得出（不然这句话又被白吞）")
    chk(ex('{"other": 1}') is None,
        "模型在走 JSON 协议却没有 reply 字段 → 静默，绝不把原始 JSON 发给别人")
    chk(ex('{"reply": 1}') == "1", "reply 是数字也当文本发（不静默）")
    chk(ex('```json\n{"reply": "好的，我下午过去"}\n```') == "好的，我下午过去",
        "套了一层 ``` 围栏照样能剥出来")
    chk(ex("我才不接这个话") == "我才不接这个话", "没有花括号 → 整段当回复")
    chk(ex("") is None and ex(None) is None, "空输入 = 静默")
    chk(ex('{"reply": ') is None, "想做 JSON 却吐坏了 → 静默（fail-safe）")

    print("   端到端：make_reply 里这两个字符串真的能出话")
    class _LLM:
        def __init__(self, raw):
            self.raw = raw

        def chat(self, system, messages):
            return self.raw

    msgs = [{"content": "这个多少钱", "is_self": 0, "sender": "wxid_z",
             "time": "10-01 08:00"}]
    for raw in ("价格是 {100} 元，回头聊", '看这个 {"a":1} 的例子'):
        rep = auto_reply.make_reply(_LLM(raw), {"mode": "self"}, msgs, {}, {}, group=True)
        chk(rep, f"{raw!r} → 有回复（实际 {rep!r}）")
    rep_null = auto_reply.make_reply(_LLM('{"reply": null}'), {"mode": "self"},
                                     msgs, {}, {}, group=True)
    chk(rep_null == "", "明确静默仍然是静默（回复为空串 = 不发）")


# ---------------- T6：群聊上下文不许把发言人全塌成「对方」 ----------------

def t6_transcript_speakers():
    print("T6. 群聊上下文：拿不到 sender 时给可区分的兜底，绝不塞 wxid")
    msgs = [
        {"content": "在吗", "is_self": 0, "sender": "wxid_z", "time": "08:00"},
        {"content": "我也问一句", "is_self": 0, "time": "08:01"},          # 两条都没有 sender
        {"content": "我回一句", "is_self": 1, "time": "08:02"},
    ]
    notes = []
    txt = auto_reply.build_transcript(msgs, {}, unknown_note=notes)
    chk("对方1" in txt and "对方2" in txt,
        f"没 sender 的两条**可区分**，不再都是「对方」（实际 {txt!r}）")
    chk("wxid_" not in txt and "@chatroom" not in txt,
        "文本里没有 wxid/roomid（CLAUDE.md 硬约定）")
    chk(notes and "编号" in notes[0], f"给了「发言人未知/编号含义」的说明（实际 {notes}）")

    # sender_name（SessionTable 那条路带的微信显示名）优先用上
    t2 = auto_reply.build_transcript(
        [{"content": "晚上聚", "is_self": 0, "sender": "wxid_z",
          "sender_name": "群昵称-老王", "time": "08:03"}], {}, )
    chk("群昵称-老王" in t2 and "对方" not in t2,
        f"sender_name 优先当发言人（实际 {t2!r}）")

    # last_sender_display_name 是同一个东西的另一个字段名
    t3 = auto_reply.build_transcript(
        [{"content": "收到", "is_self": 0, "sender": "wxid_l",
          "last_sender_display_name": "小李", "time": "08:04"}], {})
    chk("小李" in t3 and "对方" not in t3, f"last_sender_display_name 也认（实际 {t3!r}）")

    # 只有 wxid、没有显示名 → 不能把 wxid 当名字用
    names = auto_reply.contact_names([{"wxid": "wxid_n", "name": "wxid_n"}])
    t4 = auto_reply.build_transcript(
        [{"content": "喂", "is_self": 0, "sender": "wxid_n", "time": "08:05"}], names)
    chk("wxid_n" not in t4 and "对方1" in t4,
        f"显示名本身就是 wxid 时退回编号，绝不塞 id（实际 {t4!r}）")

    # 单聊单条、有联系人表显示名：保持原样
    t5 = auto_reply.build_transcript(
        [{"content": "吃饭没", "is_self": 0, "sender": "wxid_z", "time": "08:06"}],
        {"wxid_z": "张三"})
    chk(t5 == "[08:06] 张三: 吃饭没", f"走联系人表的老路不变（实际 {t5!r}）")
    chk(auto_reply.build_transcript([], {}) == "", "空历史返回空串（调用方据此跳过）")


# ---------------- T7：编号不复用 ----------------

def t7_id_not_reused():
    print("T7. 编号不复用：删掉 t1 之后再加不会又发一个 t1")
    recs = [_task("t1"), _task("t2")]
    chk(scheduler._next_id(recs) == "t3", "接着最大编号往后排")
    chk(scheduler._next_id([_task("t3")]) == "t4", "只剩 t3 时给 t4")
    chk(scheduler._next_id([]) == "t1", "空列表给 t1")
    chk(scheduler._next_id([_task("t1"), _task("t3")]) == "t4",
        "中间有空洞也往后接（不复用空洞）")

    # 本次运行里删掉过的编号也不再发（见 T2：同一 tick 里删 t2 再加会拿到 t3）
    before = list(scheduler._RETIRED_IDS)
    try:
        scheduler._retire_id("t1")
        chk(scheduler._next_id([_task("t2")]) == "t3",
            "躲开本次运行里删掉的编号（t1 已退休 → 不发 t1）")
        scheduler._retire_id("t1")
        chk(scheduler._RETIRED_IDS.count("t1") == 1, "重复退休不会重复记账")
    finally:
        scheduler._RETIRED_IDS[:] = before


# ---------------- 用法文案和实际子命令一致 ----------------

def t8_usage_matches_impl():
    print("T8. _USAGE 文案和实际子命令对得上")
    chk("开|关 —— 总开关" in scheduler._USAGE and "编号|all" not in scheduler._USAGE,
        "定时文案不再承诺 /定时 开|关 <编号|all>")
    # 文案里承诺的每个子命令，都要在 handle_command 里真的有人接
    for frag in ("/auto on | off", "add", "del", "mode", "review", "ctx"):
        chk(frag in auto_reply._USAGE, f"auto 文案里有 {frag!r}")
    # 更硬的一条：文案里的子命令必须真的被 handle_command 认（走一遍）
    with TempSettings({"auto_reply": {"chats": [{"wxid": "wxid_z", "name": "张三"}]}}):
        cfg = settings.effective({})
        client = FakeClient(_CONTACTS)
        for arg in ("status", "review on", "review off", "ctx 25", "mode 张三 self"):
            out, _ch = auto_reply.handle_command(arg, cfg, client)
            chk("用法：" not in out,
                f"/auto {arg} 有实现、不该回用法（实际 {out[:40]!r}）")
        # 定时那侧同理：文案里的子命令都得真的有用（「不带参数=看列表」会带
        # 用法文案，所以这里按**行为**判定，不看有没有「用法」两个字）
        cfg_s = settings.effective({})
        out_s, _c = scheduler.handle_command("", cfg_s, _resolve_one)
        chk("定时任务" in out_s and "定时任务：" in out_s, "/定时 空参 = 看列表")
        scheduler.handle_command("关", cfg_s, _resolve_one)
        chk(scheduler.enabled(settings.effective({})) is False, "/定时 关 真的关了")
        scheduler.handle_command("开", cfg_s, _resolve_one)
        chk(scheduler.enabled(settings.effective({})) is True, "/定时 开 真的开了")
        out_del, ch_del = scheduler.handle_command("删 t99", cfg_s, _resolve_one)
        chk(not ch_del and "没有编号" in out_del, "/定时 删 <编号> 有实现")


def main():
    print("=" * 60)
    print("scheduler / auto_reply 回归自测（不联网、不碰微信、不启动 bot）")
    print("=" * 60)
    # 这个自测会把 settings 指到临时文件，但保险起见：先确认真的没指错
    real_settings = settings.SETTINGS_PATH
    assert os.path.basename(real_settings) == "settings.json"
    for fn in (t1_add_keeps_master_switch, t2_merge_save_keeps_action_changes,
               t3_bad_at_only_skips_itself, t4_mutual_exclusion,
               t5_group_reply_parsing, t6_transcript_speakers,
               t7_id_not_reused, t8_usage_matches_impl):
        fn()
        print("")
    assert settings.SETTINGS_PATH == real_settings, "别把真配置文件路径改回不去"
    print("=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）")
    print("=" * 60)


if __name__ == "__main__":
    main()
