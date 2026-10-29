#!/bin/sh
# CLUD - portable Claude Code / omp launcher for Linux (Raspberry Pi OS, Ubuntu, Debian, ...)
# and macOS. Runs entirely off this drive. Start it from the file manager (CLUD.desktop on
# Linux, CLUD.command in the Finder), or with
#   sh /media/$USER/CLUD/CLUD.sh [folder]      (macOS: sh /Volumes/CLUD/CLUD.sh)
# With both agents on the stick it asks which to start; CLUD_AGENT=claude or omp skips that.
USB="$(cd "$(dirname "$0")" && pwd)"
case "$(uname -s)" in
    Darwin) os=macos ;;
    *) os=linux ;;
esac

fail() {
    echo "CLUD: $*" >&2
    # Keep the window open when started from the file manager
    if [ -n "$CLUD_IN_WINDOW" ]; then printf "Press Enter to close. "; read -r _; fi
    exit 1
}

# Double-clicked without a terminal: open one and run in it
if [ ! -t 0 ] || [ ! -t 1 ]; then
    [ "$os" = macos ] && exec open -a Terminal "$USB/CLUD.command"
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

# Pick the build for this computer
if [ "$os" = macos ]; then
    # A Terminal running under Rosetta says x86_64 on an Apple Silicon Mac too
    if [ "$(uname -m)" = arm64 ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then
        arch=macos-arm64
    else
        fail "this Mac has an Intel processor. CLUD on macOS needs an Apple Silicon Mac (M1 or later)."
    fi
    cache_root="$HOME/Library/Caches/clud"
else
    # Pi OS 32-bit can run a 64-bit kernel (uname says aarch64) under 32-bit programs, so
    # ask about the userland too
    bits="$(getconf LONG_BIT 2>/dev/null || echo 64)"
    case "$(uname -m)" in
        x86_64|amd64) arch=linux-x64 ;;
        aarch64|arm64) arch=linux-arm64 ;;
        armv6l|armv7l|armv8l|arm*) bits=32 ;;
        *) fail "this computer's CPU ($(uname -m)) isn't supported. CLUD runs on 64-bit x86 and ARM." ;;
    esac
    if [ "$bits" = 32 ]; then
        fail "this is a 32-bit system. CLUD needs 64-bit Linux; on a Raspberry Pi 3, 4 or 5, install
      Raspberry Pi OS (64-bit) with Raspberry Pi Imager."
    fi
    if ldd --version 2>&1 | grep -qi musl; then
        fail "this Linux uses musl (Alpine?). CLUD's Linux build needs glibc (Debian, Ubuntu, Fedora, Pi OS, ...)."
    fi
    cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/clud"
fi
BIN="$USB/$arch"
[ "$os" = macos ] && platform=macOS || platform=Linux
[ -d "$BIN/py" ] || fail "this stick has no $arch build. Run the CLUD Installer again and tick $platform."

# Which agent: the one on the stick, or ask when there are both
agent="${CLUD_AGENT:-}"
if [ -z "$agent" ]; then
    if [ -f "$BIN/claude.exe" ] && [ -f "$BIN/omp.exe" ]; then
        printf '\n  CLUD - which agent?\n\n    1  Claude Code\n    2  omp  (Claude, GPT, Gemini, local models, ...)\n\n'
        while :; do
            printf "  Type 1 or 2 and press Enter: "
            read -r answer || exit 1
            case "$answer" in
                1) agent=claude; break ;;
                2) agent=omp; break ;;
            esac
        done
    elif [ -f "$BIN/omp.exe" ]; then
        agent=omp
    else
        agent=claude
    fi
fi
case "$agent" in
    claude|omp) [ -f "$BIN/$agent.exe" ] || fail "this stick has no $agent. Run the CLUD Installer again and tick it." ;;
    *) fail "CLUD_AGENT is \"$agent\"; use claude or omp" ;;
esac

# Files on the stick may not be runnable: a stick mounted "noexec", or by hand without
# exec permissions. Then run a copy from the host's cache instead (one-time, ~350 MB per
# agent), kept until the stick is updated.
if [ ! -x "$BIN/$agent.exe" ] || [ ! -x "$BIN/py/bin/python3.exe" ]; then
    version="$(cksum < "$USB/clud-version.json" 2>/dev/null | cut -d' ' -f1)"
    cache="$cache_root/$arch-${version:-unknown}"
    if [ ! -x "$cache/$agent.exe" ] || [ ! -x "$cache/py/bin/python3.exe" ]; then
        echo "This drive doesn't allow running programs from it. Copying CLUD to $cache (one time)..."
        [ -d "$cache" ] || rm -rf "$cache_root"
        mkdir -p "$cache" && cp -R "$BIN/py" "$cache/" && cp "$BIN/$agent.exe" "$cache/" \
            && chmod -R u+x "$cache" || fail "couldn't copy CLUD to $cache"
    fi
    BIN="$cache"
    [ -x "$BIN/$agent.exe" ] || fail "can't run programs from $BIN either (is the home folder noexec?)"
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
[ "$os" = macos ] && server=mac-desktop || server=linux-desktop
mcp="{\"mcpServers\":{\"$server\":{\"type\":\"stdio\",\"command\":\"$(esc "$BIN/py/bin/python3.exe")\",\
\"args\":[\"-I\",\"-B\",\"$(esc "$USB/mcp/clud_desktop.py")\"]}}}"

printf '\033]0;CLUD on %s\007' "$(hostname 2>/dev/null || uname -n)"
# Start in the host's home folder (CLUD works on the host), or in the folder given
if [ -n "$1" ]; then cd "$1" || fail "can't open $1"; else cd "$HOME" || cd /; fi
# Permission prompts are off: CLUD acts without asking (see config/CLAUDE.md for its own rules)
if [ "$agent" = claude ]; then
    if [ "$os" = macos ]; then
        # On a Mac, Claude Code moves the login from config/.credentials.json into the host's
        # Keychain the first time it refreshes it, and deletes the file. Move it back when it
        # exits (or, after a crash, on the next start) so it stays on the stick, off the Mac.
        login_item="Claude Code-credentials-$(printf '%s' "$CLAUDE_CONFIG_DIR" | shasum -a 256 | cut -c1-8)"
        reclaim_login() {
            login="$(security find-generic-password -s "$login_item" -w 2>/dev/null)" && [ -n "$login" ] || return 0
            (umask 077 && printf '%s\n' "$login" > "$CLAUDE_CONFIG_DIR/.credentials.json.new") \
                && mv -f "$CLAUDE_CONFIG_DIR/.credentials.json.new" "$CLAUDE_CONFIG_DIR/.credentials.json" \
                && security delete-generic-password -s "$login_item" >/dev/null 2>&1
        }
        reclaim_login
        # Closing the Terminal window hangs up on us: still put the login back. (A trap waits
        # for Claude Code to exit first, so Ctrl+C inside it does no harm.)
        trap 'reclaim_login; exit 129' HUP
        trap 'reclaim_login; exit 130' INT
        trap 'reclaim_login; exit 143' TERM
    fi
    "$BIN/claude.exe" --dangerously-skip-permissions --mcp-config "$mcp"
    status=$?
    [ "$os" = macos ] && reclaim_login
    name="Claude Code"
else
    # omp keeps logins, settings and sessions in config/omp, and its native library next to
    # itself, unpacked there on first start. It reads CLUD's computer-use server from
    # .mcp.json there (servers you add go to mcp.json), CLUD's settings from omp.yml, and
    # the brief from config/CLAUDE.md.
    export PI_CODING_AGENT_DIR="$USB/config/omp"
    export PI_NATIVES_DIR="$BIN/omp-natives"
    mkdir -p "$PI_CODING_AGENT_DIR" && printf '%s\n' "$mcp" > "$PI_CODING_AGENT_DIR/.mcp.json" \
        || fail "can't write to $PI_CODING_AGENT_DIR (is the stick read-only?)"
    # omp still writes logs to ~/.omp; remove it afterwards unless the host had it already
    [ -n "$HOME" ] && [ ! -e "$HOME/.omp" ] && host_omp="$HOME/.omp"
    "$BIN/omp.exe" --allow-home --auto-approve --config "$USB/omp.yml" \
        --append-system-prompt "$USB/config/CLAUDE.md"
    status=$?
    [ -n "${host_omp:-}" ] && rm -rf "$host_omp"
    name=omp
fi
if [ -n "$CLUD_IN_WINDOW" ] && [ $status -ne 0 ]; then
    printf "%s exited with code %s. Press Enter to close. " "$name" "$status"; read -r _
fi
exit $status
