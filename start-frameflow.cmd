@echo off
setlocal
cd /d "%~dp0"

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-frameflow.ps1" %*
set "FRAMEFLOW_EXIT_CODE=%ERRORLEVEL%"

if not "%FRAMEFLOW_EXIT_CODE%"=="0" (
  echo.
  echo FrameFlow could not be started. See the message above.
  pause
)

exit /b %FRAMEFLOW_EXIT_CODE%
