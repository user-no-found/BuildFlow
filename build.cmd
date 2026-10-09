@echo off
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if defined PYTHON (
  "%PYTHON%" -m buildflow %*
) else (
  python -m buildflow %*
)
exit /b %errorlevel%
