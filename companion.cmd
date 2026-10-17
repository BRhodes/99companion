@echo off
rem start.cmd without opening the browser: the relay and the dashboard, each restarted when its code changes.
rem companion.cmd --no-relay leaves the relay to relay.cmd.
cd /d "%~dp0"
title 99companion
call tools\py.cmd -m companion.reloader %*
