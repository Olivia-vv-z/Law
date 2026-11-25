# =====================================================================
#  ocr_worker.ps1  —  Windows 内置 OCR 工作进程（由 聊天记录排序.py 调用）
#  依赖：Windows 10/11 自带 OCR（zh-Hans-CN），无需安装任何第三方组件
#  输出：JSON，含每张图片的识别文本行 + 纵向相对位置
# =====================================================================
param(
    [Parameter(Mandatory = $true)][string]$ListFile,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Mode = 'normal'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing

# --- WinRT 异步等待辅助 ---------------------------------------------
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($op, $resultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
    $netTask = $asTask.Invoke($null, @($op))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$StorageFileT = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$DecoderT     = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$SoftwareBmpT = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$StreamT      = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$OcrResultT   = [Windows.Media.Ocr.OcrResult, Windows.Media.Ocr, ContentType = WindowsRuntime]
$OcrEngineT   = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]

# --- 创建 OCR 引擎（优先中文） --------------------------------------
$engine = $null
$engineLang = ''
try {
    $langT = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $lang = $langT::new('zh-Hans-CN')
    $engine = $OcrEngineT::TryCreateFromLanguage($lang)
    if ($engine) { $engineLang = 'zh-Hans-CN' }
} catch { }
if (-not $engine) {
    $engine = $OcrEngineT::TryCreateFromUserProfileLanguages()
    if ($engine) { $engineLang = $engine.RecognizerLanguage.LanguageTag }
}
if (-not $engine) {
    $payload = @{ ok = $false; engine = ''; error = 'Windows OCR 引擎不可用：请确认系统已安装中文语言包'; items = @() }
    [System.IO.File]::WriteAllText($OutFile, ($payload | ConvertTo-Json -Depth 8), (New-Object System.Text.UTF8Encoding $false))
    exit 2
}

$maxDim = $OcrEngineT::MaxImageDimension
$scale = 2.0                # OCR 前放大倍数：2 倍对截图小字识别最稳
$blockH = 1600              # 长截图分块高度（原图像素），避免整图缩放丢字
$overlap = 120              # 分块重叠，防止切断文字

# 增强模式：灰度 + 对比度拉伸，用于浅灰时间标签识别失败的图
$colorMatrix = New-Object System.Drawing.Imaging.ColorMatrix
if ($Mode -eq 'enhanced') {
    $colorMatrix.Matrix00 = 1.9; $colorMatrix.Matrix11 = 1.9; $colorMatrix.Matrix22 = 1.9
    $colorMatrix.Matrix33 = 1.0; $colorMatrix.Matrix44 = 1.0
    $colorMatrix.Matrix40 = -1.05; $colorMatrix.Matrix41 = -1.05; $colorMatrix.Matrix42 = -1.05
}

# --- OCR 单张图片 ---------------------------------------------------
function Invoke-OcrImage {
    param([string]$Path)

    $item = [ordered]@{
        path = $Path; ok = $false; width = 0; height = 0
        lineCount = 0; error = ''; lines = @()
    }

    $src = $null
    try { $src = [System.Drawing.Image]::FromFile($Path) }
    catch {
        $item.error = '无法读取图片: ' + $_.Exception.Message
        return $item
    }

    try {
        $w = $src.Width; $h = $src.Height
        $item.width = $w; $item.height = $h

        $allLines = New-Object System.Collections.ArrayList
        $offset = 0
        while ($true) {
            $blockHActual = [Math]::Min($blockH, $h - $offset)
            if ($blockHActual -le 0) { break }

            # 裁剪一块
            $rect = New-Object System.Drawing.Rectangle 0, $offset, $w, $blockHActual
            $block = New-Object System.Drawing.Bitmap $w, $blockHActual
            $gb = [System.Drawing.Graphics]::FromImage($block)
            if ($Mode -eq 'enhanced') {
                $attr = New-Object System.Drawing.Imaging.ImageAttributes
                $attr.SetColorMatrix($colorMatrix)
                $dest = New-Object System.Drawing.Rectangle 0, 0, $w, $blockHActual
                $gb.DrawImage($src, $dest, $offset, 0, $w, $blockHActual, [System.Drawing.GraphicsUnit]::Pixel, $attr)
            } else {
                $gb.DrawImage($src, (New-Object System.Drawing.Rectangle 0, 0, $w, $blockHActual), $rect, [System.Drawing.GraphicsUnit]::Pixel)
            }
            $gb.Dispose()

            # 放大
            $bw = [int]($w * $scale); $bh = [int]($blockHActual * $scale)
            $big = New-Object System.Drawing.Bitmap $bw, $bh
            $g2 = [System.Drawing.Graphics]::FromImage($big)
            $g2.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $g2.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $g2.DrawImage($block, 0, 0, $bw, $bh)
            $g2.Dispose()
            $block.Dispose()

            $tmp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), ('ocr_' + [Guid]::NewGuid().ToString('N') + '.png'))
            $big.Save($tmp, [System.Drawing.Imaging.ImageFormat]::Png)
            $big.Dispose()

            try {
                $file    = Await ($StorageFileT::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
                $stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ($StreamT)
                $decoder = Await ($DecoderT::CreateAsync($stream)) ($DecoderT)
                $bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ($SoftwareBmpT)
                $result  = Await ($engine.RecognizeAsync($bitmap)) ($OcrResultT)
                $stream.Dispose()

                $lines = @($result.Lines)
                for ($i = 0; $i -lt $lines.Count; $i++) {
                    $line  = $lines[$i]
                    $words = @($line.Words)
                    if ($words.Count -eq 0) { continue }
                    $top = 999999.0; $left = 999999.0
                    for ($j = 0; $j -lt $words.Count; $j++) {
                        $r = $words[$j].BoundingRect
                        if ($r.Top -lt $top) { $top = $r.Top }
                        if ($r.Left -lt $left) { $left = $r.Left }
                    }
                    # 还原到原图坐标
                    $globalTop = $offset + ($top / $scale)
                    $ratio = [Math]::Round($globalTop / $h, 4)
                    [void]$allLines.Add([ordered]@{
                            text  = $line.Text
                            top   = [int]$globalTop
                            ratio = $ratio
                            left  = [int]($left / $scale)
                        })
                }
            } finally {
                if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
            }

            if ($offset + $blockHActual -ge $h) { break }
            $offset = $offset + $blockHActual - $overlap
        }

        # 去重（分块重叠区可能识别同一行两次）
        $seen = @{}
        $dedup = New-Object System.Collections.ArrayList
        foreach ($ln in $allLines) {
            $key = [string]$ln.top + '|' + [string]$ln.text
            if (-not $seen.ContainsKey($key)) {
                $seen[$key] = 1
                [void]$dedup.Add($ln)
            }
        }

        $item.lines = @($dedup)
        $item.lineCount = $dedup.Count
        $item.ok = $true
    } catch {
        $item.error = $_.Exception.Message
    } finally {
        if ($src) { $src.Dispose() }
    }
    return $item
}

# --- 主流程 ---------------------------------------------------------
$paths = @([System.IO.File]::ReadAllLines($ListFile, [System.Text.Encoding]::UTF8) | Where-Object { $_.Trim() -ne '' })
$items = New-Object System.Collections.ArrayList
$idx = 0
foreach ($p in $paths) {
    $idx++
    Write-Output ("[OCR {0}/{1}] {2}" -f $idx, $paths.Count, [System.IO.Path]::GetFileName($p))
    $r = Invoke-OcrImage -Path $p
    [void]$items.Add($r)
}

$payload = [ordered]@{
    ok      = $true
    engine  = $engineLang
    mode    = $Mode
    scale   = $scale
    count   = $items.Count
    items   = @($items)
}
$json = $payload | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding $false))
Write-Output ("[OCR] 完成，共 {0} 张" -f $items.Count)
