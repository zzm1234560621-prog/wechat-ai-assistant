# 用 Windows 自带的 Windows.Media.Ocr 识别图片里的文字（离线、免费）。
#
# 为什么用系统 OCR 而不是接视觉模型：不花钱、不联网、不用额外 key。
# 代价是只能认"图里的字"（截图、聊天记录截图、带字的图），
# 看不懂风景照/表情包的内容——那种要视觉模型，见 image_read.py 的 vision 模式。
#
# 用法： powershell -File ocr.ps1 -Path "C:\...\xxx.jpg"
# 输出： 第一行引擎语言，随后 ---- 分隔线，之后是识别出的文本（可能为空）
param([string]$Path)

Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($op, $type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    $task.Wait(60000) | Out-Null
    $task.Result
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $langs = ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
              ForEach-Object { $_.LanguageTag }) -join ", "
    Write-Output "ENGINE: none"
    Write-Output "LANGUAGES: $langs"
    exit 1
}
Write-Output ("ENGINE: " + $engine.RecognizerLanguage.LanguageTag)
Write-Output "----"

try {
    $file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result  = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    Write-Output $result.Text
} catch {
    Write-Output "ERROR: $($_.Exception.Message)"
    exit 2
}
