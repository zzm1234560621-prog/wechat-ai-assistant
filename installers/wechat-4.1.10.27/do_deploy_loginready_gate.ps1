# 部署「收紧就绪判据」版 version.dll（需管理员；本脚本自身不判权限，由调用方提权）
#
# 为什么单独一个脚本而不是复用 do_replace_dll.ps1：那个脚本读的是固定的
# `version_new.dll` 且不重启微信；这次要的是「备份带时间戳 → 换 → 重启 → 记日志」，
# 并且**必须**把新旧 SHA256 都写进日志（换 DLL 出过事，见 CLAUDE.md 的 hook 铁律）。
#
# 用法（普通窗口即可，会弹 UAC）：
#   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','<本文件>'
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '_common.ps1')

$dir = $PSScriptRoot
$log = Join-Path $dir 'deploy-loginready-log.txt'
$new = Join-Path $dir 'version_new.dll'

function L($s) { $s | Out-File $log -Append -Encoding utf8 }

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 收紧就绪判据版部署 ===" | Out-File $log -Encoding utf8 -Force

$WX = Find-Weixin
if (-not $WX) {
    L "[X] 没找到微信安装目录，无法替换 DLL。"
    L "    已找过：HKCU/HKLM\SOFTWARE\Tencent\Weixin、`$env:ProgramFiles\Tencent\Weixin。"
    L "=== DONE (no weixin) ==="
    exit 1
}
L "    [0] 微信目录：$WX"
if (-not (Test-Path $new)) { L "[X] 找不到待部署的 $new"; L "=== DONE ==="; exit 1 }
L "    [0] 新 DLL：$new ($((Get-Item $new).Length) 字节, $((Get-FileHash $new).Hash))"

# [1] 备份微信目录里现役那份（带时间戳，不动包里那些 *_backup.dll）
L "[1] 备份现役 DLL"
$dst = Join-Path $WX 'version.dll'
if (Test-Path $dst) {
    $bak = Join-Path $WX ("version.dll.bak_" + (Get-Date -Format 'yyyyMMdd_HHmmss'))
    try {
        Copy-Item $dst $bak -Force -ErrorAction Stop
        L "   已备份到 $bak（$((Get-FileHash $bak).Hash)）"
    } catch { L "   备份失败: $($_.Exception.Message)" }
} else { L "   微信目录里没有 version.dll" }

# [2] 关闭微信（先优雅、再强杀）——DLL 被占用时替换会失败
L "[2] 关闭微信"
Get-Process Weixin -EA SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } |
    ForEach-Object { L "   CloseMainWindow PID=$($_.Id) -> $($_.CloseMainWindow())" }
Start-Sleep -Seconds 4
Get-Process Weixin, WeChatAppEx -EA SilentlyContinue | ForEach-Object {
    try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch {}
}
Start-Sleep -Seconds 3
L "   残留 Weixin: $(@(Get-Process Weixin -EA SilentlyContinue).Count)"

# [3] 替换 + 校验哈希（哈希不一致要能在日志里一眼看出来）
L "[3] 替换 version.dll"
try {
    Copy-Item $new $dst -Force -ErrorAction Stop
    $h = (Get-FileHash $dst -Algorithm SHA256).Hash
    L "   已替换，SHA256 = $h"
    if ($h -eq (Get-FileHash $new -Algorithm SHA256).Hash) { L "   哈希一致 OK" }
    else { L "   哈希不一致 FAIL（别继续用）" }
} catch { L "   替换失败: $($_.Exception.Message)" }

# [4] 重启微信（后台拉起，不阻塞）
L "[4] 重启微信"
$exe = Join-Path $WX 'Weixin.exe'
if (Test-Path $exe) {
    try { Start-Process -FilePath $exe -ErrorAction Stop; L "   已启动 $exe" }
    catch { L "   启动失败: $($_.Exception.Message)" }
} else { L "   没找到 $exe" }

L "=== DONE ==="
