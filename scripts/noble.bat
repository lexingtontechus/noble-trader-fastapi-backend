@echo off
REM noble — CLI wrapper for Noble Trader calibration tools
REM Usage: noble.bat calibration

set "REPO_ROOT=%~dp0.."
set "VENV_PY=%REPO_ROOT%\.venv\Scripts\python.exe"
set "SRC=%REPO_ROOT%\src"

if not exist "%VENV_PY%" (
    echo ERROR: .venv\Scripts\python.exe not found at %VENV_PY% >&2
    echo Run: cd %REPO_ROOT% && uv sync >&2
    exit /b 1
)

set "PYTHONPATH=%SRC%"

if "%~1"=="calibration" (
    "%VENV_PY%" "%REPO_ROOT%\scripts\noble_calibration.py"
    exit /b %errorlevel%
)

echo Usage: %~nx0 ^<command^>
echo.
echo Commands:
echo   calibration   Run 24hr calibration report (scripts\noble_calibration.py)
echo.
exit /b 1
