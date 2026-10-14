CLUD - portable Claude Code
===========================

Run:     double-click CLUD.exe  (opens Claude Code in the host PC's user
         folder) or drag a folder onto CLUD.exe to start Claude there.
         CLUD.exe just runs the hidden CLUD.cmd; edit that to change startup.
Careful: CLUD runs with --dangerously-skip-permissions. It won't ask before
         running commands or clicking things, on whatever PC it's plugged into.
Brief:   config\CLAUDE.md tells Claude it runs off this stick, that its job
         is the host PC, and to prefer computer use. Edit it to taste.
Login:   first run asks you to /login in the browser. The login is saved in
         \config on this stick, so treat the stick like a key: if you lose
         it, sign out of the session at claude.ai settings.
Update:  run the CLUD Installer again and pick this drive. Your login,
         settings and work\ folder are kept.

Layout:  app\     Claude Code (claude.exe)
         git\     Git for Windows (Claude Code needs its bash.exe on Windows)
         py\      standalone Python 3.13 with Windows-MCP (computer use:
                  screenshots, mouse, keyboard). Telemetry is off.
         mcp\     starter script for Windows-MCP
         config\  your settings, history, memory, login
         work\    notes and scratch files

Needs a 64-bit Windows 10/11 host. Nothing is installed on the host.
