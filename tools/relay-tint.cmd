@echo off
rem Just the relay, with the experimental vendor backgrounds on (--tint-store: in a vendor's window, the components
rem not on your shopping list show on another background). Settings > Game turns it on for start.cmd's window.
rem Stops a relay that's already running first.
cd /d "%~dp0.."
call tools\relay-stop.cmd
title 99companion relay (tint-store)
call tools\py.cmd -m companion.relay --tint-store %*
