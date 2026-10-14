#!/bin/sh
# CLUD Installer for macOS and Linux. Fetches uv (and through it Python + Textual) into
# .tools/ next to this file the first time, so nothing gets installed system-wide.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOLS="$HERE/.tools"
UV="$TOOLS/uv/uv"

if [ ! -x "$UV" ]; then
    case "$(uname -s)" in
        Darwin) os="apple-darwin" ;;
        Linux)
            os="unknown-linux-gnu"
            if ldd --version 2>&1 | grep -qi musl; then os="unknown-linux-musl"; fi ;;
        *) echo "Unsupported system: $(uname -s). On Windows, run CLUD-Installer.bat."; exit 1 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) arch="x86_64" ;;
        arm64|aarch64) arch="aarch64" ;;
        *) echo "Unsupported CPU: $(uname -m)"; exit 1 ;;
    esac
    url="https://github.com/astral-sh/uv/releases/latest/download/uv-$arch-$os.tar.gz"
    echo "Fetching uv, one time only, about 20 MB..."
    mkdir -p "$TOOLS/uv"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$url" | tar -xz -C "$TOOLS/uv" --strip-components 1
    else
        wget -qO- "$url" | tar -xz -C "$TOOLS/uv" --strip-components 1
    fi
    if [ ! -x "$UV" ]; then
        echo "Could not download uv. Check your internet connection."
        exit 1
    fi
fi

export UV_CACHE_DIR="$TOOLS/uv-cache"
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_PYTHON_PREFERENCE="only-managed"
export CLUD_UV="$UV"

exec "$UV" run --quiet --python 3.13 "$HERE/installer/clud_installer.py" "$@"
