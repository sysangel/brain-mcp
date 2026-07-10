#!/bin/zsh
# Install (or reinstall) the nightly backstop reflection job (03:30 local).
# Backstop only: the primary trigger is the debt check at brain-server startup
# (see src/brain/autoreflect.py); both route through the brain-autoreflect
# wrapper, so this nightly run no-ops whenever a debt reflect already ran.
# Disable any time with:  ./scripts/install-launchd.sh --remove
#
# macOS only. The plist is rendered from brain-reflect.plist.template because
# launchd expands neither $HOME nor env vars in Label/Standard*Path — the real
# absolute BRAIN_DIR is substituted in at install time.
set -euo pipefail

LABEL="local.brain.reflect"

# Derive the repo root from this script's own location (scripts/ -> repo root),
# matching config.py's package-relative BRAIN_DIR default, so this works no
# matter where the repo is checked out.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BRAIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/brain-reflect.plist.template"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_N="$(id -u)"

if [[ "${1:-}" == "--remove" ]]; then
    launchctl bootout "gui/${UID_N}/${LABEL}" 2>/dev/null || true
    rm -f "$DST"
    echo "removed ${LABEL}"
    exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$BRAIN_DIR/cache"
launchctl bootout "gui/${UID_N}/${LABEL}" 2>/dev/null || true
# Render the template with the real label + absolute repo path.
sed -e "s#__LABEL__#${LABEL}#g" -e "s#__BRAIN_DIR__#${BRAIN_DIR}#g" "$TEMPLATE" > "$DST"
launchctl bootstrap "gui/${UID_N}" "$DST"
launchctl print "gui/${UID_N}/${LABEL}" >/dev/null && echo "installed ${LABEL} (nightly 03:30; logs in ${BRAIN_DIR}/cache/reflect.log)"
