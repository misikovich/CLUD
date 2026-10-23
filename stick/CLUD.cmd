@echo off
rem CLUD - portable Claude Code launcher. Runs entirely off this drive.
setlocal
set "USB=%~dp0"
set "USB=%USB:~0,-1%"
set "CLUD_HOME=%USB%"

set "PATH=%USB%\app;%USB%\mcp;%USB%\git\cmd;%USB%\git\usr\bin;%USB%\py;%USB%\py\Scripts;%PATH%"
set "CLAUDE_CONFIG_DIR=%USB%\config"
set "CLAUDE_CODE_GIT_BASH_PATH=%USB%\git\bin\bash.exe"
rem Self-updates would install onto the host PC; update the stick with the CLUD Installer instead
set "DISABLE_AUTOUPDATER=1"
rem Python on an SD card can be slow to cold-start; give the computer-use server time
set "MCP_TIMEOUT=120000"

title CLUD on %COMPUTERNAME%
rem Start in the host PC's user folder (CLUD works on the host), or in a folder dropped onto CLUD.exe
if "%~1"=="" (cd /d "%USERPROFILE%") else (cd /d "%~1")
rem Permission prompts are off: CLUD acts without asking (see config\CLAUDE.md for its own rules).
rem The computer-use server comes from mcp\windows-mcp.json, so CLUD.sh can use its own on Linux.
call claude --dangerously-skip-permissions --mcp-config "%USB%\mcp\windows-mcp.json"
endlocal
