@echo off
cd /d "%~dp0"
title Lenskart Frame Matcher Bot
:loop
python bot.py
echo.
echo Bot band ho gaya. 10 second mein dobara start hoga (band karne ke liye ye window close karo).
timeout /t 10 >nul
goto loop
