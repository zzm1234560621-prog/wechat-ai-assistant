"""用 PyWxDump 解密并导出微信聊天记录为统一 JSONL（data/history.jsonl）。

三步流程：
  1. wxdump info      -> 获取账号信息与数据库密钥（微信必须已登录）
  2. wxdump decrypt   -> 用密钥解密数据库
  3. 读取解密后的 SQLite -> 归一化为 data/history.jsonl

运行前请确保：
  - pip install pywxdump
  - 微信电脑版已登录（取密钥需要）
  - 建议以管理员身份运行

用法：
  python export_history.py            # 自动执行 1->2->3
  python export_history.py --info     # 只跑第 1 步
  python export_history.py --decrypt  # 只跑第 2 步
  python export_history.py --convert  # 只跑第 3 步
"""
import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
OUT = Path("data")
OUT.mkdir(exist_ok=True)
HISTORY = OUT / "history.jsonl"


def find_wechat_dirs():
    """定位微信数据目录（兼容 3.x 的 WeChat Files 和 4.x 的 xwechat_files）。"""
    hits = []
    roots = []
    # ⚠️ 4.x 这一条**先问微信自己记在哪儿**，别写死 ~/Documents ——
    # 用户把数据搬到别的盘之后，写死的路径只会返回「找不到」而**不报错**。
    # 检测逻辑只有一份，在 image_cache.data_root()（见那里的注释）。
    try:
        import image_cache
        r = image_cache.data_root()
        if r:
            roots.append(Path(r))            # ...\xwechat_files
            roots.append(Path(r).parent)     # 它上一级，以防布局不同
    except Exception:
        pass
    roots += [
        HOME / "Documents" / "WeChat Files",
        HOME / "Documents" / "xwechat_files",
        HOME / "Documents",
    ]
    for root in roots:
        if not root.exists():
            continue
        for p in root.glob("*"):
            if p.is_dir() and ((p / "Msg").exists() or (p / "msg").exists()):
                hits.append(p)
    return list(dict.fromkeys(hits))


def run(cmd):
    print("  > " + " ".join(cmd))
    subprocess.run(cmd, check=False)


def step_info():
    """第 1 步：取账号信息与密钥。"""
    print("[1/3] 获取微信账号信息与密钥 ...")
    run([sys.executable, "-m", "pywxdump", "info"])
    print("  如果上面报错，可改用：wxdump info  （或 wxdump.exe info）")
    print("  记下输出的 key（密钥），下一步要用。")


def step_decrypt(key=None, db_dir=None):
    """第 2 步：解密数据库。"""
    print("[2/3] 解密数据库 ...")
    if not key:
        key = input("  请输入上一步得到的 key：").strip()
    if not db_dir:
        dirs = find_wechat_dirs()
        if not dirs:
            print("  未自动找到微信数据目录，请手动指定。")
            return
        db_dir = dirs[0] / "Msg" if (dirs[0] / "Msg").exists() else dirs[0] / "msg"
    print(f"  数据目录：{db_dir}")
    run([sys.executable, "-m", "pywxdump", "decrypt", "-k", key, "-i", str(db_dir), "-o", "./decrypted"])
    print("  解密完成，输出在 ./decrypted")


def _format_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return str(ts)


def _load_nicknames(db_path):
    """尽力从 Contact 表读取 wxid -> 昵称 的映射。"""
    mapping = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT UserName, NickName FROM Contact")
        for wxid, nick in cur.fetchall():
            if isinstance(nick, bytes):
                nick = nick.decode("utf-8", "ignore")
            mapping[wxid] = nick
        conn.close()
    except Exception:
        pass
    return mapping


def step_convert(decrypted_dir="./decrypted"):
    """第 3 步：把解密后的 MSG 库转成统一 JSONL。"""
    print("[3/3] 归一化聊天记录为 JSONL ...")
    dec = Path(decrypted_dir)
    if not dec.exists():
        print(f"  未找到 {dec}，请先跑 --decrypt")
        return
    nicknames = {}
    total = 0
    with HISTORY.open("w", encoding="utf-8") as out:
        for db in sorted(dec.rglob("*.db")):
            if "Contact" in db.name or "MicroMsg" in db.name:
                # 顺带收集昵称映射
                nicknames.update(_load_nicknames(db))
                continue
            try:
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                cur = conn.cursor()
                # 有些库的表名是 MSG，字段名见下方注释
                cur.execute(
                    "SELECT StrTalker, StrContent, IsSender, CreateTime, Type "
                    "FROM MSG WHERE Type = 1 ORDER BY CreateTime"
                )
                for talker, content, is_self, ts, mtype in cur.fetchall():
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", "ignore")
                    if not content:
                        continue
                    talker = talker if isinstance(talker, str) else str(talker)
                    sender = nicknames.get(talker, talker)
                    out.write(json.dumps({
                        "talker": talker,
                        "sender": sender,
                        "content": content,
                        "time": _format_time(ts),
                        "is_self": int(is_self or 0),
                        "type": int(mtype or 0),
                    }, ensure_ascii=False) + "\n")
                    total += 1
                conn.close()
            except Exception as e:
                print(f"  跳过 {db.name}: {e}")
    print(f"  完成，共导出 {total} 条文本消息 -> {HISTORY}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--info", action="store_true")
    ap.add_argument("--decrypt", action="store_true")
    ap.add_argument("--convert", action="store_true")
    ap.add_argument("--key", default=None)
    args = ap.parse_args()

    if not (args.info or args.decrypt or args.convert):
        args.info = args.decrypt = args.convert = True

    if args.info:
        step_info()
    if args.decrypt:
        step_decrypt(key=args.key)
    if args.convert:
        step_convert()


if __name__ == "__main__":
    main()
