@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

".venv\Scripts\python.exe" -c "import importlib.util as u,sys;sys.exit(1 if [m for m in ('anthropic','setuptools','yaml','pypdf') if u.find_spec(m) is None] else 0)" >nul 2>nul
if errorlevel 1 (
    echo [setup] venv missing or broken, running installer ...
    echo.
    call "%~dp0install.bat"
    goto :eof
)

".venv\Scripts\python.exe" bot.py
pause
