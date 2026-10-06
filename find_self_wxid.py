"""在自己的电脑上找出「我自己的 wxid」，并可以写进 config.yaml。

**为什么单独一个脚本**（2026-10-05 部署真机）：另一台电脑装完包之后
`self_wxid` 是空的，而这个 hook 构建的 `/GetSelfProfile` **也不返回 wxid**
——于是「我/对方」分不清，表现是 bot 把自己以前说过的话当成用户的新提问再答一遍。
而**能自动认的那一级要 bot 跑起来才生效**；用户真正需要的是：装完就能把值填好。

这里的判据**完全离线**、不需要 hook、不需要跑 bot、不需要解密任何库：
微信 4.x 的账号目录名就是 `<wxid>_<数字后缀>`（例如 `wxid_a1b2c3d4e5f6g7_2895`），
目录在 `image_cache.data_root()` 底下（`xwechat_files`，数据搬过盘也能找到）。

用法：
    .venv\\Scripts\\python.exe find_self_wxid.py            # 只看，不改任何文件
    .venv\\Scripts\\python.exe find_self_wxid.py --apply     # 写进 config.yaml（先备份）
"""
import argparse
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# wxid 的形状：4~20 位字母数字。微信自己也可能有别的形状（旧号/企业微信），
# 所以**只要求**以 `wxid_` 开头、后面是非空的字母数字串——认不准就不认（见下）。
_WXID_RE = re.compile(r"^wxid_[A-Za-z0-9]{3,32}$")


def wxid_from_account_name(name):
    """账号目录名 → wxid。认不准返回 ""（**绝不猜**）。

    `wxid_a1b2c3d4e5f6g7_2895` → `wxid_a1b2c3d4e5f6g7`。
    wxid 自己带下划线，所以只能从**最后一个下划线**切：切完还要满足 wxid 的形状，
    否则宁可不给（给错等于把别人的 wxid 当自己，比认不出更糟）。
    """
    s = str(name or "").strip()
    if not s.startswith("wxid_"):
        return ""
    # 目录名末尾那段是数字后缀；没有后缀（就是纯 wxid）时也接受
    if "_" in s:
        head, _, tail = s.rpartition("_")
        if tail.isdigit() and head:
            s = head
    return s if _WXID_RE.match(s) else ""


def find_self_wxid():
    """本机所有账号目录 → `[(wxid, 账号目录), ...]`；一个都认不出就是空列表。

    **多账号全列出来**：本项目不做多账号，但「用户这台机器上有两个号」是现实，
    替用户挑一个就是替他决定用哪个号发消息 —— 所以列出来让他自己选/自己填。
    """
    import image_cache

    out = []
    seen = set()
    for d in image_cache.account_dirs():
        wxid = wxid_from_account_name(os.path.basename(d.rstrip("\\/")))
        if wxid and wxid not in seen:
            seen.add(wxid)
            out.append((wxid, d))
    return out


def apply_to_config(path, wxid):
    """把 `self_wxid: ...` 那一行改掉（先备份、再原子写）。返回说明文字。

    ⚠️ `config.yaml` 是**带注释的手写文件**，程序平时**从不回写它**（这是项目约定：
    用户手写的注释不能被机器冲掉）。这里是个例外，所以代价必须显式承担：
      * 只动 `self_wxid:` **那一行**，其余一个字节都不改；
      * 改之前先备份成 `config.yaml.bak-selfwxid-<时间戳>`；
      * 找不到那一行就**不改**（如实说），绝不追加一个用户看不懂的块进去。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    pat = re.compile(r"(?m)^self_wxid:[ \t]*.*$")
    if not pat.search(raw):
        return "", ("config.yaml 里没有 `self_wxid:` 这一行（可能是很旧的配置），"
                    "**没有改任何东西**；请手工加一行：self_wxid: \"%s\"" % wxid)
    new = pat.sub('self_wxid: "%s"' % wxid, raw, count=1)
    if new == raw:
        return "", "config.yaml 里的 self_wxid 已经是 %s，不用改。" % wxid

    backup = "%s.bak-selfwxid-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return backup, "已写入：self_wxid: \"%s\"（原文件备份在 %s）" % (wxid, os.path.basename(backup))


def main(argv=None):
    ap = argparse.ArgumentParser(description="离线找出自己的 wxid（不碰 hook、不跑 bot）")
    ap.add_argument("--apply", action="store_true",
                    help="把结果写进 config.yaml（只改 self_wxid 那一行，先备份）")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"),
                    help="要改的配置文件（默认项目根 config.yaml）")
    args = ap.parse_args(argv)

    try:
        import image_cache
        root = image_cache.data_root()
    except Exception as e:                                    # noqa: BLE001
        root = None
        print("⚠️ 找不到微信数据根目录：%s: %s" % (type(e).__name__, e))
    print("微信数据目录：%s" % (root or "（没找到）"))

    found = find_self_wxid()
    if not found:
        print("\n❌ 没认出来。这台机器上可能：")
        print("   · 还没登录过微信 4.x（先登录一次，让它把账号目录建出来）")
        print("   · 或者微信数据不在默认位置（`~/Documents/xwechat_files`）")
        print("   · 或者账号目录名不是 `<wxid>_<数字>` 这个形状")
        print("   → 那就用 bot 的另一条路：起 bot，日志里会写"
              "「自己 wxid 从库里认出来了: …」，把那个值填进 config.yaml。")
        return 1

    print("\n找到 %d 个账号：" % len(found))
    for wxid, d in found:
        print("  %s   （目录 %s）" % (wxid, d))

    if not args.apply:
        print("\n只读模式，没改任何文件。要写进 config.yaml 加 --apply：")
        print("  .venv\\Scripts\\python.exe find_self_wxid.py --apply")
        return 0

    if len(found) > 1:
        print("\n⚠️ 这台机器上有多个账号：**本脚本不替你挑**（填错就是拿别人的号发消息）。")
        print("   请手动把要用的那个填进 config.yaml 的 self_wxid。")
        return 2

    backup, msg = apply_to_config(args.config, found[0][0])
    print("\n%s" % msg)
    return 0 if backup else 1


if __name__ == "__main__":
    sys.exit(main())
