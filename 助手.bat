@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto nopython
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
python console.py
echo.
pause
goto :eof

:nopython
echo [Error] Python not found. Install Python 3.8+ 64-bit and check Add to PATH.
echo Or run: winget install -e --id Python.Python.3.10
pause
