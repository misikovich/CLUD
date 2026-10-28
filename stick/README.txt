CLUD - portable Claude Code
===========================

Windows: double-click CLUD.exe (opens Claude Code in the host PC's user
         folder) or drag a folder onto CLUD.exe to start Claude there.
         CLUD.exe just runs the hidden CLUD.cmd; edit that to change startup.
Linux:   double-click CLUD (CLUD.desktop) in the file manager. On Raspberry
         Pi OS choose "Execute" when it asks. Or in a terminal:
             sh /media/$USER/CLUD/CLUD.sh [folder]
         Works on 64-bit Linux: Raspberry Pi 3/4/5 with 64-bit Raspberry Pi
         OS, and x64 PCs (Ubuntu, Debian, Mint, Fedora, ...).
Agents:  with both Claude Code and omp on the stick, CLUD asks which to
         start (set CLUD_AGENT=claude or omp to skip the question). omp works
         with many model providers; its logins and settings are in
         config\omp. omp.yml holds the settings CLUD passes to it.
Careful: CLUD runs with --dangerously-skip-permissions (omp: --auto-approve).
         It won't ask before running commands or clicking things, on
         whatever PC it's plugged into.
Brief:   config\CLAUDE.md tells Claude it runs off this stick, that its job
         is the host PC, and to prefer computer use. Edit it to taste.
Login:   first run asks you to /login in the browser. The login is saved in
         \config on this stick (shared by Windows and Linux), so treat the
         stick like a key: if you lose it, sign out of the session at
         claude.ai settings.
Update:  run the CLUD Installer again and pick this drive. Your login,
         settings and work\ folder are kept.

Layout:  app\          Claude Code and/or omp for Windows (claude.exe, omp.exe)
         py\           Python 3.13 with Windows-MCP (computer use on Windows)
         linux-arm64\  Claude Code / omp + Python for 64-bit ARM Linux (Pi)
         linux-x64\    Claude Code / omp + Python for 64-bit x86 Linux
                       (Linux programs end in .exe so FAT32 lets them run)
         mcp\          computer-use starters; clud_desktop.py is the Linux one
         config\       your settings, history, memory, login
         work\         notes and scratch files

Nothing is installed on the host. Computer-use telemetry is off.
