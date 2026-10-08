@echo off
setlocal
set "NEXT_DISABLE_DEVTOOLS=1"
set "EXTRA_ARGS=-OpenBrowser"
if /I "%~1"=="--bootstrap" set "EXTRA_ARGS=-Bootstrap -OpenBrowser"
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\dev_up.ps1" %EXTRA_ARGS%
if errorlevel 1 (
  echo.
  echo Startup failed. Check logs under logs\*.err.log
  exit /b 1
)
echo.
echo Stack started successfully.
if /I not "%~1"=="--bootstrap" echo Tip: first run can use "start_dev.bat --bootstrap"
exit /b 0
