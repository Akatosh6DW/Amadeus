@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\bootstrap_deploy.ps1" %*
if errorlevel 1 (
  echo Deployment failed. Review the message above, then run deploy.cmd again.
  pause
  exit /b 1
)
