@echo off
cd /d "%~dp0"
set VISION_HOST=192.168.1.138
REM PATH adds: .venv\Scripts for the edge-tts CLI, and static_ffmpeg's bin for
REM ffmpeg.exe (MP3 -> PCM). opus.dll is handled by stackchan_mcp\_libs\ at
REM import time, so it is NOT on PATH here.
set PATH=%~dp0.venv\Scripts;%~dp0.venv\Lib\site-packages\static_ffmpeg\bin\win32;%PATH%
set LOG=%TEMP%\stackchan-mcp-daemon.log

REM Watchdog with escalation. The venv's python.exe is a uv trampoline that
REM intermittently fails with "No Python at ..." (exit code 103) even when the
REM base interpreter is present and healthy; on 2026-07-02 a login-spawned
REM watchdog got wedged failing that way on every loop for ~2h while the same
REM command worked from any fresh process. Escalation ladder:
REM   1. normal: launch via .venv\Scripts\python.exe
REM   2. after 3 consecutive exit-103s: bypass the trampoline and run the base
REM      interpreter (home= from pyvenv.cfg) against the venv's site-packages
REM   3. after 10 consecutive failed loops of any kind: respawn a fresh
REM      watchdog process via the VBS and exit this (possibly wedged) one
set TRAMPOLINE_FAILS=0
set LOOP_FAILS=0

:loop
REM If another gateway is already listening on 8767, this watchdog is a
REM duplicate (double launch, or a respawned chain after the original
REM recovered) -- exit quietly instead of crash-looping on the ownership lock.
netstat -ano | findstr "LISTENING" | findstr ":8767" >nul 2>&1
if not errorlevel 1 (
  echo [%date% %time%] another gateway is already listening on 8767 -- exiting this watchdog >> "%LOG%"
  exit /b 0
)
if %LOOP_FAILS% GEQ 10 goto respawn
if %TRAMPOLINE_FAILS% GEQ 3 goto fallback

echo [%date% %time%] starting stackchan-mcp daemon >> "%LOG%"
".venv\Scripts\python.exe" -m stackchan_mcp serve --transport streamable-http >> "%LOG%" 2>&1
set EC=%errorlevel%
if %EC% EQU 103 (set /a TRAMPOLINE_FAILS+=1) else (set TRAMPOLINE_FAILS=0)
goto exited

:fallback
REM Re-read the base interpreter dir from pyvenv.cfg on every attempt (so a
REM uv python upgrade is picked up). Must use site.addsitedir, NOT PYTHONPATH:
REM stackchan_mcp is an editable install wired up via a .pth file, and .pth
REM files are only processed for site dirs.
set BASE_HOME=
for /f "tokens=1,* delims== " %%a in ('findstr /b /c:"home" ".venv\pyvenv.cfg"') do set BASE_HOME=%%b
if not defined BASE_HOME (
  echo [%date% %time%] fallback failed: could not read home= from .venv\pyvenv.cfg >> "%LOG%"
  set EC=1
  set TRAMPOLINE_FAILS=0
  goto exited
)
echo [%date% %time%] starting stackchan-mcp daemon (base-interpreter fallback: %BASE_HOME%) >> "%LOG%"
"%BASE_HOME%\python.exe" -c "import site, sys, runpy; site.addsitedir(r'%~dp0.venv\Lib\site-packages'); sys.argv = ['stackchan_mcp', 'serve', '--transport', 'streamable-http']; runpy.run_module('stackchan_mcp', run_name='__main__', alter_sys=True)" >> "%LOG%" 2>&1
set EC=%errorlevel%
REM Whatever happened, give the trampoline another chance on the next loop.
set TRAMPOLINE_FAILS=0
goto exited

:exited
echo [%date% %time%] daemon exited (code %EC%), restarting in 5s >> "%LOG%"
if %EC% NEQ 0 (set /a LOOP_FAILS+=1) else (set LOOP_FAILS=0)
REM ping-based delay (timeout needs a real console; this runs hidden via wscript)
ping -n 6 127.0.0.1 >nul
goto loop

:respawn
REM Respawn via Task Scheduler first: the new process is spawned by the
REM Task Scheduler service, fully outside this (possibly wedged) process
REM tree. On 2026-07-06 a wedged watchdog's "start wscript" respawn failed
REM silently and everything stayed dead. Fall back to start if the task is
REM missing.
echo [%date% %time%] %LOOP_FAILS% consecutive failed launches -- respawning a fresh watchdog process and exiting this one >> "%LOG%"
schtasks /Run /TN "StackChan Gateway" >nul 2>&1
if errorlevel 1 (
  echo [%date% %time%] schtasks respawn failed -- falling back to start wscript >> "%LOG%"
  start "" wscript.exe "%~dp0stackchan-daemon-start.vbs"
)
exit /b 1
