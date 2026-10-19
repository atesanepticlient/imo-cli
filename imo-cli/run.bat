@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title imo-cli Run
cd /d "%~dp0"

REM -- Find project dir: works if this bat is inside imo-cli\ or one folder above it --
if exist "pyproject.toml" (
  REM already in imo-cli\
) else if exist "imo-cli\pyproject.toml" (
  cd /d "%~dp0imo-cli"
) else (
  echo [ERROR] Could not find pyproject.toml.
  echo Put run.bat inside the imo-cli folder or next to it.
  pause
  exit /b 1
)

REM -- Always run this folder's module. A global `imo.exe` can be a stale,
REM -- unprotected version from an earlier installation and must never win.
set IMO=python -m imocli
python -c "import imocli" >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing this project first via pip install -e . ...
  python -m pip install -e .
  if errorlevel 1 (
    echo [ERROR] Install failed. Run setup.bat and check errors.
    pause
    exit /b 1
  )
)

REM -- Direct passthrough: run.bat --device XYZ  ->  imo run --device XYZ
REM --                run.bat devices       ->  imo devices
if not "%~1"=="" (
  if /i "%~1"=="run" (
    call %IMO% %*
    goto :end
  )
  if /i "%~1"=="devices" goto :passthrough
  if /i "%~1"=="doctor" goto :passthrough
  if /i "%~1"=="init" goto :passthrough
  if /i "%~1"=="disconnect" goto :passthrough
  if /i "%~1"=="mirror" goto :passthrough
  if /i "%~1"=="report" goto :passthrough
  if /i "%~1"=="dump" goto :passthrough
  if /i "%~1"=="open" goto :passthrough
  REM default: treat args as "imo run" args
  call %IMO% run %*
  goto :end
)

:menu
echo.
echo ============================================
echo  imo-cli - easy run (workflow unchanged)
echo  Folder: %CD%
echo ============================================
echo  1. Run on ALL devices (live dashboard)
echo  2. Run on ONE device
echo  3. Run on ALL + save log file
echo  4. List devices
echo  5. Diagnose (doctor)
echo  6. Install agent (init)
echo  7. Stop one device (disconnect)
echo  0. Exit
echo.
set /p CHOICE="Pick [0-7]: "

if "%CHOICE%"=="1" (
  call %IMO% run
  goto :end
)
if "%CHOICE%"=="2" (
  echo.
  call %IMO% devices
  echo.
  set /p SERIAL="Enter device serial: "
  if "!SERIAL!"=="" (
    echo [ERROR] No serial entered.
    pause
    goto :menu
  )
  call %IMO% run --device !SERIAL!
  goto :end
)
if "%CHOICE%"=="3" (
  if not exist "logs" mkdir "logs"
  for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set STAMP=%%t
  set LOGFILE=logs\run-!STAMP!.ndjson
  echo Saving log to: !LOGFILE!
  call %IMO% run --json "!LOGFILE!"
  echo.
  echo Log saved: !LOGFILE!
  echo View report: %IMO% report "!LOGFILE!"
  goto :end
)
if "%CHOICE%"=="4" (
  call %IMO% devices
  pause
  goto :menu
)
if "%CHOICE%"=="5" (
  call %IMO% doctor
  pause
  goto :menu
)
if "%CHOICE%"=="6" (
  call %IMO% init
  pause
  goto :menu
)
if "%CHOICE%"=="7" (
  echo.
  call %IMO% devices
  echo.
  set /p SERIAL="Enter device serial to stop: "
  if "!SERIAL!"=="" (
    echo [ERROR] No serial entered.
    pause
    goto :menu
  )
  call %IMO% disconnect !SERIAL!
  pause
  goto :menu
)
if "%CHOICE%"=="0" exit /b 0

echo Invalid choice.
pause
goto :menu

:passthrough
call %IMO% %*
goto :end

:end
echo.
echo Done. Press any key to close (Ctrl+C already stopped the run if it was running).
pause >nul
