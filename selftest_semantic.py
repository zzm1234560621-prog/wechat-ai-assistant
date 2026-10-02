"""semantic.py 自测：本地语义检索的**逻辑与诚实性**（不需要模型、不联网、不碰微信）。

这一份**故意不依赖真模型**：所有「检索算得对不对」和「出错时说得实不实」的部分，
都用注入的假嵌入器验证。真正需要真模型的那一步（下模型、嵌出有意义的向量）
只能由用户跑一次 `--setup` + `--build` 才算验过——**本文件不假装验过它**。

最要紧的一条断言在 T5：**索引没建时必须报错，绝不能悄悄退回关键词搜索。**
悄悄退化是最坏的一种失效：用户以为自己用的是语义检索，然后奇怪为什么
「换个说法就搜不到」——而他根本不知道实际跑的是别的。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import semantic      # noqa: E402

_PASS = 0
_OK = True


def check(label, cond, extra=""):
    global _PASS, _OK
    _PASS += 1
    if cond:
        print(f"  ✅ {label}")
    else:
        _OK = False
        print(f"  ❌ {label}" + (f"  → {extra}" if extra else ""))
    return bool(cond)


def sec(t):
    print(f"\n── {t} ──")


class FakeEmb:
    """确定性假嵌入器：文本 → 预先给好的向量（没给就是全 0）。"""

    def __init__(self, table=None, dim=3, bad_count=False, bad_dim=False):
        self.table = table or {}
        self.dim = dim
        self.bad_count = bad_count
        self.bad_dim = bad_dim

    def encode(self, texts):
        if self.bad_count:
            return [[0.0] * self.dim]
        out = []
        for i, t in enumerate(texts):
            if self.bad_dim and i == 1:
                out.append([0.0] * (self.dim + 1))
            else:
                out.append(list(self.table.get(t, [0.0] * self.dim)))
        return out


def _cfg(tmp, **sem):
    d = {"index_dir": tmp, "model": "test-model", "min_score": 0.30, "topk": 6}
    d.update(sem)
    return {"semantic": d}


def _write_index(tmp, index):
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)


def _mk(docs, dim=3, model="test-model"):
    return {"model": model, "dim": dim, "built_at": 1.0, "count": len(docs),
            "docs": docs}


def t1_cfg_and_math():
    sec("T1 · 配置合并与余弦相似度")
    c = semantic.cfg_of({})
    check("默认 enabled=False（没下模型时打开只会让人困惑）",
          c["enabled"] is False, c["enabled"])
    check("默认 topk/min_score 有值", c["topk"] == 6 and c["min_score"] == 0.30, c)
    c2 = semantic.cfg_of({"semantic": {"topk": 3, "enabled": True}})
    check("用户配置覆盖默认", c2["topk"] == 3 and c2["enabled"] is True, c2)
    check("覆盖不影响没写的项", c2["min_score"] == 0.30, c2)

    check("同向量 → 1.0", abs(semantic.cosine([1, 0], [1, 0]) - 1.0) < 1e-9)
    check("正交 → 0.0", abs(semantic.cosine([1, 0], [0, 1])) < 1e-9)
    check("反向 → -1.0", abs(semantic.cosine([1, 0], [-1, 0]) + 1.0) < 1e-9)
    check("长度不一致 → 0（不当成相似）", semantic.cosine([1, 0], [1, 0, 0]) == 0.0)
    check("零向量 → 0（不除零）", semantic.cosine([0, 0], [1, 1]) == 0.0)
    check("空向量 → 0", semantic.cosine([], [1]) == 0.0)


def t2_index_read_honest(tmp):
    sec("T2 · 读索引：四种坏情况都**分开说**（不许混成「没有索引」）")
    p = os.path.join(tmp, "idx")

    idx, err = semantic.load_index(_cfg(p))
    check("文件不存在 → 明说「还没有语义索引」", idx is None and "还没有语义索引" in err, err)
    check("并且给出**建索引的命令**", "--build" in err, err)

    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "index.json"), "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    idx, err = semantic.load_index(_cfg(p))
    check("JSON 坏了 → 明说「文件坏了」（不是「没有索引」）",
          idx is None and "坏了" in err, err)
    check("坏文件也要给出重建命令", "--build" in err, err)

    _write_index(p, {"model": "m", "dim": 3})           # 缺 docs
    idx, err = semantic.load_index(_cfg(p))
    check("结构不对 → 明说「结构不对」", idx is None and "结构" in err, err)

    _write_index(p, {"model": "m", "dim": 3, "docs": []})
    idx, err = semantic.load_index(_cfg(p))
    check("docs 空 → 明说「索引是空的」", idx is None and "空" in err, err)

    _write_index(p, {"model": "m", "docs": [{"v": [1, 0, 0]}]})   # 缺 dim
    idx, err = semantic.load_index(_cfg(p))
    check("缺 dim → 明说缺 dim", idx is None and "dim" in err, err)

    _write_index(p, _mk([{"v": [1, 0, 0], "x": "a", "t": 1}]))
    idx, err = semantic.load_index(_cfg(p))
    check("正常索引 → 读得出来", idx is not None and err == "", err)


def t3_save_roundtrip(tmp):
    sec("T3 · 落盘往返（原子写）")
    p = os.path.join(tmp, "rt")
    index = _mk([{"v": [0.1, 0.2, 0.3], "x": "你好", "t": 123, "w": "张三", "s": 1}])
    ok, msg = semantic.save_index(_cfg(p), index)
    check("写好了", ok, msg)
    check("没有留下临时文件", not os.path.exists(os.path.join(p, "index.json.tmp")),
          os.listdir(p))
    got, err = semantic.load_index(_cfg(p))
    check("读回来一模一样", got == index, err)
    check("报告里带条数与大小", "1 条" in msg and "MB" in msg, msg)


def t4_build(tmp):
    sec("T4 · 建索引：上限、空内容、嵌入器不听话时都不许硬建")
    rows = [{"time": 100 + i, "content": f"第{i}条消息", "is_self": i % 2,
             "who": "张三"} for i in range(6)]
    rows.append({"time": 999, "content": "   ", "is_self": 0})   # 空内容

    cfg = _cfg(tmp, max_messages=100, max_chars=100)
    emb = FakeEmb({m["content"]: [1.0, 0.0, 0.0] for m in rows}, dim=3)
    index, rep = semantic.build(rows, cfg, embedder=emb)
    check("建起来了", index is not None, rep[:120])
    check("空内容被跳过（6 条而不是 7 条）", index and index["count"] == 6, index["count"])
    check("记了 dim", index and index["dim"] == 3, index and index["dim"])
    check("报告里说了跳过几条", "跳过空内容 1 条" in rep, rep[:80])

    # 上限：到顶要**明说没建全**。
    # ⚠️ 上限是对「**取多少条候选**」生效的，空内容还会再被跳过 —— 所以取 3 条候选
    #    只嵌进去 2 条（第 3 条是空内容）。**代码这么写是对的**，报告里也必须报
    #    「实际嵌了几条」而不是「取了几条」（自测第一次跑就是在这个数字上抓出错报的）。
    index2, rep2 = semantic.build(rows, _cfg(tmp, max_messages=3, max_chars=100),
                                  embedder=emb)
    check("到上限 → 取 3 条候选、跳过空内容后嵌 2 条",
          index2 and index2["count"] == 2, index2 and index2["count"])
    check("到上限 → truncated 标记", index2 and index2["truncated"] is True, index2)
    check("到上限 → **明说「没建全」**", "没建全" in rep2, rep2[:200])
    check("报告里的条数是**实际嵌进去的**（2，不是取到的 3）",
          "只嵌了最近 2 条" in rep2, rep2[:240])
    check("topped 时保留的是**最近的**两条",
          index2 and [d["t"] for d in index2["docs"]] == [104, 105],
          index2 and [d["t"] for d in index2["docs"]])

    # 配置被夹取要**告警**（静默改用户配置在本项目禁止）
    _i3, rep3 = semantic.build(rows, _cfg(tmp, max_messages=99999999, max_chars=100),
                               embedder=emb)
    check("max_messages 被夹取 → 报告里告警", "被夹到" in rep3, rep3[-120:])

    # 嵌入器返回条数对不上 → 不建
    i4, rep4 = semantic.build(rows, cfg, embedder=FakeEmb(dim=3, bad_count=True))
    check("嵌入条数对不上 → **不建**", i4 is None and "对不上" in rep4, rep4[:120])
    # 维度不一致 → 不建
    i5, rep5 = semantic.build(rows, cfg, embedder=FakeEmb(dim=3, bad_dim=True))
    check("维度不一致 → **不建**", i5 is None and "维度不一致" in rep5, rep5[:120])

    # 一条可嵌的都没有
    i6, rep6 = semantic.build([{"time": 1, "content": "  "}], cfg, embedder=emb)
    check("没有可嵌内容 → 明说，不建空索引", i6 is None and "没有可嵌入" in rep6,
          rep6[:120])


def t5_search_no_silent_fallback(tmp):
    sec("T5 · 检索：**索引没建就报错，绝不悄悄退回关键词**（本文件最要紧的一条）")
    p = os.path.join(tmp, "s1")
    cfg = _cfg(p, model="test-model", min_score=0.30)
    emb = FakeEmb({"q": [1.0, 0.0, 0.0]}, dim=3)

    hits, err = semantic.search("q", cfg, embedder=emb)
    check("没有索引 → 命中为空", hits == [], hits)
    check("没有索引 → **有错误文本**（不是静默给了别的结果）", bool(err), err)
    check("没有索引 → 错误里点名「不会用语义检索」", "不会用语义检索" in err, err)
    check("没有索引 → 给出建索引命令", "--build" in err, err)

    # 建好之后再搜：按分数排序
    docs = [
        {"v": [1.0, 0.0, 0.0], "x": "完全相关", "t": 300, "w": "张三", "s": 0},
        {"v": [0.8, 0.6, 0.0], "x": "比较相关", "t": 200, "w": "张三", "s": 1},
        {"v": [0.0, 1.0, 0.0], "x": "无关", "t": 100, "w": "李四", "s": 0},
    ]
    _write_index(p, _mk(docs))
    hits, err = semantic.search("q", cfg, embedder=emb)
    check("有索引 → 无错误", err == "", err)
    check("按分数从高到低", [h["text"] for h in hits] == ["完全相关", "比较相关"], hits)
    check("低于 min_score 的被滤掉（无关那条不在）",
          all("无关" != h["text"] for h in hits), hits)
    check("分数带出来了", hits and hits[0]["score"] == 1.0, hits)
    check("谁说的也带出来了", hits[1]["who"] == "张三" and hits[1]["is_self"] is True, hits)

    # topk 生效
    hits3, _e = semantic.search("q", cfg, k=1, embedder=emb)
    check("k=1 只回 1 条", len(hits3) == 1, hits3)

    # min_score 调到**不可能达到**的值 → 允许一条都不回（宁可少给）。
    # （第一次我写 0.999，而完全相关的余弦正好是 1.0 > 0.999，所以它照常返回——
    #  是**我的期望写错了**，不是代码错了。）
    hits4, err4 = semantic.search("q", _cfg(p, min_score=1.5), embedder=emb)
    check("min_score 高到不可能 → 空结果但**没有错误**（空 ≠ 失败）",
          hits4 == [] and err4 == "", (hits4, err4))

    # 模型对不上 → 明说，不给结果
    hits5, err5 = semantic.search("q", _cfg(p, model="另一个模型"), embedder=emb)
    check("索引是别的模型建的 → 报错且**不给结果**", hits5 == [] and "别的模型" in err5,
          err5)
    check("并说清「分数没有意义」", "没有意义" in err5, err5)

    # 维度对不上 → 明说
    hits6, err6 = semantic.search("q", _cfg(p), embedder=FakeEmb({"q": [1.0, 0.0]}, dim=2))
    check("查询维度对不上 → 报错且不给结果", hits6 == [] and "维度对不上" in err6, err6)

    # 空查询
    hits7, err7 = semantic.search("   ", _cfg(p), embedder=emb)
    check("空查询 → 报错", hits7 == [] and "空" in err7, err7)


def t6_model_ready(tmp):
    sec("T6 · 本地模型：只认本地目录（结构上不联网）")
    d = os.path.join(tmp, "m1")
    ok, why = semantic.model_ready(_cfg(tmp, model_dir=d))
    check("目录不存在 → 没就位", not ok, why)
    check("并给出 --setup", "--setup" in why, why)

    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "config.json"), "w").write("{}")
    ok2, why2 = semantic.model_ready(_cfg(tmp, model_dir=d))
    check("目录在但没有权重 → 仍算没就位（别把半成品当成品）", not ok2, why2)
    check("并说清「没有权重文件」", "权重" in why2, why2)

    open(os.path.join(d, "model.safetensors"), "wb").write(b"x")
    ok3, why3 = semantic.model_ready(_cfg(tmp, model_dir=d))
    check("有权重 → 就位", ok3, why3)

    # 没装 sentence-transformers 时要**如实说装什么**，而不是抛异常
    b, err = semantic.load_backend(_cfg(tmp, model_dir=d))
    if b is None:
        check("没装依赖 → 如实说装什么（不给栈）", "pip install" in err, err[:120])
    else:
        check("环境里确实有 sentence-transformers → 加载成功", True)


def t7_status_text(tmp):
    sec("T7 · --status：每一句都可核实")
    p = os.path.join(tmp, "st")
    txt = semantic.status_text(_cfg(p, model_dir=os.path.join(tmp, "nope")))
    check("说模型没有", "没有" in txt, txt[:80])
    check("说索引不可用", "不可用" in txt, txt[:200])
    check("关了要说「聊天里不会用它」", "不会用它" in txt, txt[-200:])

    _write_index(p, _mk([{"v": [1, 0, 0], "x": "a", "t": 1000}]))
    d = os.path.join(tmp, "m2")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "model.bin"), "wb").write(b"x")
    txt2 = semantic.status_text(
        {"semantic": {"index_dir": p, "model_dir": d, "enabled": True}})
    check("模型就位显示 ✅", "就位 ✅" in txt2, txt2[:200])
    check("索引可用显示 ✅ 且给出条数/维度", "可用 ✅" in txt2 and "1 条 × 3 维" in txt2,
          txt2[:300])
    check("开着时不再说「不会用它」", "不会用它" not in txt2, txt2[-200:])


def t8_clamp():
    sec("T8 · 夹取要**报告**，不许静默改用户配置")
    v, capped = semantic._clamp_int(5, 1, 10, 3)
    check("范围内不动", v == 5 and capped is False, (v, capped))
    v2, c2 = semantic._clamp_int(999, 1, 10, 3)
    check("超上限 → 夹到 10 且标记被夹", v2 == 10 and c2 is True, (v2, c2))
    v3, c3 = semantic._clamp_int(0, 2, 10, 3)
    check("低于下限 → 夹到 2 且标记被夹", v3 == 2 and c3 is True, (v3, c3))
    v4, c4 = semantic._clamp_int("不是数字", 1, 10, 7)
    check("不是数字 → 回默认且标记（不静默当成某个数）", v4 == 7 and c4 is True, (v4, c4))


def main():
    print("=" * 60)
    print("semantic.py 自测（不需要模型、不联网、不碰微信）")
    print("=" * 60)
    tmp = tempfile.mkdtemp(prefix="selftest_semantic_")
    try:
        t1_cfg_and_math()
        t2_index_read_honest(tmp)
        t3_save_roundtrip(tmp)
        t4_build(tmp)
        t5_search_no_silent_fallback(tmp)
        t6_model_ready(tmp)
        t7_status_text(tmp)
        t8_clamp()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 60)
    print(f"全部通过 ✅ （{_PASS} 项）" if _OK else f"有失败项 ❌ （{_PASS} 项）")
    print("=" * 60)
    return 0 if _OK else 1


if __name__ == "__main__":
    sys.exit(main())
