@echo off
REM Hourly reap of leaked Claude scheduled runs. Invoked by Windows Task Scheduler.
REM
REM Pass --dry-run to report without killing, or --list to see every verdict.

setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -m claude_run_reaper.run %*
set RC=%ERRORLEVEL%

if %RC% NEQ 0 echo claude_run_reaper exited with code %RC%
exit /b %RC%
