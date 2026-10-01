@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] venv not found, running installer first ...
    echo.
    call "%~dp0install.bat"
    goto :eof
)

".venv\Scripts\python.exe" setup_llm.py
pause
