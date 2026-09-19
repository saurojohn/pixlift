@echo off
REM PixLift Windows 停止脚本
setlocal

set SCRIPT_DIR=%~dp0
set PROJECT_DIR=%SCRIPT_DIR%..
cd /d "%PROJECT_DIR%"

REM 通过端口找进程
set PORT=%PORT%
if "%PORT%"=="" set PORT=8000

for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    echo [pixlift] killing pid %%a
    taskkill /F /PID %%a 2>nul
)

echo [pixlift] stopped
endlocal