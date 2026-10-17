@echo off
rem Stop any relay already running (relay.cmd and relay-tint.cmd call this first, so either one restarts it).
rem Sessions survive: the relay saves where each one is every few seconds and a new one picks them up.
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -match 'companion\.relay(\s|$)' }; foreach ($x in $p) { Write-Host ('stopping the relay already running (pid ' + $x.ProcessId + ')'); Stop-Process -Id $x.ProcessId -Force }; if ($p) { Start-Sleep -Milliseconds 800 }"
