@echo off
setlocal
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\dev_down.ps1"
exit /b %errorlevel%

