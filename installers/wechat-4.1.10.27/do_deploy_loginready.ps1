# 以管理员身份运行：部署「登录就绪探测」版 version.dll，并结束微信
$ErrorActionPreference = 'Continue'
$dir   = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$build = Join-Path $dir 'src-4.1.10.27\WeChat-Hook-4.1.10.27\x64\Release\version.dll'
$log   = Join-Path $dir 'deploy-loginready-log.txt'
$WX    = 'C:\Program Files\Tencent\Weixin'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

"[1] 归档新构建" | Out-File $log -Append -Encoding utf8
try {
    Copy-Item $build (Join-Path $dir 'version_loginready.dll') -Force -ErrorAction Stop
    "  已存为 version_loginready.dll" | Out-File $log -Append -Encoding utf8
} catch { "  归档失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[2] 备份当前 version.dll" | Out-File $log -Append -Encoding utf8
try {
    $cur = Join-Path $WX 'version.dll'
    if (Test-Path $cur) {
        $h = (Get-FileHash $cur -Algorithm SHA256).Hash
        Copy-Item $cur (Join-Path $dir 'version_prev_backup.dll') -Force
        "  旧 DLL SHA256 = $h  → 备份为 version_prev_backup.dll" | Out-File $log -Append -Encoding utf8
    } else {
        "  当前目录没有 version.dll" | Out-File $log -Append -Encoding utf8
    }
} catch { "  备份失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[3] 结束微信" | Out-File $log -Append -Encoding utf8
Get-Process Weixin, WeChatAppEx -EA SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop
          "  killed $($_.Name) $($_.Id)" | Out-File $log -Append -Encoding utf8 }
    catch { "  失败 $($_.Name) $($_.Id): $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }
}
Start-Sleep -Seconds 4
"  残留 Weixin: $(@(Get-Process Weixin -EA SilentlyContinue).Count)" | Out-File $log -Append -Encoding utf8

"[4] 部署新 DLL" | Out-File $log -Append -Encoding utf8
try {
    Copy-Item $build (Join-Path $WX 'version.dll') -Force -ErrorAction Stop
    $nh = (Get-FileHash (Join-Path $WX 'version.dll') -Algorithm SHA256).Hash
    "  已部署，SHA256 = $nh" | Out-File $log -Append -Encoding utf8
} catch { "  部署失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8 }

"[5] 微信目录确认" | Out-File $log -Append -Encoding utf8
Get-ChildItem $WX -File | ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }

"=== DONE ===" | Out-File $log -Append -Encoding utf8
