@echo off
rem SSD-LLM web UI launcher
cd /d "%~dp0"

set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" (
    echo [ERROR] Virtual environment not found at %PY%
    echo Run: py -m pip install -e .
    pause
    exit /b 1
)

set "PORT=8300"

rem ---- Stop any already-running SSD-LLM server before starting a new one ----
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    echo [INFO] Stopping existing SSD-LLM web server on port %PORT% ^(PID %%a^)
    taskkill /F /PID %%a >nul 2>&1
)
rem Kill orphaned engine child processes left behind by the old server.
taskkill /F /IM llama-server.exe >nul 2>&1

echo Starting SSD-LLM web UI at http://127.0.0.1:%PORT%
"%PY%" -m ssd_llm.cli web --host 127.0.0.1 --port %PORT%
pause
