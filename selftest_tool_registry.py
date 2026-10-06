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
6. 工具的**可达范围放宽了**（2026-10-05 真踩过：`send_file` 从「只认 `msg/file/`」
   放宽成「也认盘上绝对路径」，代码/规格都改了），但**模型真正读的那几处没跟着说**
   → 模型照旧回「做不到」，功能等于没做。模型可见的文本必须说全。

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
    #
    # 例外只有一条，而且是**用户拍板的**（2026-10-03）：「打电话只删描述」——
    # `call` 的 TOOLS 定义删掉了（模型看不到、不会再提），但 `t_call` 与
    # callgate / hook 的 `/CallVoip` 全保留，所以它现在**注定是孤儿**。
    # 这条例外写在代码里、带原因和恢复动作，别的孤儿照样算失败。
    print("── 1 · TOOLS 与 t_* 处理器双向对齐 ──")
    no_handler = [n for n in names if n not in handlers]
    check("每个工具都有处理器（否则模型点了报「没有名为 X 的工具」）",
          not no_handler, f"缺处理器：{no_handler}")
    intended_orphans = {"call": "描述已删、代码保留：把 TOOLS 里那条 call 加回去即可恢复"}
    orphan = [n for n in handlers if n not in names and n not in intended_orphans]
    check("没有孤儿处理器（有实现却没声明，模型不知道它存在）",
          not orphan,
          f"未声明：{orphan}（故意保留的孤儿：{sorted(intended_orphans)}）")
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
        check(f"{len(names)} 个工具名都出现在示例的 system_prompt 里（缺 {len(miss)} 个）",
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

    # ── 5 · 按**契约**注册的工具（插件 + `files.py` 这类核心消费者）──────────
    #
    # 为什么和上面四项**分开查**：示例 config 的 system_prompt 不可能预先写上一个
    # 第三方插件的名字，所以第 3 项那条检查对它们天然不适用。这不是放松 ——
    # 契约要求它们的指导**随定义自带**（`guidance`），比写在示例 config 里
    # **更靠近定义**，而这条正是 `send_asset` 那次静默失效的根治办法。
    print("── 5 · 按契约注册的工具：自带 guidance + 不与内置重名 ──")
    try:
        import files            # noqa: F401  导入即注册 computer_files
    except ImportError:
        print("  ℹ️  没有 files.py（这一项跳过）")
    import plugins

    contract = [t for t in plugins.REGISTRY.tools()
                if plugins.REGISTRY.get(t["name"])["source"] != "builtin"]
    if not contract:
        skill("没有任何按契约注册的工具")
    else:
        no_g = [t["name"] for t in contract
                if not (plugins.REGISTRY.get(t["name"]).get("guidance") or "").strip()]
        check(f"{len(contract)} 个契约工具都自带 guidance（示例 config 不可能预先教到它们）",
              not no_g,
              f"缺 guidance：{no_g}（插件名不可能预先写在 config.example.yaml 里，"
              f"所以指导必须随定义走）")

        clash = sorted({t["name"] for t in contract} & set(names))
        check("契约工具没有和内置 TOOLS 重名", not clash, f"撞名：{clash}")

        shape = {tuple(sorted(t.keys())) for t in contract}
        check("契约工具的形状与内置同形（name/description/parameters）",
              shape <= {("description", "name", "parameters")}, shape)

        # ⚠️ **存了必须真的送出去**：`guidance_text()` 就是那条送出的路
        # （`bot.system_now()` 把它拼进系统提示）。只存不送等于**装作处理了**
        # 那个老 bug，而且比不存更坏 —— 看代码的人会以为这条路是通的。
        sent = plugins.REGISTRY.guidance_text()
        miss = [t["name"] for t in contract if t["name"] not in sent]
        check("这些 guidance **真的会进系统提示**（存了不送比不存更坏）",
              not miss, f"送不出去的：{miss}")

    # ── 6 · 可达范围放宽了，**模型看得见的那几处**必须跟着说 ──────────────
    #
    # `send_file` 的可达范围在 2026-10-04（T9）就从「只认微信 `msg/file/`」放宽成
    # 「`msg/file/` ∪ 盘上任意路径（过 `files.path_ok`）」了：代码、`t_send_file`
    # 的 docstring、`docs/computer-files-spec.md` 全改了 —— **但模型真正读的两处没改**：
    # `TOOLS['send_file'].description` 还写着「只给文件名，不要带目录或盘符」，
    # 两份 config 的 system_prompt 还写着「按文件名在微信收/发过的文件里定位」。
    # 后果（2026-10-05 真机）：用户说「把桌面上 TF/TF/1.docx 发到群里」，助手照那份
    # 说明**如实拒绝** —— 它没说谎，它只是**不知道**；用户看到的就是「这功能做不到」。
    # 所以这条钉死：**模型可见的文本必须说全两种给法**，否则放宽等于没放宽。
    print("── 6 · send_file 的两种来源都写进了模型可见的文本 ──")
    _sf = next((t for t in agent_tools.TOOLS if t["name"] == "send_file"), None)
    _desc = (_sf or {}).get("description") or ""
    check("send_file 的说明提到按文件名找（`msg/file/`）这一路",
          "msg/file/" in _desc, "老那条路丢了？模型会以为盘上那份也能按名字找到")
    check("send_file 的说明提到「绝对路径」（盘上那份文件）这一路",
          "绝对路径" in _desc,
          "放宽只写在代码里 = 模型照旧回「发不了电脑上的文件」（2026-10-05 真机）")
    for _label, _prompt in (("config.example.yaml", pe),
                            ("config.yaml", prompt_of(li, "config.yaml"))):
        if _prompt is None:
            skill(f"{_label} 没有 system_prompt——跳过")
            continue
        check(f"{_label} 的 system_prompt 也提到「绝对路径」",
              "绝对路径" in _prompt,
              "模型可见的指导没跟上放宽：开发机上 / 发布包里会静默差一半")

    print("=" * 66)
    if _ok:
        print("全部通过 ✅")
        return 0
    print("有失败项 ❌")
    return 1

if __name__ == "__main__":
    sys.exit(main())
