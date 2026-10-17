@echo off
rem The Python the .cmd files run: this project's own (runtime\, set up by tools\setup.ps1 the first time), or
rem one already installed that has what requirements.txt asks for.  Usage: call tools\py.cmd <python arguments>
setlocal
set "OWN=%~dp0..\runtime\python.exe"
if exist "%OWN%" goto own
python -c "import sys, numpy, PIL; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    endlocal & python %*
    exit /b
)
echo.
echo First run: setting up Python for 99 Companion (a one-time download of about 40 MB)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
:own
endlocal & "%~dp0..\runtime\python.exe" %*
