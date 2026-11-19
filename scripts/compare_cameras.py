#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The DIY-Port project
"""Compare cameras side by side from diy-port logs, e.g. one camera's old feed and its ONVIF
replacement on two instances. For each camera, over the last --minutes: objects reported to
Protect (distinct tracks by type), plates read, frame rates, time per detection pass, minutes
falling behind, and stream trouble. Appends one line per camera to --out.

  scripts/compare_cameras.py native:AABBCC000001:relayed onvif:AABBCC000002:onvif

Each camera is instance:deviceID[:label]; the instance's log is
~/.diy-port/<instance>/logs/diyport.log. Standard library only, so Apple's python3 runs it.
"""
import argparse
import ast
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

FRAMES = re.compile(r"frames for deviceID=(\w+) over \d+ s: \d+ delivered \(([\d.]+)/s\), "
                    r"\d+ analysed \(([\d.]+)/s\).*?([\d.]+) ms a pass")
PLATE = re.compile(r"plate '(\w+)' for vehicle .*deviceID=(\w+)")
EVENT = re.compile(r"sent EventSmartDetect \(id=\d+\) payload=(\{.*\})\s*$")
TROUBLE = re.compile(r"not streaming|pull died|exited rc=|attempt (?:[2-9]|\d\d)")


def scan(path, since, ids):
    stats = {i: {"tracks": set(), "plates": set(), "frames": [], "behind": 0, "trouble": 0}
             for i in ids}
    f = open(path, errors="replace")
    with f:
        for line in f:
            if line[:19] < since or "deviceID" not in line:
                continue
            m = FRAMES.search(line)
            if m and m.group(1) in stats:
                s = stats[m.group(1)]
                s["frames"].append(tuple(float(x) for x in m.group(2, 3, 4)))
                s["behind"] += " WARNING " in line
                continue
            m = PLATE.search(line)
            if m and m.group(2) in stats:
                stats[m.group(2)]["plates"].add(m.group(1))
                continue
            m = EVENT.search(line)
            if m:
                try:
                    p = ast.literal_eval(m.group(1))
                except (ValueError, SyntaxError):
                    continue
                if p.get("deviceID") in stats:
                    for d in p.get("descriptors", []):
                        stats[p["deviceID"]]["tracks"].add((d.get("objectType"), d.get("trackerID")))
                continue
            if TROUBLE.search(line):
                for i in stats:
                    if i in line:
                        stats[i]["trouble"] += 1
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cameras", nargs="+", help="instance:deviceID[:label]")
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--out", default=os.path.expanduser("~/.diy-port/compare.log"))
    args = ap.parse_args()

    now = datetime.now()
    since = (now - timedelta(minutes=args.minutes)).strftime("%Y-%m-%d %H:%M:%S")
    by_instance = {}
    for spec in args.cameras:
        inst, dev, *label = spec.split(":")
        by_instance.setdefault(inst, []).append((dev, label[0] if label else dev))

    lines = []
    for inst, cams in by_instance.items():
        log = os.path.expanduser(f"~/.diy-port/{inst}/logs/diyport.log")
        try:
            stats = scan(log, since, [d for d, _ in cams])
        except OSError as e:
            print(f"compare_cameras: {e}", file=sys.stderr)
            continue
        for dev, label in cams:
            s = stats[dev]
            kinds = Counter(t for t, _ in s["tracks"])
            objs = " ".join(f"{k}={kinds[k]}" for k in sorted(kinds, key=str)) or "none"
            if s["frames"]:
                n = len(s["frames"])
                fps, done, ms = (sum(v[i] for v in s["frames"]) / n for i in range(3))
                frames = f"fps {fps:.1f} analysed {done:.1f} pass {ms:.0f}ms behind {s['behind']}/{n}min"
            else:
                frames = "no frame stats (not streaming?)"
            plates = ",".join(sorted(s["plates"])) or "-"
            lines.append(f"{now:%Y-%m-%d %H:%M} {label:<8} objects {objs} | plates {plates} | "
                         f"{frames} | trouble {s['trouble']}")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "a") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
