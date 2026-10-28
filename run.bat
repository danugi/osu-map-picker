@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title osu! Map Picker
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "requirements.txt" (
    echo [ERROR] requirements.txt not found next to run.bat.
    echo Keep run.bat in the repository root.
    pause
    exit /b 1
)

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
set "HAVE_PYTHON="
set "PY_BOOT="

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "HAVE_PYTHON=1"
    ) else (
        echo [ERROR] The existing .venv is not a compatible Python 3.12+ environment.
        echo Rename or remove the .venv folder and run run.bat again.
        pause
        exit /b 1
    )
)

if not defined HAVE_PYTHON (
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "HAVE_PYTHON=1"
            set "PY_BOOT=py -3"
        )
    )
)

if not defined HAVE_PYTHON (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
        if not errorlevel 1 (
            set "HAVE_PYTHON=1"
            set "PY_BOOT=python"
        )
    )
)

if not defined HAVE_PYTHON (
    echo.
    echo [ERROR] Compatible Python 3.12 or newer was not found.
    echo Install Python from:
    echo   https://www.python.org/downloads/windows/
    echo During installation enable "Add python.exe to PATH".
    echo Then double-click run.bat again.
    echo.
    pause
    exit /b 1
)

if not exist "%VENV_PY%" (
    echo Creating project-local virtual environment .venv ...
    %PY_BOOT% -m venv ".venv"
    if errorlevel 1 (
        echo [ERROR] Could not create .venv. Check the Python installation and disk permissions.
        pause
        exit /b 1
    )
)

"%VENV_PY%" -c "import streamlit, httpx, dotenv, keyring" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies from requirements.txt ...
    "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. See the messages above.
        pause
        exit /b 1
    )
) else (
    echo Dependencies already installed; skipping pip install.
)

set "DATA="
if defined OSU_PICKER_DATA_DIR (
    set "DATA=%OSU_PICKER_DATA_DIR%"
    echo Using OSU_PICKER_DATA_DIR: !DATA!
) else (
    if defined LOCALAPPDATA (
        set "DATA=%LOCALAPPDATA%\OsuMapPicker"
    ) else (
        set "DATA=%USERPROFILE%\OsuMapPicker"
    )
)

set "DATA_EXISTED=0"
if exist "!DATA!" (
    set "DATA_EXISTED=1"
    pushd "!DATA!" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] The data path exists but is not a usable directory:
        echo   !DATA!
        echo Existing data will not be moved or replaced.
        goto data_error
    )
    popd
) else (
    mkdir "!DATA!" 2>nul
    if errorlevel 1 (
        if defined OSU_PICKER_DATA_DIR goto data_error
        echo [WARN] Could not create the preferred data directory:
        echo   !DATA!
        set "DATA=%USERPROFILE%\OsuMapPicker"
        echo [WARN] Using the per-user alternative:
        echo   !DATA!
        if not exist "!DATA!" mkdir "!DATA!" 2>nul
        if errorlevel 1 goto data_error
    )
)

if not exist "!DATA!\." goto data_error
> "!DATA!\.osu-map-picker-write-test" echo probe 2>nul
if errorlevel 1 (
    if "!DATA_EXISTED!"=="0" if not defined OSU_PICKER_DATA_DIR (
        echo [WARN] No write access to the preferred path:
        echo   !DATA!
        set "DATA=%USERPROFILE%\OsuMapPicker"
        echo [WARN] Using the per-user alternative:
        echo   !DATA!
        if not exist "!DATA!" mkdir "!DATA!" 2>nul
        if errorlevel 1 goto data_error
        > "!DATA!\.osu-map-picker-write-test" echo probe 2>nul
        if errorlevel 1 goto data_error
        del "!DATA!\.osu-map-picker-write-test" >nul 2>&1
    ) else (
        echo [ERROR] No write access to the data directory:
        echo   !DATA!
        echo Existing data will not be moved or replaced.
        goto data_error
    )
)
del "!DATA!\.osu-map-picker-write-test" >nul 2>&1

set "OSU_PICKER_DATA_DIR=!DATA!"
set "OSU_PICKER_PORTABLE=1"
if defined OSU_PICKER_DB (
    echo Using OSU_PICKER_DB override: %OSU_PICKER_DB%
)

echo.
echo Starting the local app at 127.0.0.1 ...
echo Console stays open while the application is running.
echo Data directory: !DATA!
echo API setup: first-run screen or Settings ^> API. .env is also supported.
echo.
"%VENV_PY%" launcher.py
set "APP_EXIT=%ERRORLEVEL%"
echo.
if not "%APP_EXIT%"=="0" (
    echo [ERROR] The application exited with code %APP_EXIT%.
) else (
    echo Application stopped.
)
echo Diagnostics: !DATA!\logs\launcher.log
echo Press any key to close this console.
pause >nul
exit /b %APP_EXIT%

:data_error
echo.
echo [ERROR] The persistent data directory could not be created or written.
echo Check permissions, or set OSU_PICKER_DATA_DIR to another folder under
echo your user profile, for example:
echo   set OSU_PICKER_DATA_DIR=%USERPROFILE%\OsuMapPicker
echo Then run run.bat again. Existing databases are never deleted or replaced.
pause
exit /b 1
