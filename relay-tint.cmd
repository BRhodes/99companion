@echo off
rem Long-lived: keep this window open while you play. The relay, with the store-tint experiment on
rem (in a vendor's window, the components not on your shopping list show on another background). relay.cmd runs it without.
rem Stops a relay that's already running first.
cd /d "%~dp0"
call relay-stop.cmd
title 99companion relay (tint-store)
call tools\py.cmd -m companion.relay --tint-store %*
