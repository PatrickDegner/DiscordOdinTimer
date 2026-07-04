@echo off
setlocal EnableExtensions

REM Discord Odin Timer startup script
CD /D "%~dp0"

set "PYTHON_CMD="
python --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 (
    set "PYTHON_CMD=python"
) ELSE (
    py -3 --version >nul 2>&1
    IF %ERRORLEVEL% EQU 0 (
        set "PYTHON_CMD=py -3"
    )
)

IF "%PYTHON_CMD%"=="" (
    ECHO ERROR: Python was not found.
    ECHO Install Python 3 and ensure either "python" or "py" is available in PATH.
    ECHO Download: https://www.python.org/downloads/
    PAUSE
    EXIT /B 1
)

TITLE Discord Odin Timer

ECHO ========================================
ECHO        Discord Odin Timer Bot
ECHO ========================================
ECHO.
ECHO Using Python command: %PYTHON_CMD%
ECHO.

IF NOT EXIST "venv" (
    ECHO Creating virtual environment...
    %PYTHON_CMD% -m venv venv
    IF %ERRORLEVEL% NEQ 0 (
        ECHO ERROR: Failed to create virtual environment.
        PAUSE
        EXIT /B %ERRORLEVEL%
    )
) ELSE (
    ECHO Virtual environment already exists.
)

IF NOT EXIST "venv\Scripts\python.exe" (
    ECHO ERROR: Could not find venv\Scripts\python.exe
    PAUSE
    EXIT /B 1
)

ECHO Installing/updating required packages...
venv\Scripts\python.exe -m pip install --upgrade pip
IF %ERRORLEVEL% NEQ 0 (
    ECHO ERROR: Failed to upgrade pip.
    PAUSE
    EXIT /B %ERRORLEVEL%
)

venv\Scripts\python.exe -m pip install -r requirements.txt
IF %ERRORLEVEL% NEQ 0 (
    ECHO ERROR: Failed to install dependencies from requirements.txt
    PAUSE
    EXIT /B %ERRORLEVEL%
)

ECHO.
ECHO Starting Discord Odin Timer Bot...
ECHO Press Ctrl+C to stop.
ECHO.

venv\Scripts\python.exe main.py
set "EXIT_CODE=%ERRORLEVEL%"

ECHO.
IF %EXIT_CODE% NEQ 0 (
    ECHO Bot exited with errors (exit code: %EXIT_CODE%).
) ELSE (
    ECHO Bot exited successfully.
)

ECHO.
ECHO Press any key to close this window...
PAUSE >nul
EXIT /B %EXIT_CODE%
