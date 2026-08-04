@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
title AnyHardware AI - System Check

set "PASS=0"
set "WARN=0"
set "FAIL=0"

echo.
echo  ============================================================
echo   AnyHardware AI - System ^& Requirements Check
echo  ============================================================
echo.

rem ===================== Python =====================
set "PYCMD="
where py.exe >nul 2>&1 && set "PYCMD=py.exe"
if not defined PYCMD where python.exe >nul 2>&1 && set "PYCMD=python.exe"
if defined PYCMD (
    "%PYCMD%" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if !errorlevel! EQU 0 (
        echo   [OK]   Python %PYCMD% is 3.10 or newer
        set /a PASS+=1
    ) else (
        echo   [FAIL] Python found but it is older than 3.10
        echo         Install Python 3.10+ from https://www.python.org/downloads/
        set /a FAIL+=1
    )
) else (
    echo   [FAIL] Python was not found on PATH.
    echo         Install Python 3.10+ from https://www.python.org/downloads/
    echo         Tick "Add python.exe to PATH" during install.
    set /a FAIL+=1
)

rem ===================== pip =====================
if defined PYCMD (
    "%PYCMD%" -m pip --version >nul 2>&1
    if !errorlevel! EQU 0 (
        echo   [OK]   pip is available
        set /a PASS+=1
    ) else (
        echo   [FAIL] pip is not available. Repair the Python install.
        set /a FAIL+=1
    )
)

rem ===================== curl =====================
where curl.exe >nul 2>&1 && (
    echo   [OK]   curl.exe available ^(needed by download-models.bat^)
    set /a PASS+=1
) || (
    echo   [WARN] curl.exe not found. Use Windows 10 1803+ or install curl.
    set /a WARN+=1
)

rem ===================== llama.cpp =====================
where llama-server.exe >nul 2>&1 && (
    echo   [OK]   llama-server found on PATH
    set /a PASS+=1
) || (
    if exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\ggml.llamacpp" (
        echo   [OK]   llama.cpp found via WinGet packages
        set /a PASS+=1
    ) else (
        echo   [WARN] llama-server not found. Install it with:
        echo         winget install llama.cpp
        echo         or build from https://github.com/ggerganov/llama.cpp
        set /a WARN+=1
    )
)

rem ===================== Browser (optional) =====================
set "EDGE="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=yes"
if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=yes"
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "EDGE=yes"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "EDGE=yes"
if defined EDGE (
    echo   [OK]   Edge/Chrome found ^(browser tools will work^)
    set /a PASS+=1
) else (
    echo   [WARN] No Edge or Chrome found. Browser tools will be disabled.
    set /a WARN+=1
)

rem ===================== Machine facts =====================
for /f "delims=" %%a in ('powershell -NoProfile -Command "(Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors"') do set "CPUS=%%a"
for /f "delims=" %%a in ('powershell -NoProfile -Command "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)"') do set "TOTRAM=%%a"
for /f "delims=" %%a in ('powershell -NoProfile -Command "[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1MB,1)"') do set "FREERAM=%%a"
for /f "delims=" %%a in ('powershell -NoProfile -Command "$dl='%~d0'.TrimEnd(':');$s=Get-PSDrive -Name $dl;$gb=$s.Free/1GB;$gb.ToString('0.0')"') do set "FREEDISK=%%a"
echo.
echo  ------------------------------------------------------------
echo   Machine: %CPUS% logical CPUs ^| %TOTRAM% GiB total RAM ^| %FREERAM% GiB free RAM
echo   Free disk on %~d0 : %FREEDISK% GiB
echo  ------------------------------------------------------------

rem ===================== Project install state =====================
if exist ".venv\Scripts\python.exe" (
    echo   [OK]   Project virtual environment found ^(.venv^)
    set /a PASS+=1
) else (
    echo   [WARN] No .venv yet. Create it with:
    echo         py -m pip install -e .
    set /a WARN+=1
)

rem ===================== Models present =====================
set "MODELS_COUNT=0"
for %%f in (models\*.gguf) do set /a MODELS_COUNT+=1
if %MODELS_COUNT% GTR 0 (
    echo   [OK]   %MODELS_COUNT% model file^(s^) found in models\
    set /a PASS+=1
) else (
    echo   [WARN] No models found. Run download-models.bat to fetch one.
    set /a WARN+=1
)

echo.
echo  ============================================================
echo   Result: %PASS% OK  ^|  %WARN% WARNINGS  ^|  %FAIL% FAILURES
echo  ============================================================
if %FAIL% GTR 0 (
    echo.
    echo   Some required items are missing. Fix the [FAIL] items above.
) else (
    echo.
    echo   Your machine is ready. Next steps:
    echo     1. Run  download-models.bat   to get a GGUF model into models\
    echo     2. Run  start-web.bat         to launch the web UI on http://127.0.0.1:8300
    echo     or    ssd-llm web --port 8300
)
echo.
pause
