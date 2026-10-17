@echo off
rem Double-click to play: sets up Python the first time (tools\py.cmd), then opens the relay, the dashboard
rem and, once it's answering, the dashboard in your browser.
cd /d "%~dp0"
title 99 Companion
call tools\py.cmd -c "pass" || exit /b 1
start "99companion relay" cmd /k relay.cmd
start "99companion dashboard" cmd /k companion.cmd
echo Opening the dashboard...
powershell -NoProfile -Command "for ($i = 0; $i -lt 60; $i++) { try { (New-Object Net.Sockets.TcpClient('127.0.0.1', 8099)).Close(); break } catch { Start-Sleep 1 } }"
start "" http://127.0.0.1:8099/
