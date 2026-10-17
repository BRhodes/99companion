@echo off
rem Start the AC client for account 2 (accounts.local.json) through the relay.
cd /d "%~dp0"
python tools\aclaunch.py 2
if errorlevel 1 pause
