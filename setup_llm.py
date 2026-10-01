"""交互式配置模型：选编号 → 填 key → 自动写进 settings.json。

用法：python setup_llm.py   （或双击「配置模型.bat」）
配置写在 settings.json，会覆盖 config.yaml，所以不用手改任何文件。
"""
import getpass
import json
import os
import sys
import urllib.request

import settings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 预设由 providers.py 统一提供（.bat 向导和微信命令共用一份）
from providers import PROVIDER_PRESETS  # noqa: E402

# 「自定义」只在 .bat 里出现：需要交互式输入接口地址和模型名
_CUSTOM = {
    "short": "自定义",
    "name": "自定义（中转站 / 其他）",
    "provider": "openai",
    "base_url": "",
    "model": "",
    "models": [],
    "key_url": "问服务商要",
    "note": "手动填接口地址和模型名",
}

PROVIDERS = list(PROVIDER_PRESETS) + [_CUSTOM]
CUSTOM_INDEX = len(PROVIDERS)


def mask(k):
    k = str(k)
    return k if len(k) <= 8 else k[:4] + "****" + k[-4:]


def ask(prompt, default=""):
    s = input(prompt).strip()
    return s or default


def clean_key(raw):
    """去掉常见的手打前缀。key 本身就以 sk- 开头，
    用户手打一遍再粘贴就会变成 sk-sk-...，这坑踩过。"""
    k = str(raw).strip().strip('"').strip("'")
    while k.startswith("sk-sk-"):
        k = k[3:]
    return k


def pick_provider():
    print("=" * 58)
    print("  选择要用的模型服务")
    print("=" * 58)
    for i, p in enumerate(PROVIDERS, 1):
        print(f"  [{i}] {p['name']}")
        print(f"      {p['note']}")
    print("  [0] 取消")
    print("=" * 58)

    while True:
        c = input("请输入编号：").strip()
        if c == "0":
            return None
        if c.isdigit() and 1 <= int(c) <= len(PROVIDERS):
            return PROVIDERS[int(c) - 1]
        print("  无效编号，请重新输入。")


def notify_wechat(text):
    """把配置结果发到文件传输助手。

    走的是 hook 的 /SendTextMsg 接口，所以需要微信和 version.dll 正在运行。
    返回 (是否成功, 说明)。
    """
    import yaml
    try:
        base_cfg = yaml.safe_load(open(os.path.join(BASE_DIR, "config.yaml"), encoding="utf-8")) or {}
    except Exception:
        base_cfg = {}
    cfg = settings.effective(base_cfg)
    url = (cfg.get("aixed_base_url") or "http://127.0.0.1:30001").rstrip("/") + "/SendTextMsg"
    to = (cfg.get("target_chats") or ["filehelper"])[0]
    payload = {"wxidorgid": to, "msg": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True, to
    except Exception as e:
        return False, str(e)


def main():
    p = pick_provider()
    if not p:
        print("已取消。")
        return 0

    print()
    print("=" * 58)
    print(f"  已选：{p['name']}")
    print(f"  接口地址：{p['base_url'] or '（待填）'}")
    print(f"  默认模型：{p['model'] or '（待填）'}")
    print(f"  拿 key ：{p['key_url']}")
    print("=" * 58)

    base_url = p["base_url"]
    model = p["model"]
    if not base_url:
        base_url = ask("  请输入接口地址（base_url）：")
    if not model:
        model = ask("  请输入模型名：")

    if p["models"]:
        print(f"\n  可选模型：{', '.join(p['models'])}")
        model = ask(f"  用哪个模型（回车 = {model}）：", model)

    print()
    print("  粘贴 API Key（输入时不回显）。")
    # getpass 在非终端（比如管道输入）下会直接卡住，所以先判断是不是终端
    if sys.stdin.isatty():
        try:
            raw = getpass.getpass("  Key：")
        except Exception:
            raw = input("  Key：")
    else:
        raw = input("  Key：")
    key = clean_key(raw)
    if not key:
        print("  [X] 没填 key，已取消。")
        return 1
    print(f"  已读取 {len(key)} 个字符：{mask(key)}")
    if len(key) < 20:
        print("  [!] 这个 key 看起来偏短，确认一下有没有少拷。")

    # 写进 settings.json（优先级高于 config.yaml，所以不用改 config.yaml）
    data = settings.load()
    data.update({
        "provider": p["provider"],
        "base_url": base_url,
        "model": model,
        "api_key": key,
    })
    settings.save(data)
    print(f"\n  [√] 已写入 settings.json")
    print(f"      provider = {p['provider']}")
    print(f"      base_url = {base_url}")
    print(f"      model    = {model}")
    print(f"      api_key  = {mask(key)}")

    # 顺手测一下，省得之后在微信里才知道配错了
    print("\n  正在测试连通性 ...")
    test_ok, test_msg = False, ""
    try:
        from llm import ChatLLM
        c = ChatLLM(model=model, api_key=key, base_url=base_url,
                    max_tokens=32, temperature=0.7, provider=p["provider"])
        out = c.chat("你是测试助手。", [{"role": "user", "content": "只回两字：成功"}])
        test_ok, test_msg = True, f"测试通过，模型回了「{out.strip()[:20]}」"
        print(f"  [√] {test_msg}")
    except Exception as e:
        test_msg = f"测试失败：{str(e)[:180]}"
        print(f"  [X] {test_msg}")
        print("      配置已保存，但可能有问题——检查 key、接口地址、模型名，或网络。")

    # 把这套配置发到文件传输助手，方便你在微信里直接看到
    print("\n  正在把配置信息发到文件传输助手 ...")
    summary = "\n".join([
        "【模型配置已更新】",
        f"服务商：{p['name']}",
        f"协议：{p['provider']}",
        f"接口地址：{base_url}",
        f"模型：{model}",
        f"API Key：{mask(key)}",
        f"连通性：{'✅ ' + test_msg if test_ok else '❌ ' + test_msg}",
        "",
        "发 /help 查看所有命令；",
        "发 /status 随时查看当前配置。",
    ])
    sent, info = notify_wechat(summary)
    if sent:
        print(f"  [√] 已发送到「{info}」")
    else:
        print(f"  [!] 没能发出去：{info}")
        print("      通常是微信没启动、或 hook 没加载（需要看到 30001 端口在监听）。")
        print("      配置已经存好了，不影响使用。")

    print("\n  ── 下一步 ──")
    print("  如果 bot 正在运行，重启它才会读到新配置（双击「启动助手.bat」）。")
    input("  按回车退出 ...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(1)
