@echo off
setlocal

cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
    echo [setup] Creating project virtual environment...
    python -m venv "%VENV%" || exit /b 1
)

"%PY%" -c "import textual" >nul 2>nul
if errorlevel 1 (
    echo [setup] Installing CLI-Downloader...
    "%PY%" -m pip install -e . || exit /b 1
)

"%PY%" -m cli_downloader shell %*
