@echo off
rem ccm: launcher used instead of claude, so the bot can read the screen and type for you (no tmux). Usage: ccm [claude args...]
if exist "%~dp0..\venv\Scripts\python.exe" (
  "%~dp0..\venv\Scripts\python.exe" "%~dp0..\lib\ccterm.py" %*
) else (
  py -3 "%~dp0..\lib\ccterm.py" %*
)
exit /b %ERRORLEVEL%
