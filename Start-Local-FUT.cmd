@echo off
if "%~1"=="" goto autodetect
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Play-Local-FUT.ps1" -GameDirectory "%~1"
goto done
:autodetect
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Play-Local-FUT.ps1"
:done
if errorlevel 1 echo Launch stopped with an error. Read the message above.
pause
