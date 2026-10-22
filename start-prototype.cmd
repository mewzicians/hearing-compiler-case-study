@echo off
cd /d "%~dp0"
python -m hearing_compiler serve --port 0 --open
if errorlevel 1 pause
