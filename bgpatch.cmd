@echo off
rem Make client-windows-3d-accel-bg.exe (keeps drawing when unfocused) in both client folders.
rem Writing into Program Files needs administrator rights, so this asks Windows for them (UAC prompt).
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
python tools\bgpatch.py "C:\Program Files (x86)\Microsoft Games\Asheron's Call"
python tools\bgpatch.py "C:\Program Files (x86)\Microsoft Games\Asheron's Call2"
pause
