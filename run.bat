@echo off
title NewsForge — AI News Publisher
color 0B

echo.
echo  ============================================
echo    NewsForge — AI News Publisher Dashboard
echo  ============================================
echo.

:: Find Python
where py >nul 2>&1
if %errorlevel%==0 (set PYTHON=py) else (
  where python >nul 2>&1
  if %errorlevel%==0 (set PYTHON=python) else (
    where python3 >nul 2>&1
    if %errorlevel%==0 (set PYTHON=python3) else (
      echo  [ERROR] Python not found.
      echo  Download from: https://www.python.org/downloads/
      echo  Make sure to check "Add Python to PATH" during install.
      pause
      exit /b 1
    )
  )
)

echo  Python found: %PYTHON%
echo.

:: Install dependencies
echo  Installing / checking dependencies...
%PYTHON% -m pip install flask anthropic requests beautifulsoup4 python-dotenv --quiet --upgrade
if %errorlevel% neq 0 (
  echo  [ERROR] Failed to install dependencies.
  pause
  exit /b 1
)

echo  Dependencies ready.
echo.
echo  Starting server at http://localhost:5000
echo  Press Ctrl+C to stop.
echo.

:: Open browser after 2 seconds
start "" timeout /t 2 >nul 2>&1
start "" http://localhost:5000

:: Run Flask
cd /d "%~dp0"
%PYTHON% app.py

pause
