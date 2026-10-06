"""自测：离线找出自己的 wxid（find_self_wxid.py）。

不用真微信、不碰 hook、不联网：
  * `wxid_from_account_name()` 是纯函数，直接穷举形状（含**认不准就不认**的反例）；
  * `apply_to_config()` 在临时目录里改一份假 config.yaml，验证
    「只动 self_wxid 那一行、先备份、找不到那一行就不改」。

用法：python selftest_self_wxid.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import find_self_wxid as fsw

_FAIL = []


def chk(cond, msg):
    print(("  ok  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        _FAIL.append(msg)


def main():
    print("=" * 60)
    print("离线找 wxid 自测（无微信 / 不碰 hook / 不联网）")
    print("=" * 60)

    print("\n1) 账号目录名 → wxid（纯函数）")
    good = [
        ("wxid_a1b2c3d4e5f6g7_2895", "wxid_a1b2c3d4e5f6g7"),
        ("wxid_abcdefghijklmnopqrstuvw_123", "wxid_abcdefghijklmnopqrstuvw"),
        ("wxid_aaaaaaaaaaaa_0", "wxid_aaaaaaaaaaaa"),
        ("wxid_h8i9j0k1l2m3n4_1", "wxid_h8i9j0k1l2m3n4"),
        # 没有数字后缀的（有的机器目录名就是纯 wxid）也认
        ("wxid_aaaaaaaaaaaa", "wxid_aaaaaaaaaaaa"),
    ]
    for name, want in good:
        got = fsw.wxid_from_account_name(name)
        chk(got == want, "%s → %s" % (name, got))

    bad = [
        "",                       # 空
        "filehelper",             # 不是 wxid
        "all_users",              # 微信自己的目录
        "wxid_",                  # 只有前缀
        "wxid_ab",                # 太短（<3 位）
        "wxid_" + "a" * 40,       # 太长
        "wxid_abcd-efg_1",        # 带非法字符
        "wxid_abc def_1",         # 带空格
        "notwxid_aaaaaaaaaaaa_1", # 前缀不对
        "wxid_aaaaaaaaaaaa_abc",  # 后缀不是数字 → 整串当 wxid 也非法（带下划线的尾巴）
    ]
    for name in bad:
        got = fsw.wxid_from_account_name(name)
        chk(got == "", "认不准就不认：%r → %r" % (name, got))

    print("\n2) 多账号全列出（绝不替用户挑）")
    # `find_self_wxid()` 里是**函数内** `import image_cache`，所以替换
    # `image_cache.account_dirs` 就够（不用去动模块全局），测完还原。
    import image_cache
    saved = image_cache.account_dirs
    try:
        image_cache.account_dirs = lambda: [
            os.path.join("X:", "xwechat_files", "wxid_aaaa1111bbbb_100"),
            os.path.join("X:", "xwechat_files", "wxid_cccc2222dddd_200"),
            os.path.join("X:", "xwechat_files", "all_users"),      # 非账号目录
        ]
        got = fsw.find_self_wxid()
        chk([w for w, _d in got] == ["wxid_aaaa1111bbbb", "wxid_cccc2222dddd"],
            "两个账号都列出来、非账号目录被过滤：%r" % (got,))
    finally:
        image_cache.account_dirs = saved
    chk(image_cache.account_dirs is saved, "（探针函数可替换，测完已还原）")

    print("\n3) apply：只改 self_wxid 那一行 + 先备份")
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("# 注释必须原样留着\n"
                    'provider: "openai"\n'
                    'self_wxid: ""\n'
                    "target_chats:\n  - \"filehelper\"\n")
        backup, msg = fsw.apply_to_config(cfg, "wxid_a1b2c3d4e5f6g7")
        after = open(cfg, encoding="utf-8").read()
        chk('self_wxid: "wxid_a1b2c3d4e5f6g7"' in after, "值写进去了")
        chk("# 注释必须原样留着" in after and 'provider: "openai"' in after
            and 'target_chats:' in after,
            "★ 其余行一个字节都没动（注释也保住）")
        chk(backup and os.path.exists(backup), "先备份了：%s" % os.path.basename(backup or ""))
        chk(open(backup, encoding="utf-8").read().count('self_wxid: ""') == 1,
            "备份里是改之前的内容")

        # 再跑一次：幂等，不该产生第二份备份
        backup2, msg2 = fsw.apply_to_config(cfg, "wxid_a1b2c3d4e5f6g7")
        chk(backup2 == "", "同样内容再写一次不重复备份：%s" % msg2)

        # 认不出那一行 → 绝不追加、绝不动文件
        cfg2 = os.path.join(td, "old.yaml")
        with open(cfg2, "w", encoding="utf-8") as f:
            f.write('provider: "openai"\n')      # 很旧的配置，没有 self_wxid 行
        before = open(cfg2, encoding="utf-8").read()
        backup3, msg3 = fsw.apply_to_config(cfg2, "wxid_a1b2c3d4e5f6g7")
        chk(backup3 == "" and open(cfg2, encoding="utf-8").read() == before,
            "★ 找不到那一行就不改文件，只如实告诉用户怎么加：%s" % msg3[:40])

    print()
    if _FAIL:
        print("失败 %d 项 ❌" % len(_FAIL))
        for f in _FAIL:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
