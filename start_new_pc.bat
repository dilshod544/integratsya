@echo off
rem Bank Intelligence Platform - Windows launcher, no administrator rights needed.
rem First run downloads portable Python and PostgreSQL into this folder; later runs start instantly.
rem Messages are ASCII only: cmd.exe misreads UTF-8 text in batch files on many Windows setups.
setlocal EnableExtensions DisableDelayedExpansion
title Bank Intelligence Platform
cd /d "%~dp0"
set "ROOT=%CD%"

set "APP_PORT=8000"
set "PY_VERSION=3.11.9"
set "PY_DIR=%ROOT%\python_embed"
set "PG_VERSION=16.10-1"
set "PG_PORT=5433"
set "PG_DIR=%ROOT%\pgsql"
set "PG_DATA=%ROOT%\data\pgdata"
set "PG_LOG=%ROOT%\data\postgres.log"
set "PG_STARTED_HERE="

echo ===================================================
echo   Bank Intelligence Platform
echo   Avtomatik sozlash va ishga tushirish
echo ===================================================
echo.

rem ---------------- 1. Python ----------------
set "PY_CMD="
if exist "%PY_DIR%\python.exe" set "PY_CMD=%PY_DIR%\python.exe"
if not defined PY_CMD call :try_python py
if not defined PY_CMD call :try_python python
if not defined PY_CMD call :install_python
if not defined PY_CMD goto fail
echo [+] Python: %PY_CMD%

echo [+] Kutubxonalar tekshirilmoqda - requirements.txt ...
"%PY_CMD%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo [!] Kutubxonalarni o'rnatib bo'lmadi. Internet aloqasini tekshiring.
    goto fail
)

rem ---------------- 2. PostgreSQL ----------------
if not defined DATABASE_URL if exist ".env" for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if /i "%%a"=="DATABASE_URL" set "DATABASE_URL=%%b"
if defined DATABASE_URL set "DATABASE_URL=%DATABASE_URL:"=%"
if defined DATABASE_URL (
    echo [+] Baza: .env dagi DATABASE_URL ishlatiladi.
    goto db_ready
)

if exist "%PG_DIR%\bin\pg_ctl.exe" goto pg_installed
echo.
echo [+] Portativ PostgreSQL %PG_VERSION% yuklanmoqda - taxminan 320 MB, faqat birinchi marta.
call :download "https://get.enterprisedb.com/postgresql/postgresql-%PG_VERSION%-windows-x64-binaries.zip" "%ROOT%\pgsql.zip"
if errorlevel 1 goto fail
echo [+] Arxivdan chiqarilmoqda, bir necha daqiqa ketishi mumkin...
where tar.exe >nul 2>&1
if errorlevel 1 goto pg_unzip_ps
tar.exe -xf "%ROOT%\pgsql.zip" -C "%ROOT%" --exclude "pgsql/pgAdmin 4" --exclude "pgsql/StackBuilder" --exclude "pgsql/symbols" --exclude "pgsql/doc"
tar.exe -xf "%ROOT%\pgsql.zip" -C "%ROOT%" "pgsql/pgAdmin 4/python/vcruntime140.dll" "pgsql/pgAdmin 4/python/vcruntime140_1.dll" >nul 2>&1
:pg_unzip_ps
if not exist "%PG_DIR%\bin\pg_ctl.exe" powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ROOT%\pgsql.zip' -DestinationPath '%ROOT%' -Force"
del /q "%ROOT%\pgsql.zip" >nul 2>&1
if not exist "%PG_DIR%\bin\pg_ctl.exe" (
    echo [!] PostgreSQL arxivini ochib bo'lmadi.
    goto fail
)
rem PostgreSQL needs the Visual C++ runtime; take it from the archive when Windows does not have it.
if not exist "%SystemRoot%\System32\vcruntime140.dll" copy /y "%PG_DIR%\pgAdmin 4\python\vcruntime140.dll" "%PG_DIR%\bin\" >nul 2>&1
if not exist "%SystemRoot%\System32\vcruntime140_1.dll" copy /y "%PG_DIR%\pgAdmin 4\python\vcruntime140_1.dll" "%PG_DIR%\bin\" >nul 2>&1
:pg_installed

if exist "%PG_DATA%\PG_VERSION" goto pg_initialized
echo [+] Yangi baza yaratilmoqda: data\pgdata
if not exist "%ROOT%\data" mkdir "%ROOT%\data"
"%PG_DIR%\bin\initdb.exe" -D "%PG_DATA%" -U postgres -A trust -E UTF8 --locale=C > "%ROOT%\data\initdb.log" 2>&1
if errorlevel 1 (
    type "%ROOT%\data\initdb.log"
    echo.
    echo [!] Bazani yaratib bo'lmadi. Ko'pincha Visual C++ Redistributable yetishmaydi:
    echo     https://aka.ms/vs/17/release/vc_redist.x64.exe
    rmdir /s /q "%PG_DATA%" >nul 2>&1
    goto fail
)
:pg_initialized

"%PG_DIR%\bin\pg_isready.exe" -q -h localhost -p %PG_PORT%
if not errorlevel 1 goto pg_running
echo [+] PostgreSQL ishga tushirilmoqda - port %PG_PORT% ...
"%PG_DIR%\bin\pg_ctl.exe" -D "%PG_DATA%" -l "%PG_LOG%" -o "-p %PG_PORT% -c listen_addresses=localhost" -w -t 60 start >nul
if errorlevel 1 (
    echo [!] PostgreSQL ishga tushmadi. Log: data\postgres.log
    if exist "%PG_LOG%" type "%PG_LOG%"
    goto fail
)
set "PG_STARTED_HERE=1"
:pg_running
set "DATABASE_URL=postgresql://postgres@localhost:%PG_PORT%/bank_intelligence"
echo [+] Baza: portativ PostgreSQL, localhost:%PG_PORT%

:db_ready

rem ---------------- 3. Server ----------------
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%APP_PORT% .*LISTENING"') do (
    echo [+] Port %APP_PORT% band - eski jarayon PID %%p to'xtatilmoqda.
    taskkill /PID %%p /F >nul 2>&1
)

set "PYTHONPATH=%ROOT%;%ROOT%\api"
echo.
echo ===================================================
echo   Server ishga tushmoqda
echo   Dashboard: http://localhost:%APP_PORT%/dashboard/index.html
echo   Swagger:   http://localhost:%APP_PORT%/docs
echo   To'xtatish: Ctrl+C
echo ===================================================
echo.
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start "" http://localhost:%APP_PORT%/dashboard/index.html"
"%PY_CMD%" -m uvicorn api.main:app --host 0.0.0.0 --port %APP_PORT%

if defined PG_STARTED_HERE "%PG_DIR%\bin\pg_ctl.exe" -D "%PG_DATA%" -m fast stop >nul 2>&1
echo.
echo Server to'xtadi.
pause
exit /b 0

:fail
echo.
echo [!] Ishga tushirish to'xtatildi. Yuqoridagi xabarni ko'ring.
pause
exit /b 1


rem ================= subroutines =================

:try_python
rem The Microsoft Store "python" alias exists but does not run, so the candidate is executed, not just located.
%1 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY_CMD=%1"
exit /b 0

:install_python
echo [!] Kompyuterda mos Python 3.9+ topilmadi.
echo [+] Portativ Python %PY_VERSION% yuklanmoqda - o'rnatishsiz...
call :download "https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip" "%ROOT%\python_embed.zip"
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%ROOT%\python_embed.zip' -DestinationPath '%PY_DIR%' -Force"
del /q "%ROOT%\python_embed.zip" >nul 2>&1
if not exist "%PY_DIR%\python.exe" (
    echo [!] Python arxivini ochib bo'lmadi.
    exit /b 1
)
rem Embedded Python ignores PYTHONPATH; its search path comes only from this file.
> "%PY_DIR%\python311._pth" (
    echo python311.zip
    echo .
    echo ..
    echo Lib\site-packages
    echo import site
)
call :download "https://bootstrap.pypa.io/get-pip.py" "%PY_DIR%\get-pip.py"
if errorlevel 1 exit /b 1
"%PY_DIR%\python.exe" "%PY_DIR%\get-pip.py" --no-warn-script-location -q
if errorlevel 1 (
    echo [!] pip o'rnatilmadi.
    exit /b 1
)
del /q "%PY_DIR%\get-pip.py" >nul 2>&1
set "PY_CMD=%PY_DIR%\python.exe"
exit /b 0

:download
echo     %~1
if exist "%~2" del /q "%~2" >nul 2>&1
where curl.exe >nul 2>&1
if errorlevel 1 goto download_ps
curl.exe -L --fail --retry 3 --progress-bar -o "%~2" "%~1"
if not errorlevel 1 exit /b 0
if exist "%~2" del /q "%~2" >nul 2>&1
:download_ps
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri '%~1' -OutFile '%~2'"
if not errorlevel 1 if exist "%~2" exit /b 0
echo [!] Yuklab bo'lmadi: %~1
echo     Internet aloqasini yoki proxy sozlamalarini tekshiring.
exit /b 1
