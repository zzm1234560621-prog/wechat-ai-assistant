# 打「可以给别的电脑装」的产品包。
#
# 用法（项目根目录下）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_package.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_package.ps1 -NoZip
#
# 产出：
#   <项目上一级>\dist\wechat-ai-assistant-<日期>\      目录，可直接整个拷走
#   <项目上一级>\dist\wechat-ai-assistant-<日期>.zip   单文件，发给别人用这个
#
# 四条规矩（改这个脚本时别破坏它们）：
#   1. **绝不把私人数据打进包**：`data\`、`*.log`、`test_images\`、以及安装脚本的运行日志
#      （`installers\**\*-log.txt`，里面带本机用户名和绝对路径）一律排除。
#   2. **绝不把真实配置打进包**：仓库里的 `config.yaml` / `settings.json` 是本机那份
#      （含 API key），包里放的是 `config.example.yaml` / `settings.example.json` 的副本。
#   3. **不打包 `.venv`**：目标机器上由 `install.bat` 自己建（跨机器拷虚拟环境一定坏）。
#   4. **不带 3.9.x 的微信安装包**：那是 wcferry 旧后端用的，主线（4.x + hook）用不上，
#      白白多 272MB。
#
# ⚠️ 本文件必须存成 UTF-8 **带 BOM**（PowerShell 5.1 没 BOM 时中文会乱码）。
param(
    [string]$OutDir,
    [switch]$NoZip
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot          # tools\ 的上一级 = 项目根
$stamp = Get-Date -Format 'yyyy.MM.dd'
$name = "wechat-ai-assistant-$stamp"
$dist = if ($OutDir) { $OutDir } else { Join-Path (Split-Path -Parent $root) 'dist' }
$pkg = Join-Path $dist $name

Write-Output "项目根：$root"
Write-Output "输出到：$pkg"

if (Test-Path $pkg) { Remove-Item $pkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pkg | Out-Null

# ── 1 · 根目录的源码与入口 ────────────────────────────────────────────
Copy-Item (Join-Path $root '*.py')  $pkg -Force
Copy-Item (Join-Path $root '*.bat') $pkg -Force
# `README.zh-CN.md` / `LICENSE` 是 2026-10-06 公开到 GitHub 时加的：README 中英双语，
# 拿到包的人也拿到同一份协议与免责声明（包里那份 README.md 顶部的语言切换就指向它）。
foreach ($f in @('README.md', 'README.zh-CN.md', 'LICENSE', 'CLAUDE.md', 'requirements.txt')) {
    $p = Join-Path $root $f
    if (Test-Path $p) { Copy-Item $p $pkg -Force } else { Write-Warning "缺少 $f" }
}

# ⚠️ 两个微信官方安装程序（WeChatWin_4.1.10.27.exe / WeChatSetup-3.9.12.51.exe）
# **2026-10-06 起不进 git**（走 LFS 会烧掉免费额度，约两次克隆就见底），
# 但它们**必须进这个包** —— 普通用户就是靠包里那份 `⓪` 一键装的。
# 所以：git 里没有是正常的，这里没有才是问题（下面 step 4 会 Write-Warning）。

# ── 2 · 示例配置改名成实配（包里的 key 一定是空的）────────────────────
Copy-Item (Join-Path $root 'config.example.yaml')   (Join-Path $pkg 'config.yaml')   -Force
Copy-Item (Join-Path $root 'settings.example.json') (Join-Path $pkg 'settings.json') -Force
# 示例文件**也要留着**：`selftest_web.py` 会同时检查 config.yaml 和 config.example.yaml
# 里都注册了 `search.enabled`（防「只改了一处」）。包里只留实配的话，用户跑
# selftest_all.py 会看到一份假失败——那不是他装错了，是我们少放了一个文件。
Copy-Item (Join-Path $root 'config.example.yaml')   $pkg -Force
Copy-Item (Join-Path $root 'settings.example.json') $pkg -Force

# ── 3 · 文档与工具 ────────────────────────────────────────────────────
# ⚠️ **这里是一个显式清单，新目录必须手动加进来** —— 加漏了不会报错，
# 而是「开发机上好用、发布包里静默失效」（本项目被咬过好几次）。
# `plugins/` 是 2026-10-04 加的插件目录：少了它，README 里「复制 `plugins/_example.py`」
# 就是死指令，而 `selftest_plugins.py` §11 会在朋友的机器上失败。
# `searxng/` 是 2026-10-05 加的**随包携带的搜索后端**：少了它，别人机器上
# `web_search` 永远用不了（`search.home` 会指到一个不存在的目录）——
# 这正是这条倡议当初存在的理由。它的 .venv/缓存由下面那段剪掉。
# 回归：`selftest_portable.py` 有一条「代码要用的目录都在这个清单里」。
foreach ($d in @('docs', 'tools', 'plugins', 'searxng')) {
    $p = Join-Path $root $d
    if (Test-Path $p) { Copy-Item $p $pkg -Recurse -Force }
}

# `hook_doctor.py`（hook 版本 / 启动闸门的一站式只读诊断）**必须在包根也放一份**：
# 用的人是「拿到包的人」，让他 cd 进 tools\ 再敲路径是多一道坎——而少了它，
# 2026-10-06 那种「换了新包仍起不来」就只能靠猜（真机就吃了这一口：
# 微信目录里的 hook 是旧的，而没有任何地方对比过包里那份与已装那份）。
# `selftest_portable.py` 钉着「包根有 hook_doctor.py 且能 import hook_check」。
$doctor = Join-Path $root 'tools\hook_doctor.py'
if (Test-Path $doctor) {
    Copy-Item $doctor (Join-Path $pkg 'hook_doctor.py') -Force
} else {
    Write-Warning '缺少 tools\hook_doctor.py（出问题时包里没有诊断工具）'
}

# searxng 复制完之后，**必须剪掉两样东西**（它们会在开发机上长出来）：
#   * `.venv\`（约 91MB）—— venv 里记的是绝对路径，跨机器拷必坏，和 bot 自己的 .venv
#     同一条规矩；新机器上由「一键部署」第 3 步现建。
#   * `sxng_cache_*.db` —— 运行期缓存，谁都不要。
$sx = Join-Path $pkg 'searxng'
if (Test-Path $sx) {
    if (Test-Path (Join-Path $sx '.venv')) {
        Remove-Item (Join-Path $sx '.venv') -Recurse -Force
        Write-Output '已剪掉 searxng\.venv（venv 绝不跨机器拷）'
    }
    $junk = @(Get-ChildItem $sx -Recurse -Force -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -like 'sxng_cache_*' -or $_.Name -eq '__pycache__' })
    if ($junk) {
        $junk | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Output "已剪掉 searxng 的运行期缓存 $($junk.Count) 项"
    }
}

# ── 4 · hook 那一套（4.1.10.27 主线）────────────────────────────────
$srcInst = Join-Path $root 'installers\wechat-4.1.10.27'
$dstInst = Join-Path $pkg  'installers\wechat-4.1.10.27'
New-Item -ItemType Directory -Force -Path $dstInst | Out-Null

Copy-Item (Join-Path $srcInst '*.ps1') $dstInst -Force     # 含 _common.ps1
Copy-Item (Join-Path $srcInst '*.dll') $dstInst -Force     # version.dll 及其各个备份/变体
foreach ($f in @('WeChatWin_4.1.10.27.exe', 'WeChat-Hook-4.1.10.27.zip')) {
    $p = Join-Path $srcInst $f
    if (Test-Path $p) { Copy-Item $p $dstInst -Force } else { Write-Warning "缺少 $f（包里的 hook 装不上）" }
}
# 安装脚本的运行日志绝不入包（带本机用户名和绝对路径）
Get-ChildItem $dstInst -Filter '*-log.txt' -File -ErrorAction SilentlyContinue | Remove-Item -Force

# hook 源码：排除编译产物（.obj/.pdb/.tlog 合计 ~46MB）与 x64_Version 构建目录，
# 但**保留** x64\Release\version.dll —— do_deploy_loginready.ps1 要用它。
$srcSrc = Join-Path $srcInst 'src-4.1.10.27'
if (Test-Path $srcSrc) {
    $dstSrc = Join-Path $dstInst 'src-4.1.10.27'
    robocopy $srcSrc $dstSrc /E /XD x64_Version `
        /XF *.obj *.pdb *.tlog *.exp *.lib *.iobj *.ipdb *.ilk *.log `
        /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 复制 hook 源码失败，退出码 $LASTEXITCODE" }
}

# ── 5 · 给用户看的首页 ───────────────────────────────────────────────
$quickstart = @'
【微信 AI 助手 —— 安装说明】

这个包是给**没装过**的电脑用的，全程大概 15 分钟。
详细文档在 README.zh-CN.md（英文版 README.md），这里只是最短路径。

■ 先知道两件事
  1. 本工具往微信进程里注入 hook DLL，**违反微信用户协议**，有封号风险。
     建议先拿小号试，风险自担。
  2. hook 是按微信 **4.1.10.27** 这一个版本编译的，微信一升级就失效。
     装 hook 时会顺手挡住微信自动更新，别自己去升级微信。

■ 再知道一件（2026-10-06 起）：**助手要以管理员身份运行**
  启动助手时会弹一次 UAC，**点「是」**。这不是可选步骤：语音条要读微信进程内存，
  而普通权限读不了提权开的微信（Windows 的完整性级别限制）。
  点「否」也能起来，但语音条会明确告诉你读不出来。
  （为什么、以及"开机自启那条路不会自动提权"，见 docs\admin-elevation-notes.md）

■ 装法（推荐）：**双击 `一键部署.bat`，然后一路回车**（它就等于助手.bat → [9]，省掉按菜单）
  它按真实顺序走一遍：
    ⓪ 查微信版本（没装 / 不是 4.1.10.27 就装包里自带的那份官方安装程序）
    ① 装 hook 进微信（会弹 UAC，要管理员权限——这一步不做，后面全白搭）
    ② 装 Python 依赖（要联网下载，第一次几分钟）
    ③ 可选组件（语音转文字 / 网上搜索 / 文件格式包 / 语义检索；**逐项问你**，跳过不影响其它功能）
    ④ 启动助手（后台运行）
    ⑤ 就地配「用哪个模型 + API Key」（不用去微信里打字）
  前提：这台电脑要有 **64 位 Python**（推荐 3.11）。`一键部署.bat` 找不到会直接告诉你
  装哪条命令（winget install -e --id Python.Python.3.11），装完再双击一次即可。
  装完之后日常用 **助手.bat**： [3] 启动 / [4] 停 / [5] 看状态 / [6] 看日志。
  ⚠️ 微信版本必须是 4.1.10.27（微信里「设置 → 关于微信」看一眼）：hook 是按**这一个
     版本**编译的，换个版本就注不进去——而且**不报错**（DLL 会被微信正常加载，但 30001
     永远没人监听，你只会看到助手一直「连不上 30001」）。第 ⓪ 步会自动装包里那份；
     手工装就双击 installers\wechat-4.1.10.27\WeChatWin_4.1.10.27.exe，弹
     「你已安装新版本的微信，安装更早的版本？」时点**「继续安装」**。
     装微信会掉一次登录态，**装完要重新扫码登录**。
  装完**重启微信**，浏览器打开 http://127.0.0.1:30001/QueryDB/status
  能返回 JSON 就说明 hook 装好了（"IsLogin": 1 才是真的登录成功）。

■ 装法（手工，等价于 [9]；每条都要在**管理员** PowerShell 里跑）
  1. cd "<解压出来的目录>\installers\wechat-4.1.10.27"
     powershell -NoProfile -ExecutionPolicy Bypass -File .\do_hook_install.ps1
     （微信版本不对时，才先跑一次 .\do_install.ps1）
     脚本会自己找微信目录和你的用户目录，**不用改任何东西**；
     结果看同目录的 hook-install-log.txt。
  2. 双击 **install.bat**，等依赖装完（会自己建 .venv）。
  3. 双击 **启动助手.bat**（或 助手.bat → [3]）。
  4. 双击 **配置模型.bat** 按提示填；也可以之后在微信里发  /api <你的key>

■ 可选组件（语音转文字 / 网上搜索 / 文件格式包 / 语义检索）—— 它们**不随主程序装**
  原因很实在：语音要下几百 MB 的本地模型，搜索后端要一份自己专用的虚拟环境，
  语义检索会拖进 torch，这些都不能塞进主程序的安装里。所以 `一键部署.bat` 的第 ③ 步
  会**逐项问你**；跳过了也完全不影响聊天、发消息、读文件、定时。
  以后想装/想关：双击 **可选组件.bat**（它也会告诉你每一项现在什么状态）。
  **四项默认都会装**（一路回车就齐），只有两处会停下来问：语义检索的「建索引要停助手」，
  以及你想跳过某一项时按 n。
  · 语音转文字：装 faster-whisper + pilk，并下**本地模型**（大小看 config.yaml 的
    `audio.model`，默认 small 约 464MB，走 hf-mirror 镜像）。音频一个字节都不出本机。
  · 网上搜索：包**自带 SearXNG 源码**（searxng\），这一步会在它的目录里建一份
    **自己专用的 .venv** 并装依赖，**装完自动打开**（把 search.enabled 写进 settings.json）
    并把服务起起来。装好之后助手启动也会把它一起带起来（config.yaml 的 search.autostart）。
  · 文件格式增强包：一条命令装七个（视频 av、.msg 邮件 extract-msg、压缩包 py7zr/rarfile、
    老 Office xlrd/olefile、PDF 内嵌图 Pillow），几十 MB，装完立刻多会读那几种格式。
    ⚠️ `.rar` 光装 rarfile 还不够——它只是个壳，真正解压要外部程序（unrar / 7-Zip）。
       状态屏会把这两件事分开说，不会把「装了 rarfile」报成「.rar 能读了」。
  · 本地语义检索：它最重（torch 几百 MB + 本地模型 + 建索引）。装完**自动打开**
    （semantic.enabled 写进 settings.json）；最后一步「建索引」**要先停一下助手**——
    装的时候它会问你，同意就自动「停助手 → 建索引 → 起回来」，不同意就只把命令留给你。
  四项的「以后还要不要自动装」记在 settings.json 的 `optional` 里（关掉 = 不再自动装，
  **已经装好的东西不会动**）。它写 settings.json 而不是 config.yaml，是因为程序
  从不回写带注释的 config.yaml（装完自动打开的那两个开关同理）。

■ 装完之后怎么用
  双击 **助手.bat** 就是全部： [3] 启动 / [4] 停止重启 / [5] 看状态 /
  [6] 看日志 / [8] 更多…（配模型 / 真机自检 / 跑自测 / hook / 自启）。
  菜单里按 **[L]** 可以在**中文 / English** 之间切换界面语言（微信里发 `/lang en` 也行）。
  然后**全程在微信里操作**，直接跟助手说话就行。

■ 出问题了看哪
  · **先跑 `hook_doctor.py`**（只读，助手开着也能跑）：
      .venv\Scripts\python.exe hook_doctor.py
    它会一次说清四件事：微信目录里的 hook 是哪一版（**和包里那份比**）、
    运行中的 hook 是哪一版、启动闸门为什么没开、微信数据目录里的库有没有在被写。
    ⚠️ 最常踩的一条：**解压新包不会替换微信目录里那份 hook**（两个文件）。
       助手一直刷「hook 已加载，但数据库打不开（微信没登录？）」时，先跑这一条：
         (Get-Item "C:\Program Files\Tencent\Weixin\version.dll").Length
       是 **519168** 就是旧的（新版 **527360**）。修法：助手.bat → [8] → [7] → [4]
       只替换 version.dll，然后**完全退出微信再打开、扫码登录**。
       详细判据见 README 的「助手一直刷…怎么办」一节。
  · README.md 的「助手一直刷「hook 已加载，但数据库打不开」怎么办」一节
     （最常见的问题都在那儿；中文版是 README.zh-CN.md）
  · 助手没反应 → 先看 bot.log（或 助手.bat → [6]）
  · 想自测（不需要真微信、不碰 hook）：助手.bat → [8] → 跑全部自测

■ 这个包里**没有**什么
  · 没有聊天记录、没有本机配置、没有 API key（config.yaml 是示例，key 是空的）
  · 没有 Python 虚拟环境（install.bat 会自己建；SearXNG 那份也一样，由第 ③ 步现建）
  · 没有语音模型（几百 MB，第 ③ 步现下；不下就只是用不了语音转文字）
  · 没有 3.9.x 的微信安装包（旧后端才要，主线用不上）
'@
Set-Content -Path (Join-Path $pkg '从这里开始.txt') -Value $quickstart -Encoding UTF8

# ── 6 · 自检：私人数据不许入包 ───────────────────────────────────────
$fail = @()
if (Test-Path (Join-Path $pkg 'data'))       { $fail += '包里有 data\（私人运行数据）' }
if (Test-Path (Join-Path $pkg 'test_images')){ $fail += '包里有 test_images\（私人图片）' }
if (Test-Path (Join-Path $pkg '.venv'))      { $fail += '包里有 .venv\（不该跨机器拷）' }
# 随包携带的 SearXNG：**必须在**（否则别人机器上搜索永远用不了），
# 且它自己的 venv / 运行期缓存**必须不在**（跨机器拷必坏）。
$sx = Join-Path $pkg 'searxng'
if (-not (Test-Path (Join-Path $sx 'searx\webapp.py'))) {
    $fail += '包里没有 searxng\searx\webapp.py（搜索后端没随包 → 别人机器上 web_search 用不了）'
}
# ⚠️ 这条盯的是 2026-10-05 真踩的坑：仓库根的 `.gitignore` 里那条没锚定的 `data/`
# 曾把 `searxng/searx/data/` 整个挡在 git 外面（磁盘上有、git 里没有）。打包走的是磁盘
# 所以当时没露，但「从克隆重打包」就会静默少这块。这里钉住它，别再回去。
if (-not (Test-Path (Join-Path $sx 'searx\data\engine_traits.json'))) {
    $fail += 'searxng\searx\data\ 缺必需运行期数据（engine_traits.json 等）'
}
if (Test-Path (Join-Path $sx '.venv')) {
    $fail += '包里有 searxng\.venv\（跨机器拷必坏，应由一键部署现建）'
}
$sxjunk = @(Get-ChildItem $sx -Recurse -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'sxng_cache_*' })
if ($sxjunk) { $fail += "包里有 searxng 运行期缓存：$($sxjunk.Name -join ', ')" }
$logs = Get-ChildItem $pkg -Recurse -File -Filter '*-log.txt' -ErrorAction SilentlyContinue
if ($logs) { $fail += "包里有安装日志：$(($logs | ForEach-Object { $_.Name }) -join ', ')" }
$anylog = Get-ChildItem $pkg -Recurse -File -Filter '*.log' -ErrorAction SilentlyContinue
if ($anylog) { $fail += "包里有 .log 文件：$(($anylog | ForEach-Object { $_.Name }) -join ', ')" }
$cfg = Get-Content (Join-Path $pkg 'config.yaml') -Raw
if ($cfg -notmatch 'api_key:\s*""') { $fail += 'config.yaml 里的 api_key 不是空的' }
$st = Get-Content (Join-Path $pkg 'settings.json') -Raw
if ($st -notmatch '"api_key":\s*""') { $fail += 'settings.json 里的 api_key 不是空的' }

if ($fail.Count) {
    Write-Output ''
    Write-Output '自检失败 ❌：'
    $fail | ForEach-Object { Write-Output "  - $_" }
    exit 1
}
Write-Output '自检通过 ✅（无 data\ / test_images\ / .venv\ / 日志，api_key 为空）'

# ── 7 · 统计 + 压缩 ──────────────────────────────────────────────────
$files = Get-ChildItem $pkg -Recurse -File
$sizeMB = [math]::Round((($files | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Output "打包目录：$($files.Count) 个文件，$sizeMB MB"

if (-not $NoZip) {
    $zip = Join-Path $dist "$name.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Write-Output "正在压缩（$sizeMB MB，这一步最慢）..."
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    # ⚠️ **必须自己写条目，不能用 ZipFile::CreateFromDirectory。**
    # 在 .NET Framework 上它把条目名写成**反斜杠**（`installers\x\y.ps1`），而 ZIP 规范
    # 只认正斜杠。Windows 资源管理器能容忍，但 macOS/Linux 的 unzip、以及一些解压/
    # 上传服务会把 `installers\x\y.ps1` 当成**一个文件名里带反斜杠的文件**——
    # 整个目录结构就散了。这个包是要发给别人的，不能赌对方用什么解压。
    # （实测：CreateFromDirectory 出来的 243 条里有 166 条是反斜杠。）
    #
    # Fastest 就够：里面的微信安装包本身已经是压缩过的，再压也省不下多少。
    $fs = [System.IO.File]::Create($zip)
    try {
        $arch = New-Object -TypeName System.IO.Compression.ZipArchive -ArgumentList `
            $fs, ([System.IO.Compression.ZipArchiveMode]::Create)
        try {
            $prefixLen = $pkg.TrimEnd('\').Length + 1
            foreach ($f in $files) {
                $rel = $f.FullName.Substring($prefixLen).Replace('\', '/')
                $entry = $arch.CreateEntry(
                    $rel, [System.IO.Compression.CompressionLevel]::Fastest)
                $es = $entry.Open()
                try {
                    $ins = [System.IO.File]::OpenRead($f.FullName)
                    try { $ins.CopyTo($es) } finally { $ins.Dispose() }
                } finally { $es.Dispose() }
            }
        } finally { $arch.Dispose() }
    } finally { $fs.Dispose() }

    $zipMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)

    # 压完回头验一遍：条目名里不许有反斜杠（上面那段注释说的坑）
    $z = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $badSep = @($z.Entries | Where-Object { $_.FullName -like '*\*' })
        $nEntries = $z.Entries.Count
    } finally { $z.Dispose() }
    if ($badSep.Count) {
        Write-Warning "zip 里有 $($badSep.Count) 条用了反斜杠（例：$($badSep[0].FullName)）"
    }
    Write-Output "已生成：$zip  ($zipMB MB，$nEntries 个条目，路径分隔符全部为正斜杠：$($badSep.Count -eq 0))"
}
Write-Output '完成 ✅'
