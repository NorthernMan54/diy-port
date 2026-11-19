#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
# shellcheck disable=SC2034  # sourced: the caller uses these variables
# Where a diy-port instance keeps its files, sourced by diyport_service.sh and macos_launchd.sh
# (set USER_HOME first). Each instance has a folder of its own:
#
#   ~/.diy-port/$DIYPORT_INSTANCE/     (DIYPORT_INSTANCE is "native" when not set)
#     diyport.env        settings: AIPORT_* and DIYPORT_* identity
#     state/            adopt state, device name, firmware pin (DIYPORT_STATE_DIR overrides)
#     logs/diyport.log   diy-port's log, rotated beside it
#     logs/launchd.log  launchd's own errors for this instance
#     restart           touch it to restart diy-port
#     service.pid
#
# DIYPORT_ENV_FILE, DIYPORT_LOG, DIYPORT_RESTART_FILE and DIYPORT_PIDFILE still override one path.
# Until `macos_launchd.sh install` moves them, a "native" instance with ~/.diyport.env and no
# ~/.diy-port/native/diyport.env keeps the old paths straight in the home folder.

INSTANCE=${DIYPORT_INSTANCE:-native}
if ! [[ "$INSTANCE" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "DIYPORT_INSTANCE must be lowercase letters, digits and -, not: $INSTANCE" >&2
    exit 1
fi
BASE=$USER_HOME/.diy-port/$INSTANCE

LEGACY_ENV_FILE=$USER_HOME/.diyport.env
LEGACY_STATE_DIR=$USER_HOME/.diyport-state
LEGACY_LOG=$USER_HOME/diyport.log
LEGACY_RESTART_FILE=$USER_HOME/.diyport-restart
LEGACY_PIDFILE=$USER_HOME/.diyport-service.pid

if [ "$INSTANCE" = native ] && [ ! -e "$BASE/diyport.env" ] && [ -e "$LEGACY_ENV_FILE" ]; then
    LEGACY=1
    ENV_FILE=${DIYPORT_ENV_FILE:-$LEGACY_ENV_FILE}
    TRIGGER=${DIYPORT_RESTART_FILE:-$LEGACY_RESTART_FILE}
    LOG=${DIYPORT_LOG:-$LEGACY_LOG}
    PIDFILE=${DIYPORT_PIDFILE:-$LEGACY_PIDFILE}
    DEFAULT_STATE_DIR=$LEGACY_STATE_DIR
else
    LEGACY=""
    ENV_FILE=${DIYPORT_ENV_FILE:-$BASE/diyport.env}
    TRIGGER=${DIYPORT_RESTART_FILE:-$BASE/restart}
    LOG=${DIYPORT_LOG:-$BASE/logs/diyport.log}
    PIDFILE=${DIYPORT_PIDFILE:-$BASE/service.pid}
    DEFAULT_STATE_DIR=$BASE/state
fi
