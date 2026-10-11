@echo off
REM Subcontract OS - one-click setup and start on Windows.
REM Creates .venv on first run, installs dependencies into it, loads the demo, starts the app.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating virtual environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Python 3.10+ not found. Install it from https://www.python.org/downloads/ and tick "Add to PATH".
    pause & exit /b 1
  )
)

set PY=.venv\Scripts\python.exe
"%PY%" -c "import fastapi, uvicorn, sqlalchemy, docx, openpyxl, pypdf, reportlab, cryptography" 2>nul
if errorlevel 1 (
  echo [2/4] Installing dependencies into .venv ...
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -r requirements.txt reportlab
  if errorlevel 1 ( echo ERROR: dependency installation failed - see messages above. & pause & exit /b 1 )
)

if not exist "var\subcontract_os.db" (
  echo [3/4] Loading demo data...
  "%PY%" -m seed.make_samples
  "%PY%" -m seed.demo_seed
)

echo [4/4] Starting Subcontract OS on http://localhost:8000  (Ctrl+C to stop)
start "" http://localhost:8000
"%PY%" -m uvicorn app.main:app --port 8000
