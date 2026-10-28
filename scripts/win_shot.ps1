# win_shot.ps1 - save a full-screen PNG (used to eyeball a window from WSL).
# ASCII only (PS 5.1 reads BOM-less UTF-8 .ps1 as CP932).
param(
    [Parameter(Position = 0)][string]$Out = "C:\Users\<user>\shot.png"
)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()
Write-Host "SHOT_OK $Out $($b.Width)x$($b.Height)"
