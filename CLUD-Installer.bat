@echo off
rem Double-click starter for the CLUD Installer (the work happens in CLUD-Installer.ps1).
rem Opens it in Windows Terminal when available, else in a PowerShell window: the classic
rem console that runs .bat files often has a font without the characters the TUI draws.
setlocal
set "PSARGS=-NoProfile -ExecutionPolicy Bypass -File "%~dp0CLUD-Installer.ps1""

rem With arguments (--list, --target ...) or already inside Windows Terminal: run right here
if not "%~1"=="" goto inline
if defined WT_SESSION goto inline

where wt.exe >nul 2>&1
if not errorlevel 1 (
    start "" wt.exe -w new --title "CLUD Installer" powershell.exe %PSARGS% -Pause
    exit /b 0
)
start "" powershell.exe %PSARGS% -Pause
exit /b 0

:inline
powershell.exe %PSARGS% %*
exit /b %ERRORLEVEL%
