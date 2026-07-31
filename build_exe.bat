@echo off
REM ============================================================================
REM  ClipTrimmer - one-click standalone .exe builder
REM
REM  Double-click this file. It will:
REM    1. Make sure PyInstaller is installed
REM    2. Download FFmpeg and bundle it INSIDE the exe (fully offline result)
REM    3. Produce dist\ClipTrimmer\ClipTrimmer.exe (--onedir, no console window)
REM
REM  --onedir is used deliberately: a --onefile build re-extracts ~100 MB to a
REM  temp folder on every launch, which is slow and wastes disk. Ship the whole
REM  dist\ClipTrimmer folder.
REM
REM  Requirements: Python 3.8+ installed and on PATH, plus internet (build time).
REM  The finished ClipTrimmer.exe needs neither Python nor internet to run.
REM ============================================================================

setlocal EnableExtensions
pushd "%~dp0"
title Building ClipTrimmer.exe

echo.
echo ==========================================================
echo   Building ClipTrimmer.exe   (this takes a few minutes)
echo ==========================================================
echo.

REM ---- 1. Locate Python -------------------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python was not found on your PATH.
  echo         Install Python 3.8+ from https://www.python.org/downloads/
  echo         and tick "Add Python to PATH" during setup, then re-run this.
  goto :fail
)
echo [1/5] Using Python: %PY%
%PY% --version

REM ---- 2. Install / update PyInstaller ---------------------------------------
echo.
echo [2/5] Installing PyInstaller...
REM  NOTE: we deliberately do NOT run "pip install --upgrade pip" here - that
REM  mutates the user's global Python install as a side effect of a build.
%PY% -m pip install --upgrade --quiet pyinstaller
if errorlevel 1 (
  echo [ERROR] Could not install PyInstaller via pip.
  goto :fail
)

REM ---- 3. Download + stage FFmpeg --------------------------------------------
echo.
echo [3/5] Downloading FFmpeg (~25 MB)...
set "FF_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
set "FF_ZIP=%~dp0ffmpeg_dl.zip"
if exist "%FF_ZIP%" del /q "%FF_ZIP%"

REM Prefer curl.exe (built into Win10/11, streams to disk - fast).
where curl >nul 2>&1
if %errorlevel%==0 (
  echo       using curl...
  curl -L --fail --progress-bar -o "%FF_ZIP%" "%FF_URL%"
) else (
  echo       curl not found, falling back to PowerShell...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri '%FF_URL%' -OutFile '%FF_ZIP%'"
)
if not exist "%FF_ZIP%" (
  echo [ERROR] FFmpeg download failed. Check your internet connection.
  goto :fail
)

echo       unpacking...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$zip='%FF_ZIP%';" ^
  "$ext=Join-Path $PWD 'ffmpeg_extract';" ^
  "if(Test-Path $ext){Remove-Item -Recurse -Force $ext};" ^
  "Expand-Archive -Path $zip -DestinationPath $ext -Force;" ^
  "$ff=Get-ChildItem -Path $ext -Recurse -Filter 'ffmpeg.exe'  | Select-Object -First 1;" ^
  "$fp=Get-ChildItem -Path $ext -Recurse -Filter 'ffprobe.exe' | Select-Object -First 1;" ^
  "if(-not $ff -or -not $fp){throw 'ffmpeg/ffprobe not found in archive'};" ^
  "$dest=Join-Path $PWD 'ffmpeg';" ^
  "New-Item -ItemType Directory -Force -Path $dest | Out-Null;" ^
  "Copy-Item $ff.FullName (Join-Path $dest 'ffmpeg.exe')  -Force;" ^
  "Copy-Item $fp.FullName (Join-Path $dest 'ffprobe.exe') -Force;" ^
  "Remove-Item $zip -Force; Remove-Item -Recurse -Force $ext;"
if errorlevel 1 (
  echo [ERROR] FFmpeg download/extraction failed. Check your internet connection.
  goto :fail
)
if not exist "ffmpeg\ffmpeg.exe"  ( echo [ERROR] ffmpeg.exe missing.  & goto :fail )
if not exist "ffmpeg\ffprobe.exe" ( echo [ERROR] ffprobe.exe missing. & goto :fail )

REM ---- 4. Build the exe with PyInstaller -------------------------------------
echo.
echo [4/5] Compiling ClipTrimmer.exe ... (full log -^> build_log.txt)
set "LOG=%~dp0build_log.txt"
%PY% -m PyInstaller ^
  --onedir ^
  --noconfirm ^
  --noconsole ^
  --name ClipTrimmer ^
  --add-data "%~dp0ffmpeg\ffmpeg.exe;ffmpeg" ^
  --add-data "%~dp0ffmpeg\ffprobe.exe;ffmpeg" ^
  --distpath "%~dp0dist" ^
  --workpath "%TEMP%\cliptrimmer_build" ^
  --specpath "%TEMP%\cliptrimmer_build" ^
  "%~dp0ClipTrimmer.pyw"  > "%LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] PyInstaller build failed. Full details saved to build_log.txt
  echo         ---- last lines of the log ----
  powershell -NoProfile -Command "Get-Content -LiteralPath '%LOG%' -Tail 20"
  goto :fail
)

REM ---- 5. Clean up staging files --------------------------------------------
echo.
echo [5/5] Cleaning up...
if exist "ffmpeg" rmdir /s /q "ffmpeg"
if exist "%TEMP%\cliptrimmer_build" rmdir /s /q "%TEMP%\cliptrimmer_build"

echo.
if exist "dist\ClipTrimmer\ClipTrimmer.exe" (
  echo ==========================================================
  echo   SUCCESS  -  dist\ClipTrimmer\ClipTrimmer.exe is ready.
  echo   Keep the whole ClipTrimmer folder together and double-click
  echo   the exe inside it. No Python or internet needed.
  echo ==========================================================
) else (
  echo [ERROR] Build finished but dist\ClipTrimmer\ClipTrimmer.exe was not produced.
  goto :fail
)
echo.
pause
popd
endlocal
exit /b 0

:fail
echo.
echo Build did not complete. Nothing was installed system-wide.
echo.
pause
popd
endlocal
exit /b 1
