@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title imo-cli Setup
cd /d "%~dp0"

REM -- Find project dir: works if this bat is inside imo-cli\ or one folder above it --
if exist "pyproject.toml" (
  REM already in imo-cli\
) else if exist "imo-cli\pyproject.toml" (
  cd /d "%~dp0imo-cli"
) else (
  echo [ERROR] Could not find pyproject.toml.
  echo Put setup.bat inside the imo-cli folder or next to it.
  pause
  exit /b 1
)

echo ============================================
echo  imo-cli setup - no workflow code is changed
echo  Folder: %CD%
echo ============================================
echo.

REM -- 1. Python check (need 3.10+) --
where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo        Tick "Add python.exe to PATH" during install, then re-run setup.bat.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
  set MAJOR=%%a
  set MINOR=%%b
)
echo Python found: !PYVER!
if !MAJOR! LSS 3 (
  echo [ERROR] Python 3.10+ required.
  pause
  exit /b 1
)
if !MAJOR! EQU 3 if !MINOR! LSS 10 (
  echo [ERROR] Python 3.10+ required, found !PYVER!.
  pause
  exit /b 1
)

REM -- 2. Install package (creates the "imo" command) --
echo.
echo [1/4] Installing imo-cli ...
python -m pip install --upgrade pip
if errorlevel 1 (
  echo [WARN] pip upgrade failed, continuing...
)
python -m pip install -e .
if errorlevel 1 (
  echo [ERROR] pip install failed. Check errors above.
  pause
  exit /b 1
)
echo [OK] Installed.

REM -- Make "imo" usable even if Python Scripts is not on PATH (pip --user install) --
for /d %%D in ("%APPDATA%\Python\*") do (
  if exist "%%D\Scripts\imo.exe" set "PATH=%%D\Scripts;!PATH!"
)
if exist "%APPDATA%\Python\Python314\Scripts\imo.exe" set "PATH=%APPDATA%\Python\Python314\Scripts;!PATH!"

REM -- 3. adb check --
echo.
echo [2/4] Checking adb ...
where adb >nul 2>nul
if errorlevel 1 (
  if defined ADB_PATH (
    echo ADB_PATH=%ADB_PATH%
  ) else (
    echo [WARN] adb not on PATH.
    echo        Install Android platform-tools or set ADB_PATH, e.g.:
    echo        setx ADB_PATH "C:\platform-tools\adb.exe"
    echo        Continuing anyway - "imo doctor" will confirm.
  )
) else (
  adb version
  echo [OK] adb found.
)

REM -- 4. Protected operational commands --
echo.
echo [3/4] Authentication is required before operational checks.
echo [NEXT] Start or configure the auth server, then run: python -m imocli login
echo [SKIP] doctor is intentionally not run before login.

REM -- 5. Protected agent initialization --
echo.
echo [4/4] Agent initialization is also protected.
echo [NEXT] After login, run: python -m imocli init

echo.
echo ============================================
echo  Setup finished. Next: python -m imocli login
echo ============================================
pause
