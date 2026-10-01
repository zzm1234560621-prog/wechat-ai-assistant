# 以管理员身份运行：放置 version.dll + 禁用微信自动更新
# 结果写到 hook-install-log.txt
$ErrorActionPreference = 'Continue'
$dir = 'D:\wechat-ai-assistant\wechat-ai-assistant\installers\wechat-4.1.10.27'
$log = Join-Path $dir 'hook-install-log.txt'
$WX  = 'C:\Program Files\Tencent\Weixin'

# 注意：提权后 $env:APPDATA 可能指向管理员账户，所以这里全部写死绝对路径
$UPD  = 'C:\Users\zzm12\AppData\Roaming\Tencent\xwechat\update'
$USER = 'zzm12'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

"[1] 放置 version.dll 到微信安装目录" | Out-File $log -Append -Encoding utf8
try {
    Copy-Item (Join-Path $dir 'version.dll') (Join-Path $WX 'version.dll') -Force -ErrorAction Stop
    $h = (Get-FileHash (Join-Path $WX 'version.dll') -Algorithm SHA256).Hash
    "  已放置，SHA256 = $h" | Out-File $log -Append -Encoding utf8
} catch {
    "  失败: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
}

"[2] 禁用微信自动更新（按仓库作者的方法：ACL 拒绝写入更新目录）" | Out-File $log -Append -Encoding utf8
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

"[3] 校验" | Out-File $log -Append -Encoding utf8
Get-ChildItem $WX -File | ForEach-Object { "  $($_.Name)" | Out-File $log -Append -Encoding utf8 }
"  --- update 目录权限 ---" | Out-File $log -Append -Encoding utf8
(cmd /c "cacls `"$UPD\patch`"" 2>&1) | Out-File $log -Append -Encoding utf8

"=== DONE ===" | Out-File $log -Append -Encoding utf8
