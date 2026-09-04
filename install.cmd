@echo off
setlocal
where py >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0scripts\install.py" %*
) else (
  python "%~dp0scripts\install.py" %*
)
exit /b %errorlevel%
