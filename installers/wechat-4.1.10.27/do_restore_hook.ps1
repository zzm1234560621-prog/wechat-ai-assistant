# 以管理员身份运行：把 hook 的 version.dll 装回去，并重启微信
$ErrorActionPreference = 'Continue'
$dir = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$log = Join-Path $dir 'restore-hook-log.txt'
$WX  = 'C:\Program Files\Tencent\Weixin'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

"[1] 结束微信" | Out-File $log -Append -Encoding utf8
Get-Process Weixin,WeChatAppEx,WeixinUpdate -EA SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 4
"  残留 Weixin: $(@(Get-Process Weixin -EA SilentlyContinue).Count)" | Out-File $log -Append -Encoding utf8

"[2] 装回 version.dll" | Out-File $log -Append -Encoding utf8
try {
    if (Test-Path (Join-Path $WX 'version.dll.disabled')) {
        Move-Item (Join-Path $WX 'version.dll.disabled') (Join-Path $WX 'version.dll') -Force -ErrorAction Stop
        $h = (Get-FileHash (Join-Path $WX 'version.dll') -Algorithm SHA256).Hash
        "  已装回，SHA256 = $h" | Out-File $log -Append -Encoding utf8
    } else {
        "  version.dll.disabled 不在，跳过" | Out-File $log -Append -Encoding utf8
    }
} catch { "  失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[3] 微信目录" | Out-File $log -Append -Encoding utf8
Get-ChildItem $WX -File | ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }

"=== DONE ===" | Out-File $log -Append -Encoding utf8
