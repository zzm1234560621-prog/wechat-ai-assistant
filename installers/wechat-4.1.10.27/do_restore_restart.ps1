# 以管理员身份运行：恢复 hook 的 version.dll，并结束微信（不负责重启）
$ErrorActionPreference = 'Continue'
$dir = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$log = Join-Path $dir 'restore-restart-log.txt'
$WX  = 'C:\Program Files\Tencent\Weixin'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

"[1] 恢复 version.dll" | Out-File $log -Append -Encoding utf8
$dis = Join-Path $WX 'version.dll.disabled'
$dll = Join-Path $WX 'version.dll'
try {
    if (Test-Path $dll) { Remove-Item $dll -Force }
    if (Test-Path $dis) {
        Move-Item $dis $dll -Force -ErrorAction Stop
        $h = (Get-FileHash $dll -Algorithm SHA256).Hash
        "  已恢复，SHA256 = $h" | Out-File $log -Append -Encoding utf8
    } else {
        "  找不到 version.dll.disabled" | Out-File $log -Append -Encoding utf8
    }
} catch { "  失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[2] 结束微信" | Out-File $log -Append -Encoding utf8
Get-Process Weixin, WeChatAppEx -EA SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop
          "  killed $($_.Name) $($_.Id)" | Out-File $log -Append -Encoding utf8 }
    catch { "  失败 $($_.Name) $($_.Id): $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }
}
Start-Sleep -Seconds 4
"  残留 Weixin: $(@(Get-Process Weixin -EA SilentlyContinue).Count)" | Out-File $log -Append -Encoding utf8

"[3] 微信目录确认" | Out-File $log -Append -Encoding utf8
Get-ChildItem $WX -File | ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }

"=== DONE ===" | Out-File $log -Append -Encoding utf8
