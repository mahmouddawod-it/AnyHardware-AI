@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
title AnyHardware AI - Model Downloader

set "MODELS_DIR=%~dp0models"
if not exist "%MODELS_DIR%" mkdir "%MODELS_DIR%"

where curl.exe >nul 2>&1 || (
    echo [ERROR] curl.exe was not found.
    echo AnyHardware AI needs curl ^(ships with Windows 10 1803+^).
    pause
    exit /b 1
)

rem ---- Free physical RAM in GiB ----
for /f "delims=" %%a in ('powershell -NoProfile -Command "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"') do set "FREERAM_KB=%%a"
set /a FREERAM_GB=%FREERAM_KB%/1048576
if %FREERAM_GB% LSS 1 set "FREERAM_GB=1"

:menu
cls
echo.
echo  ============================================================
echo   AnyHardware AI - Model Downloader
echo  ============================================================
echo   Download folder : %MODELS_DIR%
echo   Free RAM now    : %FREERAM_GB% GiB  (models need ~2x their size in RAM)
echo  ------------------------------------------------------------
if %FREERAM_GB% GEQ 18 (
    echo   Recommended for your RAM: anything below ^(Qwen2.5-14B and smaller^)
) else if %FREERAM_GB% GEQ 10 (
    echo   Recommended for your RAM: Qwen2.5-7B and smaller
) else if %FREERAM_GB% GEQ 6 (
    echo   Recommended for your RAM: Qwen2.5-3B and smaller
) else if %FREERAM_GB% GEQ 4 (
    echo   Recommended for your RAM: Qwen2.5-1.5B and smaller
) else (
    echo   Recommended for your RAM: Qwen2.5-0.5B only
)
echo  ------------------------------------------------------------
echo   [1] Qwen2.5-0.5B-Instruct  Q4_K_M   397 MB    needs ~2 GiB RAM
echo   [2] Qwen2.5-1.5B-Instruct  Q4_K_M   1.1 GB    needs ~4 GiB RAM
echo   [3] Qwen2.5-3B-Instruct   Q4_K_M   2.1 GB    needs ~6 GiB RAM
echo   [4] Qwen2.5-7B-Instruct   Q4_K_M   4.7 GB    needs ~10 GiB RAM   (2 parts)
echo   [5] Qwen2.5-14B-Instruct  Q4_K_M   9.0 GB    needs ~18 GiB RAM  (3 parts)
echo   [0] Exit
echo  ------------------------------------------------------------
set "CHOICE="
set /p "CHOICE=Pick a model number: "
if "%CHOICE%"=="0" exit /b 0
if "%CHOICE%"=="1" goto dl_05
if "%CHOICE%"=="2" goto dl_15
if "%CHOICE%"=="3" goto dl_3
if "%CHOICE%"=="4" goto dl_7
if "%CHOICE%"=="5" goto dl_14
goto menu

:dl_05
set "NAME=Qwen2.5-0.5B-Instruct Q4_K_M"
call :download_file "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf" "%MODELS_DIR%\qwen2.5-0.5b-instruct-q4_k_m.gguf"
goto after_download

:dl_15
set "NAME=Qwen2.5-1.5B-Instruct Q4_K_M"
call :download_file "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf" "%MODELS_DIR%\qwen2.5-1.5b-instruct-q4_k_m.gguf"
goto after_download

:dl_3
set "NAME=Qwen2.5-3B-Instruct Q4_K_M"
call :download_file "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf" "%MODELS_DIR%\qwen2.5-3b-instruct-q4_k_m.gguf"
goto after_download

:dl_7
set "NAME=Qwen2.5-7B-Instruct Q4_K_M"
echo.
echo   Downloading %NAME% (part 1 of 2)
call :download_file "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf" "%MODELS_DIR%\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"
echo   Downloading %NAME% (part 2 of 2)
call :download_file "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf" "%MODELS_DIR%\qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf"
goto after_download

:dl_14
set "NAME=Qwen2.5-14B-Instruct Q4_K_M"
echo.
echo   Downloading %NAME% (part 1 of 3)
call :download_file "https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m-00001-of-00003.gguf" "%MODELS_DIR%\qwen2.5-14b-instruct-q4_k_m-00001-of-00003.gguf"
echo   Downloading %NAME% (part 2 of 3)
call :download_file "https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m-00002-of-00003.gguf" "%MODELS_DIR%\qwen2.5-14b-instruct-q4_k_m-00002-of-00003.gguf"
echo   Downloading %NAME% (part 3 of 3)
call :download_file "https://huggingface.co/Qwen/Qwen2.5-14B-Instruct-GGUF/resolve/main/qwen2.5-14b-instruct-q4_k_m-00003-of-00003.gguf" "%MODELS_DIR%\qwen2.5-14b-instruct-q4_k_m-00003-of-00003.gguf"
goto after_download

:after_download
echo.
echo   Done. Files saved to: %MODELS_DIR%
echo.
pause
goto menu

rem ============================================================
rem  Subroutine: download one file with resume support
rem  Usage: call :download_file "<url>" "<destination>"
rem  A ".part" file is kept while downloading, so an interrupted
rem  run can resume where it stopped (curl -C -).
rem ============================================================
:download_file
set "SRC=%~1"
set "DST=%~2"
set "PART=%~2.part"
set "OKFILE=%~2.ok"
if exist "%OKFILE%" (
    echo   [SKIP] %~nx2 already downloaded
    exit /b 0
)
if exist "%DST%" (
    type nul > "%OKFILE%"
    echo   [SKIP] %~nx2 already downloaded
    exit /b 0
)
echo   Downloading %~nx2 to %MODELS_DIR%
curl.exe -L -C - --retry 3 --retry-delay 2 -o "%PART%" "%SRC%"
set "RC=%errorlevel%"
if %RC% EQU 0 goto dl_done
if %RC% EQU 33 goto dl_done
echo   [WARN] curl exited with code %RC% - the file may be incomplete.
echo          Run this script again to resume the download.
exit /b 0
:dl_done
if exist "%PART%" move /y "%PART%" "%DST%" >nul
type nul > "%OKFILE%"
echo   [OK] %~nx2
exit /b 0
