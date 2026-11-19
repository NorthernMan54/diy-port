#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
# Run diy-port on macOS and restart it without a new terminal or password.
#
#   scripts/macos_launchd.sh install     as a launchd agent under your account (recommended)
#   cd ~/Code/diy-port && scripts/diyport_service.sh     in a terminal
#
# No sudo: macOS lets any user bind ports below 1024. (Run with sudo it still works, as before.)
#
# Each instance's files are in ~/.diy-port/$DIYPORT_INSTANCE/ ("native" when not set; see
# scripts/diyport_paths.sh). Settings come from its diyport.env (see scripts/diyport.env.example):
# AIPORT_* settings and DIYPORT_* identity, one KEY=value per line, re-read on every restart.
# Only those keys are taken; the file is never run as a script. DIYPORT_BIND_IP binds :443 and
# :10001 to that address only, so several instances can share a Mac.
#
#   touch ~/.diy-port/native/restart     restart diy-port (new code, new settings), no sudo needed
#   Ctrl-C in this terminal             stop it
#
# If diy-port exits on its own it is restarted after 5 s. Output goes to the instance's
# logs/diyport.log, rotated by size (DIYPORT_LOG_MAX_MB, 50 by default; DIYPORT_LOG_KEEP old files, 5), and to the
# terminal when there is one. caffeinate keeps the Mac awake while this runs.
set -u

if [ "$(uname)" != Darwin ] && [ "$(id -u)" -ne 0 ] && [ -z "${DIYPORT_SERVICE_TEST:-}" ]; then
    echo "run with sudo: it binds port 443 on the interface's address" >&2
    exit 1
fi
# shellcheck disable=SC2034  # read by diyport_paths.sh
USER_HOME=$(eval echo "~${SUDO_USER:-${USER:-$(id -un)}}")
REPO=${DIYPORT_SERVICE_TEST:-$(cd "$(dirname "$0")/.." && pwd)}  # the test points it at a stub
# PIDFILE holds diy-port's process group, so a killed wrapper does not orphan :443.
# shellcheck disable=SC1091  # resolved at runtime from the script's own directory
. "$(dirname "$0")/diyport_paths.sh"

load_settings() {
    # Clear the previous run's settings so a removed line really goes away.
    for k in $(env | grep -E '^(AIPORT|DIYPORT)_[A-Z0-9_]*=' | cut -d= -f1); do
        case $k in
            DIYPORT_INSTANCE|DIYPORT_ENV_FILE|DIYPORT_RESTART_FILE|DIYPORT_LOG|DIYPORT_PIDFILE) ;;
            *) unset "$k" ;;
        esac
    done
    if [ ! -r "$ENV_FILE" ]; then
        echo "diyport_service: no $ENV_FILE (copy scripts/diyport.env.example)" >&2
        return 1
    fi
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%%#*}
        line=$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -z "$line" ] && continue
        key=${line%%=*}
        value=${line#*=}
        value=${value#\"}; value=${value%\"}
        if [[ "$key" =~ ^(AIPORT|DIYPORT)_[A-Z0-9_]+$ && "$line" == *=* ]]; then
            export "$key=$value"
        else
            echo "diyport_service: ignoring line in $ENV_FILE: $line" >&2
        fi
    done < "$ENV_FILE"
    export DIYPORT_STATE_DIR=${DIYPORT_STATE_DIR:-$DEFAULT_STATE_DIR}
    local missing=""
    for k in DIYPORT_IFACE DIYPORT_MAC DIYPORT_DEVICE_ID; do
        [ -n "${!k:-}" ] || missing="$missing $k"
    done
    if [ -n "$missing" ]; then
        echo "diyport_service: $ENV_FILE is missing$missing" >&2
        return 1
    fi
}

# A root instance on its own address (DIYPORT_BIND_IP) adds it to the interface itself: an
# alias isn't kept across reboots, and this runs at boot.
ensure_alias() {
    [ -n "${DIYPORT_BIND_IP:-}" ] && [ "$(id -u)" -eq 0 ] && [ "$(uname)" = Darwin ] || return 0
    ifconfig "$DIYPORT_IFACE" 2>/dev/null | grep -q "inet $DIYPORT_BIND_IP " && return 0
    echo "diyport_service: adding $DIYPORT_BIND_IP to $DIYPORT_IFACE"
    ifconfig "$DIYPORT_IFACE" alias "$DIYPORT_BIND_IP" 255.255.255.255
}

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }

child=""
stop_child() {
    if [ -n "$child" ] && { kill -0 -- "-$child" 2>/dev/null || kill -0 "$child" 2>/dev/null; }; then
        kill -INT -- "-$child" 2>/dev/null || kill -INT "$child"   # its whole process group
        kill -CONT -- "-$child" 2>/dev/null   # a stopped process can't act on the signal
        for _ in $(seq 1 20); do
            kill -0 -- "-$child" 2>/dev/null || break
            sleep 0.5
        done
        kill -TERM -- "-$child" 2>/dev/null
        kill -CONT -- "-$child" 2>/dev/null
        wait "$child" 2>/dev/null
    fi
    child=""
}
stop_stale() {
    # A killed wrapper's process group can outlive its run_all.py leader; check both.
    local old
    old=$(cat "$PIDFILE" 2>/dev/null) || return 0
    if [ -n "$old" ] && ps -p "$old" -o command= 2>/dev/null | grep -q "run_all.py"; then
        echo "diyport_service: stopping diy-port (pid $old) left running by an earlier start"
        child=$old
        stop_child
    elif [ -n "$old" ] && pgrep -g "$old" -f \
            "(discovery|http_api|ucp4_client|avclient)\.py" >/dev/null 2>&1; then
        echo "diyport_service: stopping orphaned diy-port group (pgid $old)"
        child=$old
        stop_child
    fi
    rm -f "$PIDFILE"
}
trap 'echo "diyport_service: stopping"; stop_child; rm -f "$PIDFILE"; exit 0' INT TERM

# One writer rotates the log; it ignores Ctrl-C/TERM so the stop is still logged. Its sizes are
# read once, here (a change needs a full restart, not ~/.diyport-restart).
env_number() {
    local v
    v=$(sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" 2>/dev/null | tail -1 | sed 's/[[:space:]#].*//')
    [[ "$v" =~ ^[0-9]+(\.[0-9]+)?$ ]] && echo "$v" || echo "$2"
}
mkdir -p "$(dirname "$LOG")"
if [ -x "$REPO/venv/bin/python" ] && [ -f "$(dirname "$0")/log_writer.py" ]; then
    exec > >("$REPO/venv/bin/python" "$(dirname "$0")/log_writer.py" "$LOG" \
                 --max-mb "${DIYPORT_LOG_MAX_MB:-$(env_number DIYPORT_LOG_MAX_MB 50)}" \
                 --keep "${DIYPORT_LOG_KEEP:-$(env_number DIYPORT_LOG_KEEP 5)}") 2>&1
else
    echo "diyport_service: no venv at $REPO/venv/bin/python; logging to stderr" >&2
fi
[ -n "${DIYPORT_SERVICE_TEST:-}" ] || caffeinate -is -w $$ &

while true; do
    if ! load_settings; then
        sleep 5
        continue
    fi
    ensure_alias
    stop_stale
    seen=$(mtime "$TRIGGER")
    echo "diyport_service: starting diy-port at $(date '+%F %T') (settings from $ENV_FILE)"
    # Restore SIGINT before exec; job control gives diy-port its own group for stop_child.
    bind=()
    [ -n "${DIYPORT_BIND_IP:-}" ] && bind=(--bind-ip "$DIYPORT_BIND_IP")
    set -m
    "$REPO/venv/bin/python" -c 'import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execv(sys.argv[1], sys.argv[1:])' "$REPO/venv/bin/python" "$REPO/diyport/run_all.py" \
        --iface "$DIYPORT_IFACE" --state-dir "$DIYPORT_STATE_DIR" --mac "$DIYPORT_MAC" \
        --device-id "$DIYPORT_DEVICE_ID" ${bind[@]+"${bind[@]}"} < /dev/null &   # a background job reading the
    # terminal is stopped (SIGTTIN): ffmpeg and ffprobe read stdin
    child=$!
    echo "$child" > "$PIDFILE"
    set +m
    while kill -0 "$child" 2>/dev/null; do
        sleep 1
        if [ "$(mtime "$TRIGGER")" != "$seen" ]; then
            echo "diyport_service: $TRIGGER touched, restarting"
            stop_child
            break
        fi
    done
    if [ -n "$child" ]; then
        wait "$child"
        echo "diyport_service: diy-port exited ($?), restarting in 5 s"
        child=""
        sleep 5
    fi
done
