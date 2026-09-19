@echo off
REM PixLift Windows 后台启动脚本
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..
cd /d "%PROJECT_DIR%"

if not exist ".venv" (
    echo [pixlift] no .venv found. Run: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -e .
    exit /b 1
)

if not exist "pixlift\bin\realesrgan-ncnn-vulkan.exe" (
    if "%REAL_ESRGAN_BINARY%"=="" (
        echo [pixlift] WARNING: binary not found. Run: python -m pixlift.bin_setup
        echo [pixlift] (or set REAL_ESRGAN_BINARY=C:\path\to\binary.exe)
        echo [pixlift] Service will start but /api/health will return 503.
    )
)

if not exist "logs" mkdir logs

set HOST=%HOST%
if "%HOST%"=="" set HOST=0.0.0.0
set PORT=%PORT%
if "%PORT%"=="" set PORT=8000
set LOG_LEVEL=%LOG_LEVEL%
if "%LOG_LEVEL%"=="" set LOG_LEVEL=info

echo [pixlift] starting on %HOST%:%PORT%
start /b "" .venv\Scripts\python.exe run.py --host %HOST% --port %PORT% --log-level %LOG_LEVEL% >> logs\pixlift.log 2>&1

echo [pixlift] started (logs\pixlift.log)
echo [pixlift] open http://localhost:%PORT%
endlocal