@echo off
title Pipeline Hub - EXE Builder
cd /d "%~dp0"
echo.
echo  ============================================
echo   Building PipelineHub.exe  (takes ~1 min)
echo  ============================================
echo.
echo  [1/2] Installing build tools...
python -m pip install --quiet --upgrade pyinstaller pywebview pillow
if errorlevel 1 (
    echo.
    echo  ERROR: pip failed. Is Python installed and on PATH?
    pause
    exit /b 1
)
echo  [2/2] Compiling...
python -m PyInstaller --onefile --noconsole --name PipelineHub --distpath "%~dp0." --workpath "%~dp0_build" --specpath "%~dp0_build" pipeline_hub.py
if errorlevel 1 (
    echo.
    echo  ERROR: build failed. Screenshot this window and send it to Claude.
    pause
    exit /b 1
)
rmdir /s /q "%~dp0_build" 2>nul
echo.
echo  ============================================
echo   Done!  PipelineHub.exe is in this folder.
echo   Right-click it - Pin to taskbar.
echo  ============================================
echo.
pause
