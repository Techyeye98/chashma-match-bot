@echo off
rem Runs a SEPARATE test bot on this PC (its own token in .env.test, its own data in data-test).
rem The real @ChashmaMatchBot on the VPS keeps running untouched.
cd /d "%~dp0"
title Frame Matcher TEST bot
if not exist ".env.test" (
  copy ".env.example" ".env.test" >nul
  echo .env.test bana di hai. Isme TEST bot ka token daalo, phir ye file dobara chalao.
  notepad ".env.test"
  pause
  exit /b
)
set FM_ENV_FILE=.env.test
set FM_DATA_DIR=data-test
python bot.py
pause
