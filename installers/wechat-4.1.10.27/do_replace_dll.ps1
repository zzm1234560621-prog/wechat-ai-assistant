# 以管理员身份运行：用新版 version.dll 替换旧的，并关闭微信以便重新加载
$ErrorActionPreference = 'Continue'
$dir = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$log = Join-Path $dir 'replace-dll-log.txt'
$WX  = 'C:\Program Files\Tencent\Weixin'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

"[1] 备份旧 DLL" | Out-File $log -Append -Encoding utf8
try {
    if (Test-Path (Join-Path $WX 'version.dll')) {
        Copy-Item (Join-Path $WX 'version.dll') (Join-Path $dir 'version_old_backup.dll') -Force
        "  已备份到 $dir\version_old_backup.dll" | Out-File $log -Append -Encoding utf8
    }
} catch { "  备份失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[2] 关闭微信（先优雅、再强杀）" | Out-File $log -Append -Encoding utf8
Get-Process Weixin -EA SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } |
    ForEach-Object { "  CloseMainWindow PID=$($_.Id) -> $($_.CloseMainWindow())" | Out-File $log -Append -Encoding utf8 }
Start-Sleep -Seconds 4
Get-Process Weixin,WeChatAppEx -EA SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 3
"  残留 Weixin: $(@(Get-Process Weixin -EA SilentlyContinue).Count)" | Out-File $log -Append -Encoding utf8

"[3] 替换 version.dll" | Out-File $log -Append -Encoding utf8
try {
    Copy-Item (Join-Path $dir 'version_new.dll') (Join-Path $WX 'version.dll') -Force -ErrorAction Stop
    $h = (Get-FileHash (Join-Path $WX 'version.dll') -Algorithm SHA256).Hash
    "  已替换，SHA256 = $h" | Out-File $log -Append -Encoding utf8
} catch { "  替换失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"=== DONE ===" | Out-File $log -Append -Encoding utf8
