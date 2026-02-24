@echo off
setlocal
cd /d "%~dp0"

echo.
echo  M365 Audit Portal
echo  ==================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo  Install Python 3.8+ from https://python.org and try again.
    echo.
    pause
    exit /b 1
)

:: Install / update Flask if needed
echo  Checking dependencies...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo  [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

:: Open browser after short delay
echo  Starting portal at http://127.0.0.1:5000
echo  Close this window to stop the server.
echo.
start "" /b cmd /c "timeout /t 2 >nul && start http://127.0.0.1:5000"

:: Start Flask
python portal\app.py

pause
