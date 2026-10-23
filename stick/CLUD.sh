#!/bin/sh
# CLUD - portable Claude Code launcher for Linux (Raspberry Pi OS, Ubuntu, Debian, ...).
# Runs entirely off this drive. Start it from the file manager (CLUD.desktop), or with
#   sh /media/$USER/CLUD/CLUD.sh [folder]
USB="$(cd "$(dirname "$0")" && pwd)"

fail() {
    echo "CLUD: $*" >&2
    # Keep the window open when started from the file manager
    if [ -n "$CLUD_IN_WINDOW" ]; then printf "Press Enter to close. "; read -r _; fi
    exit 1
}

# Double-clicked without a terminal: open one and run in it
if [ ! -t 0 ] || [ ! -t 1 ]; then
    for term in x-terminal-emulator lxterminal gnome-terminal konsole xfce4-terminal mate-terminal \
                kgx foot alacritty kitty xterm; do
        command -v "$term" >/dev/null 2>&1 || continue
        export CLUD_IN_WINDOW=1
        case "$term" in
            gnome-terminal|kgx) exec "$term" -- sh "$USB/CLUD.sh" "$@" ;;
            *) exec "$term" -e sh "$USB/CLUD.sh" "$@" ;;
        esac
    done
    fail "no terminal app found. Open a terminal and run: sh \"$USB/CLUD.sh\""
fi

# Pick the build for this computer. Pi OS 32-bit can run a 64-bit kernel (uname says
# aarch64) under 32-bit programs, so ask about the userland too.
bits="$(getconf LONG_BIT 2>/dev/null || echo 64)"
case "$(uname -m)" in
    x86_64|amd64) arch=linux-x64 ;;
    aarch64|arm64) arch=linux-arm64 ;;
    armv6l|armv7l|armv8l|arm*) bits=32 ;;
    *) fail "this computer's CPU ($(uname -m)) isn't supported. CLUD runs on 64-bit x86 and ARM." ;;
esac
if [ "$bits" = 32 ]; then
    fail "this is a 32-bit system. Claude Code needs 64-bit Linux; on a Raspberry Pi 3, 4 or 5, install
      Raspberry Pi OS (64-bit) with Raspberry Pi Imager."
fi
if ldd --version 2>&1 | grep -qi musl; then
    fail "this Linux uses musl (Alpine?). CLUD's Linux build needs glibc (Debian, Ubuntu, Fedora, Pi OS, ...)."
fi
BIN="$USB/$arch"
[ -f "$BIN/claude.exe" ] || fail "this stick has no $arch build. Run the CLUD Installer again and tick Linux."

# Files on the stick may not be runnable: a stick mounted "noexec", or by hand without
# exec permissions. Then run a copy from the host's cache instead (one-time, ~350 MB).
if [ ! -x "$BIN/claude.exe" ] || [ ! -x "$BIN/py/bin/python3.exe" ]; then
    version="$(sed -n 's/.*"claude": *"\([^"]*\)".*/\1/p' "$USB/clud-version.json" 2>/dev/null)"
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/clud/$arch-${version:-unknown}"
    if [ ! -x "$cache/claude.exe" ]; then
        echo "This drive doesn't allow running programs from it. Copying CLUD to $cache (one time)..."
        rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/clud"
        mkdir -p "$cache" && cp -R "$BIN/." "$cache/" && chmod -R u+x "$cache" \
            || fail "couldn't copy CLUD to $cache"
    fi
    BIN="$cache"
    [ -x "$BIN/claude.exe" ] || fail "can't run programs from $BIN either (is the home folder noexec?)"
fi

export CLUD_HOME="$USB"
export CLAUDE_CONFIG_DIR="$USB/config"
export PATH="$BIN:$PATH"
# Self-updates would install onto the host; update the stick with the CLUD Installer instead
export DISABLE_AUTOUPDATER=1
# Python on an SD card or slow stick can take a while to cold-start
export MCP_TIMEOUT=120000

# The computer-use server, passed on the command line so the Windows one (cmd.exe-based)
# stays out of the way here and vice versa. JSON-escape the paths: \ and " are legal in them.
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
mcp="{\"mcpServers\":{\"linux-desktop\":{\"type\":\"stdio\",\"command\":\"$(esc "$BIN/py/bin/python3.exe")\",\
\"args\":[\"-I\",\"-B\",\"$(esc "$USB/mcp/clud_desktop.py")\"]}}}"

printf '\033]0;CLUD on %s\007' "$(hostname 2>/dev/null || uname -n)"
# Start in the host's home folder (CLUD works on the host), or in the folder given
if [ -n "$1" ]; then cd "$1" || fail "can't open $1"; else cd "$HOME" || cd /; fi
# Permission prompts are off: CLUD acts without asking (see config/CLAUDE.md for its own rules)
"$BIN/claude.exe" --dangerously-skip-permissions --mcp-config "$mcp"
status=$?
if [ -n "$CLUD_IN_WINDOW" ] && [ $status -ne 0 ]; then
    printf "Claude Code exited with code %s. Press Enter to close. " "$status"; read -r _
fi
exit $status
