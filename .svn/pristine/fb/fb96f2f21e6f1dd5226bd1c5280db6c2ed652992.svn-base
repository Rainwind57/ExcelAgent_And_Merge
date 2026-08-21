@echo off
chcp 936 >nul
REM ============================================================
REM  Stop services by port: 8666 (codemaker serve) / 8000 (FastAPI)
REM  Usage: stop.bat             interactive
REM         stop.bat --no-pause  called from other scripts
REM ============================================================
echo Stopping services...

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8666 " ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F /T >nul 2>nul && echo   stopped CodeMaker Serve (PID %%a)
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
  taskkill /PID %%a /F /T >nul 2>nul && echo   stopped FastAPI (PID %%a)
)

if /i "%1"=="--no-pause" exit /b 0
echo Done.
pause
exit /b 0
