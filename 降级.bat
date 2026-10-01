@echo off
cd /d "%~dp0"
echo Starting downgrade with admin rights...
powershell -NoProfile -Command "Start-Process -FilePath 'python' -ArgumentList 'downgrade.py' -WorkingDirectory '%~dp0' -Verb RunAs"
