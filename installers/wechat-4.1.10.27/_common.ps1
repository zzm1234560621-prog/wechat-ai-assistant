# 同目录 8 个 do_*.ps1 共用的定位逻辑（微信目录 / 登录用户 / 结束微信进程）。
# 用法（在每个 do_*.ps1 的开头）：
#     . (Join-Path $PSScriptRoot '_common.ps1')
#
# ⚠️ 本文件必须存成 UTF-8 **带 BOM**：Windows PowerShell 5.1 在没有 BOM 时会按
# 系统代码页读脚本，在 GBK（936）机器上中文全部变乱码。本机代码页是 65001 看不出问题，
# 换台电脑就会暴露——而「换台电脑」正是这个包存在的意义。
#
# 为什么要有这个文件：以前这 8 个脚本各自把「项目目录 / 微信目录 / 用户名」
# 硬编码成本机路径（项目所在盘符、用户目录），
# README 只能要求用户「换机器先手工改开头三行」。那不是能直接装的产品。
# 现在全部自动探测，脚本可以放在任意目录、装到任意机器。

# ── 这条主线唯一支持的微信版本（唯一真源，别再各处写一遍）──────────────────
# hook（version.dll）是按 4.1.10.27 的**函数偏移**编译的。装在别的版本上，DLL 会被微信
# 正常加载、**不报错、不崩**，只是挂钩失败 —— 30001 永远没人监听，用户看到的是
# 「bot 一直连不上 30001」，而安装脚本打的是「已放置，成功」。
# 2026-10-04 真机踩到：另一台电脑是 4.1.15.13，[9] 一键配置走完、日志全绿，端口从没通。
# 所以「装 hook」之前必须先过版本闸（Test-WeixinVersion），常量只在这里写一份。
$WX_WANTED_VERSION = '4.1.10.27'

function Find-Weixin {
    # 返回微信 4.x 安装目录；找不到返回 $null（调用方必须如实报错，不许瞎写一个路径）。
    # 4.x 把安装路径写在 HKCU\SOFTWARE\Tencent\Weixin；提权后 HKCU 仍是同一个用户，读得到。
    foreach ($key in @('HKCU:\SOFTWARE\Tencent\Weixin', 'HKLM:\SOFTWARE\Tencent\Weixin')) {
        try {
            $k = Get-ItemProperty $key -ErrorAction Stop
            if ($k.InstallPath) {
                $p = $k.InstallPath.TrimEnd('\')
                if (Test-Path (Join-Path $p 'Weixin.exe')) { return $p }
            }
        } catch { }
    }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $root) { continue }
        foreach ($sub in @('Tencent\Weixin', 'Tencent\WeChat')) {
            $c = Join-Path $root $sub
            if (Test-Path (Join-Path $c 'Weixin.exe')) { return $c }
        }
    }
    return $null
}

function Get-LoginUserAppData {
    # 提权之后 $env:APPDATA 会指向**管理员**账户，而微信的更新目录在**登录用户**名下。
    # 依次尝试：控制台登录用户 → explorer.exe 的属主 → 谁的 xwechat 目录真的存在 → $env:APPDATA。
    # 全都认不出来就返回 $null —— 调用方必须把「这一步没做」说出来，不许静默跳过。
    try {
        $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop
        if ($cs.UserName) {
            $p = Join-Path $env:SystemDrive ('Users\' + $cs.UserName.Split('\')[-1] + '\AppData\Roaming')
            if (Test-Path $p) { return $p }
        }
    } catch { }
    try {
        $e = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction Stop |
             Select-Object -First 1
        if ($e) {
            $o = Invoke-CimMethod -InputObject $e -MethodName GetOwner -ErrorAction Stop
            if ($o.User) {
                $p = Join-Path $env:SystemDrive ('Users\' + $o.User + '\AppData\Roaming')
                if (Test-Path $p) { return $p }
            }
        }
    } catch { }
    foreach ($u in @(Get-ChildItem (Join-Path $env:SystemDrive 'Users') -Directory -ErrorAction SilentlyContinue)) {
        $p = Join-Path $u.FullName 'AppData\Roaming'
        if (Test-Path (Join-Path $p 'Tencent\xwechat')) { return $p }
    }
    if ($env:APPDATA -and (Test-Path $env:APPDATA)) { return $env:APPDATA }
    return $null
}

function Get-WeixinUpdateDir {
    # 微信自动更新把 4.1.10.27 顶掉，hook 就废了（甚至崩）。要卡住它就得知道这个目录。
    param([string]$AppData)
    if (-not $AppData) { $AppData = Get-LoginUserAppData }
    if (-not $AppData) { return $null }
    return (Join-Path $AppData 'Tencent\xwechat\update')
}

function Get-AppDataUserName {
    # C:\Users\<用户名>\AppData\Roaming -> <用户名>
    # cacls/icacls 要的是**用户名**（不是 domain\user，也不是 AppData 这一层）。
    #
    # ⚠️ 这里踩过一次：`Split-Path (Split-Path $AppData -Parent) -Leaf` 只往上退一级，
    # 于是 `...\<用户名>\AppData\Roaming` 被解析成 **AppData**。后果不是报错退出，
    # 而是 `cacls ... /P AppData:N` 去拒绝一个**不存在的账户**——「禁用微信自动更新」
    # 这一步**从来没生效过**，日志里只有一行 cacls 报错，看起来像「跑过了」。
    # 所以往上要退**两级**（Roaming -> AppData -> 用户名）。
    param([string]$AppData)
    if (-not $AppData) { return $null }
    $p = $AppData.TrimEnd('\')
    for ($i = 0; $i -lt 2; $i++) { $p = Split-Path $p -Parent }
    if (-not $p) { return $null }
    return (Split-Path $p -Leaf)
}

function Stop-Weixin {
    # 微信 4.x 是主进程 Weixin + 一堆子进程，都得清掉才能换 DLL。返回结束掉的进程数。
    $n = 0
    foreach ($name in @('Weixin', 'WeChatAppEx', 'WeChatOCR', 'WeixinUpdate')) {
        Get-Process $name -ErrorAction SilentlyContinue | ForEach-Object {
            try { Stop-Process -Id $_.Id -Force -ErrorAction Stop; $n++ } catch { }
        }
    }
    return $n
}

function Get-WeixinVersion {
    param([string]$Dir)
    if (-not $Dir) { return $null }
    $exe = Join-Path $Dir 'Weixin.exe'
    if (-not (Test-Path $exe)) { return $null }
    try { return (Get-Item $exe).VersionInfo.ProductVersion } catch { return $null }
}

function Test-WeixinVersion {
    # 'ok' = 就是目标版本，可以装 hook；
    # 'mismatch' = 明确是别的版本（装了也不会生效，必须换版本）；
    # 'unknown' = 读不出版本号（没装 / 拿不到文件版本资源）。
    # ⚠️ 调用方**必须**区分 unknown 和 mismatch：读不出**不等于**版本不对
    #    （可能只是那个 exe 没有版本资源），一律拦下来会挡住本来能装的机器。
    param([string]$Dir)
    $v = Get-WeixinVersion -Dir $Dir
    if (-not $v) { return 'unknown' }
    if ($v.Trim() -eq $WX_WANTED_VERSION) { return 'ok' }
    return 'mismatch'
}
