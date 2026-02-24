@echo off
setlocal
cd /d "%~dp0"

echo.
echo   ========================================
echo     M365 Audit Portal
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

:: ---- Check PowerShell 7 (optional) ----
echo   [..] Checking PowerShell 7...
if exist "%~dp0bin\pwsh\pwsh.exe" (
    echo   [OK] Bundled pwsh found
) else (
    where pwsh >nul 2>&1
    if errorlevel 1 (
        echo   [!!] PowerShell 7 not found — portal UI works but audit scripts need pwsh
        echo          Install from: https://aka.ms/install-powershell
    ) else (
        echo   [OK] PowerShell 7 available
    )
)

:: ---- Install / update dependencies ----
echo.
echo   [..] Installing Python dependencies...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo   [FAIL] Failed to install dependencies.
    echo          Try: pip install -r requirements.txt
    pause
    exit /b 1
)
echo   [OK] Dependencies installed

:: ---- Check PowerShell modules ----
if exist "%~dp0modules\PnP.PowerShell" (
    echo   [OK] PowerShell modules found
) else (
    echo   [!!] PowerShell modules not downloaded yet
    echo          Run: pwsh -File Build-Toolkit.ps1
)

:: ---- Create runtime directories ----
if not exist "%~dp0data" mkdir "%~dp0data"
if not exist "%~dp0logs" mkdir "%~dp0logs"
if not exist "%~dp0tenants" mkdir "%~dp0tenants"

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
