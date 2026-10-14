# Draws the CLUD icon and writes a multi-size .ico (PNG entries, Vista+).
param([string]$Out = (Join-Path $PSScriptRoot '..\stick\clud.ico'))

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

function Draw([int]$s) {
    $bmp = New-Object System.Drawing.Bitmap $s, $s
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'; $g.TextRenderingHint = 'AntiAliasGridFit'
    $g.Clear([System.Drawing.Color]::Transparent)
    $m = [Math]::Max(1, [int]($s / 64)); $w = $s - 2 * $m; $r = [int]($s * 0.22)
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $p.AddArc($m, $m, $r, $r, 180, 90); $p.AddArc($m + $w - $r, $m, $r, $r, 270, 90)
    $p.AddArc($m + $w - $r, $m + $w - $r, $r, $r, 0, 90); $p.AddArc($m, $m + $w - $r, $r, $r, 90, 90)
    $p.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 217, 119, 87))), $p)
    # Too small for the word: just a "C"
    $text = if ($s -lt 48) { 'C' } else { 'CLUD' }
    $size = if ($s -lt 48) { $s * 0.72 } else { $s * 0.25 }
    # Text as a path, centred on its real ink bounds (DrawString pads glyphs unevenly,
    # which pushed the small "C" off-centre)
    $t = New-Object System.Drawing.Drawing2D.GraphicsPath
    $t.AddString($text, (New-Object System.Drawing.FontFamily 'Segoe UI Black'), [int][System.Drawing.FontStyle]::Bold, $size,
        (New-Object System.Drawing.PointF 0, 0), [System.Drawing.StringFormat]::GenericTypographic)
    # Snap the ink box to whole pixels with even slack on both axes, so it sits exactly
    # centred on the pixel grid (matters at 16-24px, where half a pixel is visible)
    $b = $t.GetBounds()
    $w = [Math]::Round($b.Width); if (($s - $w) % 2) { $w += 1 }
    $h = [Math]::Round($b.Height); if (($s - $h) % 2) { $h += 1 }
    $mx = New-Object System.Drawing.Drawing2D.Matrix
    $mx.Translate(($s - $w) / 2, ($s - $h) / 2)
    $mx.Scale($w / $b.Width, $h / $b.Height)
    $mx.Translate(-$b.X, -$b.Y)
    $t.Transform($mx)
    $g.FillPath([System.Drawing.Brushes]::White, $t)
    $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    , $ms.ToArray()
}

$sizes = 16, 20, 24, 32, 40, 48, 64, 128, 256
$pngs = foreach ($s in $sizes) { , (Draw $s) }

$ico = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter $ico
$bw.Write([uint16]0); $bw.Write([uint16]1); $bw.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $d = if ($sizes[$i] -ge 256) { 0 } else { $sizes[$i] }
    $bw.Write([byte]$d); $bw.Write([byte]$d); $bw.Write([byte]0); $bw.Write([byte]0)
    $bw.Write([uint16]1); $bw.Write([uint16]32)
    $bw.Write([uint32]$pngs[$i].Length); $bw.Write([uint32]$offset)
    $offset += $pngs[$i].Length
}
foreach ($png in $pngs) { $bw.Write($png) }
$bw.Flush()
[IO.File]::WriteAllBytes([IO.Path]::GetFullPath($Out), $ico.ToArray())
