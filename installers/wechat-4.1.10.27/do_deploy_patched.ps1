# 以管理员身份运行：部署我们自己编译的（已补 g_IsLogin）version.dll，并重启微信
$ErrorActionPreference = 'Continue'
$dir = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$log = Join-Path $dir 'deploy-patched-log.txt'
$WX  = 'C:\Program Files\Tencent\Weixin'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

"[1] 备份当前 DLL（以便回退）" | Out-File $log -Append -Encoding utf8
try {
    Copy-Item (Join-Path $WX 'version.dll') (Join-Path $dir 'version_606208_backup.dll') -Force
    "  已备份为 version_606208_backup.dll" | Out-File $log -Append -Encoding utf8
} catch { "  备份失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[2] 优雅关闭微信（尽量保住登录态）" | Out-File $log -Append -Encoding utf8
Get-Process Weixin -EA SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } |
    ForEach-Object { "  CloseMainWindow PID=$($_.Id) -> $($_.CloseMainWindow())" | Out-File $log -Append -Encoding utf8 }
Start-Sleep -Seconds 8
$still = @(Get-Process Weixin -EA SilentlyContinue)
"  优雅关闭后残留: $($still.Count)" | Out-File $log -Append -Encoding utf8
if ($still.Count -gt 0) {
    "  改为强制结束" | Out-File $log -Append -Encoding utf8
    Get-Process Weixin,WeChatAppEx -EA SilentlyContinue | ForEach-Object {
        try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Seconds 3
}

"[3] 部署补丁版 DLL" | Out-File $log -Append -Encoding utf8
try {
    Copy-Item (Join-Path $dir 'version_patched.dll') (Join-Path $WX 'version.dll') -Force -ErrorAction Stop
    $h = (Get-FileHash (Join-Path $WX 'version.dll') -Algorithm SHA256).Hash
    "  已部署，SHA256 = $h" | Out-File $log -Append -Encoding utf8
} catch { "  部署失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"  残留 Weixin: $(@(Get-Process Weixin -EA SilentlyContinue).Count)" | Out-File $log -Append -Encoding utf8
"=== DONE ===" | Out-File $log -Append -Encoding utf8
