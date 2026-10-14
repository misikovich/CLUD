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
Update:  Claude Code updates itself, and the launcher points npm at this
         stick so updates land here. To force it, rebuild with
         build-clud.ps1 -Force on your main PC.

Layout:  node\    portable Node.js
         git\     PortableGit (Claude Code needs its bash.exe on Windows)
         app\     Claude Code itself
         py\      standalone Python with Windows-MCP (computer use:
                  screenshots, mouse, keyboard). Telemetry is off.
         mcp\     starter script for Windows-MCP
         config\  your settings, history, memory, login
         work\    your projects

Needs a 64-bit Windows 10/11 host. Nothing is installed on the host.
