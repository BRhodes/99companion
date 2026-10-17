@echo off
rem Just the relay (start.cmd's window runs it for you). Keep this window open while you play.
rem Stops a relay that's already running first.
cd /d "%~dp0.."
call tools\relay-stop.cmd
title 99companion relay
call tools\py.cmd -m companion.relay %*
