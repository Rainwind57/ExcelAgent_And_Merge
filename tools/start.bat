@echo off
setlocal
cd /d "%~dp0\.."
echo [1] pwd=%CD%

if not exist .env (
  echo no_env
  pause
  exit /b 1
)
echo [2] env_ok

where codemaker >nul 2>nul
if errorlevel 1 (
  echo no_cm
  pause
  exit /b 1
)
echo [3] cm_ok

set "CM_AUTH=%USERPROFILE%\.local\share\codemaker\auth.json"
echo [4] CM_AUTH=%CM_AUTH%
if not exist "%CM_AUTH%" goto :no_login
echo [5] auth_exists
findstr /c:"netease-codemaker" "%CM_AUTH%" >nul 2>nul
if errorlevel 1 goto :no_login
echo [6] login_ok
goto :after_login

:no_login
echo no_login_branch
pause
exit /b 1

:after_login
echo [7] loading env
REM .env is UTF-8 with Chinese comments. cmd for/f parses with GBK and
REM multi-byte chars at comment-line ends swallow adjacent key lines
REM (OPENCODE_SERVER_USERNAME / CODEMAKER_MODEL lost -> serve 401,
REM model dropdown empty). Convert .env to GBK temp file via PowerShell,
REM then for/f reads it correctly.
set "_ENVGBK=%TEMP%\_aitable_env_gbk.txt"
powershell -NoProfile -Command "Get-Content -LiteralPath '.env' -Encoding UTF8 | Out-File -LiteralPath '%_ENVGBK%' -Encoding Default"
for /f "usebackq eol=# tokens=1,* delims==" %%a in ("%_ENVGBK%") do (
  if not "%%a"=="" set "%%a=%%b"
)
del "%_ENVGBK%" >nul 2>nul
echo [8] env_loaded USERNAME=%OPENCODE_SERVER_USERNAME%

echo [9] check serve port
netstat -ano | findstr ":8666 " | findstr "LISTENING" >nul
if errorlevel 1 (
  echo [9a] starting serve
  start "CodeMaker Serve" cmd /k "codemaker serve --port 8666 --hostname 0.0.0.0"
)
echo [10] wait serve
set /a _tries=0
:serve_wait
set /a _tries+=1
if %_tries% gtr 60 goto serve_fail
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr ":8666 " | findstr "LISTENING" >nul
if errorlevel 1 goto serve_wait
echo [11] serve_ready
REM probe /api/model: 200=ok, 401=cred mismatch, other=warn
set "_PROBE_CODE="
for /f %%c in ('powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8666/api/model' -Headers @{Authorization='Basic '+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('%OPENCODE_SERVER_USERNAME%:%OPENCODE_SERVER_PASSWORD%'))} -TimeoutSec 10; $r.StatusCode } catch { $_.Exception.Response.StatusCode.value__ }"') do set "_PROBE_CODE=%%c"
if "%_PROBE_CODE%"=="200" goto :probe_ok
if "%_PROBE_CODE%"=="401" goto :probe_401
echo [11a][!] serve /api/model returned '%_PROBE_CODE%' (models may be empty)
goto serve_done
:probe_ok
echo [11a] serve /api/model OK
goto serve_done
:probe_401
echo [11a][X] serve 401 Unauthorized: OPENCODE_SERVER_USERNAME/PASSWORD mismatch
echo     stale serve window may hold old creds. Close it and rerun start.bat.
goto serve_done
:serve_fail
echo serve_fail
pause
exit /b 1
:serve_done

echo [12] check api port
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if errorlevel 1 (
  echo [12a] starting api
  start "FastAPI Backend" cmd /k "uv run python server/main.py"
)
echo [13] wait api (startup inits index+skills, may exceed 30s)
set /a _tries=0
:api_wait
set /a _tries+=1
if %_tries% gtr 120 goto api_fail
ping -n 2 127.0.0.1 >nul
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul
if errorlevel 1 goto api_wait
REM port bound but startup() may still run; probe /docs until 200
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/docs' -TimeoutSec 5; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>nul
if errorlevel 1 goto api_wait
echo [14] api_ready
goto api_done
:api_fail
echo api_fail
pause
exit /b 1
:api_done

echo [15] backend startup will open browser automatically
echo [16] done
exit /b 0
