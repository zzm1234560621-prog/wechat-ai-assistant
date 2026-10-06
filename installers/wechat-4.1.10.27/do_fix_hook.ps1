param([string]$Dll, [string]$Log)
$ErrorActionPreference = "Continue"
# Same rule as every other do_*.ps1 here: dot-source _common.ps1 instead of writing a
# second "find the WeChat folder" routine. Body is kept pure ASCII on purpose.
. (Join-Path $PSScriptRoot '_common.ps1')
$here = $PSScriptRoot
if (-not $Dll) { $Dll = Join-Path (Split-Path (Split-Path $here -Parent) -Parent) "version.dll" }
if (-not $Log) { $Log = Join-Path $here "hook-fix-log.txt" }
# If not found via package root guess, try the sibling hook folder layout.
if (-not (Test-Path $Dll)) {
    $alt = Join-Path $here "version.dll"
    if (Test-Path $alt) { $Dll = $alt }
}
$WX = Find-Weixin
if (-not $WX) { $WX = "C:\Program Files\Tencent\Weixin" }
$dst = Join-Path $WX "version.dll"

function W([string]$m) { Write-Host $m; $m | Out-File $Log -Append -Encoding ascii }
"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File $Log -Encoding ascii
W "SRC  $Dll"
W "DST  $dst"

if (-not (Test-Path $Dll)) { W "FAIL: source dll not found"; W "RESULT: FAIL (no source)"; exit 2 }
if (-not (Test-Path $dst)) { W "FAIL: weixin version.dll not found (weixin installed elsewhere?)"; W "RESULT: FAIL (no dest)"; exit 3 }

$s1 = (Get-FileHash $Dll -Algorithm SHA256).Hash
$d1 = (Get-FileHash $dst -Algorithm SHA256).Hash
W "before: src=$($s1.Substring(0,16)) size=$((Get-Item $Dll).Length)"
W "before: dst=$($d1.Substring(0,16)) size=$((Get-Item $dst).Length)   <- 519168 = OLD, 527360 = NEW"

if ($s1 -eq $d1) {
    W "nothing to do: dst already equals src"
    W "RESULT: OK (already new)"
    exit 0
}

try {
    $bak = "$dst.bak_" + (Get-Date -Format yyyyMMdd_HHmmss)
    Copy-Item $dst $bak -Force -ErrorAction Stop
    W "backup $bak"
    Copy-Item $Dll $dst -Force -ErrorAction Stop
} catch {
    W "FAIL copy: $($_.Exception.Message)"
    W "HINT: close Weixin completely (tray icon too) and run again as Administrator."
    W "RESULT: FAIL (copy)"
    exit 4
}

$d2 = (Get-FileHash $dst -Algorithm SHA256).Hash
W "after:  dst=$($d2.Substring(0,16)) size=$((Get-Item $dst).Length)"
if ($d2 -eq $s1) {
    W "RESULT: OK"
    W "NEXT: start Weixin, log in (scan QR), then open http://127.0.0.1:30001/QueryDB/status"
    exit 0
}
W "RESULT: FAIL (still different)"
exit 5
