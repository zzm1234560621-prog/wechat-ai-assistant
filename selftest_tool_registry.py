"""工具注册表的全量一致性自测——防「加了一处、漏了另一处」。

## 为什么要有这份自测（2026-10-02 真踩过）

`send_asset`（素材暂存转发）当时的状态是：

* `agent_tools.TOOLS` 里**有**它；
* 开发机上的 `config.yaml`（system_prompt）里**有**教模型怎么用它的那 8 行；
* 但是**推到 GitHub / 打进产品包的那份 `config.example.yaml` 里一个字都没有**——
  连 `assets:` 这个配置段都没有。

后果不是「报错」，而是**静默失效**：在开发机上这个功能好使，换台电脑/用发布包，
模型虽然能从 TOOLS 里看到这个工具，却拿不到「用户说『刚才那张发给他』时必须调用
send_asset」「暂存区空着要说清、绝不许编一张」这些判据——功能就悄悄废了。
这正是项目最怕的那一类 bug（见 CLAUDE.md「静默失效是最大的坑」）。

**为什么当时没被挡住**：`selftest_web.py` 只对 `web_search` 这一个工具检查了
「两处注册」，没有任何测试做**全量**交叉校验。这份自测补的就是这个洞。

## 它守的规矩（项目自己的约定，这里把它变成闸门）

1. `TOOLS` 声明了却**没有** `t_<name>` 处理器 → 模型点了只会收到「没有名为 X 的工具」；
2. 有 `t_<name>` 处理器但 `TOOLS` **没声明** → 孤儿处理器，模型压根不知道它存在；
3. 工具名没出现在 **`config.example.yaml` 的 system_prompt** 里 → 发出去就丢指导；
4. `config.yaml` 有、`config.example.yaml` **没有**的顶层配置段 → 同上，功能只在开发机上活着；
5. `TOOLS` 里重名、或缺 `description` / `parameters` → 模型容易用错。

用法：
    .venv\\Scripts\\python.exe selftest_tool_registry.py
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import agent_tools  # noqa: E402

try:
    import yaml
except ImportError:                                    # pragma: no cover
    print("❌ 缺少 PyYAML（正式依赖，缺了才是问题）")
    sys.exit(1)

_ok = True


def check(label, cond, detail=""):
    global _ok
    if cond:
        print(f"  ✅ {label}")
    else:
        _ok = False
        print(f"  ❌ {label}  {detail}")
    return cond


def skill(label):
    print(f"  ⏭  {label}")


def load_cfg(name):
    p = os.path.join(BASE, name)
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def prompt_of(cfg, name):
    """取顶层 system_prompt。**不猜别的路径**——它挪了位置就该让这份自测失败，
    而不是静默跳过一个已经失效的检查。"""
    if cfg is None:
        return None
    v = cfg.get("system_prompt")
    return v if isinstance(v, str) else None


def main():
    names = [t["name"] for t in agent_tools.TOOLS]
    handlers = sorted(m[2:] for m in dir(agent_tools.ToolBox) if m.startswith("t_"))

    print(f"工具注册表一致性（TOOLS {len(names)} 个 / 处理器 {len(handlers)} 个）")
    print("=" * 66)

    # ── 1 · TOOLS ↔ 处理器，双向不许有缺口 ───────────────────────────────
    print("── 1 · TOOLS 与 t_* 处理器双向对齐 ──")
    no_handler = [n for n in names if n not in handlers]
    check("每个工具都有处理器（否则模型点了报「没有名为 X 的工具」）",
          not no_handler, f"缺处理器：{no_handler}")
    orphan = [n for n in handlers if n not in names]
    check("没有孤儿处理器（有实现却没声明，模型不知道它存在）",
          not orphan, f"未声明：{orphan}")
    dupes = sorted({n for n in names if names.count(n) > 1})
    check("TOOLS 里没有重名", not dupes, f"重名：{dupes}")

    # ── 2 · 每个工具的 schema 完整 ────────────────────────────────────────
    print("── 2 · 工具的 description / parameters 完整 ──")
    no_desc = [t["name"] for t in agent_tools.TOOLS
               if not (t.get("description") or "").strip()]
    check("每个工具都有 description", not no_desc, f"缺说明：{no_desc}")
    no_param = [t["name"] for t in agent_tools.TOOLS if not t.get("parameters")]
    check("每个工具都有 parameters", not no_param, f"缺 schema：{no_param}")

    # ── 3 · 发出去的那份配置必须教到每个工具 ─────────────────────────────
    print("── 3 · config.example.yaml（发出去的那份）教到了每个工具 ──")
    ex = load_cfg("config.example.yaml")
    check("config.example.yaml 存在且能解析", ex is not None)
    pe = prompt_of(ex, "config.example.yaml")
    if pe is None:
        check("config.example.yaml 里有顶层 system_prompt", False,
              "system_prompt 挪位置了？这份自测的检查点会失效，先修它")
    else:
        miss = [n for n in names if n not in pe]
        check(f"25 个工具名都出现在示例的 system_prompt 里（缺 {len(miss)} 个）",
              not miss, f"发出去就丢指导：{miss}")

    # ── 4 · 两份配置的顶层段必须对齐（开发机有、发出去没有 = 功能只在本地活）──
    print("── 4 · config.yaml 与 config.example.yaml 顶层配置段对齐 ──")
    li = load_cfg("config.yaml")
    if li is None:
        skill("本机没有 config.yaml（干净克隆/新机器）——跳过对齐检查")
    else:
        only_live = sorted(set(li) - set(ex))
        check("没有「只在 config.yaml 里」的配置段（那种段发出去就没了）",
              not only_live,
              f"这些段只在开发机上有，发布包里没有：{only_live}")
        only_ex = sorted(set(ex) - set(li))
        if only_ex:
            # 反过来只是「示例里多写了」，不影响用户能不能用，只提示不判失败
            print(f"  ℹ️  示例里多出的段（不判失败）：{only_ex}")
        pl = prompt_of(li, "config.yaml")
        if pl is None:
            skill("config.yaml 里没有顶层 system_prompt——跳过")
        else:
            miss_live = [n for n in names if n not in pl]
            check("开发机这份 system_prompt 也教到了每个工具",
                  not miss_live, f"本地缺：{miss_live}")

    print("=" * 66)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1


if __name__ == "__main__":
    sys.exit(main())
