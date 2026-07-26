@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo Starting turb-gpt-free-register and RoxyBrowser...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Startup failed. Review the error above and the .runtime logs.
    pause
    exit /b %EXIT_CODE%
)

echo.
echo Startup completed. This window will close in 3 seconds.
ping 127.0.0.1 -n 4 >nul
exit /b 0
