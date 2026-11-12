# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared decode for transcoded ONVIF cameras (AIPORT_ONVIF_SHARED_DECODE): detection decodes
the video1 that onvif_push sends Protect, read from its DetectServer socket, instead of
pulling the camera a second time. Each analysed frame takes the wall time the push gave
Protect, so boxes line up with Protect's timeline. avclient picks the source
(_shared_decode_source) and runs the ffmpeg; this module picks the frames and feeds it."""

import logging
import socket
import time

import onvif_push

log = logging.getLogger("diyport.avclient")


def keep_ratio(fps, detect_fps):
    """(keep, of): analyse `keep` of every `of` pushed frames, spread evenly. A whole
    stride (every Kth frame) can't hit detect_fps from 15 fps: live 2026-11-07 on
    92DE633106E8 (15 fps) round(15/10) = 2 gave 7.5 fps and a "falling behind" warning
    every minute. Integers, so ffmpeg's select and the wall-time feeder agree exactly."""
    of = max(1, int(round(float(fps))))
    return min(detect_fps, of), of


def keeps(n, keep):
    """True for input frame n (from 0) that the shared decode analyses: the frames where
    floor(n * keep / of) steps up (Bresenham), keep of every `of`, evenly spread."""
    k, of = keep
    return (n + 1) * k // of > n * k // of


def select_filter(keep):
    """The ffmpeg select filter for keeps() (n counts input frames from 0)."""
    k, of = keep
    return f"select=gt(floor((n+1)*{k}/{of})\\,floor(n*{k}/{of}))"


def feed(device_id, proc, sock_path, keep, walls):
    """Copies the pushed video1 from its DetectServer into ffmpeg, queueing the wall time
    of each video frame ffmpeg keeps (keeps) for the frame reader. Our encoder never reorders frames
    (allow-frame-reordering=false), so each video tag decodes to one picture, in order."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    frames = 0
    try:
        # The transcode (and its socket) starts after detection: wait for it.
        for attempt in range(30):
            try:
                sock.connect(sock_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if attempt == 29 or proc.poll() is not None:
                    raise
                time.sleep(0.5)
        f = sock.makefile("rb", buffering=0)
        for kind, wall, data in onvif_push.read_detect_records(f.read):
            if kind == onvif_push.DETECT_FRAME:
                if keeps(frames, keep):
                    walls.append(wall)
                frames += 1
            proc.stdin.write(data)
    except (OSError, ValueError) as exc:
        log.info("shared decode feed for deviceID=%s ended: %r", device_id, exc)
    finally:
        sock.close()
        try:
            proc.stdin.close()
        except OSError:
            pass
    log.info("shared decode feed for deviceID=%s ended after %d frames", device_id, frames)
