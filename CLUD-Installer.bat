@echo off
rem CLUD Installer for Windows. Fetches uv (and through it Python + Textual) into .tools\
rem next to this file the first time, so nothing gets installed system-wide.
setlocal
title CLUD Installer
set "HERE=%~dp0"
set "TOOLS=%HERE%.tools"
set "UV=%TOOLS%\uv\uv.exe"

if not exist "%UV%" (
    echo Fetching uv, one time only, about 20 MB...
    if not exist "%TOOLS%" mkdir "%TOOLS%"
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12';" ^
        "Invoke-WebRequest 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile '%TOOLS%\uv.zip';" ^
        "Expand-Archive -Force '%TOOLS%\uv.zip' '%TOOLS%\uv'; Remove-Item '%TOOLS%\uv.zip'"
    if not exist "%UV%" (
        echo Could not download uv. Check your internet connection.
        pause
        exit /b 1
    )
)

set "UV_CACHE_DIR=%TOOLS%\uv-cache"
set "UV_PYTHON_INSTALL_DIR=%TOOLS%\python"
set "UV_PYTHON_PREFERENCE=only-managed"
set "CLUD_UV=%UV%"

"%UV%" run --quiet --python 3.13 "%HERE%installer\clud_installer.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
