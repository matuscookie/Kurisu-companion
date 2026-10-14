@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python kurisu_companion.py
echo.
pause
