@echo off
cd /d "%~dp0"
python main.py --config config.json --map-guide-wmc
pause
