# 以管理员身份运行：放置 version.dll + 禁用微信自动更新
# 结果写到 hook-install-log.txt
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

# 项目目录 = 本脚本所在目录（以前写死成本机绝对路径，换台电脑就废）
$dir = $PSScriptRoot
$log = Join-Path $dir 'hook-install-log.txt'

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $log -Encoding utf8

# 微信安装目录自动探测（以前写死成厂商默认路径）
$WX = Find-Weixin
if (-not $WX) {
    "[X] 没找到微信安装目录；请先确认微信 4.1.10.27 已安装。" | Out-File $log -Append -Encoding utf8
    "    已找过：HKCU/HKLM\SOFTWARE\Tencent\Weixin、`$env:ProgramFiles\Tencent\Weixin。" | Out-File $log -Append -Encoding utf8
    "=== DONE (no weixin) ===" | Out-File $log -Append -Encoding utf8
    exit 1
}
"    [0] 微信目录：$WX" | Out-File $log -Append -Encoding utf8

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
