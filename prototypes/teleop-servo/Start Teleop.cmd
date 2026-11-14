@echo off
cd /d "%~dp0"
python pc\teleop.py
if errorlevel 1 pause
