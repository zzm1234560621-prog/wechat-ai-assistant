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
import agent_tools         # noqa: E402
import groups              # noqa: E402
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
    for frag in ("/auto on | off", "add", "del", "mode", "persona", "address",
                 "学习", "review", "ctx"):
        chk(frag in auto_reply._USAGE, f"auto 文案里有 {frag!r}")
    # 更硬的一条：文案里的子命令必须真的被 handle_command 认（走一遍）
    with TempSettings({"auto_reply": {"chats": [{"wxid": "wxid_z", "name": "张三"}]}}):
        cfg = settings.effective({})
        client = FakeClient(_CONTACTS)
        for arg in ("status", "review on", "review off", "ctx 25", "mode 张三 self",
                    "persona 张三 随便点", "persona 全局 正式一点", "persona 张三 学习",
                    "address 张三 老张", "address 张三", "address 张三 学习"):
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


def _auto_tool_stub(llm_factory=None):
    """只借 `ToolBox.t_auto_reply` 这一个方法，把它的依赖喂进来。

    这样测的是**真的工具层代码**（含范围校验），而不是另写一份等价逻辑。
    """

    class _Stub:
        t_auto_reply = agent_tools.ToolBox.t_auto_reply
        t_group = agent_tools.ToolBox.t_group

        def __init__(self):
            self.cfg = {}
            self.cfg_changed = False
            self.client = None
            self.llm_factory = llm_factory
            # 每次实时读临时 settings，改动立刻可见（和生产里 cfg_provider 一致）
            self.cfg_provider = lambda: settings.effective({})

        def _resolve(self, who):
            return [{"wxid": "wxid_z", "name": "张三", "remark": ""}]

        def _one(self, who):
            """和真的 ToolBox._one 同形状：重名不静默取第一个。"""
            cands = self._resolve(who)
            if not cands:
                return None, f"没找到「{who}」。"
            if len(cands) > 1:
                return None, f"「{who}」匹配到多个人：请用全名。"
            return cands[0], None

    return _Stub()


def t9_review_scope_is_explicit():
    """审核的范围必须**显式**：每会话一份，全局只是默认值。

    真机踩过（2026-10-01）：用户说「给李四加上自动回复，不用我同意内容」，
    模型调 `review` + `review=false` **没带 who** → 按 `/auto review` 的定义改了
    **全局默认**，于是**所有**自动回复会话的审核都被关了。
    根因不是模型撒谎（它其实补了一句「注意：审核是全局开关」），而是
    **工具说明只教了 `action=review, review=false` 这种写法、压根没提 who**，
    加上工具层允许漏参数静默改全局 —— 要堵的是**静默扩大影响面**那一头。
    """
    print("T9. 审核范围：只改某人 vs 改全局（模型漏参数不许静默改全局）")
    init = {"auto_reply": {"enabled": True, "review": False,
                           "chats": [{"wxid": "wxid_z", "name": "张三"}]}}
    with TempSettings(init) as tmp:
        tb = _auto_tool_stub()

        # 1) 模型没带 who：**必须拦住**，不许落到全局
        out = tb.t_auto_reply({"action": "review", "review": True})
        chk(auto_reply.section(settings.effective({})).get("review") is False,
            "review 不带 who → 全局默认**没被动**（拦住，而不是静默改全局）")
        chk("范围" in out, f"返回里明确要求先说明范围（实际 {out[:50]!r}）")

        # 2) 带 who：只改那一个人，全局默认原封不动
        out = tb.t_auto_reply({"action": "review", "review": True, "who": "wxid_z"})
        sec = auto_reply.section(settings.effective({}))
        chk(sec.get("review") is False, "只改某人时全局默认**不变**")
        chk((sec.get("chats") or [{}])[0].get("review") is True,
            "那个人的 review 被单独设为 True")
        chk("张三" in out, "返回里点名了这个人（模型能如实复述）")

        # 3) 只有显式 who=全局 才允许改全局
        out = tb.t_auto_reply({"action": "review", "review": True, "who": "全局"})
        sec = auto_reply.section(settings.effective({}))
        chk(sec.get("review") is True, "显式 who=全局 → 全局默认被改")
        chk("全局" in out, "返回里说清这是全局（不是某个人）")

        # 4) 单独设过的人**覆盖**全局默认 —— 每个对象各管各的
        tb.t_auto_reply({"action": "review", "review": False, "who": "wxid_z"})
        cfg = settings.effective({})
        rec = auto_reply.chats(cfg)["wxid_z"]
        chk(auto_reply.review_on(rec, cfg) is False and
            auto_reply.section(cfg).get("review") is True,
            "单人设置覆盖全局默认（全局 True、他 False → 单独设的赢）")

        # 5) on/off 传 who：不静默丢掉，必须说明它是全局总开关
        out = tb.t_auto_reply({"action": "on", "who": "张三"})
        chk(auto_reply.enabled(settings.effective({})) is True, "on 真的开了总开关")
        chk("全局总开关" in out,
            f"返回里说清 on 是全局总开关（实际 {out[-90:]!r}）")
        _ = tmp


def t10_relative_time():
    """相对时间：「10分钟后 / 半小时后 / 2小时后 / 3天后」= **只触发一次**。

    真机踩过（2026-10-01）：用户说「10分钟后给李四发你好」，`parse_when` 里没有
    相对分支 → 掉到最下面的 `_hhmm()` 兜底 → `int("10分钟后")` 抛错 →
    工具回「时间「10分钟后」没看懂。例：9:00、09:30」，于是让用户改说具体时刻。
    **是缺分支，不是有意拒绝**（代码里连一条相关测试都没有）。
    """
    print("T10. 相对时间：10分钟后 / 半小时后 / 几小时后 / 几天后")
    now = datetime(2026, 10, 1, 19, 23, 45)
    for when, date, at in (
        ("10分钟后", "2026-10-01", "19:34"),       # 19:33:45 → 向上取整到 19:34
        ("十分钟后", "2026-10-01", "19:34"),
        ("10分钟之后", "2026-10-01", "19:34"),     # 用户原话就是「10分钟之后」
        ("20分钟以后", "2026-10-01", "19:44"),
        ("半个小时后", "2026-10-01", "19:54"),
        ("半个小时以后", "2026-10-01", "19:54"),
        ("2小时后", "2026-10-01", "21:24"),
        ("2个钟头后", "2026-10-01", "21:24"),
        ("3天后", "2026-10-04", "19:24"),
        ("再过20分钟后", "2026-10-01", "19:44"),
    ):
        spec = scheduler.parse_when(when, now=now)
        chk(spec == {"repeat": "once", "date": date, "at": at},
            f"「{when}」→ 只一次 {date} {at}（实际 {spec}）")

    spec = scheduler.parse_when("10分钟后", now=datetime(2026, 10, 1, 23, 55, 10))
    chk(spec == {"repeat": "once", "date": "2026-10-02", "at": "00:06"},
        f"跨午夜要落到第二天（实际 {spec}）")

    spec = scheduler.parse_when("10分钟后", now=datetime(2026, 10, 1, 19, 23, 0))
    chk(spec["at"] == "19:33", f"整分时不多加一分钟（实际 {spec['at']}）")

    chk(scheduler.parse_when("9点半", now=now) == {"repeat": "daily", "at": "09:30"},
        "「9点半」= 每天 09:30")
    chk(scheduler.parse_when("每天9点半", now=now) == {"repeat": "daily", "at": "09:30"},
        "「每天9点半」= 每天 09:30")

    spec = scheduler.parse_when("10分钟后", now=now)
    nx = scheduler.initial_next(spec, now=now)
    chk(nx is not None and 9 * 60 <= nx - now.timestamp() <= 11 * 60,
        f"next_ts 落在 10 分钟附近（实际差 {(nx - now.timestamp()) if nx else None} 秒）")

    for bad in ("半分钟后", "0分钟后", "0小时后"):
        try:
            scheduler.parse_when(bad, now=now)
            chk(False, f"「{bad}」应当如实报错")
        except ValueError:
            chk(True, f"「{bad}」如实报错（不静默当成别的）")

    # 回归：老的写法一个都不能变
    chk(scheduler.parse_when("9:00", now=now) == {"repeat": "daily", "at": "09:00"},
        "回归：「9:00」仍是每天")
    chk(scheduler.parse_when("明天9:00", now=now)["repeat"] == "once",
        "回归：「明天9:00」仍是只一次")
    chk(scheduler.parse_when("每30分钟", now=now) ==
        {"repeat": "interval", "every_minutes": 30},
        "回归：「每30分钟」仍是间隔，没被相对分支抢走")
    chk(scheduler.parse_when("每周一 9:00", now=now)["repeat"] == "weekly",
        "回归：「每周一 9:00」仍是每周")


# ---------------- T11：每个人一份人设（/auto persona） ----------------

class _CaptureLLM:
    """把 system prompt 抓下来——用来验证「自定义人设真的进了提示词」。"""

    def __init__(self, raw="好"):
        self.raw = raw
        self.system = None

    def chat(self, system, messages):
        self.system = system
        return self.raw


def t11_per_person_persona():
    """每个人一套语气：/auto persona 单人 / 全局 / 清空 / 范围保护。

    2026-10-01 用户提的：「回复每个人的时候都有一个不同的人设」。
    生成那一半（`persona_for` / `make_reply`）本来就存在，缺的是**能改**的那一半——
    /auto 没有 persona 子命令、工具也没有这个动作，只能手编 config.yaml。
    这一条同时钉死两件事：
      * 单人 / 全局 / 清空 三条路都真的落盘，且**整体替换**语义不变；
      * mode（self/assistant）改叫**身份**、persona 才叫**人设**——
        以前两个都叫「人设」，用户一定会改错东西。
    """
    print("T11. 每个人一份人设：单人 / 全局 / 清空 / 范围保护")
    init = {"auto_reply": {"enabled": True, "chats": [
        {"wxid": "wxid_z", "name": "张三", "mode": "self", "review": None, "persona": ""},
        {"wxid": "wxid_zf", "name": "张三丰", "mode": "self", "review": None, "persona": ""},
    ]}}
    with TempSettings(init) as tmp:
        client = FakeClient(_CONTACTS)

        # 每条命令都**重新读一次配置**再跑：生产里 bot 就是这么做的
        # （bot.handle_command 的 /auto 分支、工具的 cfg_provider 都是实时读）。
        # 拿旧快照当基准会把上一条命令的改动写回去丢掉——`_save` 的注释里
        # 专门写了这一点，测试也得按真实用法走。
        def run(arg):
            return auto_reply.handle_command(arg, settings.effective({}), client)

        # 1) 单人：设 → 落盘在**那个人的条目**里（不是另造一个键）
        out, ch = run("persona 张三 跟张三别那么正式，随便点")
        chk(ch and "随便点" in out, "单人设人设有回执")
        rec_z = [r for r in tmp.read()["auto_reply"]["chats"]
                 if r["wxid"] == "wxid_z"][0]
        chk(rec_z["persona"] == "跟张三别那么正式，随便点", "人设落盘到那个人的条目里")

        # 2) 最长前缀：名单里同时有「张三」和「张三丰」时不许认错人
        run("persona 张三丰 对张三丰要用敬语")
        chats = auto_reply.chats(settings.effective({}))
        chk("敬语" in chats["wxid_zf"]["persona"] and
            "敬语" not in chats["wxid_z"]["persona"],
            "「张三丰」认到张三丰，没被「张三」前缀吃掉")

        # 3) 不带描述 = 查看（不改配置）
        out, ch = run("persona 张三")
        chk(not ch and "随便点" in out, "不带描述 = 看当前人设，不改配置")

        # 4) 名单外的人：拒绝，**绝不顺手 add**
        #    （顺手加人 = 替用户决定要不要自动回复这个人，正是要堵的「静默扩大影响面」）
        before = len(auto_reply.chat_list(settings.effective({})))
        out, ch = run("persona 李四 客气点")
        chk(not ch and "名单里没有" in out and "add" in out,
            f"名单外的人被拒并给出下一步（实际 {out[:40]!r}）")
        chk(len(auto_reply.chat_list(settings.effective({}))) == before,
            "拒绝时**没有**偷偷把人加进自动回复名单")

        # 5) 裸 /auto persona → 用法（别拿空串去查名单）
        out, ch = run("persona")
        chk(not ch and "用法：" in out, "裸 /auto persona 回用法，不崩")

        # 6) 全局：写进 settings.json 的 persona_self，**不碰** assistant
        run("persona 全局 你回所有人时简短直接一点")
        s = settings.load()["auto_reply"]
        chk("简短直接" in s.get("persona_self", ""), "全局 self 人设落盘")
        chk("persona_assistant" not in s, "没说要 assistant 就不动它（不静默扩大范围）")
        run("persona 全局 assistant 你是我的助理，礼貌简洁")
        chk("助理" in settings.load()["auto_reply"]["persona_assistant"],
            "全局 assistant 人设可以单独设")

        # 7) 分层：单人**整体替换**全局；没单独设的人吃全局
        c = settings.effective({})
        own = auto_reply.persona_for(auto_reply.chats(c)["wxid_z"], auto_reply.section(c))
        chk(own.startswith("跟张三别那么正式") and "简短直接" not in own,
            "单人是整体替换（全局那句不参与拼接）")
        chk("简短直接" in auto_reply.persona_for({"mode": "self"}, auto_reply.section(c)),
            "没单独设的人吃全局默认")

        # 8) 真的进了 system prompt（不然前面全是自说自话）
        llm = _CaptureLLM()
        auto_reply.make_reply(
            llm, {"wxid": "wxid_z", "mode": "self", "persona": "跟张三别那么正式，随便点"},
            [{"content": "在吗", "is_self": 0, "sender": "wxid_z", "time": "10-01 08:00"}],
            {}, c)
        chk("随便点" in (llm.system or ""), "自定义人设真的进了 system prompt")

        # 9) 全局「清空」= **删键**。写空串会盖住 config.yaml 那份，
        #    于是「恢复默认」反而变成「config.yaml 也不生效、只剩代码兜底」。
        run("persona 全局 self 清空")
        s = settings.load()["auto_reply"]
        chk("persona_self" not in s and "persona_assistant" in s,
            "清空全局 self = 删掉那个键（assistant 那份留着）")
        chk(auto_reply.section(settings.effective(
            {"auto_reply": {"persona_self": "config 里的"}})).get("persona_self") == "config 里的",
            "删键之后 config.yaml 那份重新生效")

        # 10) 单人「清空」= 回到默认那一份
        run("persona 张三 清空")
        chk(auto_reply.chats(settings.effective({}))["wxid_z"]["persona"] == "",
            "单人清空后记录里是空串（走回默认）")

        # 11) 太长**如实拒绝**，不静默截断（截断可能正好切掉「不确定别编」）
        out, ch = run("persona 张三 " + "很" * 400)
        chk(not ch and "太长" in out, "超长如实拒绝")
        chk(auto_reply.chats(settings.effective({}))["wxid_z"]["persona"] == "",
            "拒绝时没留下半截人设")

    # 12) 工具层：漏 who 不许静默改全局（和 T9 的审核同一条规矩）
    init2 = {"auto_reply": {"enabled": True, "chats": [
        {"wxid": "wxid_z", "name": "张三", "mode": "self", "review": None, "persona": ""}]}}
    with TempSettings(init2):
        tb = _auto_tool_stub()
        out = tb.t_auto_reply({"action": "persona", "persona": "随便点"})
        chk("范围" in out, f"persona 不带 who → 要求先说范围（实际 {out[:40]!r}）")
        chk("persona_self" not in settings.load().get("auto_reply", {}),
            "漏 who 时全局人设**没被动**")

        out = tb.t_auto_reply({"action": "persona", "who": "张三",
                               "persona": "用第一人称、别暴露你是 AI、口语简短"})
        chk(tb.cfg_changed, "工具带 who 时真的改了配置")
        chk("第一人称" in auto_reply.chats(settings.effective({}))["wxid_z"]["persona"],
            "工具把人设写到了那个人身上")
        chk("张三" in out, "回执里点名了这个人（模型能如实复述）")

        tb.t_auto_reply({"action": "persona", "who": "全局",
                         "persona": "回所有人时都客气一点"})
        chk("客气一点" in settings.load()["auto_reply"].get("persona_self", ""),
            "只有显式 who=全局 才改全局默认")


# ---------------- T12：从历史对话里学语气 ----------------

class _HistoryStub:
    """替掉 `live_history.query_contact_history`：喂固定历史，并记录调用参数。

    这里只替**这一个库调用**（本用例要测的是学习逻辑，不是 SQL）；
    顺带把「只查一次、查的是那个人的会话、limit 用配置值」这几条契约钉住。
    """

    def __init__(self, msgs):
        self.msgs = list(msgs)
        self.calls = []

    def __call__(self, client, talker, limit=50, keyword=None):
        self.calls.append((talker, limit))
        return list(self.msgs)


class _LearnLLM:
    """假模型：把 system / prompt 抓下来，供断言「送出去的到底是谁的话」。"""

    def __init__(self, raw):
        self.raw = raw
        self.system = None
        self.prompt = None
        self.calls = 0

    def chat(self, system, messages):
        self.calls += 1
        self.system = system
        self.prompt = messages[0]["content"]
        return self.raw


def _hist(n_mine=8, n_theirs=8, extra=()):
    """一段混合历史：我说的话 + 对方的话（+ 可选的别的类型）。"""
    msgs = []
    for i in range(max(n_mine, n_theirs)):
        if i < n_mine:
            msgs.append({"content": f"我说的话{i}", "is_self": 1, "local_type": 1,
                         "time": f"10-01 09:{i:02d}"})
        if i < n_theirs:
            msgs.append({"content": f"对方的话{i}", "is_self": 0, "local_type": 1,
                         "time": f"10-01 09:{i:02d}"})
    msgs.extend(extra)
    return msgs


_LEARNED = ("你正在代替我本人回复消息。用第一人称、口语简短、不要暴露你是 AI，"
            "不确定的事别编。对这个人说话很随意，爱开玩笑，常用「行」「哈哈」。")


def t12_learn_persona_from_history():
    """「开启自动回复之后，AI 能从历史对话学出对这个人的语气」。

    2026-10-01 用户提的。几条要害：
      * **只送我自己发出去的话**给模型（要复制的是我对这个人怎么说话；
        对方的话没信息量，还白多送一份隐私出去）；
      * 加进名单时自动学一次，但**绝不许盖掉已经设过的人设**——
        用户明说「重新学习」才覆盖；
      * 学不成（没 key / 没历史 / 读库出错）**如实说**，绝不假装学好了，
        而且**不影响加人**（人照样进名单，先用默认人设）。
    """
    print("T12. 学语气：只用我自己的话 / 不覆盖已有 / 学不成如实说")
    real_qch = auto_reply.live_history.query_contact_history
    try:
        stub = _HistoryStub(_hist(extra=[{"content": "[图片]", "is_self": 1,
                                          "local_type": 3, "time": "10-01 10:00"}]))
        auto_reply.live_history.query_contact_history = stub

        init = {"auto_reply": {"enabled": True, "chats": [
            {"wxid": "wxid_l", "name": "李四", "mode": "self", "review": None,
             "persona": ""}]}}
        with TempSettings(init) as tmp:
            client = FakeClient(_CONTACTS)
            llm = _LearnLLM(_LEARNED)

            # 1) /auto add 新的人 → 自动学一次
            out, changed = auto_reply.handle_command(
                "add 张三", settings.effective({}), client,
                llm_factory=lambda: llm)
            chk(changed and "已加入自动回复" in out, "加人本身成功")
            rec = auto_reply.chats(settings.effective({}))["wxid_z"]
            chk(rec["persona"] == _LEARNED, "加进来时就学到了人设")
            chk(rec["persona_source"] == "learned" and rec["persona_n"] == 8,
                f"来源和样本数记对了（source={rec.get('persona_source')!r}, "
                f"n={rec.get('persona_n')!r}）：图片那条不算语气样本")
            chk(len(stub.calls) == 1 and stub.calls[0][0] == "wxid_z",
                f"只读了一次库、且查的是那个人的会话（实际 {stub.calls}）")
            chk(stub.calls[0][1] == auto_reply.LEARN_SAMPLE,
                f"limit 用默认样本数（实际 {stub.calls[0][1]}）")

            # 2) **只把我自己的话送出去**
            chk("我说的话0" in (llm.prompt or ""), "样本里有我自己发的话")
            chk("对方的话0" not in (llm.prompt or ""),
                "对方的话**一个都没有**送出去（学的是我的语气，也少送一份隐私）")
            chk("第一人称" in (llm.system or "") and "不要暴露自己是 AI" in (llm.system or ""),
                "学习提示词里要求把底线一并写进人设（整体替换会连底线一起换掉）")

            # 3) 再 add 一次：已有学来的人设 → **不动**，也不该多花一次模型调用
            calls_before = llm.calls
            out, _ch = auto_reply.handle_command(
                "add 张三", settings.effective({}), client, llm_factory=lambda: llm)
            chk(llm.calls == calls_before, "已有人设时不再学（不白花模型调用）")
            chk("人设没动" in out, f"并且明说人设没动（实际 {out[-60:]!r}）")

            # 4) 手写的人设更不许被盖
            auto_reply.handle_command("persona 张三 我手写的语气", settings.effective({}),
                                      client)
            rec = auto_reply.chats(settings.effective({}))["wxid_z"]
            chk(rec["persona"] == "我手写的语气" and rec["persona_source"] == "manual",
                "手写的人设记成 manual")
            auto_reply.handle_command("add 张三", settings.effective({}), client,
                                      llm_factory=lambda: llm)
            chk(auto_reply.chats(settings.effective({}))["wxid_z"]["persona"] == "我手写的语气",
                "手写的人设**没被**自动学习盖掉")

            # 5) 用户明说「重新学习」才覆盖
            llm.raw = "你正在代替我本人回复。第一人称、口语简短、别暴露你是 AI。很正式。"
            out, changed = auto_reply.handle_command(
                "persona 张三 重新学习", settings.effective({}), client,
                llm_factory=lambda: llm)
            rec = auto_reply.chats(settings.effective({}))["wxid_z"]
            chk(changed and rec["persona"].startswith("你正在代替我本人回复。第一人称"),
                "明说重新学习才覆盖")
            chk(rec["persona_source"] == "learned", "覆盖后来源改回 learned")

            # 6) 学不成要如实说，绝不假装学好
            few = _HistoryStub(_hist(n_mine=2, n_theirs=3))
            auto_reply.live_history.query_contact_history = few
            auto_reply.handle_command("persona 张三 清空", settings.effective({}), client)
            before = auto_reply.chats(settings.effective({}))["wxid_z"]["persona"]
            out, changed = auto_reply.handle_command(
                "persona 张三 学习", settings.effective({}), client,
                llm_factory=lambda: llm)
            chk(not changed and "学不出语气" in out,
                f"样本不够时如实说学不出（实际 {out[:50]!r}）")
            chk(auto_reply.chats(settings.effective({}))["wxid_z"]["persona"] == before,
                "学不成时人设一个字都没动")

            # 7) 没有模型能力：明说学不了，不是静默成功
            out, changed = auto_reply.handle_command(
                "persona 张三 学习", settings.effective({}), client, llm_factory=None)
            chk(not changed and ("学不了" in out or "没接模型" in out),
                f"没接模型时如实说（实际 {out[:50]!r}）")

            # 8) 全局没有可学的历史：拦住，别把「学习」俩字当人设正文存进去
            out, changed = auto_reply.handle_command(
                "persona 全局 学习", settings.effective({}), client,
                llm_factory=lambda: llm)
            chk(not changed and "没法从历史里学" in out,
                f"全局「学习」被拦住（实际 {out[:40]!r}）")
            chk("persona_self" not in settings.load()["auto_reply"],
                "没有把「学习」两个字当成全局人设存下来")

            # 9) 学出来的太长：按句末截断，并且**说出来**
            auto_reply.live_history.query_contact_history = stub
            llm.raw = "语气随意。" * 100          # 600 字，远超 PERSONA_MAX
            out, changed = auto_reply.handle_command(
                "persona 张三 重新学习", settings.effective({}), client,
                llm_factory=lambda: llm)
            rec = auto_reply.chats(settings.effective({}))["wxid_z"]
            chk(changed and len(rec["persona"]) <= auto_reply.PERSONA_MAX,
                f"超长被压到上限内（实际 {len(rec['persona'])} 字）")
            chk("太长" in out, "压过之后明确告诉用户（不许静默改内容）")
            _ = tmp

        # 10) 工具层：学语气必须点名是谁；带 who 时走真的工具代码
        stub2 = _HistoryStub(_hist())
        auto_reply.live_history.query_contact_history = stub2
        init2 = {"auto_reply": {"enabled": True, "chats": [
            {"wxid": "wxid_z", "name": "张三", "mode": "self", "review": None,
             "persona": ""}]}}
        with TempSettings(init2):
            tb = _auto_tool_stub(llm_factory=lambda: _LearnLLM(_LEARNED))
            tb.client = FakeClient(_CONTACTS)
            out = tb.t_auto_reply({"action": "learn"})
            chk("是谁" in out, f"learn 不带 who 被拦住（实际 {out[:40]!r}）")
            chk(auto_reply.chats(settings.effective({}))["wxid_z"]["persona"] == "",
                "拦住时没有乱学")

            out = tb.t_auto_reply({"action": "learn", "who": "张三"})
            chk(tb.cfg_changed, "带 who 的 learn 真的改了配置")
            chk(auto_reply.chats(settings.effective({}))["wxid_z"]["persona"] == _LEARNED,
                "工具层学到了人设（走的是 handle_command 同一条实现）")
            chk("学到" in out or "学出" in out, "回执里说了是学到的（模型能如实复述）")
    finally:
        auto_reply.live_history.query_contact_history = real_qch


def t13_address_from_history():
    """从聊天记录识别「我平时怎么称呼他」，并且拿它当别名解析联系人。

    2026-10-01 用户提的。要害：
      * 称呼和人设**分开存、分开注入**——用户重写/手写人设时，称呼不该跟着丢；
      * `/auto address 谁 学习` **一个字都不动人设**（连 persona_source 都不许动，
        否则用户以后分不清哪段是自己写的）；
      * 模型没按 JSON 走（老格式/散文）时，**人设照学、称呼一个都不许猜**——
        猜错会让「给老张发消息」发错人；
      * 称呼当别名时，**和库里的精确匹配合并**：万一另一个人备注真叫「老张」，
        必须交给重名保护去问，绝不能静默挑一个。
    """
    print("T13. 称呼：从历史识别 / 独立于人设 / 当别名解析")
    real_qch = auto_reply.live_history.query_contact_history
    try:
        stub = _HistoryStub(_hist())
        auto_reply.live_history.query_contact_history = stub

        init = {"auto_reply": {"enabled": True, "chats": [
            {"wxid": "wxid_z", "name": "张三", "mode": "self", "review": None,
             "persona": ""}]}}
        with TempSettings(init) as tmp:
            client = FakeClient(_CONTACTS)

            def run(arg, llm_factory=None):
                return auto_reply.handle_command(arg, settings.effective({}), client,
                                                 llm_factory=llm_factory)

            def rec_z():
                return auto_reply.chats(settings.effective({}))["wxid_z"]

            # 1) 模型按 JSON 给两样 → 称呼和人设**一起**学到
            llm = _LearnLLM('{"address": "老张", "persona": '
                            '"你正在代替我本人回复。第一人称、简短、别暴露你是 AI。"}')
            out, changed = run("persona 张三 学习", llm_factory=lambda: llm)
            chk(changed and rec_z()["address"] == "老张", "学到了称呼")
            chk(rec_z()["address_source"] == "learned", "称呼来源记成 learned")
            chk(rec_z()["persona"].startswith("你正在代替我本人回复"), "人设也学到了")
            chk("老张" in out, "回执里说出了称呼（模型能如实复述）")

            # 2) 回复时**独立注入**：手写人设也不会把称呼弄丢
            msgs = [{"content": "在吗", "is_self": 0, "sender": "wxid_z",
                     "time": "10-01 08:00"}]
            cap = _CaptureLLM()
            auto_reply.make_reply(cap, rec_z(), msgs, {}, settings.effective({}))
            chk("老张" in (cap.system or ""), "回复提示词里带上了称呼")
            hand = {"wxid": "wxid_z", "mode": "self", "address": "老张",
                    "persona": "我手写的人设：第一人称、别暴露你是 AI、简短。"}
            cap2 = _CaptureLLM()
            auto_reply.make_reply(cap2, hand, msgs, {}, settings.effective({}))
            chk("老张" in (cap2.system or ""), "手写人设时称呼照样注入（独立于 persona）")

            # 3) `/auto address 谁 学习`：只学称呼，**人设和它的来源都不许动**
            before_persona = rec_z()["persona"]
            before_src = rec_z()["persona_source"]
            before_n = rec_z()["persona_n"]
            llm.raw = ('{"address": "张哥", "persona": "这段人设一个字都不该被写进去"}')
            out, changed = run("address 张三 学习", llm_factory=lambda: llm)
            chk(changed and rec_z()["address"] == "张哥", "只学称呼时称呼确实更新了")
            chk(rec_z()["persona"] == before_persona, "人设正文一个字没动")
            chk(rec_z()["persona_source"] == before_src and rec_z()["persona_n"] == before_n,
                "人设的**来源**也没被改成 learned（否则用户分不清哪段是自己写的）")

            # 4) 模型没按 JSON 走：人设照学，称呼**不许猜**、保持原样
            llm.raw = "你正在代替我本人回复。第一人称、口语简短、别暴露你是 AI。"
            out, _ch = run("persona 张三 重新学习", llm_factory=lambda: llm)
            chk(rec_z()["persona"].startswith("你正在代替我本人回复"), "散文格式照样学人设")
            chk(rec_z()["address"] == "张哥", "没按 JSON 走时**不猜称呼**，原来那个保持不动")

            # 5) 模型说「没有固定称呼」= 有效的学习结果（空串，不是 None）
            llm.raw = '{"address": "", "persona": "你正在代替我本人回复。第一人称、简短。"}'
            run("persona 张三 重新学习", llm_factory=lambda: llm)
            chk(rec_z()["address"] == "", "空串 = 模型明确说没固定称呼，照实存下来")
            cap3 = _CaptureLLM()
            auto_reply.make_reply(cap3, rec_z(), msgs, {}, settings.effective({}))
            chk("怎么称呼对方" not in (cap3.system or ""), "没称呼时提示词里不提称呼")

            # 6) 模型给的称呼太长 → 当没有处理（并说出来），**不许截一半存**
            llm.raw = ('{"address": "' + "老" * 30 + '", '
                       '"persona": "你正在代替我本人回复。第一人称、简短。"}')
            out, _ch = run("persona 张三 重新学习", llm_factory=lambda: llm)
            chk(rec_z()["address"] == "", "超长称呼按「没有称呼」处理，没存半截")
            chk("太长" in out, "并且明确告诉了用户")

            # 7) 手写称呼 + 清空
            out, changed = run("address 张三 老张")
            chk(changed and rec_z()["address"] == "老张", "手写称呼落盘")
            chk(rec_z()["address_source"] == "manual", "手写来源记成 manual")
            bad, _ch = run("address 张三 " + "老" * 30)
            chk("太长" in bad, "手写超长称呼如实拒绝")
            chk(rec_z()["address"] == "老张", "拒绝时没写进去")

            st = auto_reply.status_text(settings.effective({}))
            chk("称呼=你设的:老张" in st, f"状态里能看到称呼（实际 {st.splitlines()[-1]!r}）")

            run("address 张三 清空")
            chk(rec_z()["address"] == "", "清空后不再套称呼")

            # 8) 用**称呼**当名字操作（用户心里他就叫老张）
            run("address 张三 老张")
            out, changed = run("address 老张")          # 用称呼反查这个人
            chk(not changed and "老张" in out and "张三" in out,
                f"能用称呼查这个人（实际 {out[:40]!r}）")
            out, changed = run("review off 老张")
            chk(changed and rec_z()["review"] is False, "「/auto review … 老张」认得出是谁")

            # 9) 称呼当**别名**：联系人解析能认（「给老张发消息」）
            aliases = auto_reply.address_aliases(settings.effective({}))
            chk(aliases.get("老张"), f"称呼表里有老张（实际 {aliases}）")
            cands = agent_tools.resolve_contacts(_CONTACTS, "老张", aliases=aliases)
            chk(len(cands) == 1 and cands[0]["wxid"] == "wxid_z",
                f"「老张」解析到张三（实际 {cands}）")
            chk("张三" in str(cands[0].get("remark") or cands[0].get("name")),
                "用的是联系人表里的完整记录（备注是真的）")

            # 10) **别人备注真叫「老张」时不许静默挑一个**
            clash = list(_CONTACTS) + [{"wxid": "wxid_o", "name": "老王", "remark": "老张"}]
            cands2 = agent_tools.resolve_contacts(clash, "老张", aliases=aliases)
            chk(len(cands2) == 2, f"称呼和备注撞了 → 两个候选都摆出来（实际 {cands2}）")
            one, err = agent_tools.resolve_one(clash, "老张", aliases=aliases)
            chk(one is None and err and "多个人" in err,
                f"这种情况必须问用户，不能猜（实际 {err!r}）")

            # 11) 工具层：action=address
            tb = _auto_tool_stub()
            tb.client = client
            out = tb.t_auto_reply({"action": "address"})
            chk("是谁" in out, f"address 不带 who 被拦住（实际 {out[:40]!r}）")
            out = tb.t_auto_reply({"action": "address", "who": "全局", "address": "老张"})
            chk("按人" in out, f"address 不接受「全局」（实际 {out[:40]!r}）")
            out = tb.t_auto_reply({"action": "address", "who": "张三", "address": "张哥"})
            chk(tb.cfg_changed and rec_z()["address"] == "张哥",
                "工具层成功设了称呼（走的是 handle_command 同一条实现）")
            _ = tmp
    finally:
        auto_reply.live_history.query_contact_history = real_qch


def t14_groups():
    """分组：自己维护的名单，群发按组发。

    分组本身**不发消息、不碰库**（纯配置），所以这里只管两件事：
      * 增删改查真的落到 settings.json 的 groups 段、和 config.yaml 合并得上；
      * 一个名字对不上时**整批拒绝**——只加一半、剩下的悄悄算了，
        用户以后按组群发时才发现少了人，而那时消息已经发出去了。
    """
    print("T14. 分组：建 / 加 / 移 / 删 + 整批拒绝")
    with TempSettings({"groups": {}}) as tmp:
        client = FakeClient(_CONTACTS)

        def run(arg):
            return groups.handle_command(arg, settings.effective({}), _resolve_one)

        def names_of(g):
            return [m["name"] for m in groups.members(settings.effective({}), g)]

        # 1) 建组：人解析成 wxid 存下来（不是存昵称）
        out, ch = run("建 大学同学 张三、李四")
        chk(ch and "已建分组" in out, f"建组成功（实际 {out[:40]!r}）")
        disk = tmp.read()["groups"]
        chk([m["wxid"] for m in disk["大学同学"]] == ["wxid_z", "wxid_l"],
            f"落盘的是 wxid（实际 {disk}）")
        chk(names_of("大学同学") == ["张三", "李四"], "显示名也存了（不用再查库）")

        # 2) 加人：组不存在就建（「把王五加进『老同事』」不该逼用户先建组）
        out, ch = run("加 老同事 老同学群")
        chk(ch and "已建分组" in out and names_of("老同事") == ["老同学群"],
            f"往不存在的组加人会顺手建组（实际 {out[:40]!r}）")

        # 3) 重复加不叠人
        out, ch = run("加 大学同学 张三")
        chk(not ch and "本来就有" in out, f"重复加不重复落（实际 {out[:40]!r}）")
        chk(names_of("大学同学") == ["张三", "李四"], "人数没变")

        # 4) 一个名字对不上 -> **整批拒绝**，一个人都不许进去
        before = names_of("大学同学")
        out, ch = run("加 大学同学 李四、查无此人")
        chk(not ch and "一个人都没动" in out and "查无此人" in out,
            f"有人对不上就整批拒绝（实际 {out[:60]!r}）")
        chk(names_of("大学同学") == before, "拒绝时组里一个人都没多")
        chk("没找到「查无此人」" in out and "「查无此人」没找到「查无此人」" not in out,
            f"错误文案不套两层人名（实际 {out[:50]!r}）")
        _ = client

        # 5) 组名带空格：要给**针对性**的提示，不能只回一句「没找到」
        out, ch = run("建 大学 同学 张三")
        chk(not ch and "组名不能带空格" in out, f"组名带空格有针对性提示（实际 {out[:50]!r}）")

        # 6) 移人；移空了就把组删掉（留个空组只会在群发时撞「没有收件人」）
        out, ch = run("移 老同事 老同学群")
        chk(ch and "已经把组删掉" in out and groups.members(settings.effective({}), "老同事") is None,
            f"移空后组被删掉（实际 {out[:50]!r}）")

        out, ch = run("移 大学同学 张三")
        chk(ch and names_of("大学同学") == ["李四"], "移人只动那一个")
        out, ch = run("移 大学同学 李四")
        chk(ch and groups.members(settings.effective({}), "大学同学") is None, "移空了同样删组")

        # 7) 删组：人本身不动
        run("建 家人 张三")
        out, ch = run("删 家人")
        chk(ch and "人本身没动" in out and "家人" not in tmp.read()["groups"],
            f"删组只删组（实际 {out[:40]!r}）")
        out, ch = run("删 没这个组")
        chk(not ch and "没有" in out, "删不存在的组如实说")

        # 8) 状态：空组有引导语；有组时列出成员
        out, _ch = run("删 大学同学")
        chk("还没有任何分组" in run("")[0], "空分组时给建组引导")
        run("建 大学同学 张三、李四")
        st = run("")[0]
        chk("大学同学（2 人）" in st and "张三、李四" in st, f"状态列出组和成员（实际 {st[:60]!r}）")
        chk(groups.summary_line(settings.effective({})).startswith("分组（1）"),
            "摘要行给 agent 工具复述用")

        # 9) build_arg：工具和命令走同一条实现
        chk(groups.build_arg("add", group="大学同学", who="张三") == "建 大学同学 张三",
            groups.build_arg("add", group="大学同学", who="张三"))
        chk(groups.build_arg("remove", group="大学同学", who="张三") == "移 大学同学 张三",
            "build_arg: remove -> 移")
        chk(groups.build_arg("del", group="大学同学") == "删 大学同学",
            "build_arg: del -> 删")
        chk(groups.build_arg("status") == "", "status 走空参 = 看列表")
        # 工具层：建组；action 不认时如实报
        tb = _auto_tool_stub()
        tb.client = FakeClient(_CONTACTS)
        out = tb.t_group({"action": "add", "group": "测试组", "who": "张三"})
        chk(tb.cfg_changed and "测试组" in settings.load().get("groups", {}),
            f"工具层建组真的落盘（实际 {out[:40]!r}）")
        chk(groups.build_arg("不存在的动作") == "不存在的动作", "不认识的动作原样透传（命令里会回用法）")
    return True


# ---------------- T15：盯着关键词（任何会话命中就通知）----------------

def t15_watch_keywords():
    print("T15. 盯着关键词：任何会话命中就通知；非法/危险正则当场拒绝")
    init = {"watch": {"enabled": True, "chats": [], "keywords": []}}
    with TempSettings(init) as tmp:
        cfg = settings.effective({})
        out, changed = watch.handle_command("关键词 报价|合同", cfg, _resolve_one)
        chk(changed and "已加入关键词" in out, f"加进去了（实际：{out[:40]}）")
        chk(str(watch.KEYWORD_SCAN_CHARS) in out,
            "加的时候就必须说清「只扫前 N 个字符」这个真实限制（不许含糊）")
        disk = tmp.read()["watch"]["keywords"]
        chk(len(disk) == 1 and disk[0]["pattern"] == "报价|合同", f"落盘正确：{disk}")

        kws = watch.keywords(settings.effective({}))
        chk([k["pattern"] for k in watch.match_keywords("这个报价什么时候给", kws)]
            == ["报价|合同"], "命中「报价」")
        chk(watch.match_keywords("合同编号 A-12", kws) != [], "另一分支也命中（是正则不是子串）")
        chk(watch.match_keywords("今天天气不错", kws) == [], "不命中就不打扰")
        chk(watch.match_keywords("", kws) == [], "空文本不炸")

        # 多条
        watch.handle_command("关键词 发票", settings.effective({}), _resolve_one)
        chk(len(watch.keywords(settings.effective({}))) == 2, "两条关键词都在")

        # 非法正则：当场拒绝、说清原因、**不落盘**
        ok, why = watch.check_pattern("报价(")
        chk(not ok and "正则" in why, f"非法正则被拒且说清：{why[:40]!r}")
        out_bad, changed_bad = watch.handle_command("关键词 报价(", settings.effective({}),
                                                   _resolve_one)
        chk(not changed_bad and "正则" in out_bad, "非法正则不写进配置")
        chk(len(watch.keywords(settings.effective({}))) == 2, "配置没被弄坏")

        # 回溯炸弹：必须拒（一卡就把轮询/定时/看护全停了）
        for bomb in ("(a+)+$", "(ab*)*", "(x+)+"):
            ok_b, why_b = watch.check_pattern(bomb)
            chk(not ok_b and "嵌套量词" in why_b, f"拒绝回溯炸弹 {bomb!r}")

        # 太长
        ok_l, why_l = watch.check_pattern("a" * (watch.KEYWORD_MAX_LEN + 1))
        chk(not ok_l and "太长" in why_l, "超长正则被拒")
        ok_e, why_e = watch.check_pattern("   ")
        chk(not ok_e, "空模式被拒")

        # 只扫前 N 个字符：这是**真实限制**，必须有确定行为，不许假装能匹配
        kws2 = watch.keywords(settings.effective({}))
        far = ("x" * watch.KEYWORD_SCAN_CHARS) + "报价"
        chk(watch.match_keywords(far, kws2) == [],
            "截断窗口之外的内容**匹配不到**（这就是我们要如实告诉用户的限制）")
        near = "报价" + ("x" * 10)
        chk(watch.match_keywords(near, kws2) != [], "窗口之内照常匹配")

        # 删除
        out_del, ch_del = watch.handle_command("关键词 删 发票", settings.effective({}), _resolve_one)
        chk(ch_del and "已删除" in out_del, f"删除成功：{out_del[:30]!r}")
        chk(len(watch.keywords(settings.effective({}))) == 1, "删掉一条只剩一条")
        out_nf, ch_nf = watch.handle_command("关键词 删 根本没有", settings.effective({}),
                                            _resolve_one)
        chk(not ch_nf and "没有" in out_nf, "删不存在的如实说")

        # 状态里要看得到
        st = watch.status_text(settings.effective({}))
        chk("关键词" in st and "报价|合同" in st, f"状态里能看到关键词：{st[:56]!r}")

        # 工具侧走同一条实现
        chk(watch.build_arg("keyword", "发票") == "关键词 发票", "工具参数拼得对")
        chk(watch.build_arg("keyword_del", "发票") == "关键词 删 发票", "删除也拼得对")


# ---------------- T16：代回消息时不许替我承诺/约定 ----------------

def t16_no_commitment_on_my_behalf():
    """「不许替我承诺」这条硬规矩必须**无条件**进 prompt，人设整体替换也盖不掉。

    2026-10-02 真机：给「老师」学过人设之后，助手以用户本人的口吻把饭约了
    （「老师，SKP米其林可不便宜啊[捂脸] 行，明天就明天…」），再往前还主动加了
    「修好了我请您吃一顿」。当时 prompt 里只有「不确定的事别编」——那条管的是
    **事实**，管不住**替我表态**：约时间、答应赴约、承诺请客，全落在缝里。

    这条用例专门钉**加在哪**：人设（`persona_for`）是整体替换的——用户给某人设过或
    学过人设之后，`_DEFAULT_SELF` 一个字都不进 prompt。规矩只有落在 `_COMMON_RULES`
    里才对每个人生效，所以下面故意喂一段**不含这条规矩**的「学到的老师人设」，
    断言它照样出现在真正送出去的 system prompt 上。

    2026-10-02 追加后半条：这条规矩里**不许再出现现成句子**。原来写的是
    「一律不接，只回一句『我回头确认下』（或『我看下时间哈』）」——强模型当兜底文案，
    弱模型（本地 Ollama qwen3:14b）直接当**成品答案**照抄：「老师」那一路连着几条
    自动回复都是「老师，我回头确认下[捂脸]」。现在只留约束、不留文案，
    下面**反面断言** prompt 里不再有那两句，防止谁好心加回去。
    """
    print("T16. 代回消息：不许替我承诺/约定（人设整体替换也盖不掉）")
    msgs = [{"content": "明天去SKP米其林吃个饭，我请你", "is_self": 0,
             "sender": "wxid_z", "time": "10-02 20:14"}]
    # 照真机上那份「学到的」人设的样子写：只有语气，没有任何禁止替我承诺的规矩
    learned = ("你正在代替我本人回复微信。对方是我老师，称「老师」，用「您」。"
               "说话口语、简短，像平时打微信，不确定的事别编。")
    learned_rec = {"wxid": "wxid_z", "mode": "self", "persona": learned}

    cases = [
        ("单聊·学到的人设", learned_rec, {}, False),
        ("群聊·学到的人设", learned_rec, {}, True),
        ("单聊·没单独设人设（走默认）", {"mode": "self"}, {}, False),
        ("单聊·全局人设（也是整体替换）", {"mode": "self"},
         {"auto_reply": {"persona_self": "回所有人时简短直接一点"}}, False),
        ("单聊·身份=助手", {"mode": "assistant"}, {}, False),
    ]
    for label, rec, cfg, group in cases:
        cap = _CaptureLLM()
        auto_reply.make_reply(cap, rec, msgs, {}, cfg, group=group)
        sys_p = cap.system or ""
        for token in ("绝不替我做承诺", "把决定留给我本人"):
            chk(token in sys_p, f"{label}：system prompt 里有「{token}」")
        chk("赴约" in sys_p and "请客" in sys_p and "花钱" in sys_p,
            f"{label}：约时间/赴约/请客花钱都被点到（不只是句空话）")
        # ⚠️ 反面断言：prompt 里不许再出现**现成句子**。给弱模型一句可直接照抄的
        #    成品答案，它就不再判断「这条到底算不算要我承诺」，凡拿不准就整句抄。
        for token in ("我回头确认下", "我看下时间哈"):
            chk(token not in sys_p,
                f"{label}：prompt 里不该再有现成句子「{token}」（弱模型会照抄）")

    # 反面证据：上面那段「人设」里**确实没有**这条规矩——所以绿的只能是 _COMMON_RULES
    chk("绝不替我做承诺" not in learned,
        "那段人设自身不含这条规矩（规矩不是从人设里来的）")
    # 而且它就是 _COMMON_RULES 里那一条（唯一的真源，别在别处另写一份）
    chk("绝不替我做承诺" in auto_reply._COMMON_RULES,
        "规矩的唯一真源是 auto_reply._COMMON_RULES")
    # 代码里那两处真源（规则 + 默认人设）也都不许再有现成句子
    for where, text in (("_COMMON_RULES", auto_reply._COMMON_RULES),
                        ("_DEFAULT_SELF", auto_reply._DEFAULT_SELF)):
        chk("我回头确认下" not in text,
            f"{where} 里不许再有那句现成文案（弱模型会把它当万能回复）")


# ---------------- T17：提醒我（mode=remind / 「10分钟之后 我 …」）----------------

class _SchedToolStub:
    """只借 `ToolBox.t_schedule` 这一个方法（**模型那条路**），不造真 ToolBox。"""

    t_schedule = agent_tools.ToolBox.t_schedule

    def __init__(self):
        self.cfg_changed = False
        self.cfg_provider = lambda: settings.effective({})

    def _one(self, who):
        return _resolve_one(who)


def t17_remind_me():
    """「10分钟之后提醒我喝水」——到点把这句话**原样**发回控制会话。

    2026-10-03 用户要的。此前 `<对象>` 位置写「我」会去查联系人，只得到
    「没找到「我」」；动作里也没有一个「提醒我自己」。三条路都要通：
    `/定时 加提醒 …`、`/定时 加 … 我 …`、以及模型调 schedule 工具（mode=remind）。
    """
    print("T17. 提醒我：10分钟之后 / 「我」不要当联系人 / 到点只进控制会话")
    now = datetime(2026, 10, 1, 19, 23, 45)      # 与 T10 同一个基准时刻
    with TempSettings({"schedule": {"enabled": True, "tasks": []}}) as tmp:
        # 1) 显式子命令
        out, ch = scheduler.handle_command("加提醒 10分钟之后 喝水",
                                           settings.effective({}), _resolve_one, now=now)
        chk(ch and "已加定时任务" in out, f"加提醒建出任务（实际：{out[:40]!r}）")
        t = tmp.read()["schedule"]["tasks"][-1]
        chk(t.get("action") == "remind" and t.get("text") == "喝水" and not t.get("to"),
            f"提醒任务：不用对象、内容原样（实际 {t}）")
        chk(t.get("repeat") == "once" and t.get("at") == "19:34",
            f"相对时间当场算成绝对时刻（实际 {t.get('date')} {t.get('at')}）")

        # 2)「我 / 自己 / 本人」这些自称都算提醒我，绝不拿去查联系人
        for who in ("我", "自己", "本人", "我本人"):
            scheduler.handle_command(f"加 10分钟之后 {who} 吃药", settings.effective({}),
                                     _resolve_one, now=now)
            t = tmp.read()["schedule"]["tasks"][-1]
            chk(t.get("action") == "remind" and t.get("text") == "吃药"
                and not t.get("to"),
                f"「… {who} 吃药」= 提醒我自己（实际 action={t.get('action')} "
                f"to={t.get('to')!r} text={t.get('text')!r}）")

        # 2b) 回归（2026-10-04 真机自测抓出来的）：**正文开头的「我」不许被削**。
        # 旧实现按「正文开头有没有『我』这个字」削，`加提醒 2分钟之后 我是部署自检…`
        # 被削成「是部署自检…」——用户自己写的话被吃掉一个字。
        for arg, want in (
            ("加提醒 10分钟之后 我是部署自检：原文别动", "我是部署自检：原文别动"),
            ("加提醒 10分钟之后 我 我是自己写的", "我是自己写的"),
            ("加 10分钟之后 我 我自己写的正文", "我自己写的正文"),
            ("加 10分钟之后 我 本人不在", "本人不在"),
        ):
            scheduler.handle_command(arg, settings.effective({}), _resolve_one, now=now)
            t = tmp.read()["schedule"]["tasks"][-1]
            chk(t.get("action") == "remind" and t.get("text") == want,
                f"「{arg}」→ 正文一个字不动（实际 {t.get('text')!r}）")

        # 3) 反向：真人不能被抢走（否则「提醒张三」会变成提醒我）
        scheduler.handle_command("加 10分钟之后 张三 开会", settings.effective({}),
                                 _resolve_one, now=now)
        t = tmp.read()["schedule"]["tasks"][-1]
        chk(t.get("action") == "text" and t.get("to") == "wxid_z"
            and t.get("text") == "开会",
            f"真人还是发给真人（实际 {t.get('action')} {t.get('to')} {t.get('text')!r}）")

        # 4) 列表里显示成「提醒你…」（不是「发给 我」）
        out, _ = scheduler.handle_command("", settings.effective({}), _resolve_one)
        chk("提醒你" in out, "列表里写「提醒你：…」")

        # 5) 到点执行：只进控制会话，**一个联系人都没发**
        sent, notes = [], []
        due = [dict(t, next_ts=now.timestamp() - 1)
               for t in tmp.read()["schedule"]["tasks"] if t.get("action") == "remind"]
        chk(len(due) >= 1, "至少有两条提醒任务排到点")
        real_save = scheduler._save
        scheduler._save = lambda **kw: None       # 自测不写盘
        try:
            fired = scheduler.run_due(
                {"schedule": {"enabled": True, "tasks": due}}, now,
                send_text=lambda to, tx: sent.append((to, tx)), notify=notes.append)
        finally:
            scheduler._save = real_save
        chk(len(fired) == len(due), f"{len(due)} 条提醒都触发了（实际 {fired}）")
        chk(sent == [], f"提醒**不发给任何联系人**（实际 {sent}）")
        chk(any("⏰ 提醒：喝水" in n for n in notes), f"喝水那条进了控制会话（{notes}）")

        # 6) 模型那条路：t_schedule + mode=remind（模型常把 who 也填成「我」，也得对）
        stub = _SchedToolStub()
        stub.t_schedule({"action": "add", "when": "10分钟之后", "text": "站起来走两步",
                         "mode": "remind", "who": "我"})
        t = tmp.read()["schedule"]["tasks"][-1]
        chk(t.get("action") == "remind" and t.get("text") == "站起来走两步"
            and not t.get("to"),
            f"模型路（mode=remind）也对（实际 action={t.get('action')} "
            f"to={t.get('to')!r} text={t.get('text')!r}）")
        chk(stub.cfg_changed is True, "改过配置要标 cfg_changed（主循环才会 reload）")

        # 7) build_arg：mode=remind 时 who 位置那个「我」**不许混进正文**
        arg = scheduler.build_arg("add", when="10分钟之后", who="我",
                                  text="喝水", mode="remind")
        chk(arg == "addremind 10分钟之后 喝水", f"拼出来的子命令对（实际 {arg!r}）")


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
               t7_id_not_reused, t8_usage_matches_impl,
               t9_review_scope_is_explicit, t10_relative_time,
               t11_per_person_persona, t12_learn_persona_from_history,
               t13_address_from_history, t14_groups,
               t15_watch_keywords, t16_no_commitment_on_my_behalf,
               t17_remind_me):
        fn()
        print("")
    assert settings.SETTINGS_PATH == real_settings, "别把真配置文件路径改回不去"
    print("=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）")
    print("=" * 60)


if __name__ == "__main__":
    main()
