@echo off
rem ============================================================================
rem  One-click deploy for a FRESH machine: wechat version -> hook -> deps ->
rem  start -> model key. It is exactly the assistant console's [9], minus the
rem  menu step.
rem
rem  KEEP THIS FILE A THIN WRAPPER. The whole flow lives in console.py
rem  first_run(); do not re-implement any of it here, or the two copies will
rem  drift (that is how "works on my machine, silently broken in the package"
rem  happens). ASCII ONLY: every other .bat in this repo is pure ASCII because a
rem  UTF-8 .bat prints mojibake on a GBK console (codepage 936) - and
rem  selftest_portable.py now checks this.
rem ============================================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

rem Must be 64-bit Python: a 32-bit one fails later in ways nobody can read.
python -c "import struct,sys;sys.exit(0 if struct.calcsize('P')==8 else 1)" >nul 2>nul
if errorlevel 1 goto nopython32

chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python console.py first
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

:nopython32
echo [Error] A 32-bit Python was found. This package needs a 64-bit one.
echo.
echo   Run this in PowerShell, then double-click this file again:
echo        winget install -e --id Python.Python.3.11
echo.
pause
goto :eof
