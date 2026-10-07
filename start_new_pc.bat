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
if not "%errorlevel%"=="0" (
    echo [!] Kutubxonalarni o'rnatib bo'lmadi. Internet aloqasini tekshiring.
    goto fail
)

rem ---------------- 2. PostgreSQL ----------------
if not defined DATABASE_URL if exist ".env" for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if /i "%%a"=="DATABASE_URL" set "DATABASE_URL=%%~b"
if defined DATABASE_URL (
    echo [+] Baza: .env dagi DATABASE_URL ishlatiladi.
    goto db_ready
)

rem Exit codes are compared with "%errorlevel%"=="0": a program that cannot start (missing DLL)
rem returns a negative code, which "if errorlevel 1" silently treats as success.

rem pgsql\.complete is written only after a verified extraction; a half-extracted folder is reinstalled.
if exist "%PG_DIR%\.complete" goto pg_installed
if exist "%PG_DATA%\postmaster.pid" if exist "%PG_DIR%\bin\pg_ctl.exe" "%PG_DIR%\bin\pg_ctl.exe" -D "%PG_DATA%" -m fast stop >nul 2>&1
if exist "%PG_DIR%" rmdir /s /q "%PG_DIR%"
echo.
echo [+] Portativ PostgreSQL %PG_VERSION% yuklanmoqda - taxminan 320 MB, faqat birinchi marta.
call :download "https://get.enterprisedb.com/postgresql/postgresql-%PG_VERSION%-windows-x64-binaries.zip" "%ROOT%\pgsql.zip"
if not "%errorlevel%"=="0" goto fail
echo [+] Arxiv tekshirilmoqda va chiqarilmoqda, bir necha daqiqa ketishi mumkin...
call :extract_pg
set "RC=%errorlevel%"
del /q "%ROOT%\pgsql.zip" >nul 2>&1
if not "%RC%"=="0" goto fail
:pg_installed

"%PG_DIR%\bin\postgres.exe" -V >nul 2>&1
if not "%errorlevel%"=="0" call :add_vc_runtime
"%PG_DIR%\bin\postgres.exe" -V >nul 2>&1
if not "%errorlevel%"=="0" (
    echo [!] PostgreSQL dasturlari bu kompyuterda ishga tushmayapti.
    echo     Visual C++ Redistributable o'rnatib, qayta urinib ko'ring:
    echo     https://aka.ms/vs/17/release/vc_redist.x64.exe
    goto fail
)

if exist "%PG_DATA%\PG_VERSION" goto pg_initialized
echo [+] Yangi baza yaratilmoqda: data\pgdata
if exist "%PG_DATA%" rmdir /s /q "%PG_DATA%"
if not exist "%ROOT%\data" mkdir "%ROOT%\data"
"%PG_DIR%\bin\initdb.exe" -D "%PG_DATA%" -U postgres -A trust -E UTF8 --locale=C > "%ROOT%\data\initdb.log" 2>&1
if not "%errorlevel%"=="0" goto pg_init_failed
if not exist "%PG_DATA%\PG_VERSION" goto pg_init_failed
:pg_initialized

"%PG_DIR%\bin\pg_isready.exe" -q -h localhost -p %PG_PORT%
if "%errorlevel%"=="0" goto pg_running
echo [+] PostgreSQL ishga tushirilmoqda - port %PG_PORT% ...
"%PG_DIR%\bin\pg_ctl.exe" -D "%PG_DATA%" -l "%PG_LOG%" -o "-p %PG_PORT% -c listen_addresses=localhost" -w -t 60 start >nul
if not "%errorlevel%"=="0" goto pg_start_failed
"%PG_DIR%\bin\pg_isready.exe" -q -h localhost -p %PG_PORT%
if not "%errorlevel%"=="0" goto pg_start_failed
set "PG_STARTED_HERE=1"
:pg_running
set "DATABASE_URL=postgresql://postgres@localhost:%PG_PORT%/bank_intelligence"
echo [+] Baza: portativ PostgreSQL, localhost:%PG_PORT%

rem A bank_intelligence.dump next to this script (made by export_db.sh on the other computer) is restored once.
if not exist "%ROOT%\bank_intelligence.dump" goto db_ready
echo.
echo [?] bank_intelligence.dump topildi - boshqa kompyuterdan ko'chirilgan baza.
echo     Bu kompyuterdagi baza shu fayldagi ma'lumotlar bilan almashtiriladi.
choice /c YN /m "    Tiklaymi"
if errorlevel 2 goto db_ready
"%PG_DIR%\bin\dropdb.exe" -h localhost -p %PG_PORT% -U postgres --if-exists --force bank_intelligence
"%PG_DIR%\bin\createdb.exe" -h localhost -p %PG_PORT% -U postgres -E UTF8 -T template0 bank_intelligence
if not "%errorlevel%"=="0" (
    echo [!] Bazani yaratib bo'lmadi.
    goto fail
)
"%PG_DIR%\bin\pg_restore.exe" -h localhost -p %PG_PORT% -U postgres -d bank_intelligence --no-owner --no-privileges --exit-on-error "%ROOT%\bank_intelligence.dump"
if not "%errorlevel%"=="0" (
    echo [!] Faylni tiklab bo'lmadi. Fayl to'liq ko'chirilganini tekshiring.
    goto fail
)
move /y "%ROOT%\bank_intelligence.dump" "%ROOT%\data\bank_intelligence.dump.restored" >nul
echo [+] Ma'lumotlar tiklandi. Fayl data\bank_intelligence.dump.restored ga ko'chirildi.

:db_ready

rem ---------------- 3. Server ----------------
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%APP_PORT% .*LISTENING"') do (
    echo [+] Port %APP_PORT% band - eski jarayon PID %%p to'xtatilmoqda.
    taskkill /PID %%p /F >nul 2>&1
)

set "PYTHONPATH=%ROOT%;%ROOT%\api"
echo.
echo ===================================================
echo   Server ishga tushmoqda...
echo   Brauzer server tayyor bo'lganda o'zi ochiladi:
echo   http://localhost:%APP_PORT%/dashboard/index.html
echo   To'xtatish: Ctrl+C
echo ===================================================
echo.
rem The browser is opened only after /api/health answers, so a failed start never shows a dead page.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -Command "for ($i = 0; $i -lt 120; $i++) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri 'http://127.0.0.1:%APP_PORT%/api/health' | Out-Null; Start-Process 'http://localhost:%APP_PORT%/dashboard/index.html'; break } catch { Start-Sleep -Seconds 1 } }"
rem 127.0.0.1: the link printed by uvicorn opens in a browser, and with login disabled the app is not exposed to the network.
rem For access from other computers run:  set APP_HOST=0.0.0.0  before this script.
if not defined APP_HOST set "APP_HOST=127.0.0.1"
"%PY_CMD%" -m uvicorn api.main:app --host %APP_HOST% --port %APP_PORT%

if defined PG_STARTED_HERE "%PG_DIR%\bin\pg_ctl.exe" -D "%PG_DATA%" -m fast stop >nul 2>&1
echo.
echo [!] Server to'xtadi. Agar bu kutilmagan bo'lsa, yuqoridagi xabarni ko'ring.
pause
exit /b 0

:pg_init_failed
if exist "%ROOT%\data\initdb.log" type "%ROOT%\data\initdb.log"
echo.
echo [!] Bazani yaratib bo'lmadi. Log: data\initdb.log
if exist "%PG_DATA%" rmdir /s /q "%PG_DATA%"
goto fail

:pg_start_failed
echo [!] PostgreSQL ishga tushmadi. Log: data\postgres.log
if exist "%PG_LOG%" type "%PG_LOG%"
goto fail

:fail
echo.
echo [!] Ishga tushirish to'xtatildi. Yuqoridagi xabarni ko'ring.
pause
exit /b 1


rem ================= subroutines =================

:try_python
rem The Microsoft Store "python" alias exists but does not run, so the candidate is executed, not just located.
%1 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if "%errorlevel%"=="0" set "PY_CMD=%1"
exit /b 0

:extract_pg
where tar.exe >nul 2>&1
if not "%errorlevel%"=="0" goto extract_pg_ps
tar.exe -xf "%ROOT%\pgsql.zip" -C "%ROOT%" --exclude "pgsql/pgAdmin 4" --exclude "pgsql/StackBuilder" --exclude "pgsql/symbols" --exclude "pgsql/doc"
if not "%errorlevel%"=="0" goto extract_pg_bad
goto extract_pg_check
:extract_pg_ps
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Expand-Archive -LiteralPath '%ROOT%\pgsql.zip' -DestinationPath '%ROOT%' -Force"
if not "%errorlevel%"=="0" goto extract_pg_bad
:extract_pg_check
for %%f in (bin\postgres.exe bin\initdb.exe bin\pg_ctl.exe bin\pg_isready.exe bin\pg_restore.exe bin\libpq.dll share\postgres.bki) do if not exist "%PG_DIR%\%%f" goto extract_pg_bad
call :add_vc_runtime
echo ok> "%PG_DIR%\.complete"
exit /b 0
:extract_pg_bad
echo [!] PostgreSQL arxivi chala yuklangan yoki buzilgan.
echo     Internet aloqasini tekshirib, skriptni qayta ishga tushiring.
if exist "%PG_DIR%" rmdir /s /q "%PG_DIR%"
exit /b 1

:add_vc_runtime
rem Visual C++ runtime DLLs placed next to the PostgreSQL programs, so no system install (admin) is needed.
echo [+] Visual C++ runtime fayllari qo'shilmoqda...
set "VC_TMP=%ROOT%\data\vc_runtime"
if exist "%VC_TMP%" rmdir /s /q "%VC_TMP%"
mkdir "%VC_TMP%"
"%PY_CMD%" -m pip download msvc-runtime --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.11 --implementation cp --abi cp311 -d "%VC_TMP%" -q --disable-pip-version-check
if not "%errorlevel%"=="0" goto add_vc_runtime_done
for %%w in ("%VC_TMP%\*.whl") do copy /y "%%~w" "%VC_TMP%\vc.zip" >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Expand-Archive -LiteralPath '%VC_TMP%\vc.zip' -DestinationPath '%VC_TMP%\x' -Force"
for /d %%d in ("%VC_TMP%\x\*.data") do copy /y "%%~d\data\Scripts\*.dll" "%PG_DIR%\bin\" >nul
:add_vc_runtime_done
if exist "%VC_TMP%" rmdir /s /q "%VC_TMP%"
exit /b 0

:install_python
echo [!] Kompyuterda mos Python 3.9+ topilmadi.
echo [+] Portativ Python %PY_VERSION% yuklanmoqda - o'rnatishsiz...
call :download "https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-embed-amd64.zip" "%ROOT%\python_embed.zip"
if not "%errorlevel%"=="0" exit /b 1
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
if not "%errorlevel%"=="0" exit /b 1
"%PY_DIR%\python.exe" "%PY_DIR%\get-pip.py" --no-warn-script-location -q
if not "%errorlevel%"=="0" (
    echo [!] pip o'rnatilmadi.
    exit /b 1
)
del /q "%PY_DIR%\get-pip.py" >nul 2>&1
set "PY_CMD=%PY_DIR%\python.exe"
exit /b 0

:download
rem PowerShell goes first: it uses the Windows proxy settings (corporate networks); curl.exe does not.
echo     %~1
if exist "%~2" del /q "%~2" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $w=New-Object Net.WebClient; $w.Proxy=[Net.WebRequest]::GetSystemWebProxy(); $w.Proxy.Credentials=[Net.CredentialCache]::DefaultNetworkCredentials; $w.DownloadFile('%~1','%~2')"
if "%errorlevel%"=="0" if exist "%~2" exit /b 0
if exist "%~2" del /q "%~2" >nul 2>&1
where curl.exe >nul 2>&1
if not "%errorlevel%"=="0" goto download_failed
curl.exe -L --fail --retry 3 --ssl-no-revoke --progress-bar -o "%~2" "%~1"
if "%errorlevel%"=="0" if exist "%~2" exit /b 0
if exist "%~2" del /q "%~2" >nul 2>&1
:download_failed
echo [!] Yuklab bo'lmadi: %~1
echo     Internet aloqasini yoki proxy sozlamalarini tekshiring.
exit /b 1
