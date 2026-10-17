@echo off
rem Make client-windows-3d-accel-bg.exe (keeps drawing when unfocused) in the game folder and any account's own
rem folder (tools\bgpatch.py). Settings > Game > Make the background copy does the same. Writing into Program
rem Files needs administrator rights, so this asks Windows for them (UAC prompt).
cd /d "%~dp0.."
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
call tools\py.cmd tools\bgpatch.py
pause
