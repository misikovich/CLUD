#!/bin/sh
# CLUD for macOS: double-click this in the Finder. Terminal opens and runs CLUD.sh from
# this drive. Programs and logins stay on the drive; see CLUD.sh.
exec sh "$(dirname "$0")/CLUD.sh" "$@"
