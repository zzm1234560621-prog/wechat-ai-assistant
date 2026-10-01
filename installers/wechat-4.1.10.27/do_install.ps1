# 以管理员身份运行：杀掉残留微信进程，然后静默安装微信 4.1.10.27
# 结果写到 install-log.txt（提权窗口的输出回不来，所以用日志）
$ErrorActionPreference = 'Continue'
$dir = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$log = Join-Path $dir 'install-log.txt'
$WX  = 'C:\Program Files\Tencent\Weixin'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

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
Get-ChildItem $WX -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }

"[5] 主程序版本" | Out-File $log -Append -Encoding utf8
try {
    $v = (Get-Item (Join-Path $WX 'Weixin.exe')).VersionInfo
    "  Weixin.exe ProductVersion = $($v.ProductVersion)" | Out-File $log -Append -Encoding utf8
} catch { "  读取失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"=== DONE ===" | Out-File $log -Append -Encoding utf8
