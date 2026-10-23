# CLUD

### Tired of your friends, grandma or other low-tier computer users always getting in trouble with their Windonws PC? 
### **CLUD (Claude + Chud)** - is the solution!
###
**Portable Claude Code on a USB stick.** Build the stick once, from Windows, macOS or Linux.
Then plug it into any Windows PC, Linux PC or Raspberry Pi, double-click `CLUD.exe` (Windows)
or `CLUD` (Linux), and Claude works on that computer: through the shell, and through the
mouse, keyboard and screen. Nothing is installed on the host, and your login, settings and
memory travel with the stick, across both systems.

*(CLUD = Claude + chud.)*

![How CLUD works: create once, carry it, run anywhere](docs/how-it-works.svg)

## Quick start

1. **Get this repo** on any computer with internet access (`git clone`, or download the ZIP).
2. **Start the CLUD Installer:**
   - **Windows:** double-click `CLUD-Installer.bat`. It opens the installer in Windows Terminal,
     or in a PowerShell window if Windows Terminal isn't installed. You can also run
     `CLUD-Installer.ps1` directly.
   - **macOS / Linux:** run `./CLUD-Installer.sh` in a terminal
3. **Pick your USB drive, tick the platforms you want and press Install.** With all three
   (Windows, Linux ARM64 for the Raspberry Pi, Linux x64) about 1.6 GB gets downloaded and
   written; Windows alone is about 900 MB, each Linux build about 350 MB.
4. **Plug the stick in and start CLUD.** The first time, follow the prompts to sign in. The
   login is saved on the stick and works on Windows and Linux alike.
   - **Windows:** double-click **`CLUD.exe`**.
   - **Linux / Raspberry Pi:** open the drive in the file manager and double-click **`CLUD`**
     (`CLUD.desktop`). Raspberry Pi OS asks what to do with it: choose **Execute**. Or, in a
     terminal: `sh /media/$USER/CLUD/CLUD.sh`.

![The CLUD Installer drive picker](docs/installer.png)

The first time it runs, the installer fetches [uv](https://github.com/astral-sh/uv), and
through it Python and [Textual](https://github.com/Textualize/textual), into `.tools/` next to
the scripts. Nothing is installed system-wide, and later runs start in a second or two.

## What's on the stick

| Folder | What | Size |
|---|---|---|
| `app\` | Claude Code (native `claude.exe`) | ~257 MB |
| `git\` | Git for Windows: `git` plus the `bash.exe` Claude Code needs on Windows | ~400 MB |
| `py\` | Standalone Python 3.13 with [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) for computer use | ~195 MB |
| `config\` | Your Claude Code settings, history, memory and login | small |
| `work\` | Claude's notes and scratch files | small |
| `linux-arm64/` | Claude Code + Python with CLUD's computer-use server, for 64-bit ARM Linux (Raspberry Pi) | ~340 MB |
| `linux-x64/` | The same for 64-bit x86 Linux | ~350 MB |
| `CLUD.exe` | Windows launcher with the CLUD icon (runs the hidden `CLUD.cmd`) | 8 KB |
| `CLUD.desktop`, `CLUD.sh` | Linux launcher: double-click the first, it runs the second in a terminal | small |

`config\CLAUDE.md` briefs Claude on its situation: it runs from a removable drive, its job is
the computer it's plugged into, it should prefer computer use for GUI work, and it should
keep the host clean. Edit it to change how CLUD behaves.

## Linux and Raspberry Pi

CLUD runs on 64-bit Linux with glibc: Raspberry Pi 3, 4, 5 and 400/500 with **64-bit**
Raspberry Pi OS, and x64 PCs with Ubuntu, Debian, Mint, Fedora and the like. Claude Code has
no 32-bit build, so on a Pi running 32-bit Raspberry Pi OS, CLUD tells you to reinstall with
the 64-bit image. The Raspberry Pi 5's 16K memory pages are fine.

Computer use on Linux comes from `mcp/clud_desktop.py`, a small MCP server that runs on the
stick's own Python, with no root access or extra packages needed on the host:

| Desktop | Screenshots | Mouse and keyboard |
|---|---|---|
| Raspberry Pi OS (labwc, the default) and other wlroots desktops: wayfire, sway, Hyprland | yes | yes |
| Any X11 session (Raspberry Pi OS set to X11, Xfce, MATE, Cinnamon, GNOME/KDE on Xorg) | yes | yes |
| GNOME or KDE on Wayland | only via `gnome-screenshot` / `spectacle` if installed | no, Claude uses the shell |

A few Linux details:

- The Linux programs on the stick end in `.exe`. That's on purpose: Linux mounts FAT32 sticks
  so that only files ending in `.exe`, `.com` or `.bat` may run, and they're ordinary Linux
  programs otherwise.
- If the stick is mounted so nothing on it may run (`noexec`), `CLUD.sh` copies the Linux
  build to `~/.cache/clud/` once and runs it from there. That's the only case where CLUD
  writes to the host.
- Claude works in your home folder. Pass another folder as an argument to `CLUD.sh` to start
  there.

## ⚠️ It runs without permission prompts

`CLUD.cmd` starts Claude Code with `--dangerously-skip-permissions`. On whatever PC the stick
is plugged into, CLUD runs commands, edits files and clicks around **without asking first**.
The only brake is the rule in `CLAUDE.md` to stop and ask before anything destructive. Only use
CLUD on machines you're allowed to change. If you want prompts back, remove the flag from
`stick/CLUD.cmd`.

Also treat the stick like a key: it holds your Claude login. If you lose it, sign that
session out from your claude.ai account settings.

## Updating

Run the CLUD Installer again and pick the same drive. The new version is built next to the
old one and only swapped in once it's complete, so a failed or interrupted update leaves the
previous CLUD working. Your login, settings, history and `work\` folder are kept. If you
edited `CLAUDE.md`, your version is saved as `CLAUDE.md.bak`.

Claude Code's own self-updater is turned off on the stick, because it would install updates
onto the host PC instead of the stick.

## Installer options

The TUI is the default. For scripting:

```
CLUD-Installer.bat --list                 # print drives and exit
CLUD-Installer.bat --list --all           # include fixed and system drives
CLUD-Installer.bat --target E:\ --yes     # install without the TUI
CLUD-Installer.bat --target E:\ --yes --no-rename
CLUD-Installer.bat --target E:\ --yes --platforms windows,linux-arm64
```

`--platforms` takes `windows`, `linux-arm64`, `linux-x64`, or `linux` for both Linux builds.
The default is all of them. Platforms you leave out are removed from the stick on update.

On macOS/Linux, use the same options with `./CLUD-Installer.sh`. `--target` also accepts a
plain folder, which is handy for testing or for building a ZIP.

In the classic Windows console window, whose fonts lack some of the characters the TUI draws,
the installer switches to a simpler look on its own. Set `CLUD_SIMPLE_GLYPHS=1` or `0` to
force it either way.

## Requirements

- **To run CLUD:** a 64-bit Windows 10 or 11 PC, or 64-bit Linux (x64 or ARM64, glibc),
  including Raspberry Pi OS 64-bit.
- **To build the stick:** Windows, macOS or 64-bit Linux (a Raspberry Pi works too) with
  internet access.
- **The drive:** 1 GB free or more. FAT32, exFAT and NTFS all work.

Renaming the drive to `CLUD` works on Windows and macOS. On Linux, rename it yourself, for
example with `fatlabel` or `exfatlabel`. Computer use can't act on a locked screen or on
Windows admin (UAC) prompts, so Claude asks you to click those.

> **Status:** the installer and the stick are tested on Windows. Linux is tested on Raspberry
> Pi OS (trixie, 64-bit, 16K-page kernel like the Pi 5, in QEMU: `tests/pi-vm.sh`), including
> Claude using the desktop. On x64 Linux, Claude Code and the X11 computer-use backend are
> tested (the latter under Xvfb), not yet a full desktop session. `CLUD-Installer.sh` is
> tested on Linux (x64 and on the Pi), not yet on macOS.

## Repo layout

```
CLUD-Installer.ps1 / .sh   start the installer (fetch uv into .tools/, then run it)
CLUD-Installer.bat         double-click starter: opens the .ps1 in Windows Terminal
installer/
  clud_installer.py        the TUI and CLI (Textual)
  clud_core.py             finding drives, downloading, installing; no UI
stick/                     files copied onto the stick as-is
  CLUD.exe, CLUD.cmd       Windows launcher
  CLUD.desktop, CLUD.sh    Linux launcher
  config/CLAUDE.md         Claude's brief
  mcp/windows-mcp.*        computer-use server for Windows (Windows-MCP)
  mcp/clud_desktop.py      computer-use server for Linux (Wayland/wlroots and X11)
  autorun.inf, clud.ico    drive name and icon
launcher/                  dev tools (Windows): CLUD.exe source, icon generator
tests/                     test VMs: Linux installer (Mac), Windows on Arm (Mac),
                           Raspberry Pi OS (Linux host); mcp_probe.py drives clud_desktop.py
docs/                      README images
```

`stick/CLUD.exe` is committed so the stick can be built from any OS. To rebuild it after
changing `launcher/CLUD.cs` or the icon, run `launcher/make-icon.ps1` and then
`launcher/build-exe.ps1` on Windows.

## Third-party software

The installer downloads these from their official sources at install time. None of them are
included in this repo.

- [Claude Code](https://www.npmjs.com/package/@anthropic-ai/claude-code) by Anthropic, under
  Anthropic's terms. Using it needs a Claude account.
- [Git for Windows](https://gitforwindows.org/), GPLv2
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone), Python
  Software Foundation License and others
- [Windows-MCP](https://github.com/CursorTouch/Windows-MCP), MIT. Its anonymous telemetry is
  turned off by the launcher.
- [Pillow](https://python-pillow.org/) (MIT-CMU) and [python-xlib](https://github.com/python-xlib/python-xlib)
  (LGPL-2.1+), used by the Linux computer-use server
- [uv](https://github.com/astral-sh/uv) and [Textual](https://github.com/Textualize/textual),
  used by the installer, MIT / Apache-2.0

CLUD is an unofficial community project. It isn't affiliated with or endorsed by Anthropic.
