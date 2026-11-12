#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Run diy-port under launchd on macOS: started at boot or login, restarted if it exits, logging
# with rotation (scripts/diyport_service.sh does the work). Each instance has its own folder,
# ~/.diy-port/$DIYPORT_INSTANCE/ ("native" when not set; see scripts/diyport_paths.sh), and its
# own launchd job, local.diyport.<instance>.
#
#   scripts/macos_launchd.sh install     install it as a launchd agent under your account, start it
#   scripts/macos_launchd.sh restart     restart diy-port (touching its restart file also works)
#   scripts/macos_launchd.sh stop        stop it until the next start/install or login
#   scripts/macos_launchd.sh start       start it again
#   scripts/macos_launchd.sh status      launchd's view and the last log lines
#   scripts/macos_launchd.sh logs        follow its logs/diyport.log
#   scripts/macos_launchd.sh uninstall   stop it and remove the plist
#
#   sudo DIYPORT_INSTANCE=onvif scripts/macos_launchd.sh install --daemon
#
# An agent needs no sudo: macOS lets any user bind ports below 1024. An instance that pulls
# cameras straight from the LAN (ONVIF) has to be a root daemon instead (--daemon): macOS's
# Local Network privacy blocks an agent from LAN devices other than the gateway. Give it its own
# address in diyport.env (DIYPORT_BIND_IP, and AIPORT_ADVERTISE_IP for Protect); the daemon adds
# that address to DIYPORT_IFACE at every start. With a daemon on the Mac, make every instance
# a daemon on its own address: a user agent can't take the wildcard :443 once root holds a
# specific address's :443 (EADDRINUSE). After install, the daemon's start, stop, restart
# and uninstall need sudo too; status and logs don't.
#
# Installing "native" for the first time moves the old ~/.diyport.env, ~/.diyport-state and
# ~/diyport.log* into ~/.diy-port/native/ and replaces the old local.diyport agent.
#
# Files an earlier `sudo` run left owned by root have to be handed back before an agent install;
# install says which and how. The host stats line's GPU and power figures need powermetrics as
# root: allow just that command with
#   echo "$USER ALL=(root) NOPASSWD: /usr/bin/powermetrics" | sudo tee /etc/sudoers.d/diyport-powermetrics
#   sudo chmod 440 /etc/sudoers.d/diyport-powermetrics && sudo visudo -cf /etc/sudoers.d/diyport-powermetrics
set -u

die() { echo "macos_launchd: $*" >&2; exit 1; }
[ "$(uname)" = Darwin ] || die "this is for macOS"

CMD=${1:-}
DAEMON=""
[ "${2:-}" = --daemon ] && DAEMON=1

if [ "$(id -u)" -eq 0 ]; then
    [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ] \
        || die "run it with sudo from your own account, so it knows whose home folder to use"
    USER_NAME=$SUDO_USER
else
    USER_NAME=$(id -un)
fi
USER_ID=$(id -u "$USER_NAME")
USER_HOME=$(eval echo "~$USER_NAME")
REPO=$(cd "$(dirname "$0")/.." && pwd)
. "$REPO/scripts/diyport_paths.sh"

LABEL=${DIYPORT_LAUNCHD_LABEL:-local.diyport.$INSTANCE}
OLD_LABEL=local.diyport   # the single agent from before ~/.diy-port
DAEMON_PLIST=/Library/LaunchDaemons/$LABEL.plist
[ -f "$DAEMON_PLIST" ] && DAEMON=1
if [ -n "$DAEMON" ]; then
    PLIST=$DAEMON_PLIST
    DOMAIN=system
else
    PLIST=$USER_HOME/Library/LaunchAgents/$LABEL.plist
    DOMAIN=gui/$USER_ID
fi
LAUNCHD_LOG=$BASE/logs/launchd.log   # the wrapper's own errors only

case "$CMD" in
    install|uninstall|start|stop|restart)
        if [ -n "$DAEMON" ]; then
            [ "$(id -u)" -eq 0 ] || die "$LABEL is a root daemon: run this with sudo"
        else
            [ "$(id -u)" -ne 0 ] \
                || die "run it as yourself, not with sudo: the agent runs under your account (add --daemon for a root daemon)"
        fi
        ;;
esac

loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

state_dir() {
    local dir
    dir=$(sed -n 's/^[[:space:]]*DIYPORT_STATE_DIR=//p' "$ENV_FILE" 2>/dev/null | tail -1 \
        | sed 's/[[:space:]]*#.*//;s/^"//;s/"$//')
    echo "${dir:-$DEFAULT_STATE_DIR}"
}

migrate_legacy() {
    # The single instance from before ~/.diy-port becomes "native", with the same identity.
    [ -n "$LEGACY" ] && [ -z "$DAEMON" ] || return 0
    local old_plist=$USER_HOME/Library/LaunchAgents/$OLD_LABEL.plist
    if loaded "$OLD_LABEL"; then
        echo "stopping the old $OLD_LABEL agent"
        launchctl bootout "$DOMAIN/$OLD_LABEL" 2>/dev/null
    fi
    rm -f "$old_plist"
    check_not_running
    echo "moving $LEGACY_ENV_FILE and the files it uses into $BASE/"
    mkdir -p "$BASE/logs" || die "can't create $BASE"
    local old_state
    old_state=$(state_dir)
    if [ "$old_state" = "$LEGACY_STATE_DIR" ] && [ -d "$old_state" ]; then
        [ -e "$BASE/state" ] && die "$BASE/state already exists; move $old_state by hand"
        mv "$old_state" "$BASE/state" || die "couldn't move $old_state"
        # Its other settings may point inside it too (AIPORT_FACES_DIR).
        sed -i '' "s#$LEGACY_STATE_DIR#$BASE/state#g" "$LEGACY_ENV_FILE"
    fi
    mv "$LEGACY_ENV_FILE" "$BASE/diyport.env" || die "couldn't move $LEGACY_ENV_FILE"
    for f in "$LEGACY_LOG" "$LEGACY_LOG".*; do
        [ -e "$f" ] && mv "$f" "$BASE/logs/$(basename "$f")"
    done
    rm -f "$LEGACY_RESTART_FILE" "$LEGACY_PIDFILE" "$USER_HOME/Library/Logs/diyport-launchd.log"
    . "$REPO/scripts/diyport_paths.sh"   # now the new paths
}

check_ownership() {
    # Everything an agent writes must be yours: its state dir, the stream dir under /tmp, the
    # repo (compiled files, the Apple helper build) and the log. A daemon is root.
    [ -z "$DAEMON" ] || return 0
    # The stream dir is this instance's own under the shared /tmp/aiport-streams
    # (config.stream_dir: a hash of the state dir's path).
    local paths=("$REPO" "$LOG" "$(state_dir)")
    paths+=("/private/tmp/aiport-streams/$(printf %s "$(state_dir)" | shasum | cut -c1-12)")
    local bad=()
    for p in "${paths[@]}"; do
        [ -e "$p" ] || continue
        if [ -n "$(find "$p" ! -user "$USER_ID" -print -quit 2>/dev/null)" ]; then
            bad+=("$p")
        fi
    done
    if [ ${#bad[@]} -gt 0 ]; then
        echo "These still hold files owned by root from an earlier sudo run:" >&2
        printf '  %s\n' "${bad[@]}" >&2
        echo "Hand them back, then run install again:" >&2
        echo "  sudo chown -R $USER_NAME:staff$(printf ' "%s"' "${bad[@]}")" >&2
        exit 1
    fi
}

check_not_running() {
    # This instance only (its state dir), so another instance can keep running.
    # A job just booted out takes a few seconds to stop (ExitTimeOut), so wait for it first.
    local pids pattern="diyport/run_all.py .*--state-dir $(state_dir)( |$)"
    for _ in $(seq 1 30); do
        pids=$(pgrep -f "$pattern" || true)
        [ -z "$pids" ] && break
        sleep 1
    done
    if [ -n "$pids" ] && ! loaded "$LABEL"; then
        echo "$INSTANCE is already running outside launchd (pid $(echo $pids | tr ' ' ','))." >&2
        echo "Stop it first (Ctrl-C in its terminal, or: sudo kill -INT $(echo $pids | tr ',' ' '))." >&2
        exit 1
    fi
}

write_plist() {
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LAUNCHD_LOG")"
    local extra=""
    if [ -n "$DAEMON" ]; then
        # SUDO_USER/SUDO_UID: the wrapper finds your home folder from them, and the Apple
        # vehicle model's helper runs as you. No .pyc files: root's would block the agent's
        # ownership check on the shared repo.
        extra="
        <key>SUDO_USER</key><string>$USER_NAME</string>
        <key>SUDO_UID</key><string>$USER_ID</string>
        <key>PYTHONDONTWRITEBYTECODE</key><string>1</string>"
    fi
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$REPO/scripts/diyport_service.sh</string>
    </array>
    <key>WorkingDirectory</key><string>$REPO</string>
    <key>EnvironmentVariables</key>
    <dict>
        <!-- ffmpeg and ffprobe come from Homebrew; launchd's own PATH doesn't have it. -->
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>DIYPORT_INSTANCE</key><string>$INSTANCE</string>$extra
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <!-- The wrapper gives diy-port 10 s to stop cleanly before TERM. -->
    <key>ExitTimeOut</key><integer>30</integer>
    <!-- Not throttled as a background job: it analyses video in real time. -->
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>$LAUNCHD_LOG</string>
    <key>StandardErrorPath</key><string>$LAUNCHD_LOG</string>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" >/dev/null || die "wrote an invalid $PLIST"
    if [ -n "$DAEMON" ]; then
        chown root:wheel "$PLIST" && chmod 644 "$PLIST"
        # The folder stays yours: edit diyport.env and touch restart without sudo. What the
        # daemon writes inside (state, logs) is root's but readable.
        chown "$USER_NAME:staff" "$USER_HOME/.diy-port" "$BASE"
    fi
}

start() {
    loaded "$LABEL" || launchctl bootstrap "$DOMAIN" "$PLIST" || die "launchctl bootstrap failed"
    launchctl enable "$DOMAIN/$LABEL"
}

status() {
    echo "$LABEL ($([ -n "$DAEMON" ] && echo "root daemon" || echo agent), files in $BASE):"
    if loaded "$LABEL"; then
        launchctl print "$DOMAIN/$LABEL" | grep -E "^[[:space:]]*(state|pid|last exit code|runs) =" \
            | sed 's/^[[:space:]]*/  /'
    else
        echo "  not loaded"
    fi
    echo "last log lines ($LOG):"
    tail -n 5 "$LOG" 2>/dev/null | cut -c1-200 | sed 's/^/  /'
}

case "$CMD" in
    install)
        migrate_legacy
        [ -r "$ENV_FILE" ] || die "no $ENV_FILE (copy scripts/diyport.env.example)"
        [ -x "$REPO/venv/bin/python" ] || die "no venv at $REPO/venv"
        if [ -n "$DAEMON" ]; then
            grep -qE '^[[:space:]]*DIYPORT_BIND_IP=[0-9]' "$ENV_FILE" \
                || die "a daemon needs its own address: set DIYPORT_BIND_IP in $ENV_FILE"
        fi
        check_ownership
        check_not_running
        loaded "$LABEL" && launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
        write_plist
        start
        echo "installed $PLIST; diy-port starts now and at every $([ -n "$DAEMON" ] && echo boot || echo login)"
        sleep 3
        status
        ;;
    uninstall)
        loaded "$LABEL" && launchctl bootout "$DOMAIN/$LABEL"
        rm -f "$PLIST"
        echo "removed $LABEL (its files stay in $BASE)"
        ;;
    start)
        [ -f "$PLIST" ] || die "not installed (run: $0 install)"
        start
        status
        ;;
    stop)
        loaded "$LABEL" && launchctl bootout "$DOMAIN/$LABEL"
        echo "stopped $LABEL (starts again at $([ -n "$DAEMON" ] && echo boot || echo login), or with: $0 start)"
        ;;
    restart)
        loaded "$LABEL" || die "not running (run: $0 start)"
        launchctl kickstart -k "$DOMAIN/$LABEL"
        status
        ;;
    status) status ;;
    logs) exec tail -n 50 -F "$LOG" ;;
    *)
        sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//'
        exit 2
        ;;
esac
