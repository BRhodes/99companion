@echo off
cd /d "%~dp0"
start "99companion relay" cmd /k relay.cmd
start "99companion dashboard" cmd /k companion.cmd
start "" http://127.0.0.1:8099/
