@echo off
setlocal

REM Prefer bundled pwsh if you decide to include it in .\bin\pwsh\pwsh.exe
if exist "%~dp0bin\pwsh\pwsh.exe" (
  "%~dp0bin\pwsh\pwsh.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0toolkit.ps1"
  exit /b %errorlevel%
)

REM Fallback to system pwsh if available
where pwsh >nul 2>&1
if %errorlevel%==0 (
  pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0toolkit.ps1"
  exit /b %errorlevel%
)

echo PowerShell 7 (pwsh) not found. Either bundle it in .\bin\pwsh\ or install PowerShell 7.
exit /b 1
