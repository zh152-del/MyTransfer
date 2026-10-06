$ErrorActionPreference = "Stop"
$dir = "F:\work Buddy\2026-10-05-17-12-22\MyTransfer"
Set-Location $dir
$py = "C:\Program Files\Python313\pythonw.exe"
if (-not (Test-Path $py)) { $py = "pythonw" }
$p = Start-Process -FilePath $py -ArgumentList 'MyTransfer.py','--send','testfiles\测试100MB.bin' -PassThru -WorkingDirectory $dir
Start-Sleep -Seconds 6
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
$b = New-Object System.Drawing.Bitmap 1280, 800
$g = [System.Drawing.Graphics]::FromImage($b)
$g.CopyFromScreen(0, 0, [System.Drawing.Point]::Empty, $b.Size)
$out = Join-Path $dir "ui-preview.png"
$b.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $b.Dispose()
Write-Output "saved=$out"
Stop-Process -Id $p.Id -Force
