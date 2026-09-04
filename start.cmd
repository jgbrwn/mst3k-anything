@echo off
setlocal
where py >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0scripts\start.py" %*
) else (
  python "%~dp0scripts\start.py" %*
)
exit /b %errorlevel%
