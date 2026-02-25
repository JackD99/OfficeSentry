@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

:: ================================================================
:: start.cmd — 1-click launcher for the M365 Audit Portal (Windows)
::
:: Double-click this file to:
::   1. Install Python dependencies
::   2. Download PowerShell modules (if pwsh available)
::   3. Launch the portal and open your browser
:: ================================================================

echo.
echo   ========================================
echo     M365 Audit Portal — Quick Start
echo   ========================================
echo.

:: ---- Check Python ----
echo   [..] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo   [FAIL] Python not found.
    echo          Install Python 3.9+ from https://python.org
    echo          Make sure "Add Python to PATH" is checked during install.
    echo.
    pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do echo   [OK] Python %%v

:: ---- Install Python dependencies ----
echo   [..] Installing Python dependencies...
pip install -r requirements.txt -q >nul 2>&1
if errorlevel 1 (
    echo   [FAIL] Failed to install dependencies.
    echo          Try: pip install -r requirements.txt
    pause
    exit /b 1
)
echo   [OK] Python dependencies installed

:: ---- Check PowerShell 7 ----
echo   [..] Checking PowerShell 7...
set "PWSH_BIN="
if exist "%~dp0bin\pwsh\pwsh.exe" (
    set "PWSH_BIN=%~dp0bin\pwsh\pwsh.exe"
    echo   [OK] Bundled pwsh found
) else (
    where pwsh >nul 2>&1
    if not errorlevel 1 (
        set "PWSH_BIN=pwsh"
        echo   [OK] PowerShell 7 available
    ) else (
        echo   [!!] PowerShell 7 not found — portal will work but audit scripts need pwsh
        echo          Install from: https://aka.ms/install-powershell
    )
)

:: ---- Download PowerShell modules if pwsh available and modules missing ----
if defined PWSH_BIN (
    if exist "%~dp0modules\PnP.PowerShell" (
        echo   [OK] PowerShell modules found
    ) else (
        if exist "%~dp0Build-Toolkit.ps1" (
            echo.
            echo   [..] Downloading PowerShell modules (first run only^)...
            "%PWSH_BIN%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0Build-Toolkit.ps1"
            if errorlevel 1 (
                echo   [!!] Some modules may have failed — retry with: pwsh -File Build-Toolkit.ps1
            ) else (
                echo   [OK] PowerShell modules downloaded
            )
        )
    )
)

:: ---- Create runtime directories ----
if not exist "%~dp0data" mkdir "%~dp0data"
if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%~dp0tenants" mkdir "%~dp0tenants"
if not exist "%~dp0config" mkdir "%~dp0config"

echo.
echo   ========================================
echo     Starting at http://127.0.0.1:5000
echo     Press Ctrl+C to stop the server
echo   ========================================
echo.

:: Open browser after short delay
start "" /b cmd /c "timeout /t 2 >nul && start http://127.0.0.1:5000"

:: Start Flask
python portal\app.py

pause
