@echo off
rem ============================================================================
rem  Optional components: voice-to-text (faster-whisper + local model) and the
rem  local web-search backend (SearXNG).
rem
rem  They are NOT installed by install.bat on purpose: their deps must stay
rem  COMMENTED OUT in requirements.txt (a real line there makes "installed but
rem  still broken" loops), and their model / own .venv must never be copied
rem  across machines.
rem
rem  KEEP THIS FILE A THIN WRAPPER. The whole flow lives in console.py
rem  optional_menu(); do not re-implement any of it here or the two copies will
rem  drift. ASCII ONLY: a UTF-8 .bat prints mojibake on a GBK console
rem  (codepage 936), and selftest_portable.py checks this.
rem ============================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python console.py optional
echo.
pause
goto :eof

:nopython
echo [Error] Python not found. This package needs 64-bit Python 3.8+ (3.11 recommended).
echo.
echo   1^) Install it - run this in PowerShell:
echo        winget install -e --id Python.Python.3.11
echo      (or get it from python.org and tick "Add to PATH")
echo   2^) Then double-click this file again.
echo.
pause
goto :eof
