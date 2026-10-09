@echo off
setlocal DisableDelayedExpansion
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if defined PYTHON (
  "%PYTHON%" -m buildflow.bootstrap --register BUILDFLOW_PATH "%~dp0." %*
) else (
  python -m buildflow.bootstrap --register BUILDFLOW_PATH "%~dp0." %*
)
exit /b %errorlevel%
