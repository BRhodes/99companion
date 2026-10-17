@echo off
rem Restartable: reloads itself when its code changes, and catches up from the relay journal.
cd /d "%~dp0"
title 99companion dashboard
python -m companion.reloader %*
