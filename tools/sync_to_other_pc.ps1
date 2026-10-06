# 同步给「另一台电脑」的助手：把仓库里改好的文件拷到打包目录
#
# 背景一（2026-10-05 部署真机）：包里的 config.yaml 是 config.example.yaml 的拷贝，
# self_wxid 是空的，而这个 hook 构建的 /GetSelfProfile 不返回 wxid —— 换台电脑就露空，
# 表现为 bot 把自己以前说过的话当成用户的新提问再答一遍。
# 背景二（2026-10-06 第二台部署机）：补上的「从 contact 表认」是**行序经验**，那台机器上
# 它认成了别人 ⇒ is_self 恒为 0 ⇒ bot 一遍遍自己回答自己刚发出的回复（「重复回复」）。
# 修法：账号目录核实 + 认不出时的兜底闸门（见 docs\self-wxid-three-sources.md）。
#
# ⚠️ **文件清单必须跟着改动走**：这次 bot.py 调了 `live_history.self_identity_ok()`，
#    所以 live_history.py 必须在清单里 —— 少一个文件换来的不是「少个功能」，
#    而是主循环里 AttributeError 刷屏（改这块时记得回头看一遍清单）。
#
# 用法（在**开发机**上跑；默认写到「这台机器的桌面」下的助手同步目录，换台电脑也对）：
#     powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_to_other_pc.ps1
#     powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_to_other_pc.ps1 -Dest "E:\助手同步"
#     （装过 PowerShell 7 的机器把 `powershell` 换成 `pwsh` 也行；本机没装 pwsh）
#
# ⚠️ 默认值**绝不能写成某台机器的绝对路径** —— `selftest_portable.py` 会拦
#    「本机项目路径」（`<盘符>:\...\wechat-ai-assistant`），那正是它要防的坑。
#    所以默认值由 `[Environment]::GetFolderPath('Desktop')` 现算。
#
# ⚠️ 只拷**代码与文档**，绝不拷 config.yaml / settings.json / data\ ——
#    那三样是本机凭证与状态（含 API key、聊天记忆），拷过去既会泄露、
#    也会把旧游标带过去（那会造成「重启补齐、只通知不回复」）。
param(
  [string]$Dest = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # 项目根（本脚本在 tools\ 下）

if (-not $Dest) {
  $desktop = [Environment]::GetFolderPath("Desktop")
  if (-not $desktop) { $desktop = [Environment]::GetFolderPath("UserProfile") }
  $Dest = Join-Path $desktop "wechat-ai-assistant-sync"
}

# 要同步的文件（= 这次修复触及的代码 + 新脚本 + 新回归 + 新文档）
$code = @(
  "aixed_api.py",          # 改动：detect_self_wxid() + account_dir_wxids() + resolve_self_wxid()
  "bot.py",                # 改动：启动走唯一解析入口 + 认不出时的兜底闸门
  "live_history.py",       # 改动：self_identity_ok()（bot.py 要调，缺了会 AttributeError）
  "verify_real.py",        # 改动：自检与 bot 走同一条路，来源与核实都报出来
  "find_self_wxid.py",     # 离线找 wxid 的小工具（核实判据也用它）
  "selftest_aixed.py",     # 改动：四级 + 核实的回归
  "selftest_install.py",   # 改动：打包机段（四级优先级的真跑用例）
  "selftest_live_history.py",  # 改动：兜底路 sender 不是 wxid 时的回归
  "selftest_bot_loop.py",  # 改动：兜底闸门的回归
  "selftest_self_wxid.py", # 新增：离线找 wxid 的自测
  "docs\self-wxid-three-sources.md"   # 新增：这次的结论与做法
)
# 只做参考、**不覆盖**：另一台机器的 config.yaml 是用户填过模型/key 的那份
$ref = @("config.example.yaml")

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dest "docs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Dest "_reference-do-not-overwrite") | Out-Null

$missing = @()
foreach ($f in $code) {
  $src = Join-Path $root $f
  if (-not (Test-Path $src)) { $missing += $f; continue }
  Copy-Item $src (Join-Path $Dest $f) -Force
  Write-Host "[√] $f"
}
foreach ($f in $ref) {
  $src = Join-Path $root $f
  if (-not (Test-Path $src)) { $missing += $f; continue }
  Copy-Item $src (Join-Path (Join-Path $Dest "_reference-do-not-overwrite") $f) -Force
  Write-Host "[√] （参考，别覆盖）$f"
}

if ($missing.Count -gt 0) {
  Write-Warning ("这些文件没找到，别当同步成功：" + ($missing -join "、"))
}

Write-Host ""
Write-Host "同步到：$Dest"
Write-Host "里面没有 config.yaml / settings.json / data\（有意为之：那是本机凭证与状态）。"
Write-Host "下一步见 $(Join-Path $Dest 'docs\self-wxid-three-sources.md')"
