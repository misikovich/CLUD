# CLUD

**Portable Claude Code on a USB stick.** Build the stick once, from Windows, macOS or Linux.
Then plug it into any Windows PC, double-click `CLUD.exe`, and Claude works on that PC:
through the shell, and through the mouse, keyboard and screen. Nothing is installed on the
host, and your login, settings and memory travel with the stick.

*(CLUD = Claude + chud.)*

## Quick start

1. **Get this repo** on any computer with internet access (`git clone`, or download the ZIP).
2. **Start the CLUD Installer:**
   - **Windows:** double-click `CLUD-Installer.bat`
   - **macOS / Linux:** run `./CLUD-Installer.sh` in a terminal
3. **Pick your USB drive and press Install.** About 900 MB gets downloaded and written.
4. **On any Windows PC:** plug the stick in and double-click **`CLUD.exe`**. The first time,
   type `/login` to sign in. The login is saved on the stick.

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
| `CLUD.exe` | Launcher with the CLUD icon (runs the hidden `CLUD.cmd`) | 8 KB |

`config\CLAUDE.md` briefs Claude on its situation: it runs from a removable drive, its job is
the PC it's plugged into, it should prefer computer use for GUI work, and it should keep the
host clean. Edit it to change how CLUD behaves.

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
```

On macOS/Linux, use the same options with `./CLUD-Installer.sh`. `--target` also accepts a
plain folder, which is handy for testing or for building a ZIP.

## Requirements

- **To run CLUD:** a 64-bit Windows 10 or 11 PC.
- **To build the stick:** Windows, macOS or Linux with internet access.
- **The drive:** 1 GB free or more. FAT32, exFAT and NTFS all work.

Renaming the drive to `CLUD` works on Windows and macOS. On Linux, rename it yourself, for
example with `fatlabel` or `exfatlabel`. Computer use can't act on a locked screen or on
Windows admin (UAC) prompts, so Claude asks you to click those.

> **Status:** the installer and the stick are tested on Windows. `CLUD-Installer.sh` hasn't
> been tested on macOS or Linux yet.

## Repo layout

```
CLUD-Installer.bat / .sh   start the installer (fetch uv into .tools/, then run it)
installer/
  clud_installer.py        the TUI and CLI (Textual)
  clud_core.py             finding drives, downloading, installing; no UI
stick/                     files copied onto the stick as-is
  CLUD.exe, CLUD.cmd       launcher
  config/CLAUDE.md         Claude's brief
  mcp/windows-mcp.cmd      starts the computer-use server
  autorun.inf, clud.ico    drive name and icon
launcher/                  dev tools (Windows): CLUD.exe source, icon generator
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
- [uv](https://github.com/astral-sh/uv) and [Textual](https://github.com/Textualize/textual),
  used by the installer, MIT / Apache-2.0

CLUD is an unofficial community project. It isn't affiliated with or endorsed by Anthropic.
