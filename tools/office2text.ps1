# 用 COM 把**老 Office 文件**（.doc / .xls / .ppt）转成纯文本。
#
# 三条铁律（写在这里，改脚本时别破坏）：
#   ① **只读打开**：ReadOnly=$true、AddToRecentFiles=$false —— 绝不允许改用户文档；
#   ② **不弹窗口**：Word/Excel 用 Visible=$false；PowerPoint 不支持 Visible=false，
#      所以用 WithWindow=0 打开（真弹窗了也只能忍，但绝不阻塞等用户点）；
#   ③ 出错**如实报**：输出 "ERROR:…" 并以退出码 2 结束，让上层去试下一个引擎。
#
# 同时支持 Microsoft Office 与 WPS（靠 -ProgId 传 KWps.Application / KET.Application / KWpp.Application）。
#
# 用法： powershell -File office2text.ps1 -Path a.doc -Out a.txt -Kind word [-ProgId KWps.Application]
# 输出： 成功 -> "OK:<引擎>"；失败 -> "ERROR:原因"
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Out,
    [Parameter(Mandatory = $true)][ValidateSet("word", "excel", "ppt")][string]$Kind,
    [string]$ProgId = ""
)

$ErrorActionPreference = "Stop"

function New-App([string]$default) {
    $id = $default
    if ($ProgId -ne "") { $id = $ProgId }
    $t = [Type]::GetTypeFromProgID($id)
    if ($null -eq $t) { throw "这个 COM 组件没注册：$id" }
    return [Activator]::CreateInstance($t)
}

try {
    if ($Kind -eq "word") {
        $app = New-App "Word.Application"
        try {
            $app.Visible = $false
            $app.DisplayAlerts = 0
            # Open(FileName, ConfirmConversions, ReadOnly, AddToRecentFiles)
            $doc = $app.Documents.Open($Path, $false, $true, $false)
            try {
                try { $doc.SaveAs2($Out, 2) }          # 2 = wdFormatText
                catch { $doc.Content.Text | Out-File -FilePath $Out -Encoding utf8 }
            } finally { $doc.Close($false) }
        } finally { $app.Quit() }
        Write-Output "OK:word-com"
    }
    elseif ($Kind -eq "excel") {
        $app = New-App "Excel.Application"
        try {
            $app.Visible = $false
            $app.DisplayAlerts = $false
            $wb = $app.Workbooks.Open($Path, 0, $true)   # UpdateLinks=0, ReadOnly=$true
            try { $wb.SaveAs($Out, 42) } finally { $wb.Close($false) }   # 42 = xlUnicodeText
        } finally { $app.Quit() }
        Write-Output "OK:excel-com"
    }
    else {
        # PowerPoint：逐页读文本框（比 SaveAs 稳，也不会动原文件）
        $app = New-App "PowerPoint.Application"
        $pres = $null
        try {
            # Open(FileName, ReadOnly, Untitled, WithWindow): -1=真, 0=假
            $pres = $app.Presentations.Open($Path, -1, 0, 0)
            $lines = New-Object System.Collections.Generic.List[string]
            $i = 0
            foreach ($slide in $pres.Slides) {
                $i = $i + 1
                $lines.Add("— 第 $i 页 —")
                foreach ($shape in $slide.Shapes) {
                    try {
                        if ($shape.HasTextFrame -and $shape.TextFrame.HasText) {
                            $lines.Add($shape.TextFrame.TextRange.Text)
                        }
                    } catch { }
                }
            }
            $lines -join "`r`n" | Out-File -FilePath $Out -Encoding utf8
        } finally {
            if ($null -ne $pres) { $pres.Close() }
            $app.Quit()
        }
        Write-Output "OK:ppt-com"
    }
} catch {
    $msg = $_.Exception.Message
    if ($null -ne $_.Exception.InnerException) { $msg = $msg + " / " + $_.Exception.InnerException.Message }
    Write-Output ("ERROR:" + ($msg -replace "\s+", " ").Substring(0, [Math]::Min(200, $msg.Length)))
    exit 2
}
