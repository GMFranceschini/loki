#!/bin/bash
# Install (or reinstall) the daily Loki fetch as a launchd job.
#
#   scripts/install_schedule.sh            run daily at 07:00
#   scripts/install_schedule.sh 6 30       run daily at 06:30
#   scripts/install_schedule.sh --remove   uninstall the job
#
# launchd runs the job as you, only while you are logged in. If the Mac is
# asleep at the scheduled time, launchd runs the job once it wakes.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.loki.daily"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="$ROOT/scripts/com.loki.daily.plist.template"
BACKFILL=3

uninstall() {
    if [ -f "$PLIST" ]; then
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed the daily schedule ($PLIST)."
    else
        echo "No schedule was installed."
    fi
}

if [ "${1:-}" = "--remove" ] || [ "${1:-}" = "-r" ]; then
    uninstall
    exit 0
fi

HOUR="${1:-7}"
MINUTE="${2:-0}"

if ! [[ "$HOUR" =~ ^[0-9]+$ ]] || [ "$HOUR" -gt 23 ]; then
    echo "Hour must be a whole number from 0 to 23. Got: $HOUR" >&2
    exit 1
fi
if ! [[ "$MINUTE" =~ ^[0-9]+$ ]] || [ "$MINUTE" -gt 59 ]; then
    echo "Minute must be a whole number from 0 to 59. Got: $MINUTE" >&2
    exit 1
fi
if [ ! -x "$ROOT/bin/loki" ]; then
    echo "Cannot find an executable at $ROOT/bin/loki" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs"

# Replace an existing job rather than stacking a second one.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true

sed -e "s|__ROOT__|${ROOT}|g" \
    -e "s|__HOUR__|${HOUR}|g" \
    -e "s|__MINUTE__|${MINUTE}|g" \
    -e "s|__BACKFILL__|${BACKFILL}|g" \
    "$TEMPLATE" > "$PLIST"

launchctl bootstrap "gui/$(id -u)" "$PLIST"

printf 'Loki will fetch every day at %02d:%02d.\n' "$HOUR" "$MINUTE"
echo
echo "  Check it:    launchctl list | grep ${LABEL}"
echo "  Run it now:  launchctl kickstart -p gui/$(id -u)/${LABEL}"
echo "  Logs:        $ROOT/data/logs/"
echo "  Remove it:   scripts/install_schedule.sh --remove"
