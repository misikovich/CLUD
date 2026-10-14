@echo off
rem CLUD - portable Claude Code launcher. Runs entirely off this drive.
setlocal
set "USB=%~dp0"
set "USB=%USB:~0,-1%"
set "CLUD_HOME=%USB%"

set "PATH=%USB%\app;%USB%\node;%USB%\mcp;%USB%\git\cmd;%USB%\git\usr\bin;%PATH%"
set "CLAUDE_CONFIG_DIR=%USB%\config"
set "CLAUDE_CODE_GIT_BASH_PATH=%USB%\git\bin\bash.exe"
rem Python on an SD card can be slow to cold-start; give the computer-use server time
set "MCP_TIMEOUT=120000"
rem Keep npm (and Claude Code self-updates) on the stick, not the host
set "npm_config_prefix=%USB%\app"
set "npm_config_cache=%USB%\.npm-cache"

title CLUD on %COMPUTERNAME%
rem Start in the host PC's user folder (CLUD works on the host), or in a folder dropped onto this file
if "%~1"=="" (cd /d "%USERPROFILE%") else (cd /d "%~1")
rem Permission prompts are off: CLUD acts without asking (see config\CLAUDE.md for its own rules)
call claude --dangerously-skip-permissions
endlocal
