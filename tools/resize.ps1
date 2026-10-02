# 用系统自带的 System.Drawing 把图等比缩到「长边 <= MaxEdge」，另存为 JPEG。
#
# 为什么不用 Pillow：项目对第三方依赖是能不加就不加（Pillow 只在 PDF 内嵌图那条支路上是
# 可选的）；而缩图这件事 Windows 自己就有（和 tools/ocr.ps1 用系统 OCR 同一个思路）。
#
# 为什么要缩：图片送模型是按 token 计费的，分辨率越大 token 越多；长边压到 1024
# 通常肉眼够用、费用明显下来（见 docs/file-input-spec.md 第八节）。
#
# 用法： powershell -File resize.ps1 -Path "C:\...\a.jpg" -MaxEdge 1024 -Out "C:\...\a.small.jpg"
# 输出： 成功 -> "OK:宽x高"；失败 -> "ERROR:原因"（退出码 2）
param([string]$Path, [int]$MaxEdge = 1024, [string]$Out)

Add-Type -AssemblyName System.Drawing | Out-Null

try {
    $img = [System.Drawing.Image]::FromFile($Path)
} catch {
    Write-Output ("ERROR:打不开这张图：" + $_.Exception.Message)
    exit 2
}

try {
    $w = [int]$img.Width
    $h = [int]$img.Height
    $long = [Math]::Max($w, $h)
    if ($MaxEdge -le 0 -or $long -le $MaxEdge) {
        # 已经够小：原样复制，别做无意义的缩图（也避免 JPEG 二次压缩丢质量）
        $img.Dispose()
        Copy-Item -LiteralPath $Path -Destination $Out -Force
        Write-Output ("OK:{0}x{1}" -f $w, $h)
        exit 0
    }
    $scale = $MaxEdge / $long
    $nw = [int][Math]::Max(1, [Math]::Round($w * $scale))
    $nh = [int][Math]::Max(1, [Math]::Round($h * $scale))
    $bmp = New-Object System.Drawing.Bitmap($nw, $nh)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try {
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.DrawImage($img, 0, 0, $nw, $nh)
    } finally {
        $g.Dispose()
    }
    try {
        $bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Jpeg)
    } finally {
        $bmp.Dispose()
    }
    Write-Output ("OK:{0}x{1}" -f $nw, $nh)
} catch {
    Write-Output ("ERROR:" + $_.Exception.Message)
    exit 2
} finally {
    $img.Dispose()
}
