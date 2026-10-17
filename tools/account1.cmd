@echo off
rem Start the game for account 1 (Settings > Accounts) through the relay: the dashboard's play buttons do the same.
cd /d "%~dp0.."
call tools\py.cmd tools\aclaunch.py 1
if errorlevel 1 pause
