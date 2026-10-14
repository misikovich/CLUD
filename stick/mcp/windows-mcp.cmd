@echo off
rem Starts the Windows-MCP computer-use server from the stick's bundled Python.
rem Uses paths relative to this file, so it works whatever drive letter the stick gets.
setlocal
set "ROOT=%~dp0.."
set "ANONYMIZED_TELEMETRY=false"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%ROOT%\py\python.exe" -m windows_mcp serve %*
