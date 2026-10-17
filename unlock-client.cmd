@echo off
rem Close the "Empyrean Client" instance lock in running clients so another can start.
cd /d "%~dp0"
call tools\py.cmd tools\aclaunch.py --unlock
pause
