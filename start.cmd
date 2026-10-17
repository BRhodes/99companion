@echo off
rem Double-click to play. Sets up Python the first time (tools\py.cmd), then runs the relay and the dashboard
rem in this one window (companion.reloader) and opens the dashboard in your browser once it answers.
rem Leave the window open while you play; closing it stops both (game clients keep running).
cd /d "%~dp0"
title 99 Companion
call tools\py.cmd -c "pass" || exit /b 1
title 99 Companion - leave this window open while you play
start "" /b powershell -NoProfile -Command "for ($i = 0; $i -lt 90; $i++) { try { (New-Object Net.Sockets.TcpClient('127.0.0.1', 8099)).Close(); Start-Process 'http://127.0.0.1:8099/'; break } catch { Start-Sleep 1 } }"
call tools\py.cmd -m companion.reloader %*
