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
3. **Pick your USB drive, tick the platforms and agents you want and press Install.** With all
   three platforms (Windows, Linux ARM64 for the Raspberry Pi, Linux x64) about 1.1 GB gets
   written to the stick; Windows alone is about 430 MB, each Linux build about 340 MB. Adding
   [omp](#omp-more-model-providers) takes about 250 MB more per platform, plus 60–110 MB it
   unpacks on its first start.
4. **Plug the stick in and start CLUD.** The first time, follow the prompts to sign in. The
   login is saved on the stick and works on Windows and Linux alike. With both Claude Code
   and omp on the stick, CLUD first asks which one to start.
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
| `app\` | Claude Code (native `claude.exe`) and/or omp (`omp.exe`) | ~285 MB each |
| `py\` | Standalone Python 3.13 with [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) for computer use | ~140 MB |
| `config\` | Your Claude Code settings, history, memory and login; omp's in `config\omp\` | small |
| `work\` | Claude's notes and scratch files | small |
| `linux-arm64/` | Claude Code and/or omp + Python with CLUD's computer-use server, for 64-bit ARM Linux (Raspberry Pi) | ~340 MB |
| `linux-x64/` | The same for 64-bit x86 Linux | ~350 MB |
| `CLUD.exe` | Windows launcher with the CLUD icon (runs the hidden `CLUD.cmd`) | 8 KB |
| `CLUD.desktop`, `CLUD.sh` | Linux launcher: double-click the first, it runs the second in a terminal | small |

`config\CLAUDE.md` briefs Claude on its situation: it runs from a removable drive, its job is
the computer it's plugged into, it should prefer computer use for GUI work, and it should
keep the host clean. Edit it to change how CLUD behaves.

## omp: more model providers

Tick **omp** in the installer to put [omp (oh-my-pi)](https://github.com/can1357/oh-my-pi) on
the stick next to Claude Code, or instead of it. omp is a coding agent that works with many
model providers: Claude, ChatGPT/Codex, GitHub Copilot and Gemini sign-ins, API keys for
dozens more (OpenRouter, Z.AI, Kimi, ...), and local models (Ollama, LM Studio, llama.cpp).
On first start it walks you through signing in.

It runs the same way Claude Code does on the stick: same brief (`config/CLAUDE.md`), same
computer-use server, started in the host's home folder, without permission prompts
(`--auto-approve`). Its logins, settings and sessions are kept in `config/omp/`, shared by
Windows and Linux.

- To skip the question when both are on the stick, set `CLUD_AGENT=claude` or `CLUD_AGENT=omp`.
- `omp.yml` on the stick holds the settings CLUD passes to omp (no update checks, a longer
  start-up time for the computer-use server). Your own settings go to `config/omp/config.yml`.
- CLUD's computer-use server is written to `config/omp/.mcp.json` at every start. MCP servers
  you add yourself go to `config/omp/mcp.json` and are left alone.
- omp unpacks a native library (60–110 MB) into the stick's `app\` or `linux-*/` folder on
  first start, and again after an update, so that first start takes a little longer.
- omp writes logs to `.omp` in the host's user folder. CLUD deletes that folder when omp exits,
  unless the host already had one.

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

`CLUD.cmd` starts Claude Code with `--dangerously-skip-permissions`, and omp with
`--auto-approve`. On whatever PC the stick is plugged into, CLUD runs commands, edits files
and clicks around **without asking first**. The only brake is the rule in `CLAUDE.md` to stop
and ask before anything destructive. Only use CLUD on machines you're allowed to change. If
you want prompts back, remove the flags from `stick/CLUD.cmd` and `stick/CLUD.sh`.

Also treat the stick like a key: it holds your Claude login, and omp's logins and API keys.
If you lose it, sign those sessions out from your account settings and revoke the keys.

## Updating

Run the CLUD Installer again and pick the same drive. The new version is built next to the
old one and only swapped in once it's complete, so a failed or interrupted update leaves the
previous CLUD working. Your login, settings, history and `work\` folder are kept. If you
edited `CLAUDE.md`, your version is saved as `CLAUDE.md.bak`.

Claude Code's own self-updater and omp's update check are turned off on the stick, because
they would install updates onto the host PC instead of the stick.

## Installer options

The TUI is the default. For scripting:

```
CLUD-Installer.bat --list                 # print drives and exit
CLUD-Installer.bat --list --all           # include fixed and system drives
CLUD-Installer.bat --target E:\ --yes     # install without the TUI
CLUD-Installer.bat --target E:\ --yes --no-rename
CLUD-Installer.bat --target E:\ --yes --platforms windows,linux-arm64
CLUD-Installer.bat --target E:\ --yes --agents claude,omp
```

`--platforms` takes `windows`, `linux-arm64`, `linux-x64`, or `linux` for both Linux builds.
The default is all of them. `--agents` takes `claude`, `omp` or both; the default is
`claude`. Platforms and agents you leave out are removed from the stick on update (their
logins and settings in `config\` stay).

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
> tested on Linux (x64 and on the Pi), not yet on macOS. omp is tested on x64 Linux (FAT32
> and noexec sticks) and on x64 Windows 11 (in QEMU), not yet on a Raspberry Pi.

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
  omp.yml                  settings the launchers pass to omp
  config/CLAUDE.md         the agent's brief (Claude Code and omp)
  mcp/windows-mcp*         computer-use server for Windows (Windows-MCP); -omp.json is omp's
  mcp/clud_desktop.py      computer-use server for Linux (Wayland/wlroots and X11)
  autorun.inf, clud.ico    drive name and icon
launcher/                  dev tools (Windows): CLUD.exe source, icon generator
tests/                     test VMs: Linux installer (Mac), Windows 11 (Arm on a Mac, x64
                           on a Linux host), Raspberry Pi OS (Linux host); mcp_probe.py
                           drives clud_desktop.py
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
- [omp (oh-my-pi)](https://github.com/can1357/oh-my-pi), MIT, if you tick it. Using it needs
  an account or API key with a model provider, or a local model.
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone), Python
  Software Foundation License and others
- [Windows-MCP](https://github.com/CursorTouch/Windows-MCP), MIT. Its anonymous telemetry is
  turned off by the launcher.
- [Pillow](https://python-pillow.org/) (MIT-CMU) and [python-xlib](https://github.com/python-xlib/python-xlib)
  (LGPL-2.1+), used by the Linux computer-use server
- [uv](https://github.com/astral-sh/uv) and [Textual](https://github.com/Textualize/textual),
  used by the installer, MIT / Apache-2.0

CLUD is an unofficial community project. It isn't affiliated with or endorsed by Anthropic.
