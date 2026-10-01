# 以管理员身份运行：摘掉 hook 的 version.dll，并强制结束卡死的微信
$ErrorActionPreference = 'Continue'
$dir = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$log = Join-Path $dir 'remove-hook-log.txt'
$WX  = 'C:\Program Files\Tencent\Weixin'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

"[1] 结束微信（当前是卡死状态，强制结束）" | Out-File $log -Append -Encoding utf8
Get-Process Weixin,WeChatAppEx,WeixinUpdate -EA SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop
          "  killed $($_.Name) $($_.Id)" | Out-File $log -Append -Encoding utf8 }
    catch { "  失败 $($_.Name) $($_.Id): $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }
}
Start-Sleep -Seconds 5

"[2] 摘掉 version.dll（改名保留，不是删除）" | Out-File $log -Append -Encoding utf8
$dll = Join-Path $WX 'version.dll'
try {
    if (Test-Path $dll) {
        if (Test-Path "$dll.disabled") { Remove-Item "$dll.disabled" -Force }
        Move-Item $dll "$dll.disabled" -Force -ErrorAction Stop
        "  已改名为 version.dll.disabled（想恢复就把名字改回来）" | Out-File $log -Append -Encoding utf8
    } else {
        "  version.dll 本来就不在" | Out-File $log -Append -Encoding utf8
    }
} catch { "  失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[3] 确认微信目录" | Out-File $log -Append -Encoding utf8
Get-ChildItem $WX -File | ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }

"[4] 确认没有残留微信进程" | Out-File $log -Append -Encoding utf8
"  残留 Weixin: $(@(Get-Process Weixin -EA SilentlyContinue).Count)" | Out-File $log -Append -Encoding utf8
"  端口 30001: $(@(netstat -ano | Select-String '0.0.0.0:30001').Count) 条监听记录（应为 0）" | Out-File $log -Append -Encoding utf8

"=== DONE ===" | Out-File $log -Append -Encoding utf8
