"""「助手起不来 / hook 不对劲」的一站式只读诊断。**随包发布，出问题先跑它。**

用法（包目录下，助手开着也行）：
    .venv\\Scripts\\python.exe tools\\hook_doctor.py
    没有 venv 时用系统 python：python tools\\hook_doctor.py

它回答四个问题，**只读**（读注册表、读文件、一次 HTTP GET）：

  ① 微信目录里那份 `version.dll` 是哪一版？（和包里那份比：一致 / 旧了 / 没装）
  ② 运行中的 hook 是哪一版？（`/QueryDB/status` 里有没有 `LoginGateInfo`）
  ③ 闸门为什么没开？（`IsLogin` / `LoginGate` 原文 / `LoginGateInfo` 计数）
  ④ 微信的数据目录在哪、里面的库最近有没有被写？（= 到底有没有真登录）

为什么要有它（2026-10-06 真机）：用户换了新包两遍仍卡在
「hook 已加载，但数据库打不开（微信没登录？请在微信里扫码登录）」。
真相不在那句话里——**微信登录得好好的、库两秒前还在被写，而微信目录里的 hook 是旧的**
（解压新包不会替换已装的那份）。**判据全在文件与接口里，不该靠猜。**
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# 这个工具**在两个地方都要能跑**：仓库/包的 `tools\hook_doctor.py`，以及包根目录里
# 直接放一份（用户不用记目录）。所以项目根 = 「有 hook_check.py 的那一级」。
def _find_base(start):
    d = os.path.abspath(start)
    for _ in range(3):
        if os.path.isfile(os.path.join(d, "hook_check.py")):
            return d
        d = os.path.dirname(d)
    return os.path.dirname(os.path.abspath(start))


BASE = _find_base(HERE)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import hook_check  # noqa: E402  安装目录/包内 DLL 的判据只有这一份，别另写

STATUS_URL = "http://127.0.0.1:30001/QueryDB/status"


def _p(t=""):
    print(t, flush=True)


def _sec(t):
    _p(f"\n── {t} ──")


def _http_status(timeout=5):
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            return json.loads(raw), raw
        except ValueError:
            return None, raw[:400]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _save_roots():
    """微信自己记的「文件保存位置」——判据与 `image_cache._wechat_save_roots()` 同源。"""
    import re
    base = os.path.join(os.environ.get("APPDATA") or "", "Tencent", "xwechat", "config")
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out, base
    for name in sorted(names):
        if not name.lower().endswith(".ini"):
            continue
        try:
            with open(os.path.join(base, name), "r", encoding="utf-8",
                      errors="ignore") as fh:
                txt = fh.read(4096).strip()
        except OSError:
            continue
        if txt and "\n" not in txt and len(txt) <= 260 and \
                (re.match(r"^[A-Za-z]:[\\/]", txt) or txt.startswith("\\\\")):
            out.append((name, txt))
    return out, base


def _db_activity(root, limit=3):
    """`<root>\\<账号>\\db_storage` 下最新被写的 .db（多久之前）。"""
    if not os.path.isdir(root):
        return None, []
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(".db"):
                p = os.path.join(dirpath, fn)
                try:
                    hits.append((time.time() - os.path.getmtime(p),
                                 os.path.relpath(p, root)))
                except OSError:
                    pass
    hits.sort()
    return [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))], hits[:limit]


def main():
    _p("=" * 66)
    _p("微信助手 · hook / 启动闸门诊断（只读；助手开着也能跑）")
    _p("=" * 66)
    verdict = []

    _sec("① 微信目录里的 hook（version.dll）")
    inst = hook_check.installed_dll()
    bund = hook_check.bundle_dll()
    if inst:
        _p(f"   微信目录：{inst[0]}")
        _p(f"             {inst[1]} 字节  {inst[2][:16]}"
           f"  {hook_check.KNOWN.get(inst[1], '（未知构建）')}")
    else:
        d = hook_check.find_weixin_dir()
        _p(f"   ❌ 没找到：{'微信目录 ' + d + ' 下没有 version.dll' if d else '没找到微信安装目录'}")
        verdict.append("hook 没装（或微信装在别处）→ 助手菜单 [8]→[7]→[1] 装 hook")
    if bund:
        _p(f"   包里那份：{bund[1]} 字节  {bund[2][:16]}")
    else:
        _p("   （包内那份读不到——你是在开发仓里跑？）")
    if inst and bund:
        if inst[2] == bund[2]:
            _p("   ✅ 两边一致")
        else:
            _p("   ❌ **两边不一致：微信里那份是旧的**")
            verdict.append(
                "微信里装的是旧 hook（解压新包不会替换它）→ "
                "`助手.bat` → [8] 更多 → [7] Hook → [1] 装 hook，装完**重启微信**")

    _sec("② 运行中的 hook（http://127.0.0.1:30001/QueryDB/status）")
    st, raw = _http_status()
    if st is None:
        _p(f"   ❌ 探不到：{raw}")
        verdict.append("连不上 30001：微信没在跑 / hook 没装 / 端口不对")
    else:
        has_info = "LoginGateInfo" in st
        _p(f"   IsLogin = {st.get('IsLogin')}   "
           f"has LoginGateInfo = {has_info}")
        _p(f"   LoginGate = {st.get('LoginGate')!r}")
        if isinstance(st.get("LoginGateInfo"), dict):
            _p(f"   LoginGateInfo = {json.dumps(st['LoginGateInfo'], ensure_ascii=False)}")
        if has_info:
            _p("   ✅ 跑的是新构建（旧构建没有这个字段）")
        else:
            _p("   ❌ **跑的是旧构建**（缺 LoginGateInfo）")
            verdict.append("微信进程里加载的是旧 hook（**换了文件但没重启微信**？）"
                           "→ 完全退出微信（托盘也退）→ 重开 → 重新扫码")

    _sec("③ 微信数据目录与库活动（= 到底有没有真登录）")
    roots, cfg_dir = _save_roots()
    _p(f"   微信记的保存位置（{cfg_dir}）：")
    for name, txt in roots:
        _p(f"     {name}: {txt!r}")
    if not roots:
        _p("     （没有 ini——微信可能还没登录过）")
    default_root = os.path.join(os.path.expanduser("~"), "Documents", "xwechat_files")
    seen = []
    for _n, txt in roots:
        cand = os.path.join(txt, "xwechat_files")
        if os.path.isdir(cand) and os.path.normcase(cand) not in seen:
            seen.append(os.path.normcase(cand))
    if os.path.isdir(default_root) and os.path.normcase(default_root) not in seen:
        seen.append(os.path.normcase(default_root))
        _p(f"   默认位置也在：{default_root}")
    if not seen:
        _p("   ❌ 一处数据目录都没找到")
        verdict.append("找不到微信数据目录：这台机器上微信可能从来没登录成功")
    fresh_any = False
    for root in seen:
        accts, hits = _db_activity(root)
        _p(f"   {root}")
        if accts is None:
            _p("     （读不到）")
            continue
        _p(f"     账号目录 {len(accts)} 个：{accts[:4]}")
        for age, rel in hits:
            flag = "  ← 刚刚还在写" if age < 300 else ""
            _p(f"     {age:8.0f} 秒前  {rel}{flag}")
            if age < 300:
                fresh_any = True
        if not hits:
            _p("     没有 .db")
    if seen and fresh_any:
        _p("   ✅ 有库在 5 分钟内被写 ⇒ 微信**确实登录着**，问题在 hook 那一侧")
    elif seen:
        _p("   ⚠️ 5 分钟内没有库被写 ⇒ 可能是：微信真没登录 / 刚登录还没动静")

    _sec("④ 结论")
    if not verdict:
        _p("   ✅ 四项都正常。要是助手还不干活，看 bot.log 的下一步（游标/查询）。")
    else:
        for i, v in enumerate(verdict, 1):
            _p(f"   {i}) {v}")
    _p("\n（把全文复制给维护者即可。）")
    return 0 if not verdict else 1


if __name__ == "__main__":
    sys.exit(main())
