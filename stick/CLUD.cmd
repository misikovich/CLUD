@echo off
rem CLUD - portable Claude Code / omp launcher. Runs entirely off this drive.
rem With both agents on the stick it asks which to start; set CLUD_AGENT=claude or omp to skip that.
setlocal
set "USB=%~dp0"
set "USB=%USB:~0,-1%"
set "CLUD_HOME=%USB%"

set "PATH=%USB%\app;%USB%\mcp;%USB%\py;%PATH%"
set "CLAUDE_CONFIG_DIR=%USB%\config"
rem No Git Bash on the stick: Claude Code runs commands with its PowerShell tool (or with the
rem host's Git Bash, if Git for Windows is installed there); omp has a bash of its own
rem Self-updates would install onto the host PC; update the stick with the CLUD Installer instead
set "DISABLE_AUTOUPDATER=1"
rem Python on an SD card can be slow to cold-start; give the computer-use server time
set "MCP_TIMEOUT=120000"

rem Which agent: the one on the stick, or ask when there are both
set "AGENT=%CLUD_AGENT%"
if not defined AGENT if exist "%USB%\app\claude.exe" if exist "%USB%\app\omp.exe" call :ask
if not defined AGENT if exist "%USB%\app\claude.exe" set "AGENT=claude"
if not defined AGENT if exist "%USB%\app\omp.exe" set "AGENT=omp"
if not defined AGENT set "AGENT=claude"
if /i not "%AGENT%"=="claude" if /i not "%AGENT%"=="omp" (
    echo CLUD: CLUD_AGENT is "%AGENT%"; use claude or omp
    pause
    exit /b 1
)
if not exist "%USB%\app\%AGENT%.exe" (
    echo CLUD: this stick has no %AGENT%. Run the CLUD Installer again and tick it.
    pause
    exit /b 1
)

title CLUD on %COMPUTERNAME%
rem Start in the host PC's user folder (CLUD works on the host), or in a folder dropped onto CLUD.exe
if "%~1"=="" (cd /d "%USERPROFILE%") else (cd /d "%~1")
rem Permission prompts are off: CLUD acts without asking (see config\CLAUDE.md for its own rules).
rem The computer-use server comes from mcp\windows-mcp.json, so CLUD.sh can use its own on Linux.
if /i "%AGENT%"=="omp" goto omp
call claude --dangerously-skip-permissions --mcp-config "%USB%\mcp\windows-mcp.json"
exit /b %errorlevel%

:omp
rem omp keeps logins, settings and sessions in config\omp, and its native library in app\,
rem unpacked there on first start. It reads the computer-use server from .mcp.json there
rem (servers you add go to mcp.json), CLUD's settings from omp.yml, the brief from CLAUDE.md.
set "PI_CODING_AGENT_DIR=%USB%\config\omp"
set "PI_NATIVES_DIR=%USB%\app\omp-natives"
if not exist "%PI_CODING_AGENT_DIR%" mkdir "%PI_CODING_AGENT_DIR%"
rem Python is started directly, not through windows-mcp.cmd: omp would stop only cmd.exe on exit
copy /y "%USB%\mcp\windows-mcp-omp.json" "%PI_CODING_AGENT_DIR%\.mcp.json" >nul
rem omp still writes logs to %USERPROFILE%\.omp; remove it afterwards unless the PC had it already
set "HOST_OMP="
if not exist "%USERPROFILE%\.omp" set "HOST_OMP=%USERPROFILE%\.omp"
call omp --allow-home --auto-approve --config "%USB%\omp.yml" --append-system-prompt "%USB%\config\CLAUDE.md"
set "STATUS=%errorlevel%"
if defined HOST_OMP rmdir /s /q "%HOST_OMP%" 2>nul
exit /b %STATUS%

:ask
echo.
echo   CLUD - which agent?
echo.
echo     1  Claude Code
echo     2  omp  (Claude, GPT, Gemini, local models, ...)
echo.
choice /c 12 /n /m "  Press 1 or 2: "
if errorlevel 2 (set "AGENT=omp") else (set "AGENT=claude")
exit /b 0
