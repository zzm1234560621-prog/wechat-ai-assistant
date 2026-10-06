"""本地语义检索（embedding）——**只用本地模型；索引没建就如实说**。

## 这个模块解决什么

`search_in_chat` 走的是微信 fts 的关键词匹配（`acontent MATCH`）。它有个天然短板：
用户问「上次聊到怎么处理那个并发的问题」，而原话里写的是「hook 不能同时查」
——词对不上就搜不到。语义检索就是为了这个：把消息变成向量，按**意思**找。

## 四条硬约束（改之前先读）

1. **绝不静默退回关键词。** 索引没建 / 模型没下 / 索引坏了 → **如实报错并给出下一步命令**。
   悄悄退到关键词搜索是最坏的一种：用户以为自己用的是语义检索（然后奇怪为什么
   「换个说法就搜不到了」），而他根本不知道实际发生的是别的。**这条有断言守着。**
2. **绝不在聊天路径里下模型。** 下模型只由用户显式跑 `--setup` 触发（走
   `HF_ENDPOINT=https://hf-mirror.com`——本机 `huggingface.co` 不通，与 `audio_read` 同一套）。
   在轮询线程里下几百 MB 会把收消息停掉好几分钟。
3. **推理只用本地文件。** 后端从 `semantic.model_dir` 这个**本地目录**加载
   （`local_files_only=True`），结构上不可能联网。
4. **不猜、不含糊。** 索引是别的模型建的、维度对不上、文件坏了——都要**明说**是哪一种，
   而不是返回一堆看起来像结果的垃圾。

## 为什么索引要由命令行建

建索引要遍历历史 + 跑几千次嵌入，是**重活**。本项目只有 `read_worker` 一条后台线程，
而它的铁律是「绝不查库」。所以建索引**跑在命令行上（bot 停着的时候）**，
聊天路径里只提供**查**和**看状态**——这是唯一不违反 hook 铁律的做法。

## 命令行

    python semantic.py --status                  # 模型/索引现在什么状态
    python semantic.py --setup                   # 下本地模型（**只此一条下载路径**）
    python semantic.py --build [--days 90]       # 建索引（bot 要先停）
    python semantic.py --search "那个并发的问题"  # 试搜一条
"""
import json
import math
import os
import sys
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
HF_MIRROR = "https://hf-mirror.com"

# 默认模型：**小而快的中文/多语模型**，384 维，CPU 上够用。
# （换成别的必须在配置里显式改；索引里记了模型名，对不上会如实报错，不会拿旧向量凑。）
DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"

DEFAULTS = {
    "enabled": False,          # 默认关：没下模型就打开只会让人困惑
    "model": DEFAULT_MODEL,
    "model_dir": "./data/models/bge-small-zh",
    "index_dir": "./data/semantic",
    "topk": 6,
    "min_score": 0.30,         # 余弦相似度下限；低于它的不当结果（宁可少给）
    "max_messages": 2000,      # 建索引时最多收多少条（硬上限，超了**明说**没建全）
    "max_chars": 400,          # 每条消息只嵌前多少字（长消息嵌全身很费时且收益低）
}


def cfg_of(cfg=None):
    """把配置里 `semantic:` 段和默认值合起来（**不改用户那份**）。"""
    sec = dict((cfg or {}).get("semantic") or {})
    out = dict(DEFAULTS)
    out.update({k: v for k, v in sec.items() if v is not None})
    return out


def _abs(p):
    p = str(p or "").strip()
    if not p:
        return ""
    return p if os.path.isabs(p) else os.path.join(PROJECT_DIR, p)


def model_dir(cfg=None):
    return _abs(cfg_of(cfg).get("model_dir"))


def index_dir(cfg=None):
    return _abs(cfg_of(cfg).get("index_dir"))


def index_path(cfg=None):
    return os.path.join(index_dir(cfg), "index.json")


def _clamp_int(v, lo, hi, default):
    """夹取并**如实告诉调用方夹了**（静默改用户配置在这个项目里禁止）。

    返回 `(值, 是否被夹取)`。
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default, str(v) not in (None, "", str(default))
    if n < lo:
        return lo, n != lo
    if n > hi:
        return hi, n != hi
    return n, False


class _Backend:
    """本地嵌入后端。`encode(list[str]) -> list[list[float]]`。"""

    def __init__(self, model, name):
        self.model = model
        self.name = name

    def encode(self, texts):
        vecs = self.model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


def model_ready(cfg=None):
    """本地模型在不在。返回 `(在不在, 一句人话)`。**只看本地目录，不联网。**"""
    d = model_dir(cfg)
    if not d:
        return False, "没配 `semantic.model_dir`。"
    if not os.path.isdir(d):
        return False, (f"本地模型目录不存在：{d}\n"
                       f"要下模型（**只此一条下载路径**）：\n"
                       f"  .venv\\Scripts\\python.exe semantic.py --setup")
    # 至少要看到模型权重才认为「像是有」
    try:
        names = os.listdir(d)
    except OSError as e:
        return False, f"模型目录读不到：{d}（{e}）"
    has_w = any(n.endswith((".bin", ".safetensors", ".onnx", ".pt")) for n in names)
    if not has_w:
        return False, (f"模型目录里没有权重文件（只有 {names[:6]}）：{d}\n"
                       f"可能上次没下完。重下一次：semantic.py --setup")
    return True, d


def load_backend(cfg=None):
    """加载**本地**模型。返回 `(后端, 错误文本)`。

    ⚠️ `local_files_only=True`：**结构上不可能联网**。要下模型只能走 `--setup`。
    ⚠️ `sentence-transformers` 是**可选依赖**，在 requirements.txt 里**必须写成注释**
       （写成正式行会让「没装的人装完也起不来」，`faster-whisper` 踩过这个坑）。
    """
    ok, why = model_ready(cfg)
    if not ok:
        return None, why
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None, ("没装 `sentence-transformers`（可选依赖，不随主程序安装）：\n"
                      "  .venv\\Scripts\\python.exe -m pip install sentence-transformers\n"
                      "装完再下模型：semantic.py --setup")
    try:
        m = SentenceTransformer(model_dir(cfg), local_files_only=True)
    except Exception as e:
        return None, (f"从 {model_dir(cfg)} 加载本地模型失败："
                      f"{type(e).__name__}: {str(e)[:160]}\n"
                      f"（本地目录不完整时会这样；重新下：semantic.py --setup）")
    return _Backend(m, model_dir(cfg)), ""


# ── 向量数学 ────────────────────────────────────────────────────────────

def cosine(a, b):
    """余弦相似度。向量已归一化时就是点积，但这里**照样除法兜底**（别假设）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ── 索引读写 ────────────────────────────────────────────────────────────

def save_index(cfg, index):
    """落盘。返回 `(ok, 一句人话)`。**原子写**（同目录临时文件 + replace）。"""
    p = index_path(cfg)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
        os.replace(tmp, p)
        size = os.path.getsize(p)
        return True, f"{len(index.get('docs') or [])} 条，{size / 1048576:.1f} MB → {p}"
    except OSError as e:
        return False, f"索引写盘失败：{e}"


def load_index(cfg=None):
    """读索引。返回 `(索引, 错误文本)`。

    **三种失败必须分开说**（这是本模块最重要的规矩）：
      * 文件不存在   → 「还没建过索引」+ 建索引的命令；
      * JSON 坏了     → 「索引文件坏了」+ 重建的命令（**绝不当成没有索引就悄悄搜关键词**）；
      * 结构不对      → 同上，并说明缺了什么。
    """
    p = index_path(cfg)
    if not os.path.isfile(p):
        return None, ("**还没有语义索引**（所以这次不会用语义检索）。\n"
                      "建索引（要**先停 bot**——遍历历史是重活）：\n"
                      "  .venv\\Scripts\\python.exe semantic.py --build\n"
                      "（建完再看：semantic.py --status）")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, (f"**语义索引文件坏了，读不出来**：{type(e).__name__}: {e}\n"
                      f"文件：{p}\n"
                      f"重建它（不是「没有索引」，是它坏了）：semantic.py --build")
    if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
        return None, (f"**语义索引结构不对**（缺 docs 列表）：{p}\n"
                      f"重建它：semantic.py --build")
    if not data.get("docs"):
        return None, (f"语义索引是空的（一条都没建成）：{p}\n"
                      f"可能上次建的时候一条历史都没取到。重新建：semantic.py --build")
    if not data.get("dim"):
        return None, f"**语义索引缺 dim 字段**（没记录向量维度）：{p}。重建它。"
    return data, ""


# ── 建索引 ──────────────────────────────────────────────────────────────

def build(rows, cfg, embedder=None, db_model=None):
    """把历史行嵌成索引。返回 `(索引, 报告文本)`。

    **在命令行里跑，不在聊天路径里跑**（见模块头注释）。
    `db_model` 是「索引里应该记哪个模型名」——默认用配置里那个。
    `embedder` 可注入（自测用假嵌入器，不需要真模型）。
    """
    c = cfg_of(cfg)
    topk_note = ""
    cap, capped = _clamp_int(c.get("max_messages"), 1, 200000, DEFAULTS["max_messages"])
    if capped:
        topk_note += f"\n⚠️ `semantic.max_messages` 被夹到 {cap}（配置里写的是 {c.get('max_messages')}）。"
    chars, ch_clamped = _clamp_int(c.get("max_chars"), 40, 4000, DEFAULTS["max_chars"])
    if ch_clamped:
        topk_note += f"\n⚠️ `semantic.max_chars` 被夹到 {chars}。"

    take = rows[-cap:] if cap and len(rows) > cap else list(rows)
    truncated = len(rows) > len(take)

    texts, docs = [], []
    skipped = 0
    for m in take:
        body = str(m.get("content") or "").strip()
        if not body:
            skipped += 1
            continue
        texts.append(body[:chars])
        docs.append({"t": int(m.get("time") or 0),
                     "w": str(m.get("who") or m.get("sender_name") or ""),
                     "s": 1 if m.get("is_self") else 0,
                     "x": body[:chars]})

    rep = [f"待嵌入 {len(texts)} 条（跳过空内容 {skipped} 条）"]
    if not texts:
        return None, ("没有可嵌入的消息（全是空内容）。\n"
                      "确认一下会话里确实有文本消息，或换个会话再建。")

    if embedder is None:
        embedder, err = load_backend(cfg)
        if embedder is None:
            return None, err
    try:
        vecs = embedder.encode(texts)
    except Exception as e:
        return None, f"嵌入失败：{type(e).__name__}: {str(e)[:200]}"

    if len(vecs) != len(texts):
        return None, (f"嵌入结果条数对不上：给 {len(texts)} 条、回来 {len(vecs)} 条。"
                      f"**不建了**（宁可没有索引，也不建一个对不上的）。")

    dim = len(vecs[0]) if vecs else 0
    bad = [i for i, v in enumerate(vecs) if len(v) != dim]
    if bad:
        return None, f"嵌入维度不一致（第 {bad[0]} 条不是 {dim} 维）。**不建了**。"

    # 向量存成定长小数：JSON 里 384 维浮点原样存很占地方，6 位小数足够排序用。
    packed = [[round(float(z), 6) for z in v] for v in vecs]
    index = {"model": db_model or c.get("model") or DEFAULT_MODEL,
             "dim": dim, "built_at": time.time(), "count": len(docs),
             "truncated": bool(truncated),
             "docs": [dict(d, v=p) for d, p in zip(docs, packed)]}

    rep.append(f"嵌入完成：{len(docs)} 条 × {dim} 维")
    if truncated:
        # ⚠️ 说**实际嵌进去的条数**，不是「拿了几条候选」——两者会因为空内容/上限
        #    夹取而不一样，报错了数字就是在骗用户（这条是自测抓出来的）。
        rep.append(f"⚠️ **没建全**：历史有 {len(rows)} 条，只嵌了最近 {len(docs)} 条"
                   f"（上限 `semantic.max_messages={cap}`，跳过空内容 "
                   f"{len(take) - len(docs)} 条）。要全量就把它调大重建。")
    for m in take:
        if not str(m.get("content") or "").strip():
            continue
        rep.append(f"  · {time.strftime('%Y-%m-%d', time.localtime(int(m.get('time') or 0)))} "
                   f"{str(m.get('content'))[:30]}")
        if len(rep) > 14:
            rep.append("  · …（其余略）")
            break
    if topk_note:
        rep.append(topk_note)
    return index, "\n".join(rep)


# ── 检索 ────────────────────────────────────────────────────────────────

def search(query, cfg=None, k=None, embedder=None):
    """按**意思**找历史。返回 `(命中列表, 错误文本)`。

    ⚠️ **失败时返回错误文本，绝不退回关键词搜索**——见模块头第 1 条。
    """
    c = cfg_of(cfg)
    q = str(query or "").strip()
    if not q:
        return [], "查询是空的。"

    index, err = load_index(cfg)
    if index is None:
        return [], err

    # 模型对不对得上：拿别的模型建的索引，向量空间都不一样，分数没有意义
    want = str(c.get("model") or "")
    got = str(index.get("model") or "")
    if want and got and want != got:
        return [], (f"**索引是别的模型建的**，不能拿来用：\n"
                    f"  索引用的：{got}\n"
                    f"  现在配的：{want}\n"
                    f"两套向量不在同一个空间里，算出来的分数没有意义。\n"
                    f"要么把配置改回 `{got}`，要么重建索引：semantic.py --build")

    if embedder is None:
        embedder, err2 = load_backend(cfg)
        if embedder is None:
            return [], err2

    try:
        qv = embedder.encode([q])[0]
    except Exception as e:
        return [], f"查询嵌入失败：{type(e).__name__}: {str(e)[:160]}"
    if len(qv) != int(index.get("dim") or 0):
        return [], (f"**维度对不上**：查询向量 {len(qv)} 维、索引 {index.get('dim')} 维。\n"
                    f"说明查询用的模型和建索引时那个不是同一个。重建索引：semantic.py --build")

    kk, _capped = _clamp_int(k if k is not None else c.get("topk"), 1, 50,
                             DEFAULTS["topk"])
    try:
        min_score = float(c.get("min_score", DEFAULTS["min_score"]))
    except (TypeError, ValueError):
        min_score = DEFAULTS["min_score"]

    scored = []
    for d in index.get("docs") or []:
        sc = cosine(qv, d.get("v") or [])
        if sc >= min_score:
            scored.append((sc, d))
    scored.sort(key=lambda t: -t[0])
    hits = [{"score": round(sc, 4), "time": d.get("t"), "who": d.get("w"),
             "is_self": bool(d.get("s")), "text": d.get("x")} for sc, d in scored[:kk]]
    return hits, ""


def status_text(cfg=None):
    """模型 + 索引现在的状态。**每一句都必须是可核实的。**"""
    c = cfg_of(cfg)
    lines = [f"语义检索：{'开启' if c.get('enabled') is True else '关闭'}"
             f"（`semantic.enabled`）", ""]
    ok, why = model_ready(cfg)
    lines.append(f"本地模型：{'就位 ✅' if ok else '**没有** ❌'}")
    lines.append(f"  {why}")
    index, err = load_index(cfg)
    lines.append("")
    if index is None:
        lines.append("索引：**不可用** ❌")
        lines.append(f"  {err}")
    else:
        docs = index.get("docs") or []
        lines.append(f"索引：可用 ✅　{len(docs)} 条 × {index.get('dim')} 维")
        lines.append(f"  模型：{index.get('model')}")
        try:
            lines.append(f"  建于：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(index.get('built_at') or 0)))}")
        except (TypeError, ValueError, OSError):
            lines.append("  建于：（时间读不出）")
        lines.append(f"  文件：{index_path(cfg)}")
        if index.get("truncated"):
            lines.append("  ⚠️ 上次**没建全**（到了 max_messages 上限），更早的消息没进索引。")
    # ⚠️ 这一段**放在 if/else 外面**：`enabled` 是配置问题，和索引能不能用无关。
    #    以前它只在「索引可用」那一支里说，于是「模型没下 + 索引没建 + 开关也关着」
    #    时用户看不到「聊天里不会用它」这句——自测把这个缺口抓出来了。
    if c.get("enabled") is not True:
        lines.append("")
        lines.append("⚠️ `semantic.enabled` 是关的 —— 也就是说**聊天里不会用它**。"
                     "要用就把 config.yaml 里那项改成 true（默认关是有意的："
                     "没下模型/没建索引时打开只会让人困惑）。")
    return "\n".join(lines)


def _collect(cfg, days=0, talker=""):
    """收要建索引的历史行。返回 `(rows, 错误文本)`。

    **只收「用户实际会让助手管的那些会话」**：`target_chats` / `watch.chats` /
    `auto_reply.chats`，外加 `--talker` 指定的那个。

    **不做全库扫描**：用户有上万个联系人，逐个翻历史既慢又违反查询纪律
    （每一页都是一次真实的 hook 查询，而 hook 不支持并发）。
    要索引别的会话：把它加进那三份名单，或者用 `--talker <昵称|wxid>` 单独来一次。
    """
    import live_history
    from aixed_api import AixedClient

    client = AixedClient(cfg.get("aixed_base_url") or "http://127.0.0.1:30001")
    try:
        contacts = live_history.all_contacts(client) or []
    except Exception as e:
        return [], (f"取联系人失败（**建索引要先停 bot**——两路查询同时压在 hook 上"
                    f"会把微信搞崩）：{type(e).__name__}: {str(e)[:160]}")

    names = {}
    for c in contacts:
        if c.get("wxid"):
            names[str(c["wxid"])] = str(c.get("remark") or c.get("name") or "")

    targets = []

    def add(w):
        w = str(w or "").strip()
        if w and w not in targets:
            targets.append(w)

    for w in (cfg.get("target_chats") or []):
        add(w)
    for sec in ("watch", "auto_reply"):
        for r in ((cfg.get(sec) or {}).get("chats") or []):
            if isinstance(r, dict):
                add(r.get("wxid"))
    add(talker)

    if not targets:
        return [], ("没有任何要索引的会话。\n"
                    "建索引只覆盖 `target_chats` / `watch.chats` / `auto_reply.chats`"
                    "—— **不做全库扫描**（你有上万个联系人，逐个翻既慢又违反查询纪律）。\n"
                    "要么先把会话加进那三份名单，要么：semantic.py --build --talker 某人")

    since = None
    if days and int(days) > 0:
        since = time.time() - int(days) * 86400

    rows, failed = [], []
    for w in targets:
        try:
            got, _meta = live_history.collect_contact_history(
                client, w, page=200, max_items=0, since=since)
        except Exception as e:
            failed.append(f"{names.get(w) or w}（{type(e).__name__}）")
            continue
        for m in got:
            # `since` 已经交给查询层过滤过；这里再挡一道，是因为有的后端对
            # since 的处理不完全一致——宁可多花一次比较，也不把范围外的行悄悄塞进去。
            if since and int(m.get("time") or 0) < since:
                continue
            rows.append({"time": m.get("time"), "content": m.get("content"),
                         "is_self": m.get("is_self"),
                         "who": names.get(w) or w})

    if failed:
        # 如实打出来：哪些会话没取到，别让用户以为索引覆盖了它们
        print(f"[semantic] ⚠️ 这些会话没取到，**没有进索引**：{'、'.join(failed)}")
    if not rows:
        return [], ("这些会话里一条历史都没取到："
                    + "、".join(names.get(t) or t for t in targets)
                    + "\n（也可能是库句柄掉了——先跑 verify_real.py 或 /自检 看一眼。）")
    rows.sort(key=lambda m: int(m.get("time") or 0))
    return rows, ""


# ── 命令行 ──────────────────────────────────────────────────────────────

def _load_cfg():
    import yaml
    try:
        return yaml.safe_load(open(os.path.join(PROJECT_DIR, "config.yaml"),
                                   encoding="utf-8")) or {}
    except Exception:
        return {}


def setup_model(cfg=None, mirror=None):
    """下本地模型。**只由用户显式执行**（`--setup`）。返回 `(ok, 一句人话)`。"""
    os.environ.setdefault("HF_ENDPOINT", mirror or HF_MIRROR)
    d = model_dir(cfg)
    repo = cfg_of(cfg).get("model") or DEFAULT_MODEL
    return _download(repo, d)


def _download(repo, dest):
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return False, ("要下模型得先装 sentence-transformers（它带 huggingface_hub）：\n"
                       "  .venv\\Scripts\\python.exe -m pip install sentence-transformers")
    os.makedirs(dest, exist_ok=True)
    print(f"[semantic] 从 {os.environ.get('HF_ENDPOINT')} 下载 {repo} → {dest}")
    try:
        snapshot_download(repo_id=repo, local_dir=dest)
    except Exception as e:
        return False, (f"下载失败：{type(e).__name__}: {str(e)[:200]}\n"
                       f"（本机 huggingface.co 不通，所以走 hf-mirror；"
                       f"镜像也连不上就换个网络再试。）")
    return True, f"模型已就位：{dest}"


if __name__ == "__main__":
    cfg = _load_cfg()
    args = sys.argv[1:]

    def opt(name, default=""):
        if name in args and len(args) > args.index(name) + 1:
            return args[args.index(name) + 1]
        return default

    if not args or "--status" in args:
        print(status_text(cfg))
        print("\n用法：")
        print("  python semantic.py --status                  看模型/索引状态")
        print("  python semantic.py --ready                   只回退出码（脚本用，无输出）")
        print("  python semantic.py --setup                   下本地模型（只此一条下载路径）")
        print("  python semantic.py --build [--days 90]       建索引（**bot 要先停**）")
        print("  python semantic.py --search \"关键词\"        试搜一条")
    elif "--ready" in args:
        # 给控制台 / 一键部署用的**纯探针**：只回退出码，**一个字都不打**
        # （别让调用方去解析给用户看的文案；`--status` 是给人看的，而且它恒退 0）。
        _m_ok, _ = model_ready(cfg)
        _idx, _ = load_index(cfg)
        sys.exit(0 if (_m_ok and _idx is not None) else 1)
    elif "--setup" in args:
        print("先装依赖（可选依赖，不随主程序安装）：")
        print("  .venv\\Scripts\\python.exe -m pip install sentence-transformers\n")
        ok, msg = setup_model(cfg, mirror=opt("--mirror") or None)
        print(msg)
        sys.exit(0 if ok else 1)
    elif "--build" in args:
        import live_history
        import aixed_api
        try:
            days = int(opt("--days", "0") or 0)
        except ValueError:
            print("--days 要是数字（0 = 不限）")
            sys.exit(1)
        rows, err = _collect(cfg, days, opt("--talker"))
        if err:
            print(err)
            sys.exit(1)
        index, report = build(rows, cfg)
        if index is None:
            print(report)
            sys.exit(1)
        print(report)
        ok, msg = save_index(cfg, index)
        print(("✅ " if ok else "❌ ") + msg)
        sys.exit(0 if ok else 1)
    elif "--search" in args:
        hits, err = search(opt("--search"), cfg)
        if err:
            print(err)
            sys.exit(1)
        for h in hits:
            print(f"  {h['score']:.3f}  [{time.strftime('%m-%d %H:%M', time.localtime(h['time']))}] "
                  f"{'我' if h['is_self'] else (h['who'] or '对方')}：{h['text'][:60]}")
        print(f"（{len(hits)} 条；没有更多就是没有 ≥ min_score 的）")
    else:
        print("没认出来。用 --status / --setup / --build / --search")
        sys.exit(1)
