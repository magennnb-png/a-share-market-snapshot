@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0update_market.ps1" (
  echo [ERROR] update_market.ps1 was not found. Please restore the repository files.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_market.ps1" -NoPause %*
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo Update failed. Exit code: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
