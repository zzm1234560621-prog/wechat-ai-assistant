# 以管理员身份运行：放置 version.dll + 禁用微信自动更新
# 结果写到 hook-install-log.txt
#
# ⚠️ 2026-10-06 真机教训（改这个脚本前先读）：
#   这个脚本是在**提权新窗口**里跑的，窗口一关输出就没了。所以：
#     ① 每一句结论都写进 hook-install-log.txt（控制台这边靠读它回报）；
#     ② **同时**用 Write-Host 打到控制台，而且**中止时必须 write-host 大声说** ——
#        旧版撞版本闸时只写日志 + exit 2，用户看到的是"一闪而过、什么都没发生"，
#        于是以为装好了；真机就这么卡了一下午。
#     ③ 任何跟"装没装上"有关的判断，都要**把当前 DLL 的字节数摆出来**（519168=旧 / 527360=新）。
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

# 项目目录 = 本脚本所在目录（以前写死成本机绝对路径，换台电脑就废）
$dir = $PSScriptRoot
$log = Join-Path $dir 'hook-install-log.txt'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

# 「微信目录里现在那份」是什么 —— 装完/中止都要能对照
function Show-CurrentDll {
    param([string]$Where)
    $p = Join-Path $Where 'version.dll'
    if (Test-Path $p) {
        $n = (Get-Item $p).Length
        $h = (Get-FileHash $p -Algorithm SHA256).Hash.Substring(0, 16)
        $tag = if ($n -eq 527360) { 'NEW' } elseif ($n -eq 519168) { 'OLD' } else { 'unknown build' }
        $line = "    [$tag] $p  size=$n  sha256=$h"
    } else {
        $line = "    [none] $p 不存在（没装过 hook）"
    }
    $line | Out-File $log -Append -Encoding utf8
    Write-Host $line
}
$srcDll = Join-Path $dir 'version.dll'
if (Test-Path $srcDll) {
    $sn = (Get-Item $srcDll).Length
    $sh = (Get-FileHash $srcDll -Algorithm SHA256).Hash.Substring(0, 16)
    $sline = "    [src] $srcDll  size=$sn  sha256=$sh"
    $sline | Out-File $log -Append -Encoding utf8
    Write-Host $sline
}

# 微信安装目录自动探测（以前写死成厂商默认路径）
$WX = Find-Weixin
if (-not $WX) {
    "[X] 没找到微信安装目录；请先确认微信 4.1.10.27 已安装。" | Out-File $log -Append -Encoding utf8
    "    已找过：HKCU/HKLM\SOFTWARE\Tencent\Weixin、`$env:ProgramFiles\Tencent\Weixin。" | Out-File $log -Append -Encoding utf8
    "=== DONE (no weixin) ===" | Out-File $log -Append -Encoding utf8
    Write-Host "[X] 没找到微信安装目录 —— hook 没装。日志：$log"
    Write-Host "Press Enter to close..."
    Read-Host | Out-Null
    exit 1
}
"    [0] 微信目录：$WX" | Out-File $log -Append -Encoding utf8
Write-Host "    [0] 微信目录：$WX"
Show-CurrentDll -Where $WX

# ── 版本闸：版本不对就**什么都不做**（2026-10-04 加）────────────────────
# 为什么必须拦在这儿：hook 按 4.1.10.27 的函数偏移编译。装在别的版本上，DLL 会被微信
# 正常加载、**不报错、不崩**，只是挂钩失败——30001 永远没人监听。用户看到的是
# 「bot 一直连不上 30001」，而本脚本打的是「已放置，SHA256 = …」这种成功字样。
# 真机案例：另一台电脑是 4.1.15.13，一键配置 + 本脚本都"成功"，端口从没通过。
# 所以顺序也重要：**先判版本、再动任何东西**（含下面的自动更新 ACL）。
$ver = Get-WeixinVersion -Dir $WX
switch (Test-WeixinVersion -Dir $WX) {
    'ok' {
        "    [0] 版本检查：微信 $ver ✓（本 hook 唯一支持的版本）" | Out-File $log -Append -Encoding utf8
    }
    'mismatch' {
        "    [0] 版本检查：当前微信 $ver ≠ 本 hook 唯一支持的 $WX_WANTED_VERSION" | Out-File $log -Append -Encoding utf8
        "    **已中止：没有放置 version.dll、也没有改自动更新设置。**" | Out-File $log -Append -Encoding utf8
        "    放上去也不会生效——只会让人以为装好了（DLL 会被正常加载，但挂钩失败）。" | Out-File $log -Append -Encoding utf8
        "    先换成 $WX_WANTED_VERSION（本目录下自带官方安装程序）：" | Out-File $log -Append -Encoding utf8
        "      管理员 PowerShell： powershell -NoProfile -ExecutionPolicy Bypass -File .\do_install.ps1" | Out-File $log -Append -Encoding utf8
        "      或直接双击 WeChatWin_$WX_WANTED_VERSION.exe，弹「安装更早的版本？」时点「继续安装」。" | Out-File $log -Append -Encoding utf8
        "    装完**重启微信并登录**，再跑一次本脚本。" | Out-File $log -Append -Encoding utf8
        "=== DONE (version mismatch) ===" | Out-File $log -Append -Encoding utf8
        # ⚠️ 中止必须**大声说**（旧版只有上面那几行日志 + exit 2，控制台一片空白）
        Write-Host ""
        Write-Host "================================================================"
        Write-Host "[X] 已中止：微信版本是 $ver，不是本 hook 唯一支持的 $WX_WANTED_VERSION"
        Write-Host "    **version.dll 没有被替换** —— 后面每一样都别做，做了也不生效。"
        Write-Host "    做法：管理员 PowerShell 里跑 .\do_install.ps1 换成 $WX_WANTED_VERSION，"
        Write-Host "          装完重启微信并登录，再跑一次本脚本。"
        Write-Host "    日志：$log"
        Write-Host "================================================================"
        Write-Host "Press Enter to close..."
        Read-Host | Out-Null
        exit 2
    }
    default {
        "    [!] 版本检查：读不出微信版本（拿不到文件版本资源）——继续放 DLL。" | Out-File $log -Append -Encoding utf8
        "        读不出不等于版本不对；但若之后 30001 仍不通，请确认微信是 $WX_WANTED_VERSION。" | Out-File $log -Append -Encoding utf8
        Write-Host "    [!] 读不出微信版本（不等于版本不对）——继续放 DLL。"
    }
}

# 提权后 $env:APPDATA 可能指向管理员账户，所以自动找**登录用户**的目录
# （以前这里写死成开发机的用户目录和用户名）
$appData = Get-LoginUserAppData
$UPD  = Get-WeixinUpdateDir -AppData $appData
$USER = Get-AppDataUserName -AppData $appData
if ($UPD) {
    "    登录用户：$USER" | Out-File $log -Append -Encoding utf8
    "    更新目录：$UPD" | Out-File $log -Append -Encoding utf8
} else {
    "    [!] 认不出登录用户的 AppData：会跳过「禁用自动更新」（不影响 hook 本体）。" | Out-File $log -Append -Encoding utf8
}

"[1] 放置 version.dll 到微信安装目录" | Out-File $log -Append -Encoding utf8
try {
    Copy-Item (Join-Path $dir 'version.dll') (Join-Path $WX 'version.dll') -Force -ErrorAction Stop
    $h = (Get-FileHash (Join-Path $WX 'version.dll') -Algorithm SHA256).Hash
    "  已放置，SHA256 = $h" | Out-File $log -Append -Encoding utf8
} catch {
    "  失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}

"[2] 禁用微信自动更新（按仓库作者的方法：ACL 拒绝写入更新目录）" | Out-File $log -Append -Encoding utf8
if ($UPD -and $USER) {
    $cmds = @(
        "echo Y|cacls `"$UPD\update.data`" /T /P $USER`:N",
        "rd /s /q `"$UPD\patch`"",
        "md `"$UPD\patch`"",
        "echo Y|cacls `"$UPD\patch`" /T /P $USER`:N"
    )
    foreach ($c in $cmds) {
        $out = cmd /c $c 2>&1
        "  > $c" | Out-File $log -Append -Encoding utf8
        "    $out" | Out-File $log -Append -Encoding utf8
    }
} else {
    "  [!] 跳过：认不出登录用户的更新目录。" | Out-File $log -Append -Encoding utf8
    "      微信自动更新没被禁用——它升级到别的版本后 hook 会失效（甚至崩微信）。" | Out-File $log -Append -Encoding utf8
    "      请手工在「微信 → 设置 → 通用设置」里关掉自动更新，或把 $env:APPDATA 对应的用户目录指出来。" | Out-File $log -Append -Encoding utf8
}

"[3] 校验" | Out-File $log -Append -Encoding utf8
Get-ChildItem $WX -File | ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }
if ($UPD) {
    "  --- update 目录权限 ---" | Out-File $log -Append -Encoding utf8
    (cmd /c "cacls `"$UPD\patch`"" 2>&1) | Out-File $log -Append -Encoding utf8
}

"=== DONE ===" | Out-File $log -Append -Encoding utf8

# 装完之后**必须能一眼看出装上了**（旧版到这儿就结束了，用户只在打开的窗口里看到一眼）
Write-Host ""
Write-Host "================================"
Write-Host "[DONE] 装完了。现在微信目录里那份是："
Show-CurrentDll -Where $WX
Write-Host "  size=527360 才是我改过的这一版；如果你看到 519168，说明替换没成功。"
Write-Host "  → 接着**完全退出微信（托盘图标也退）再打开、扫码登录** ——"
Write-Host "    version.dll 只在微信进程启动时加载，不重启等于没换。"
Write-Host "  然后浏览器打 http://127.0.0.1:30001/QueryDB/status （有 JSON 就成）"
Write-Host "  日志：$log"
Write-Host "================================"
Write-Host "Press Enter to close..."
Read-Host | Out-Null
