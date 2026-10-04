"""usage.py（用量/费用统计）与 redact.py（送云端前脱敏）的**独立**回归自测。

跑法：
    .venv/Scripts/python.exe selftest_redact_usage.py

不联网、不碰微信、不碰 hook（30001 端口）、不需要微信在跑。
**唯一的落盘动作**：在系统临时目录里建一个用完就删的 `usage.jsonl`
（monkeypatch `usage.USAGE_PATH`），仓库里的 `data/usage.jsonl` 一个字节都不动
（脚本结尾会实测它的 mtime/size 没变，作为"没污染"的证据）。

覆盖范围（判据都是本脚本自己写的）：
  * usage.record → summary 能读回；窗口过滤（7 天 / 全部）
  * **落盘键集合必须是 6 个**：ts/provider/model/prompt_tokens/completion_tokens/kind
    —— 尤其断言**没有** api_key / base_url / 任何消息内容字段（隐私边界）
  * 坏行（半截 JSON / 缺 ts / 非 dict / 空行）不让 summary 崩，且 bad_lines 计数正确
  * 未知模型：price_of 返回 None；summarize 里**明说没有价目表**；
    est_cost 在"一个能算的都没有"时是 None（不是 0——0 会被读成"不花钱"）
  * record 的容错：坏 token 值（"abc" / 负数）当 0；USAGE_PATH 不可写时
    **只打告警、不抛异常**（主流程不能被统计带崩）
  * extract_openai_usage / extract_anthropic_usage：缺字段返回 (0,0)、不抛
  * redact：手机号/身份证/银行卡/邮箱/IPv4 各自命中且格式正确
  * redact 反例（**不许打码**）：2026、日期、¥3200、3200.50、12345、QQ 号、
    时间戳、消息序号、11 位但非手机号段的数字、浮点小数尾
  * redact 幂等（打过码的文本再过一次不该再命中）
  * enabled 的 fail-safe：{}、缺 privacy、privacy 是字符串、redact: "true"/1/None
    一律当**关闭**；只有 `is True` 才算开
  * 性能：4 万字符的"全是反例"文本必须毫秒级跑完（正则没有灾难性回溯）
"""
import json
import json
import os
import shutil
import sys
import tempfile
import time

import redact
import usage

_pass = 0
_fail = []


def chk(cond, msg):
    """打印一项判据。失败不中断，最后统一 exit 1（一次看到全部问题）。"""
    global _pass
    if cond:
        _pass += 1
        print("  ok    " + msg)
    else:
        _fail.append(msg)
        print("  FAIL  " + msg)


def sec(title):
    print()
    print(title)


# 开脱敏的配置，后面反复用
ON = {"privacy": {"redact": True}}


# ==========================================================================
sec("【0】先用一眼能看懂的样例跑一遍（人工可核对的输出）")

_sample = ("张三 手机13812341234 身份证110101199001011234 "
           "卡6222021234567890123 邮箱zhangsan@example.com 内网 192.168.1.7")
_masked, _hits = redact.redact(_sample, ON)
print("    原文：" + _sample)
print("    脱敏：" + _masked)
chk(_hits == 5, f"5 处敏感信息全部命中（实际 {_hits}）")


# ==========================================================================
sec("【1】redact.enabled —— 严格按 is True，坏配置一律当关闭")

chk(redact.enabled(ON) is True, 'privacy.redact = True → 开')
chk(redact.enabled({}) is False, "空配置 → 关")
chk(redact.enabled(None) is False, "cfg=None → 关（不抛）")
chk(redact.enabled({"privacy": {}}) is False, "有 privacy 段但没写 redact → 关")
chk(redact.enabled({"privacy": "yes"}) is False, "privacy 是字符串 → 关（不抛）")
chk(redact.enabled({"privacy": None}) is False, "privacy 是 None → 关")
chk(redact.enabled({"privacy": True}) is False, "privacy 是布尔 → 关")
chk(redact.enabled({"privacy": {"redact": "true"}}) is False,
    'redact: "true"（字符串）→ 关，不许被 bool("true") 骗成开')
chk(redact.enabled({"privacy": {"redact": "false"}}) is False, 'redact: "false" → 关')
chk(redact.enabled({"privacy": {"redact": 1}}) is False, "redact: 1（数字）→ 关")
chk(redact.enabled({"privacy": {"redact": None}}) is False, "redact: None → 关")
chk(redact.enabled("配置") is False, "cfg 是字符串 → 关（不抛）")
chk(redact.enabled({"privacy": {"redact": True, "x": 1}}) is True,
    "多写了别的键不影响判定")


# ==========================================================================
sec("【2】redact.redact —— 关闭时一个字符都不许动")

chk(redact.redact(_sample) == (_sample, 0), "cfg=None → 原样返回、命中 0")
chk(redact.redact(_sample, {}) == (_sample, 0), "空配置 → 原样返回")
chk(redact.redact(_sample, {"privacy": {"redact": "true"}}) == (_sample, 0),
    '配置写成字符串 "true" → 仍然原样返回（fail-safe 方向是"不改变现有行为"）')
chk(redact.redact("") == ("", 0), "空字符串")
chk(redact.redact(None, ON) == (None, 0), "None 原样返回、不抛")
chk(redact.redact(12345, ON) == (12345, 0), "非字符串原样返回、不抛")


# ==========================================================================
sec("【3】redact —— 各类敏感信息的格式（保留可读性）")

_m, _n = redact.redact("我手机13812341234，另一个 15900001111。", ON)
chk(_n == 2, f"两个手机号都命中（{_n}）")
chk("138****1234" in _m and "159****1111" in _m, f"手机号保留前 3 后 4：{_m}")

_m, _n = redact.redact("身份证 110101199001011234 请查一下", ON)
chk(_n == 1 and "110101********1234" in _m, f"身份证留前 6 后 4：{_m}")

_m, _n = redact.redact("尾号 6222021234567890123 的卡", ON)
chk(_n == 1 and _m.endswith("0123 的卡"), f"银行卡只留后 4 位：{_m}")
chk("6222021234567890123" not in _m, "银行卡原文没漏出去")

_m, _n = redact.redact("mail: zhangsan@example.com", ON)
chk(_n == 1 and "zh***@example.com" in _m, f"邮箱保留前 2 位 + 域名：{_m}")

_m, _n = redact.redact("服务器 192.168.1.7 和 8.8.8.8", ON)
chk(_n == 2 and "192.168.*.*" in _m and "8.8.*.*" in _m,
    f"IPv4 只留前两段：{_m}")

# 带 X 校验位的身份证（身份证最后一位可能是 X）
_m, _n = redact.redact("证号 11010119900101123X", ON)
chk(_n == 1 and "110101********123X" in _m, f"身份证末位 X 也能匹配：{_m}")

# 混合长文本：一次把五类都命中，且原文关键词一个不剩
_big = _sample
_m2, _n2 = redact.redact(_big, ON)
for _secret in ["13812341234", "110101199001011234", "6222021234567890123",
                "zhangsan@example.com", "192.168.1.7"]:
    chk(_secret not in _m2, f"原文里的 {_secret} 没出现在脱敏结果里")

# 幂等：打过码的文本再过一次不该再命中（* 不是数字，边界自然成立）
_m3, _n3 = redact.redact(_masked, ON)
chk(_n3 == 0 and _m3 == _masked, f"幂等：脱敏结果再脱敏不变（命中 {_n3}）")


# ==========================================================================
sec("【4】redact 反例 —— 这些**绝不能**被打码（打码就毁可读性）")

_cases = [
    ("2026", "普通年份"),
    ("2026年10月1日 开会", "日期"),
    ("2026-10-01", "带横线的日期"),
    ("花了¥3200", "金额（带货币符号）"),
    ("扣了 3200.50 元", "金额（小数）"),
    ("12345", "5 位数字"),
    ("123456", "6 位数字"),
    ("QQ 123456789", "QQ 号（9 位）"),
    ("时间戳 1759286400", "epoch 时间戳（10 位）"),
    ("消息序号 8821", "消息序号 / rowid"),
    ("订单号 12345678901", "11 位但不是手机号段（12 开头）"),
    ("余额 123456789012345", "15 位数字（够不到银行卡 16 位门槛）"),
    ("小数点尾 3200.1234567890123", "长数字串但前面是小点（金额形态）"),
    ("版本号 1.2.3.4", "版本号（和 IPv4 同形，靠前面的词区分）"),
    ("手机上的版本 10.1.2.3", "带空格的版本号"),
    ("version 2.0.0.1", "英文 version 前缀"),
    ("v1.2.3.4", "v 前缀的版本号"),
    ("v 1.2.3.4", "v + 空格的版本号（那个空格正是被码掉的部分）"),
    ("Python 3.11.9", "Python 版本（3.11.9 里后两段也别码）"),
    ("内核 5.15.0.1", "内核版本"),
    ("固件 2.3.4.5", "固件版本"),
    ("今天天气不错，我们下午三点见。", "普通句子"),
    ("价格 8 元/百万，输出 16 元/百万", "价目表文本"),
]
for _t, _why in _cases:
    _out, _cnt = redact.redact(_t, ON)
    chk(_cnt == 0 and _out == _t, f"不码：{_why} —— {_t!r}" +
        ("" if _cnt == 0 else f"（实际命中 {_cnt}：{_out!r}）"))

# 15 位老身份证：本站**明确不匹配**（和普通长数字太容易混），钉住这个选择
_out, _cnt = redact.redact("老号 110101900101123", ON)
chk(_cnt == 0, f"15 位老身份证不匹配（宁可漏，不误伤）：{_out!r}")

# IP 的范围校验：299 这种非法段不算 IPv4
_out, _cnt = redact.redact("不是 IP：299.1.1.1", ON)
chk(_cnt == 0, f"非法 IPv4 段（299）不匹配：{_out!r}")
# 但真正的 IP 前后加字也得照码（版本号那道闸不能误伤正常 IP）
_out, _cnt = redact.redact("服务器 192.168.1.7 端口 8080", ON)
chk(_cnt == 1 and "192.168.*.*" in _out,
    f"普通 IP 仍然照码（版本号那道闸没误伤）：{_out!r}")
# 同一句里既有版本号又有真 IP：版本号放过、真 IP 码掉（逐个数判，不是整句放过）
_out, _cnt = redact.redact("Python 3.11.9 连 1.2.3.4 不通", ON)
chk(_cnt == 1 and "Python 3.11.9 连 1.2.*.*" in _out,
    f"版本号放过、同句真 IP 仍码：{_out!r}")


# ==========================================================================
sec("【5】redact 性能 —— 大段「全是反例」的文本不许卡住")

_t0 = time.time()
_bulk = ("2026年10月1日 花了¥3200 QQ 123456789 序号 12345 "
         "时间戳 1759286400 余额 123456789012345 " * 800)
_out, _cnt = redact.redact(_bulk, ON)
_dt = time.time() - _t0
chk(_cnt == 0, f"{len(_bulk)} 字符的反例文本零命中（实际 {_cnt}）")
chk(_dt < 1.0, f"跑得快、没有灾难性回溯（{_dt:.3f}s）")


# ==========================================================================
sec("【6】usage.record → summary —— 落到临时目录，绝不碰仓库 data/usage.jsonl")

# 记住仓库那个账本的状态，结尾用它证明"没污染"
_repo_path = usage.USAGE_PATH
_repo_stat = None
try:
    _st = os.stat(_repo_path)
    _repo_stat = (_st.st_mtime_ns, _st.st_size)
except OSError:
    _repo_stat = None
chk(os.path.basename(_repo_path) == "usage.jsonl"
    and os.path.basename(os.path.dirname(_repo_path)) == "data",
    f"USAGE_PATH 指向 <项目根>/data/usage.jsonl：{_repo_path}")
chk(os.path.dirname(_repo_path) == usage.PROJECT_DIR
    or os.path.dirname(os.path.dirname(_repo_path)) == usage.PROJECT_DIR,
    "USAGE_PATH 用 __file__ 拼出来的（不是写死盘符）")

_tmpdir = tempfile.mkdtemp(prefix="selftest_usage_")
usage.USAGE_PATH = os.path.join(_tmpdir, "usage.jsonl")
try:
    chk(usage.summary(7)["calls"] == 0, "还没记账 → calls=0")
    chk(usage.summary(7)["est_cost"] is None, "没数据 → est_cost 是 None（不是 0）")
    chk("还没有记录" in usage.summarize(7), "空账本的文案是「还没有记录」")

    usage.record("openai", "deepseek-chat", 1000, 500)
    usage.record("openai", "deepseek-chat", 2000, 1000)
    usage.record("anthropic", "claude-sonnet-5", 300, 200, kind="chat")
    _s = usage.summary(7)
    chk(_s["calls"] == 3, f"三次记账都读回（{_s['calls']}）")
    chk(_s["prompt_tokens"] == 3300 and _s["completion_tokens"] == 1700,
        f"token 合计对：{_s['prompt_tokens']}/{_s['completion_tokens']}")
    chk(set(_s["by_model"]) == {"deepseek-chat", "claude-sonnet-5"},
        f"按模型分组：{sorted(_s['by_model'])}")
    chk(_s["by_model"]["deepseek-chat"]["calls"] == 2, "分组内的调用次数对")
    chk(_s["priced"] == ["deepseek-chat"], f"有价目表的模型：{_s['priced']}")
    chk(_s["unpriced"] == ["claude-sonnet-5"], f"没价目表的模型：{_s['unpriced']}")

    # 费用必须真的按价目表算出来（deepseek-chat: 输入 2 元/百万，输出 8 元/百万）
    _expect = (3000 * 2.0 + 1500 * 8.0) / 1e6
    _got = _s["by_model"]["deepseek-chat"]["est_cost"]
    chk(_got is not None and abs(_got - _expect) < 1e-12,
        f"deepseek-chat 估算费用 = {_got}（期望 {_expect}）")
    chk(_s["est_cost"] is not None and abs(_s["est_cost"] - _expect) < 1e-12,
        f"合计只含能算钱的部分 = {_s['est_cost']}")

    # 落盘的键集合：**这是隐私边界，钉死**
    with open(usage.USAGE_PATH, encoding="utf-8") as _fh:
        _first = json.loads(_fh.readline())
    chk(set(_first.keys()) == {"ts", "provider", "model", "prompt_tokens",
                               "completion_tokens", "kind"},
        f"落盘键集合恰好是 6 个约定键：{sorted(_first.keys())}")
    for _bad in ["api_key", "apikey", "key", "base_url", "content", "text",
                 "messages", "prompt", "answer", "url"]:
        chk(_bad not in _first, f"落盘内容里没有 {_bad} 字段")
    with open(usage.USAGE_PATH, encoding="utf-8") as _fh:
        _raw = _fh.read()
    chk("sk-" not in _raw and "Bearer" not in _raw and "http" not in _raw,
        "整个账本里没有密钥/URL 痕迹")

    # 传给 record 的密钥类参数根本不存在——签名就 5 个参数
    _args = usage.record.__code__.co_varnames[:usage.record.__code__.co_argcount]
    chk(_args == ("provider", "model", "prompt_tokens", "completion_tokens", "kind"),
        f"record 的参数名单：{_args}")

    # 窗口过滤：7 天内的都在，写一条 30 天前的就不该进 7 天窗口
    with open(usage.USAGE_PATH, "a", encoding="utf-8") as _fh:
        _fh.write(json.dumps({"ts": int(time.time()) - 30 * 86400,
                              "provider": "openai", "model": "old-model",
                              "prompt_tokens": 999, "completion_tokens": 999,
                              "kind": "chat"}, ensure_ascii=False) + "\n")
    _s7 = usage.summary(7)
    chk(_s7["calls"] == 3 and "old-model" not in _s7["by_model"],
        "30 天前的记录不进 7 天窗口")
    _sall = usage.summary(0)
    chk(_sall["calls"] == 4 and "old-model" in _sall["by_model"],
        "days=0 → 统计全部（含老账）")
    chk("全部时间" in usage.summarize(0), "days=0 的文案写明窗口是全部时间")
    chk("最近 7 天" in usage.summarize(7), "days=7 的文案写明窗口")

    # 坏配置的 days 不许崩
    chk(usage.summary("abc")["days"] == 7, "days 传坏值 → 退回默认 7")
    chk(isinstance(usage.summarize(None), str), "summarize(None) 返回文本、不抛")
finally:
    usage.USAGE_PATH = _repo_path
    shutil.rmtree(_tmpdir, ignore_errors=True)


# ==========================================================================
sec("【7】坏行不让 summary 崩（半截 JSON / 缺字段 / 非 dict / 空行）")

_tmpdir = tempfile.mkdtemp(prefix="selftest_usage_bad_")
usage.USAGE_PATH = os.path.join(_tmpdir, "usage.jsonl")
try:
    usage.record("openai", "deepseek-chat", 100, 50)   # 唯一一条好记录
    with open(usage.USAGE_PATH, "a", encoding="utf-8") as _fh:
        _fh.write('{"ts": 1, "model": "x", "prompt_tokens": 5\n')  # 半截 JSON
        _fh.write('这不是 JSON\n')                                  # 纯垃圾
        _fh.write('[1, 2, 3]\n')                                    # JSON 但不是 dict
        _fh.write('{"model": "no-ts", "prompt_tokens": 7}\n')       # 缺 ts
        _fh.write('{"ts": "不是数字", "model": "bad-ts"}\n')         # ts 是字符串
        _fh.write('\n')                                             # 空行（不算坏行）
    with open(usage.USAGE_PATH, encoding="utf-8") as _fh:
        _nlines = sum(1 for _ in _fh)
    _s = usage.summary(7)
    chk(_s["calls"] == 1, f"好记录照常统计（calls={_s['calls']}）")
    # 5 行坏数据：半截 JSON / 纯垃圾 / JSON 但不是 dict / 缺 ts / ts 不是数字
    # （末尾那个空行**不算**坏行——空行不是损坏）
    chk(_s["bad_lines"] == 5, f"坏行计数 = 5（实际 {_s['bad_lines']}）")
    chk(_nlines == 7, f"账本里共 7 行（1 好 + 5 坏 + 1 空行）：{_nlines}")
    chk(_s["prompt_tokens"] == 100, "坏行不影响 token 合计")
    _txt = usage.summarize(7)
    chk(isinstance(_txt, str) and "deepseek-chat" in _txt, "summarize 照样出文本")
    chk("读不动" in _txt, f"文案里如实说了有坏行被跳过：{'读不动' in _txt}")

    # 坏 token 值：不许抛，当 0
    usage.record("openai", "deepseek-chat", "abc", -5)
    usage.record("openai", "deepseek-chat", None, None)
    _s2 = usage.summary(7)
    chk(_s2["calls"] == 3, f"坏 token 值的记录仍然入账（calls={_s2['calls']}）")
    chk(_s2["by_model"]["deepseek-chat"]["prompt_tokens"] == 100,
        "坏 token 值当 0（负数也不许倒扣）")

    # 全是坏行 → 不许崩，给空统计，而且**不许说"还没有记录"**
    # （那等于把"账本被写坏了"说成"没花过钱"，是静默失败）
    usage.USAGE_PATH = os.path.join(_tmpdir, "allbad.jsonl")
    with open(usage.USAGE_PATH, "w", encoding="utf-8") as _fh:
        _fh.write("垃圾\n垃圾\n")
    _s3 = usage.summary(7)
    chk(_s3["calls"] == 0 and _s3["bad_lines"] == 2,
        f"全是坏行也不崩：calls={_s3['calls']} bad_lines={_s3['bad_lines']}")
    _t3 = usage.summarize(7)
    chk(isinstance(_t3, str) and _t3, "全坏行时文案仍可用")
    chk("没有能读出来的用量记录" in _t3 and "2 行读不动" in _t3,
        f"全坏行时如实说「读不出记录 + N 行读不动」：{_t3.splitlines()[2]!r}")
    chk("还没有记录" not in _t3,
        "全坏行时不许说「还没有记录」（那把写坏账本说成了没花过钱）")

    # 真正的空账本（文件不存在）→ 才是「还没有记录」
    usage.USAGE_PATH = os.path.join(_tmpdir, "empty.jsonl")
    _t4 = usage.summarize(7)
    chk("还没有记录" in _t4, "文件不存在时才说「还没有记录」")

    # 模型名缺失 → 不许崩（归到占位名，且明说没价目表）
    usage.USAGE_PATH = os.path.join(_tmpdir, "nomodel.jsonl")
    with open(usage.USAGE_PATH, "w", encoding="utf-8") as _fh:
        _fh.write(json.dumps({"ts": int(time.time()), "prompt_tokens": 10},
                             ensure_ascii=False) + "\n")
    _s4 = usage.summary(7)
    chk(_s4["calls"] == 1 and _s4["unpriced"], f"缺模型名也不崩：{_s4['by_model']}")

    # USAGE_PATH 不可写 → record **只告警、不抛**（主流程不能被统计带崩）
    usage.USAGE_PATH = os.path.join(_tmpdir, "no_such_dir", "usage.jsonl")
    _threw = None
    try:
        usage.record("openai", "deepseek-chat", 1, 1)
    except Exception as _e:                     # noqa: BLE001 —— 这里就是要抓一切
        _threw = _e
    chk(_threw is None, f"record 写不进去也不抛异常（实际抛了 {_threw!r}）")

    # summary 对"文件存在但是个目录"这种怪情况也不许崩
    usage.USAGE_PATH = _tmpdir
    _threw2 = None
    try:
        _s5 = usage.summary(7)
    except Exception as _e:                     # noqa: BLE001
        _threw2 = _e
        _s5 = None
    chk(_threw2 is None, f"USAGE_PATH 是目录时 summary 不崩（实际抛了 {_threw2!r}）")
    chk(_s5 is not None and _s5["calls"] == 0, "怪路径下返回空统计")
finally:
    usage.USAGE_PATH = _repo_path
    shutil.rmtree(_tmpdir, ignore_errors=True)


# ==========================================================================
sec("【8】价目表：未知模型返回 None；summarize 明说「没有价目表」")

chk(usage.price_of("deepseek-chat") == (2.0, 8.0), "deepseek-chat 有价目表")
chk(usage.price_of("deepseek-reasoner") == (4.0, 16.0), "deepseek-reasoner 有价目表")
# 2026-10-02 换成的现役模型（官方定价页核过，按高峰价填）
chk(usage.price_of("deepseek-flash") == (2.0, 8.0), "deepseek-flash 有价目表（收图的那个）")
chk(usage.price_of("deepseek-v4-pro") == (9.0, 27.0), "deepseek-v4-pro 有价目表")
chk(usage.price_of("DeepSeek-Flash") == (2.0, 8.0), "flash 也大小写不敏感")
chk(usage.price_of("DeepSeek-Chat") == (2.0, 8.0), "大小写不敏感")
chk(usage.price_of("  deepseek-chat  ") == (2.0, 8.0), "前后空白不影响")
chk(usage.price_of("openai/deepseek-chat") == (2.0, 8.0), "已知 provider 前缀会被剥掉")
# 2026-10-04 换成的现役模型：智谱 glm-4-flash-250414（官方定价页标「免费」→ 单价 0）
chk(usage.price_of("glm-4-flash-250414") == (0.0, 0.0), "glm-4-flash-250414 有价目表（免费 = 0）")
chk(usage.price_of("glm-4.7-flash") == (0.0, 0.0), "glm-4.7-flash 有价目表（免费 = 0）")
chk(usage.price_of("zhipu/glm-4-flash-250414") == (0.0, 0.0), "zhipu/ 前缀会被剥掉")
for _unknown in ["gpt-4o", "claude-sonnet-5", "qwen-plus", "moonshot-v1-8k",
                 "glm-4-flash", "glm-4-plus", "no-such-model-xyz", "", None, 123]:
    chk(usage.price_of(_unknown) is None,
        f"没有价目表就返回 None，不猜价：{_unknown!r}")
chk(usage.price_of("deepseek-chat-v3") is None,
    "同系列但不确定的型号也不许套用价格（deepseek-chat-v3）")

# 只报 token 的那种文本
_tmpdir = tempfile.mkdtemp(prefix="selftest_usage_price_")
usage.USAGE_PATH = os.path.join(_tmpdir, "usage.jsonl")
try:
    usage.record("anthropic", "claude-sonnet-5", 1000, 500)
    _txt = usage.summarize(7)
    print("    summarize 输出（未知模型）↓")
    for _ln in _txt.splitlines():
        print("      " + _ln)
    chk("没有价目表" in _txt, "明说了「没有价目表」")
    chk("只报 token 不算钱" in _txt, "明说了只报 token 不算钱")
    chk("claude-sonnet-5" in _txt, "把没有价目表的模型名列出来了")
    chk("1000" in _txt and "500" in _txt, "token 数照样报出来")
    chk("¥" not in _txt, f"算不出钱时不许出现任何金额（实际文本里有 ¥）")
    chk("本地" in _txt and "估算" in _txt, "注明这是本地记录/估算，不是账单")
    chk("失败" in _txt, "注明统计不含失败的调用")

    # 混合：一个能算一个不能算 → 报能算的，同时列出不能算的
    usage.record("openai", "deepseek-chat", 1000, 500)
    _txt2 = usage.summarize(7)
    chk("¥" in _txt2 and "没有价目表" in _txt2, "混合时既报钱也列无价目表的模型")
    chk("只含上表里能算钱的模型" in _txt2, "明说金额只覆盖能算的那部分")
    # 微信消息体量：别把 /用量 的回执搞成巨长的东西
    chk(len(_txt2) < 1500, f"文案体量可控（{len(_txt2)} 字）")

    # 免费模型（glm-4-flash-250414，单价 0）：**必须算得出 0 元**。
    # 否则会掉进 budget_status 的 `spent is None` 那条路，把「免费」说成「算不出花费」。
    usage.record("openai", "glm-4-flash-250414", 1000000, 500000)
    _free = usage.summary(7)
    chk(_free["by_model"]["glm-4-flash-250414"]["est_cost"] == 0.0,
        "免费模型的 est_cost 是 0.0，不是 None")
    chk("glm-4-flash-250414" in _free["priced"], "免费模型进 priced")
    chk("glm-4-flash-250414" not in _free["unpriced"], "免费模型不该被当成「没价目表」")
finally:
    usage.USAGE_PATH = _repo_path
    shutil.rmtree(_tmpdir, ignore_errors=True)


# ==========================================================================
sec("【9】从各家响应里取 usage")

chk(usage.extract_openai_usage(
    {"usage": {"prompt_tokens": 12, "completion_tokens": 34}}) == (12, 34),
    "OpenAI 兼容响应正常取值")
chk(usage.extract_openai_usage({"usage": {"prompt_tokens": 7}}) == (7, 0),
    "缺 completion_tokens → 0")
chk(usage.extract_openai_usage({"usage": {"completion_tokens": 7}}) == (0, 7),
    "缺 prompt_tokens → 0")
chk(usage.extract_openai_usage({}) == (0, 0), "没有 usage → (0,0)")
chk(usage.extract_openai_usage({"usage": None}) == (0, 0), "usage 是 None → (0,0)")
chk(usage.extract_openai_usage(None) == (0, 0), "resp=None → (0,0)，不抛")
chk(usage.extract_openai_usage({"usage": "oops"}) == (0, 0),
    "usage 是字符串也不抛")
chk(usage.extract_openai_usage({"usage": {"prompt_tokens": "abc"}}) == (0, 0),
    "token 是脏值 → 0，不抛")


class _U:
    input_tokens = 100
    output_tokens = 40


class _Resp:
    usage = _U()


class _EmptyResp:
    pass


chk(usage.extract_anthropic_usage(_Resp()) == (100, 40),
    "anthropic SDK 响应正常取值")
chk(usage.extract_anthropic_usage(_EmptyResp()) == (0, 0), "没有 usage 属性 → (0,0)")
chk(usage.extract_anthropic_usage(None) == (0, 0), "None → (0,0)，不抛")


# ==========================================================================
sec("【10】redact.patterns 的结构约定")

_pats = redact.patterns()
_names = [n for n, _ in _pats]
chk(isinstance(_pats, list) and len(_pats) >= 5, f"至少 5 条规则：{_names}")
for _want in ["手机号", "身份证", "银行卡", "邮箱", "IPv4"]:
    chk(_want in _names, f"有「{_want}」这条规则")
chk(all(hasattr(rx, "sub") for _, rx in _pats), "每条都是编译好的正则（有 .sub）")
chk(_names.index("身份证") < _names.index("银行卡"),
    "身份证规则排在银行卡之前（18 位别被银行卡规则先吃掉）")


# ==========================================================================
sec("【11】消费预算闸（/预算）——到上限拒绝调用，算不准就说不准")

_budget_dir = tempfile.mkdtemp(prefix="selftest_usage_budget_")
_budget_path = os.path.join(_budget_dir, "budget_usage.jsonl")
usage.USAGE_PATH = _budget_path


def _brow(model, p, c):
    with open(_budget_path, "a", encoding="utf-8") as _fh:
        _fh.write(json.dumps({"ts": int(time.time()), "provider": "openai",
                              "model": model, "prompt_tokens": p,
                              "completion_tokens": c, "kind": "chat"}) + "\n")


# 默认（daily_cost=0）不许拦，也不许多说一个字——默认行为必须和以前一模一样
chk(usage.daily_limit({}) == 0.0, "默认上限是 0（不限）")
chk(usage.budget_block_text({}) is None, "没开闸 → 不拦")
chk(usage.budget_block_text({"budget": {"daily_cost": 0}}) is None, "显式 0 → 不拦")

# 写一笔可计价的账（deepseek-chat 在价目表里）
open(_budget_path, "w").close()
_brow("deepseek-chat", 1000000, 0)
_s = usage.summary(days=1)
chk(_s["est_cost"] is not None and _s["est_cost"] > 0,
    f"有价目表的模型 → 算得出花费（{_s['est_cost']}）")

chk(usage.budget_block_text({"budget": {"daily_cost": _s["est_cost"] * 10}}) is None,
    "没到上限 → 不拦")

_big = usage.budget_block_text({"budget": {"daily_cost": _s["est_cost"] / 2}})
chk(_big is not None, "超过上限 → 拦")
chk("没有调用模型" in _big, f"拦的文案要说清「这次没调模型」：{_big[:40]!r}")
chk("最近 24 小时" in _big, "要说清窗口是「最近 24 小时」（不许写成「今天」）")
chk("budget.daily_cost" in _big, "要点出是哪个配置键在拦，用户才知道去哪儿改")

# 写歪的上限：一律按「不限」，但绝不静默（daily_limit 会打印告警）
chk(usage.daily_limit({"budget": {"daily_cost": "二十块"}}) == 0.0, "非数字 → 按不限")
chk(usage.daily_limit({"budget": {"daily_cost": float("nan")}}) == 0.0, "NaN → 按不限")
chk(usage.daily_limit({"budget": {"daily_cost": float("inf")}}) == 0.0, "inf → 按不限")
chk(usage.daily_limit({"budget": {"daily_cost": -5}}) == 0.0, "负数 → 按不限")
chk(usage.daily_limit({"budget": "这不是字典"}) == 0.0, "budget 段不是字典 → 按不限")

# 全是没价目表的模型 → 算不出花费 → **不拦**，但必须如实说算不出
open(_budget_path, "w").close()
_brow("某厂-自建模型", 500, 500)
_lim = {"budget": {"daily_cost": 0.0001}}
_lim_v, _spent, _blocked, _note = usage.budget_status(_lim)
chk(_lim_v > 0, "上限读出来了")
chk(_spent is None, "窗口内全是没价目表的模型 → spent 是 None（不是 0）")
chk(_blocked is False, "算不出花费时**不拦**（拦错了就等于整台机器没法用）")
chk("算不出" in (_note or ""), f"但要如实说明算不出：{_note!r}")
chk(usage.budget_block_text(_lim) is None, "算不出 → 不拦（且文案为空）")

# 有价目的表 + 有没价目表的混在一起：能算的部分照拦，但要说清少算了
open(_budget_path, "w").close()
_brow("deepseek-chat", 1000000, 0)
_brow("某厂-自建模型", 500, 500)
_v2, _sp2, _bl2, _nt2 = usage.budget_status({"budget": {"daily_cost": 0.00001}})
chk(_sp2 is not None and _bl2 is True, "能算的部分超了 → 拦")
chk("没算进去" in (_nt2 or ""), f"要明说有一部分没算进去：{_nt2!r}")

# 账本根本不存在 → 不拦、不抛
usage.USAGE_PATH = os.path.join(_budget_dir, "no_such_dir", "none.jsonl")
try:
    chk(usage.budget_block_text(_lim) is None, "账本不存在 → 不拦、不抛")
except Exception as _e:
    chk(False, f"账本不存在时不该抛：{_e!r}")

# /预算 的展示文案：关着和开着都要能读
usage.USAGE_PATH = _budget_path
_txt_off = usage.budget_text({"budget": {"daily_cost": 0}})
chk("没开" in _txt_off and "/预算" in _txt_off, f"关着时告诉你怎么开：{_txt_off[:50]!r}")
_txt_on = usage.budget_text({"budget": {"daily_cost": 0.00001}})
chk("已用" in _txt_on, f"开着时报已用多少：{_txt_on[:60]!r}")

usage.USAGE_PATH = _repo_path
shutil.rmtree(_budget_dir, ignore_errors=True)


# ==========================================================================
sec("【12】没污染仓库的 data/usage.jsonl")

_new_stat = None
try:
    _st2 = os.stat(_repo_path)
    _new_stat = (_st2.st_mtime_ns, _st2.st_size)
except OSError:
    _new_stat = None
chk(_new_stat == _repo_stat,
    f"仓库账本状态没变（前 {_repo_stat} / 后 {_new_stat}）")
chk(usage.USAGE_PATH == _repo_path, "USAGE_PATH 已还原成仓库路径")


# ==========================================================================
print()
print(f"通过 {_pass} 项，失败 {len(_fail)} 项。")
if _fail:
    print(f"失败 {len(_fail)} 项 ❌")
    for _f in _fail:
        print("  - " + _f)
    sys.exit(1)
print("全部通过 ✅")
