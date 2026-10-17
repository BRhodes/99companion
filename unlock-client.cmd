@echo off
rem Close the "Empyrean Client" instance lock in running clients so another can start.
cd /d "%~dp0"
python tools\aclaunch.py --unlock
pause
