#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
# Check the Mac host setup for compose.macos.yaml before starting the container:
# the alias address exists and pf forwards its :443 to AIPORT_PUBLISH_PORT.
#
#   sudo AIPORT_ADVERTISE_IP=192.0.2.39 AIPORT_IFACE=en0 scripts/macos_docker_preflight.sh
#
# The README has the commands to add either; neither survives a reboot.
set -u

die() { echo "macos_docker_preflight: $*" >&2; exit 1; }
[ "$(uname)" = Darwin ] || die "this is for macOS"
[ -n "${AIPORT_ADVERTISE_IP:-}" ] || die "set AIPORT_ADVERTISE_IP to the Mac's alias address"
IFACE=${AIPORT_IFACE:-en0}
PORT=${AIPORT_PUBLISH_PORT:-18443}
ok=1

if ifconfig "$IFACE" 2>/dev/null | grep -q "inet $AIPORT_ADVERTISE_IP "; then
    echo "ok: $AIPORT_ADVERTISE_IP is on $IFACE"
else
    echo "missing alias on $IFACE:" >&2
    echo "  sudo ifconfig $IFACE alias $AIPORT_ADVERTISE_IP 255.255.255.255" >&2
    ok=0
fi

if pfctl -a com.apple/diyport-docker -s nat 2>/dev/null | grep -q "$AIPORT_ADVERTISE_IP.*$PORT"; then
    echo "ok: pf forwards $AIPORT_ADVERTISE_IP:443 to :$PORT"
else
    echo "missing pf rule (needs root to read):" >&2
    echo "  echo 'rdr pass on $IFACE inet proto tcp from any to $AIPORT_ADVERTISE_IP port 443 -> $AIPORT_ADVERTISE_IP port $PORT' | sudo pfctl -a com.apple/diyport-docker -f -" >&2
    echo "  sudo pfctl -E" >&2
    ok=0
fi

[ "$ok" = 1 ] || exit 1
