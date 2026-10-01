@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

rem Pick a Python that wcferry can actually install on.
rem Its dependency pynng is a C extension and often has no wheel for 3.13+.
set "PY="
for %%V in (3.11 3.10 3.12 3.9) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

if not defined PY goto nopython

%PY% installer.py
pause
goto :eof

:nopython
echo [Error] No suitable Python found.
echo         Need 64-bit Python 3.8+ (3.11 recommended), on PATH or via the py launcher.
echo Install one with:
echo     winget install -e --id Python.Python.3.11
pause
