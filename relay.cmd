@echo off
rem Long-lived: keep this window open while you play. Stops a relay that's already running first.
cd /d "%~dp0"
call relay-stop.cmd
title 99companion relay
python -m companion.relay %*
