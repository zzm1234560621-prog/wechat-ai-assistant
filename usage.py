"""用量（token）与费用统计：把每次调模型的 token 记在本地，供 `/用量` 查。

**为什么要这个模块**：项目接了付费 API，但之前完全没有任何用量采集——用户
只能去服务商后台看总账，看不到「这个 bot 到底烧了多少、烧在哪」。这个模块
只做三件事：**记一行**（`record`）、**算总数**（`summary`）、**说人话**（`summarize`）。

四条硬约定（都是项目已有的铁律，不是洁癖）：

1. **绝不记隐私**。落盘的每一行只有 6 个键：
   `ts / provider / model / prompt_tokens / completion_tokens / kind`。
   **不写 api_key、不写 base_url、更不写请求内容**——聊天记录原文一旦落到
   这个文件里，就等于在项目里开了第二个"聊天记录库"，还多半是明文、
   会被同步/备份出去。要排查问题去看 bot.log，别往这里加字段。
2. **失败绝不能影响主流程**。`record()` 是在 `llm.py` 每次调完模型之后顺手
   调的；这里抛异常会把一次正常的问答带崩。所以整个函数体包在 try/except 里，
   出错只打一条 `⚠️` 告警（**不许静默**），绝不向上抛。
3. **不许编造价格**。`price_of()` 只放**有把握**的官方公开价（见下面的表），
   表里没有的模型一律返回 `None`，`summarize()` 就**明说「这个模型没有价目表，
   只报 token 不算钱」**。绝不按"差不多的模型"套一个价——那是在报假账。
   注意：这是**估算**，不是账单。DeepSeek 有夜间优惠时段（北京时间 00:30~08:30
   折扣），本地估算会**偏高**；中转/代理站的价格也和官方表无关。
4. **损坏的行不许崩**。`usage.jsonl` 是追加写的，断电/半行写入都可能留下
   半截 JSON。读的时候坏行**跳过并计数**（`bad_lines`），照常出统计。

落盘位置：`<项目根>/data/usage.jsonl`（`data/` 已被 .gitignore 忽略）。
用 `os.path.dirname(os.path.abspath(__file__))` 拼路径，不写死盘符——
换台机器、换个目录 clone 都能跑。
"""
import json
import os
import time

# 项目根目录（和 executor.py / llm.py 同一个算法：不写死盘符）。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# 用量账本。JSONL（一行一条记录）而不是 JSON 数组，理由是**追加写**：
# 每调一次模型就 append 一行，不用把整个文件读出来重写一遍。
# 代价是要容忍坏行，见 summary() 的 bad_lines。
USAGE_PATH = os.path.join(PROJECT_DIR, "data", "usage.jsonl")


# ============================================================
#  价目表 —— 单位：元 / 百万 token
# ============================================================
# **价格会变，用户可自行修改这张表**（也可以直接改 price_of 加自己的条目）。
# 这里只放**官方文档公开、且本站确认过**的条目；拿不准的**宁可空着**——
# 空着 = summarize() 里明说"没有价目表"，那是诚实的；瞎填 = 报假账。
#
# 本站有把握的只有 DeepSeek 官方（deepseek-chat / deepseek-reasoner，
# 按"输入未命中缓存 / 输出"计价）：
#   deepseek-chat      输入 2 元/百万，输出 8 元/百万
#   deepseek-reasoner  输入 4 元/百万，输出 16 元/百万
#
# 明确**没有**收录的（不是忘了，是查不到可信公开价 / 会随版本变）：
#   Claude 各档、通义千问、Kimi、智谱 GLM、OpenAI —— 这些走 unpriced 分支，
#   只报 token。用户要算钱，请自己按服务商价目表加进下面这张表。
#
# 夜间优惠提醒：DeepSeek 在北京时间 00:30~08:30 有折扣价，本表按标准价算，
# 所以估算**只会偏高、不会偏低**。
PRICE_TABLE = {
    # model（小写）: (输入单价, 输出单价)，元/百万 token
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
}

# 归一化时剥掉的 provider 前缀（只剥**已知**前缀，见 _norm_model）。
_PROVIDER_PREFIXES = ("deepseek/", "openai/", "anthropic/", "moonshot/",
                       "qwen/", "zhipu/", "azure/", "ollama/")


def _norm_model(model):
    """模型名归一化：转小写、去掉首尾空白、剥掉已知的 provider 前缀。

    `llm.py` 里 provider 和 model 是分开传的，用户也可能写成
    `deepseek/deepseek-chat`。只剥**白名单里的前缀**，不胡乱按 `/` 切——
    有些中转站的模型名本身就带斜杠，切错了会命中错误的价目表（那还不如不命中）。
    """
    s = str(model or "").strip().lower()
    for p in _PROVIDER_PREFIXES:
        if s.startswith(p):
            return s[len(p):]
    return s


def price_of(model):
    """查某个模型的价目表。返回 `(输入单价, 输出单价)`，单位**元/百万 token**。

    没有这个模型的价目表就返回 `None`（**不猜、不套用"同系列"的价格**）。
    调用方拿到 None 要么只报 token，要么明确标注"这是估算"。
    """
    return PRICE_TABLE.get(_norm_model(model))


def _to_int(v):
    """把 token 数洗成非负整数。坏值（None / "abc" / 小数）一律当 0，不抛。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


# ============================================================
#  记账
# ============================================================

def record(provider, model, prompt_tokens=0, completion_tokens=0, kind="chat"):
    """追加一条用量记录。**永不抛异常**（失败只打告警），因为它在主流程里被调。

    落的行只有这些键——**别加字段**，尤其别加 api_key / base_url / 消息内容：
        ts                 epoch 秒（int）
        provider           "openai" / "anthropic" / ...
        model              "deepseek-chat" / ...
        prompt_tokens      输入 token
        completion_tokens  输出 token
        kind               "chat" 之类，留给以后区分用途

    写入方式是「append 一行」，不开线程、不重写整个文件（和项目里
    "别开后台线程"的约定一致：调用方是单线程主循环）。
    """
    try:
        row = {
            "ts": int(time.time()),
            "provider": str(provider or ""),
            "model": str(model or ""),
            "prompt_tokens": _to_int(prompt_tokens),
            "completion_tokens": _to_int(completion_tokens),
            "kind": str(kind or "chat"),
        }
        d = os.path.dirname(USAGE_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False)
        with open(USAGE_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as e:
        # **不许静默**：打出来，但绝不向上抛——统计坏掉不能影响聊天。
        print(f"⚠️ 用量记录写入失败（不影响本次回答）：{type(e).__name__}: {e}")


# ============================================================
#  统计
# ============================================================

def summary(days=7):
    """读本地账本，按模型聚合成 dict。坏行跳过并计数，**不崩**。

    返回：
        calls              窗口内的调用次数
        prompt_tokens      窗口内输入 token 合计
        completion_tokens  窗口内输出 token 合计
        by_model           {model: {calls, prompt_tokens, completion_tokens, est_cost}}
                           est_cost 为该模型的估算费用（无价目表时为 None）
        est_cost           窗口内**能算的那部分**的估算总费用；一个都算不了 → None
        priced             窗口内有价目表的模型列表
        unpriced           窗口内没有价目表的模型列表
        bad_lines          读的时候跳过的坏行数（半截 JSON / 缺字段 / 缺 ts）
        days               统计窗口（天）

    `days <= 0` 表示不按时间过滤（统计整个账本），给"看总账"用。
    """
    days = _to_int_or_default(days, 7)
    out = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "by_model": {},
        "est_cost": None,
        "priced": [],
        "unpriced": [],
        "bad_lines": 0,
        "days": days,
    }

    # 窗口起点：days>0 才算。缺 ts 的行在 windowed 模式下算坏行（无法判断新旧），
    # 不能**猜**它的时间——猜错了就是把老账算进本周。
    windowed = days > 0
    cutoff = time.time() - days * 86400 if windowed else 0

    try:
        if not os.path.exists(USAGE_PATH):
            return out  # 还没记过账，不是错误，返回空统计
        with open(USAGE_PATH, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = (raw or "").strip()
                if not line:
                    continue  # 空行（比如文件末尾）不算坏行
                try:
                    row = json.loads(line)
                except Exception:
                    out["bad_lines"] += 1
                    continue
                if not isinstance(row, dict):
                    out["bad_lines"] += 1
                    continue
                ts = row.get("ts")
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    out["bad_lines"] += 1
                    continue
                if windowed and ts < cutoff:
                    continue  # 窗口外的老账，正常跳过（不算坏行）
                model = str(row.get("model") or "(未记模型名)")
                m = out["by_model"].setdefault(model, {
                    "calls": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "est_cost": None,
                })
                m["calls"] += 1
                m["prompt_tokens"] += _to_int(row.get("prompt_tokens"))
                m["completion_tokens"] += _to_int(row.get("completion_tokens"))
    except Exception as e:
        # 读不动（权限/文件被占）也要**说出来**，同时给一份空统计而不是崩掉。
        print(f"⚠️ 用量统计读取失败：{type(e).__name__}: {e}")
        return out

    # 合计 + 逐模型算钱
    total_cost = 0.0
    priced, unpriced = [], []
    for model in sorted(out["by_model"]):
        m = out["by_model"][model]
        out["calls"] += m["calls"]
        out["prompt_tokens"] += m["prompt_tokens"]
        out["completion_tokens"] += m["completion_tokens"]
        p = price_of(model)
        if p is None:
            unpriced.append(model)      # 没有价目表 → 只报 token
            continue
        priced.append(model)
        # 单价是「元/百万 token」，所以除 1e6。
        cost = (m["prompt_tokens"] * p[0] + m["completion_tokens"] * p[1]) / 1e6
        m["est_cost"] = cost
        total_cost += cost
    out["priced"] = priced
    out["unpriced"] = unpriced
    # 一个能算的都没有 → None（**不是 0**）：0 会被读成"不花钱"，是假话。
    out["est_cost"] = total_cost if priced else None
    return out


def _to_int_or_default(v, default):
    """配置/调用方传来的 days 容错：坏值用默认值。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _fmt_cost(c):
    """费用格式化：小额别被压成 0.00。"""
    if c is None:
        return "—"
    if c == 0:
        return "0"
    if c < 0.01:
        return f"{c:.5f}".rstrip("0").rstrip(".")
    return f"{c:.4f}".rstrip("0").rstrip(".")


def _window_text(days):
    if days <= 0:
        return "全部时间（不按窗口过滤）"
    return f"最近 {days} 天"


def summarize(days=7):
    """给 `/用量` 命令用的**中文**多行文本。永远返回字符串，不抛。"""
    s = summary(days)
    win = _window_text(s["days"])
    head = f"📊 本地用量统计（{win}）"

    if s["calls"] == 0:
        lines = [head, ""]
        if s["bad_lines"]:
            # 有坏行就**别说"还没有记录"**——那是把"账本被写坏了"说成"没花过钱"，
            # 是静默失败。这里如实说清楚两件事：读不出记录 + 有 N 行读不动。
            lines.append(f"没有能读出来的用量记录；另有 {s['bad_lines']} 行读不动"
                         "（半截 JSON 等），已跳过。")
            lines.append("（账本在 data/usage.jsonl，可以直接看；先别删，"
                         "坏行本身也是线索。）")
        else:
            lines.append("还没有记录。")
            lines.append("（下次调模型后就会开始记账；账本在 data/usage.jsonl）")
        return "\n".join(lines)

    lines = [
        head,
        f"调用 {s['calls']} 次 ｜ 输入 {s['prompt_tokens']} tokens ｜ "
        f"输出 {s['completion_tokens']} tokens",
        "",
        "按模型：",
    ]
    for model in sorted(s["by_model"]):
        m = s["by_model"][model]
        row = (f"  · {model}：{m['calls']} 次，输入 {m['prompt_tokens']} / "
               f"输出 {m['completion_tokens']} tokens")
        if m["est_cost"] is not None:
            row += f"，估算 ¥{_fmt_cost(m['est_cost'])}"
        lines.append(row)

    if s["est_cost"] is not None:
        lines.append("")
        lines.append(f"估算合计：¥{_fmt_cost(s['est_cost'])}"
                     + ("（只含上表里能算钱的模型）" if s["unpriced"] else ""))
    if s["unpriced"]:
        lines.append("")
        lines.append("下面这些模型没有价目表，只报 token 不算钱"
                     "（想要金额请自己按服务商价目表改 usage.price_of）：")
        for model in s["unpriced"]:
            lines.append(f"  · {model}")
        lines.append("（不是漏算了，是本站没有可信的公开价，不猜。）")

    if s["bad_lines"]:
        lines.append(f"⚠️ 另有 {s['bad_lines']} 行读不动（半截 JSON 等），已跳过。")

    lines.append("")
    lines.append("说明：这是**本地估算**，不是账单；只记成功拿到 usage 的调用，"
                 "失败的调用没有记录；金额按标准价算（夜间优惠会让实际更低）。")
    return "\n".join(lines)


# ============================================================
#  从各家响应里取 usage
# ============================================================

def extract_openai_usage(resp):
    """从 OpenAI 兼容响应的 `usage` 里取 `(prompt_tokens, completion_tokens)`。

    缺字段 / 结构不对一律返回 `(0, 0)`，**不抛异常**（调用点在一次问答的收尾，
    这里抛就把已经生成好的回答带崩了）。
    """
    try:
        u = (resp or {}).get("usage") or {}
        return (_to_int(u.get("prompt_tokens")), _to_int(u.get("completion_tokens")))
    except Exception:
        return (0, 0)


def extract_anthropic_usage(resp):
    """从 anthropic SDK 响应的 `usage.input_tokens/output_tokens` 取 `(输入, 输出)`。

    ***注意单位不同***：Anthropic 只给 `input_tokens`，**不含缓存读取**
    （`cache_read_input_tokens` / `cache_creation_input_tokens`）。那几个字段
    计费口径不一样，这里**不合并**——合并了就成了"自己编的输入量"。
    要算缓存的钱请另开一条记录（别偷偷加进 prompt_tokens）。
    同理缺字段返回 `(0, 0)`，不抛。
    """
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return (0, 0)
        return (_to_int(getattr(u, "input_tokens", 0)),
                _to_int(getattr(u, "output_tokens", 0)))
    except Exception:
        return (0, 0)


# ============================================================
#  独立自测入口（纯逻辑，不联网：用完就删的临时账本）
# ============================================================

if __name__ == "__main__":
    import tempfile

    _old_path = USAGE_PATH
    _tmp = tempfile.mkdtemp(prefix="usage_selftest_")
    USAGE_PATH = os.path.join(_tmp, "usage.jsonl")
    try:
        assert summary(7)["calls"] == 0, "空账本应当是 0 次"
        assert price_of("deepseek-chat") == (2.0, 8.0), "价目表读取"
        assert price_of("no-such-model-xyz") is None, "未知模型不许有价目表"
        record("openai", "deepseek-chat", 1000, 500)
        s = summary(7)
        assert s["calls"] == 1 and s["prompt_tokens"] == 1000, "记账能被读回"
        assert s["by_model"]["deepseek-chat"]["est_cost"] is not None, "能算钱"
        # 坏行不能让统计崩
        with open(USAGE_PATH, "a", encoding="utf-8") as _fh:
            _fh.write("{半截 JSON\n")
        s2 = summary(7)
        assert s2["calls"] == 1 and s2["bad_lines"] == 1, "坏行跳过并计数"
        assert isinstance(summarize(7), str), "summarize 返回文本"
        # 只落 6 个键，不许有密钥类字段
        with open(USAGE_PATH, encoding="utf-8") as _fh:
            _keys = set(json.loads(_fh.readline()).keys())
        assert _keys == {"ts", "provider", "model", "prompt_tokens",
                         "completion_tokens", "kind"}, f"落盘键集合不对：{_keys}"
        assert extract_openai_usage({"usage": {"prompt_tokens": 7}}) == (7, 0)
        assert extract_openai_usage({}) == (0, 0)
        assert extract_anthropic_usage(object()) == (0, 0)
        print("usage.py 自测通过 ✅（临时目录：" + _tmp + "）")
    finally:
        USAGE_PATH = _old_path
        import shutil
        shutil.rmtree(_tmp, ignore_errors=True)
