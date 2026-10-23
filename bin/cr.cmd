@echo off
rem cr: same commands as in Feishu (cr status, cr start, ...). See lib\crcli.py
set "HERE=%~dp0.."
if exist "%HERE%\venv\Scripts\python.exe" (
  "%HERE%\venv\Scripts\python.exe" "%HERE%\lib\crcli.py" %*
) else (
  py -3 "%HERE%\lib\crcli.py" %*
)
