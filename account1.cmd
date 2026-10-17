@echo off
rem Start the AC client for account 1 (accounts.local.json) through the relay.
cd /d "%~dp0"
call tools\py.cmd tools\aclaunch.py 1
if errorlevel 1 pause
