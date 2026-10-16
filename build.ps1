# Builds dist\ClipTrimmer\ClipTrimmer.exe (a folder app: the exe plus its
# _internal folder with Qt, mpv and ffmpeg). Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root
$py = "$root\.venv\Scripts\python.exe"

& $py "$root\tools\make_icon.py"
& $py -m PyInstaller --noconfirm --clean --windowed `
    --name ClipTrimmer `
    --icon "$root\assets\ClipTrimmer.ico" `
    --add-data "$root\clipt\fonts;clipt\fonts" `
    --add-binary "$root\bin\libmpv-2.dll;bin" `
    --add-binary "$root\bin\ffmpeg.exe;bin" `
    --add-binary "$root\bin\ffprobe.exe;bin" `
    --workpath "$root\build" --distpath "$root\dist" --specpath "$root\build" `
    "$root\ClipTrimmer.pyw"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built $root\dist\ClipTrimmer\ClipTrimmer.exe"
