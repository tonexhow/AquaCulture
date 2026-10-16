# Export the existing AquaCulture vector geometry as Android launcher PNGs.
# Windows development helper only; committed PNGs need no runtime graphics dependency.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$iconDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../webbluetooth/icons'))
foreach ($size in @(192, 512)) {
    $bitmap = New-Object Drawing.Bitmap($size, $size)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.ScaleTransform(($size / 64.0), ($size / 64.0))
    $graphics.Clear([Drawing.ColorTranslator]::FromHtml('#0f3547'))
    $fish = New-Object Drawing.Drawing2D.GraphicsPath
    $fish.AddBezier(13, 27, 22, 16, 33, 14, 45, 22)
    $fish.AddLine(45, 22, 53, 14)
    $fish.AddLine(53, 14, 53, 38)
    $fish.AddLine(53, 38, 45, 30)
    $fish.AddBezier(45, 30, 33, 38, 22, 36, 13, 27)
    $fish.CloseFigure()
    $gradient = New-Object Drawing.Drawing2D.LinearGradientBrush([Drawing.Point]::new(13, 14), [Drawing.Point]::new(53, 38), [Drawing.ColorTranslator]::FromHtml('#21b8ad'), [Drawing.ColorTranslator]::FromHtml('#1485aa'))
    $graphics.FillPath($gradient, $fish)
    $eye = New-Object Drawing.SolidBrush([Drawing.ColorTranslator]::FromHtml('#eafff9'))
    $graphics.FillEllipse($eye, 28.8, 21.8, 4.4, 4.4)
    $waves = New-Object Drawing.Drawing2D.GraphicsPath
    $waves.AddBezier(11, 42, 18, 35, 26, 35, 33, 42)
    $waves.AddBezier(33, 42, 40, 49, 47, 49, 54, 42)
    $pen = New-Object Drawing.Pen([Drawing.ColorTranslator]::FromHtml('#78e3d2'), 3.2)
    $pen.StartCap = $pen.EndCap = [Drawing.Drawing2D.LineCap]::Round
    $graphics.DrawPath($pen, $waves)
    $bitmap.Save((Join-Path $iconDirectory "icon-$size.png"), [Drawing.Imaging.ImageFormat]::Png)
    foreach ($resource in @($pen, $waves, $eye, $gradient, $fish, $graphics, $bitmap)) { $resource.Dispose() }
}
Write-Output 'Exported 192px and 512px AquaCulture launcher icons.'
