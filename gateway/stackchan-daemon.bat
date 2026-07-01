@echo off
cd /d "%~dp0"
set VISION_HOST=192.168.1.138
REM PATH adds: .venv\Scripts for the edge-tts CLI, and static_ffmpeg's bin for
REM ffmpeg.exe (MP3 -> PCM). opus.dll is handled by stackchan_mcp\_libs\ at
REM import time, so it is NOT on PATH here.
set PATH=%~dp0.venv\Scripts;%~dp0.venv\Lib\site-packages\static_ffmpeg\bin\win32;%PATH%
REM Watchdog loop: if the daemon ever exits (e.g. the intermittent uv-managed
REM python path blip), wait briefly and relaunch. The device auto-reconnects
REM via mDNS once the gateway is listening again, so a transient crash now
REM self-heals in seconds instead of going dark until the next login.
:loop
echo [%date% %time%] starting stackchan-mcp daemon >> "%TEMP%\stackchan-mcp-daemon.log"
".venv\Scripts\python.exe" -m stackchan_mcp serve --transport streamable-http >> "%TEMP%\stackchan-mcp-daemon.log" 2>&1
echo [%date% %time%] daemon exited (code %errorlevel%), restarting in 5s >> "%TEMP%\stackchan-mcp-daemon.log"
REM ping-based delay (timeout needs a real console; this runs hidden via wscript)
ping -n 6 127.0.0.1 >nul
goto loop
