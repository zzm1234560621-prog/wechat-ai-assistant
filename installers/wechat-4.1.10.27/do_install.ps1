# 以管理员身份运行：杀掉残留微信进程，然后静默安装微信 4.1.10.27
# 结果写到 install-log.txt（提权窗口的输出回不来，所以用日志）
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

# 项目目录 = 本脚本所在目录（以前写死成本机绝对路径，换台电脑就废）
$dir = $PSScriptRoot
$log = Join-Path $dir 'install-log.txt'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

# 微信安装目录自动探测；这一步是「装之前」，没装过是正常的，所以退回预期路径
$WX = Find-Weixin
if (-not $WX) { $WX = Join-Path $env:ProgramFiles 'Tencent\Weixin' }
"    [0] 目标微信目录：$WX" | Out-File $log -Append -Encoding utf8

"[1] 杀残留微信进程" | Out-File $log -Append -Encoding utf8
Get-Process Weixin,WeChatAppEx,WeChatOCR,WeixinUpdate -ErrorAction SilentlyContinue |
    ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction Stop
              "  killed $($_.Name) $($_.Id)" | Out-File $log -Append -Encoding utf8 }
        catch { "  失败 $($_.Name) $($_.Id): $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }
    }
Start-Sleep -Seconds 3
$left = @(Get-Process Weixin,WeChatAppEx -ErrorAction SilentlyContinue).Count
"  残留: $left" | Out-File $log -Append -Encoding utf8

"[2] 安装前的版本目录" | Out-File $log -Append -Encoding utf8
Get-ChildItem $WX -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }

"[3] 静默安装 WeChatWin_4.1.10.27.exe /S" | Out-File $log -Append -Encoding utf8
try {
    $p = Start-Process -FilePath (Join-Path $dir 'WeChatWin_4.1.10.27.exe') `
                       -ArgumentList '/S' -Wait -PassThru
    "  exit code: $($p.ExitCode)" | Out-File $log -Append -Encoding utf8
} catch {
    "  启动失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}
Start-Sleep -Seconds 5

"[4] 安装后的版本目录" | Out-File $log -Append -Encoding utf8
# 装完之后重新探测一次：静默安装实际落到哪个目录由安装器决定，别拿装之前的猜测当事实
$WX2 = Find-Weixin
if ($WX2) { $WX = $WX2; "    [OK] 探测到微信目录：$WX" | Out-File $log -Append -Encoding utf8 }
else      { "    [!] 装完仍探测不到微信目录（安装可能失败或被拦）。" | Out-File $log -Append -Encoding utf8 }
Get-ChildItem $WX -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }

"[5] 主程序版本" | Out-File $log -Append -Encoding utf8
try {
    $v = (Get-Item (Join-Path $WX 'Weixin.exe')).VersionInfo
    "  Weixin.exe ProductVersion = $($v.ProductVersion)" | Out-File $log -Append -Encoding utf8
} catch { "  读取失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"=== DONE ===" | Out-File $log -Append -Encoding utf8
