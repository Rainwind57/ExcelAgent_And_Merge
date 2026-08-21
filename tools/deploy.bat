@echo off
setlocal
REM ============================================================
REM  One-click deploy (first-time setup)
REM  Steps: check tools (auto-install codemaker CLI) -> login netease account
REM         -> gen .env -> install python deps -> build svn demo fixture
REM         -> build frontend -> build table index -> install table mode -> start services
REM ============================================================
cd /d "%~dp0\.."

echo ========================================
echo   AI Excel Tool - Deploy
echo ========================================
echo.

REM [1/8] check tools + env
echo [1/8] Checking tools and environment...
call :check uv
if errorlevel 1 goto :err
call :check node
if errorlevel 1 goto :err
call :check npm
if errorlevel 1 goto :err
call :check svn
if errorlevel 1 goto :err
call :check svnadmin
if errorlevel 1 goto :err
call :check_codemaker
if errorlevel 1 goto :err
call :check_login
if errorlevel 1 goto :err
call :check_env
if errorlevel 1 goto :err
echo.

REM [2/8] .env
echo [2/8] Config file .env...
if not exist .env (
  if not exist .env.example (
    echo   [!] .env.example missing, cannot generate .env
    goto :err
  )
  copy .env.example .env >nul
  echo   generated .env from .env.example
) else (
  echo   .env exists, skip
)
echo.

REM [3/8] python deps
echo [3/8] Install python deps (uv sync)...
uv sync
if errorlevel 1 (
  echo   [X] uv sync failed
  goto :err
)
echo.

REM [4/8] svn demo fixture (merge/svn/demo_svn/, merge guide data source)
echo [4/8] Build svn demo fixture...
call :setup_svn_demo
if errorlevel 1 goto :err
echo.

REM [5/8] frontend
echo [5/8] Build frontend...
pushd frontend
if not exist node_modules (
  call npm install
  if errorlevel 1 (
    echo   [X] npm install failed
    popd
    goto :err
  )
)
call npm run build
if errorlevel 1 (
  echo   [X] frontend build failed
  popd
  goto :err
)
popd
echo.

REM [6/8] table index
echo [6/8] Build table index...
uv run python -W "ignore::RuntimeWarning:runpy" -m server.agent.excel.locator.index_builder
if errorlevel 1 (
  echo   [X] index build failed
  goto :err
)
echo.

REM [7/8] table mode
echo [7/8] Install table operation mode...
uv run python tools\table-mode\install.py
if errorlevel 1 (
  echo   [X] table mode install failed
  goto :err
)
echo.

REM [8/8] start services
echo [8/8] Start services...
call tools\start.bat --no-pause
if errorlevel 1 goto :err

:done
echo.
echo ========================================
echo   Deploy done
echo ========================================
echo   Frontend : http://127.0.0.1:8000
echo   API docs : http://127.0.0.1:8000/docs
echo   Table mode : run "enter table mode" in CodeMaker after services start
echo   See       : TABLE_MODE.md
echo.
if /i not "%~1"=="--no-pause" pause
exit /b 0

:check
where %1 >nul 2>nul
if errorlevel 1 (
  echo   [X] missing: %1
  exit /b 1
)
echo   [OK] %1
exit /b 0

REM R4: codemaker auto-install + version check + pin 1.17 (never upgrade to latest)
REM     model provider = netease-codemaker (see CODEMAKER_MODEL in .env)
:check_codemaker
where codemaker >nul 2>nul
if not errorlevel 1 goto :cm_found
echo   [~] codemaker not found, auto-installing CodeMaker CLI...
powershell -Command "irm https://codemaker.netease.com/package/codemaker-cli/install.ps1 | iex"
REM installer puts binary in %USERPROFILE%\.codemaker\bin, PATH may not refresh in current session
set "PATH=%USERPROFILE%\.codemaker\bin;%PATH%"
where codemaker >nul 2>nul
if errorlevel 1 (
  echo   [X] CodeMaker CLI install failed or not in PATH
  echo       run manually: powershell -Command "irm https://codemaker.netease.com/package/codemaker-cli/install.ps1 ^| iex"
  echo       then reopen terminal and run deploy.bat
  exit /b 1
)
echo   [OK] CodeMaker CLI installed
:cm_found
echo   [OK] codemaker found
set "CM_VER="
set "CM_PIN=1.17"
for /f "delims=" %%i in ('codemaker --version') do set "CM_VER=%%i"
echo   [i] current codemaker version %CM_VER% (pin target %CM_PIN%)
echo %CM_VER% | findstr /b /c:"%CM_PIN%" >nul
if not errorlevel 1 (
  echo   [OK] codemaker %CM_VER% pinned at %CM_PIN%
  exit /b 0
)
echo   [~] codemaker %CM_VER% -^> %CM_PIN%, switching...
codemaker upgrade %CM_PIN%
if errorlevel 1 (
  echo   [X] codemaker switch to %CM_PIN% failed
  exit /b 1
)
echo   [OK] codemaker pinned to %CM_PIN%
exit /b 0

REM check netease-codemaker login (each user logs into their own CodeMaker account)
REM creds stored at %USERPROFILE%\.local\share\codemaker\auth.json (per-user, not bundled)
:check_login
set "CM_AUTH=%USERPROFILE%\.local\share\codemaker\auth.json"
if exist "%CM_AUTH%" (
  findstr /c:"netease-codemaker" "%CM_AUTH%" >nul
  if not errorlevel 1 (
    echo   [OK] logged in to netease-codemaker
    exit /b 0
  )
)
echo   [~] no netease-codemaker login detected
echo       CodeMaker LLM calls need your OWN Netease CodeMaker account (not in .env)
echo       running: codemaker providers login -p netease-codemaker
echo.
call codemaker providers login -p netease-codemaker
echo.
if exist "%CM_AUTH%" (
  findstr /c:"netease-codemaker" "%CM_AUTH%" >nul
  if not errorlevel 1 (
    echo   [OK] login success
    exit /b 0
  )
)
echo   [X] login incomplete. run manually: codemaker providers login -p netease-codemaker
echo       then rerun tools\deploy.bat
exit /b 1

REM R4: env integrity check (Python>=3.10 / Node>=18 / resources has xlsx)
:check_env
REM Node >=18
set "NODE_VER_RAW="
for /f "delims=" %%i in ('node --version') do set "NODE_VER_RAW=%%i"
if "%NODE_VER_RAW%"=="" (
  echo   [X] cannot get Node version
  exit /b 1
)
set "NODE_MAJOR=%NODE_VER_RAW:v=%"
for /f "tokens=1 delims=." %%i in ("%NODE_MAJOR%") do set "NODE_MAJOR=%%i"
if %NODE_MAJOR% LSS 18 (
  echo   [X] Node too old, need ^>=18, current %NODE_VER_RAW%
  exit /b 1
)
echo   [OK] Node %NODE_VER_RAW%
REM Python >=3.10 (managed by uv, use sys.version_info)
uv run python -c "import sys; exit(0 if sys.version_info>=(3,10) else 1)"
if errorlevel 1 (
  echo   [X] Python too old, need ^>=3.10
  exit /b 1
)
for /f "tokens=2 delims= " %%i in ('uv run python --version') do echo   [OK] Python %%i
REM resources/ exists and has .xlsx
if not exist resources\ (
  echo   [X] resources/ dir missing
  exit /b 1
)
dir /b resources\*.xlsx 2>nul | findstr /i ".xlsx" >nul
if errorlevel 1 (
  echo   [X] no .xlsx files in resources/
  exit /b 1
)
echo   [OK] resources/ has Excel tables
exit /b 0

REM build SVN demo fixture (merge guide data source, merge/svn/demo_svn/)
REM idempotent: skip if repo exists; else build from bundled seed data merge/_seed_data/
REM build_svn_real creates trunk+dev1/dev2 (big tables; subdev_1 now a trunk subdir, not a branch)
REM build_svn_small_branches adds dev3/dev4 + trunk/subdev_2/subdev_3 (small tables only,
REM exclude monster/skill_level/item_drop) and sets a per-branch conflict anchor so any dev pair conflicts
:setup_svn_demo
if exist merge\svn\demo_svn\repo (
  echo   [OK] merge\svn\demo_svn\repo exists, skip
  exit /b 0
)
if not exist merge\_seed_data\ (
  echo   [X] merge\_seed_data\ missing, cannot build svn demo fixture
  echo       bundled with VCS, missing means incomplete checkout
  exit /b 1
)
echo   [~] building svn demo fixture from seed data (build + small branches + 4 seed)...
uv run python merge\scripts\setup_svn_demo.py
if errorlevel 1 (
  echo   [X] svn demo fixture build failed
  echo       common causes: svnadmin perm / TSVNCache lock / disk space. See merge\SETUP_SVN_DEMO.md
  exit /b 1
)
exit /b 0

:err
echo.
echo Deploy failed. See messages above.
if /i not "%~1"=="--no-pause" pause
exit /b 1
