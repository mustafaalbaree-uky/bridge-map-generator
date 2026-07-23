@echo off
REM ===== KYTC Bridge Map -> Excel : double-click launcher (Windows) =====
REM First run sets things up (a minute or two). Later runs start in seconds.
REM Keep this window open while you use the app. Close it to stop the app.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python is not installed.
  echo Install Python 3 from https://www.python.org/downloads/
  echo ^(tick "Add Python to PATH" in the installer^), then double-click this again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo Creating environment ^(first run only^)...
  python -m venv .venv
)

call ".venv\Scripts\activate.bat"

if not exist ".venv\.installed" (
  echo Installing dependencies ^(first run only, may take a minute^)...
  python -m pip install --upgrade pip >nul 2>nul
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Something went wrong installing dependencies. Check your internet and try again.
    pause
    exit /b 1
  )
  echo done > ".venv\.installed"
)

echo.
echo Starting the app... your browser will open at http://localhost:8000
echo Leave this window open. Close it when you're done to stop the app.
echo.
if exist ".do_update" del ".do_update"
start "" http://localhost:8000

:run
python -m uvicorn app:app --host 127.0.0.1 --port 8000
REM If the app applied an update it leaves a .do_update flag and exits; then we
REM refresh dependencies (in case the update added any) and relaunch.
if exist ".do_update" (
  del ".do_update"
  echo.
  echo Update downloaded - refreshing and restarting...
  python -m pip install -r requirements.txt
  goto run
)

pause
