@echo off
setlocal
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0tools\start_amadeus.ps1" %*
if errorlevel 1 (
  echo Amadeus System could not start. Check the message above, then run start.cmd again.
  pause
  exit /b 1
)
