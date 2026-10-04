"""真机自检：把「只能你本人用真微信验」的那几十条，变成一条命令。

⚠️ **跑之前必须先停掉 bot**（三条入口哪条起的都行）。
原因见 CLAUDE.md 的 hook 铁律第 3 条：bot 轮询的同时再手工发查询，两路查询一起压 hook
——2026-10-01 那次把微信搞崩（Weixin.dll 读 NULL）就是这么来的。
所以这个脚本**自己会检查 bot 在不在跑**：在跑就直接拒绝，不会硬来。

它做什么（全部只读，且**故意把查询数压到最低**，跑完会报出真实查询次数）：
  1. hook 连不连得上、微信登没登录
  2. 库结构识别（4.x / 3.9.x）、轮询游标拿不拿得到、分片有没有在报错
  3. 联系人能不能读出来（按人名查历史依赖它）
  4. **发图白名单**：默认放行的到底是哪儿、真实的聊天缩略图在不在里面
     —— 也就是验「白名单收窄有没有把正常发图弄坏」
  5. 落盘状态与账本：游标有没有存下来、用量账本有没有真实数据、状态页文件在不在

跑法：
    .venv\\Scripts\\python.exe verify_real.py

它**不会**替你验证的两件事（只能人肉）：真的发一张图出去、真的让对方回一条消息。
"""
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BOT_INSTANCE_PORT = 39001        # 和 bot.py 的单实例锁同一个端口
QUERY_BUDGET_NOTE = []

_OK = 0
_BAD = 0
_WARN = 0


def line(flag, text):
    global _OK, _BAD, _WARN
    if flag == "ok":
        _OK += 1
        print(f"  ✅ {text}")
    elif flag == "bad":
        _BAD += 1
        print(f"  ❌ {text}")
    else:
        _WARN += 1
        print(f"  ⚠️ {text}")


def sec(t):
    print(f"\n── {t} ──")


def bot_is_running():
    """bot 在不在跑：抢它那个单实例锁端口。抢到再立刻松手（和 bot.py 一样用排他绑定）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    excl = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if excl is not None:
        s.setsockopt(socket.SOL_SOCKET, excl, 1)
    try:
        s.bind(("127.0.0.1", BOT_INSTANCE_PORT))
        s.listen(1)
        s.close()
        return False
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return True


def main():
    print("=" * 66)
    print("真机自检（只读；请先停掉 bot）")
    print("=" * 66)

    if bot_is_running():
        print(f"\n❌ bot 正在跑（回环端口 {BOT_INSTANCE_PORT} 被占）。")
        print("   请先停掉它再跑本脚本——bot 轮询 + 手工查询同时压 hook 会把微信搞崩。")
        print("   停掉的办法：关掉 启动助手.bat 那个窗口，或结束那个 python 进程。")
        return 2

    import bot                                       # noqa: E402
    import settings                                  # noqa: E402
    import live_history                              # noqa: E402
    import agent_tools                               # noqa: E402
    import image_cache                               # noqa: E402

    cfg = settings.effective(bot.load_config())
    backend = cfg.get("backend", "wcferry")
    base = cfg.get("aixed_base_url", "http://127.0.0.1:30001")
    print(f"\n后端: {backend}   接口: {base}   模型: {cfg.get('model')}")

    # ---------- 1. hook 与登录态 ----------
    sec("1. hook 连接与登录态")
    if backend != "aixed":
        line("warn", f"后端是 {backend}，本脚本只对 aixed 主线做完整检查")
        return _summary()

    from aixed_api import AixedClient, AixedError           # noqa: E402
    client = AixedClient(base)
    t0 = time.time()
    try:
        ok, info = client.ping()
    except Exception as e:
        line("bad", f"ping 抛异常：{e}")
        return _summary()
    dt = time.time() - t0
    QUERY_BUDGET_NOTE.append("ping")
    if not ok:
        line("bad", f"连不上 / 未就绪：{info}")
        print("     → 微信没启动、version.dll 没加载、或没登录。先解决这个，下面不用看了。")
        return _summary()
    line("ok", f"hook 就绪（{dt:.2f}s）：{info}")

    try:
        is_login = bool(client.is_login())
    except Exception as e:
        is_login = False
        line("warn", f"is_login() 抛异常：{e}")
    QUERY_BUDGET_NOTE.append("is_login")
    if is_login:
        line("ok", "微信已登录")
    else:
        line("bad", "IsLogin: 0 —— 微信停在登录界面（要在微信里扫码；force_rescan 没用）")

    # ---------- 2. 库结构 / 游标 / 分片 ----------
    sec("2. 库结构与轮询游标（这一步会发几次查库）")
    wxid = str(cfg.get("self_wxid") or "")
    if not wxid:
        try:
            wxid = client.get_self_wxid() or ""
        except Exception:
            wxid = ""
        QUERY_BUDGET_NOTE.append("get_self_wxid")
    if wxid:
        live_history.set_self_wxid(wxid)
        line("ok", f"自己的 wxid 拿到了：{wxid[:6]}…（判「哪条是我发的」要用它）")
    else:
        line("bad", "拿不到自己的 wxid —— 历史里将分不清「我」和「对方」；请在 config.yaml 填 self_wxid")

    try:
        v4 = live_history.is_wechat4(client)
    except Exception as e:
        v4 = None
        line("bad", f"库结构识别失败：{e}")
    QUERY_BUDGET_NOTE.append("is_wechat4")
    if v4 is True:
        line("ok", "识别为微信 4.x（contact.db / message_fts.db 那一套）")
    elif v4 is False:
        line("warn", "识别为 3.9.x（v3 schema）—— 如果你装的是 4.1.10.27，说明探库失败了")
    QUERY_BUDGET_NOTE.append("latest_cursor")

    t0 = time.time()
    try:
        cur = live_history.latest_cursor(client)
    except Exception as e:
        cur = None
        line("bad", f"取轮询游标失败：{e}")
    dt = time.time() - t0
    if cur is not None:
        if cur:
            if dt > 1.0:
                line("warn", f"游标拿到了（{dt:.2f}s，偏慢）：{cur}")
            else:
                line("ok", f"游标拿到了（{dt:.2f}s）：{cur}")
        else:
            line("bad", "游标是空的 —— 这就是「静默失效」的形态（查得到但没数据）")

    errs = live_history.poll_errors()
    if errs:
        names = "、".join(f"{k}({v[1]}次)" for k, v in errs.items())
        line("bad", f"有分片在报错：{names}")
    else:
        line("ok", "没有分片报错记录")

    # ---------- 3. 联系人 ----------
    sec("3. 联系人（按人名查历史依赖它）")
    QUERY_BUDGET_NOTE.append("all_contacts")
    try:
        contacts = live_history.all_contacts(client) or []
    except Exception as e:
        contacts = []
        line("bad", f"读联系人失败：{e}")
    if contacts:
        named = sum(1 for c in contacts if (c.get("remark") or c.get("name")))
        line("ok", f"读到 {len(contacts)} 个联系人/群，其中 {named} 个有显示名")
        if named < len(contacts):
            line("warn", f"{len(contacts) - named} 个没有显示名 —— 它们不会进「谁说的」"
                         f"的名字表（宁可不精确也不喂 wxid）")
    elif contacts == []:
        line("bad", "一个联系人都读不到 —— 按人名查历史、发消息都会不可用")

    # ---------- 4. 发图白名单（这条最需要真机验） ----------
    sec("4. 发图白名单：默认放行哪儿 + 聊天缩略图在不在里面")
    try:
        cfg_dirs = [str(d) for d in ((cfg.get("agent") or {}).get("send_image_dirs") or []) if str(d).strip()]
    except Exception:
        cfg_dirs = []
    try:
        allowed = agent_tools.allowed_image_dirs(cfg)
    except Exception as e:
        allowed = []
        line("bad", f"算白名单失败：{e}")

    if cfg_dirs:
        line("ok", f"config.yaml 里 agent.send_image_dirs 配了 {len(cfg_dirs)} 个目录 —— "
                   f"按**并集**处理：你配的目录 **+** 默认的图片缓存根，两处都能发")
        print(f"     你配的: {cfg_dirs}")
    else:
        print("    （send_image_dirs 为空 → 只放默认的微信图片缓存根）")

    cache_dirs = image_cache.image_cache_dirs() or []
    if cache_dirs:
        line("ok", f"找到微信图片缓存根 {len(cache_dirs)} 个："
                   + "、".join(os.path.basename(os.path.dirname(d)) + "/" + os.path.basename(d)
                              for d in cache_dirs[:3]) + ("…" if len(cache_dirs) > 3 else ""))
    else:
        line("warn", "推不出微信图片缓存根（没有本地缓存过的图？）—— 默认白名单会退回整个微信数据根目录")

    for d in allowed:
        print(f"     允许目录: {d}")

    # 真实缩略图：拿一个账号的 conversation hash 去索引里找几张
    found = 0
    checked = 0
    try:
        import hashlib
        # cache_index 的 key 是会话名的 md5（和 Msg_ 表同一套哈希）
        cands = [c for c in contacts if c.get("wxid")][:8]
        for c in cands:
            h = hashlib.md5(str(c["wxid"]).encode("utf-8")).hexdigest()
            rows = image_cache.cache_index(h) or {}
            for _lid, p in list(rows.items())[:3]:
                checked += 1
                real = os.path.realpath(p)
                inside = any(agent_tools._is_under(real, os.path.realpath(root)) for root in allowed)
                if inside:
                    found += 1
                elif checked <= 3:
                    line("bad", f"真实的缩略图**不在**白名单里：{real}")
            if checked >= 9:
                break
    except Exception as e:
        line("warn", f"索引真实缩略图时出错（不影响其它检查）：{e}")

    if checked:
        if found == checked:
            line("ok", f"抽查 {checked} 张真实缩略图，**全部**在允许目录内（白名单收窄没弄坏正常发图）")
        else:
            line("bad", f"抽查 {checked} 张，只有 {found} 张在允许目录内 —— 正常发图可能被拦")
    else:
        line("warn", "本地没抽到可用的缩略图（没缓存过图就正常）—— 这条只能等你真发一张图来验")

    # ---------- 5. 落盘状态与账本 ----------
    sec("5. 落盘状态与账本")
    state_path = os.path.join(HERE, "data", "state.json")
    if os.path.isfile(state_path):
        try:
            import json
            st = json.load(open(state_path, encoding="utf-8"))
            cur_saved = (st.get("cursor") or {}).get("cursor")
            pend = st.get("pending") or {}
            line("ok", f"data/state.json 在：游标已落盘={bool(cur_saved)}，"
                       f"待确认队列会话数={len(pend)}")
            if not cur_saved:
                line("warn", "state.json 里没有游标 —— 这次启动会按「从最新开始」收（停机期消息不补）")
        except Exception as e:
            line("bad", f"state.json 读不出来：{e}")
    else:
        line("warn", "还没有 data/state.json —— bot 第一次跑完轮询后会生成")

    usage_path = os.path.join(HERE, "data", "usage.jsonl")
    if os.path.isfile(usage_path):
        import json
        n = 0
        tok = 0
        models = set()
        for ln in open(usage_path, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            n += 1
            tok += int(d.get("prompt_tokens") or 0) + int(d.get("completion_tokens") or 0)
            models.add(str(d.get("model") or "?"))
        if n and tok > 0:
            line("ok", f"用量账本 {n} 行，合计 {tok} tokens，模型：{'、'.join(sorted(models))}"
                       f" —— 说明记账真的接到调用链上了")
        elif n:
            line("warn", f"用量账本 {n} 行但 token 全是 0 —— 可能是自测造的假数据；"
                         f"真问一句模型再发 /用量 看看")
        else:
            line("warn", "用量账本是空的（还没成功调用过模型？）")
    else:
        line("warn", "还没有 data/usage.jsonl（还没成功调用过模型）")

    log_path = os.path.join(HERE, "bot.log")
    if os.path.isfile(log_path):
        size = os.path.getsize(log_path)
        rots = sorted(f for f in os.listdir(HERE) if f.startswith("bot.log."))
        line("ok", f"bot.log {size // 1024}KB" + (f"，已轮转出 {len(rots)} 份备份" if rots else "（还没到轮转阈值）"))

    # ---------- 收尾 ----------
    print("\n" + "=" * 66)
    print(f"查询次数：约 {len(QUERY_BUDGET_NOTE)} 次（{ '、'.join(QUERY_BUDGET_NOTE) }）")
    print("还有三件只能你本人做的：")
    print("  1) 起 bot，在文件传输助手里发一句话，再发 /用量 和 /status 看有没有数据")
    print("  2) 用真 hook 发一张**聊天里已有的图**（先按上面提示把 send_image_dirs 清成 []）")
    # 第 3 项是**人工确认项**（`docs/computer-files-spec.md` 第九节）：
    # 「删除真的进了回收站」这一条**自测证不了** —— 要枚举回收站得走 Shell COM，
    # 代价过大。自动测只能证「FOF_ALLOWUNDO 旗标对 + 原路径消失」。
    # 所以不假装验证过，而是**明确列出来让人看一眼**。
    print("  3) 删一个不重要的测试文件（在微信里对它说「把 xxx 删了」，回「确认」），")
    print("     然后**去回收站看一眼它在不在** —— 这一条自测证不了（见 docs/computer-files-spec.md）。")
    print("=" * 66)
    return _summary()


def _summary():
    print(f"\n小结：✅ {_OK} 项  ⚠️ {_WARN} 项  ❌ {_BAD} 项")
    if _BAD:
        print("有 ❌：按上面每条的建议处理，处理完再跑一次。")
        return 1
    if _WARN:
        print("没有 ❌，有 ⚠️：按需处理（其中「没抽到缩略图」通常正常）。")
        return 0
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
