@echo off
setlocal
cd /d "%~dp0"

rem Vista/WMC build target: Python 3.6.x.
rem Keep behavior identical to the normal launcher; this just avoids needing
rem modern dependencies that do not install on Vista.
py -3.6 main.py --m3u-url https://fast-channels.sinclairstoryline.com/COMET/index.m3u8
if errorlevel 1 (
    echo.
    echo If the Python launcher is not installed on Vista, run this instead:
    echo python main.py --m3u-url https://fast-channels.sinclairstoryline.com/COMET/index.m3u8
    pause
)
