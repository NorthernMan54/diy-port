#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Quick on-device check of a detector backend: probe, load, time, detect.

Prints which backends are usable here, loads one the way avclient.py does
(detectors.create), times inference and lists what it finds in an image. With
--expect it exits non-zero unless that object type is detected, so it doubles as
a pass/fail smoke test after setting a backend up.

  python3 scripts/detector_check.py                                # auto backend, noise frame
  python3 scripts/detector_check.py --backend openvino --image bus.jpg --expect person
  AIPORT_OV_DEVICE=NPU python3 scripts/detector_check.py --backend openvino --runs 100

A test photo with people and a bus (Rockchip's model-zoo sample):
  curl -fsSLO https://raw.githubusercontent.com/airockchip/rknn_model_zoo/v2.3.2/examples/yolov5/model/bus.jpg

--image is read with OpenCV or Pillow when installed, else with ffmpeg (as the
Docker image does), which scales it to the model's input size as avclient.py does.
"""
import argparse
import logging
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "piport"))

import numpy as np  # noqa: E402

import detectors  # noqa: E402


def load_image(path, size):
    """HxWx3 uint8 RGB: OpenCV, else Pillow, else ffmpeg scaled to size x size."""
    try:
        import cv2
        bgr = cv2.imread(path)
        if bgr is None:
            sys.exit(f"error: could not read {path}")
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    except ImportError:
        pass
    try:
        from PIL import Image
        return np.asarray(Image.open(path).convert("RGB"))
    except ImportError:
        pass
    ffmpeg = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("error: reading --image needs opencv-python-headless, Pillow or ffmpeg")
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-vf", f"scale={size}:{size}",
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=False)
    if raw.returncode != 0 or len(raw.stdout) != size * size * 3:
        sys.exit(f"error: ffmpeg could not read {path}: {raw.stderr.decode().strip()}")
    return np.frombuffer(raw.stdout, np.uint8).reshape(size, size, 3)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default=os.environ.get("AIPORT_DETECTOR", "auto"),
                    help=f"auto or one of {', '.join(detectors.BACKENDS)} (default: auto)")
    ap.add_argument("--model", default=os.environ.get("AIPORT_MODEL_PATH"),
                    help="model file (default: the backend's default in piport/models/)")
    ap.add_argument("--image", help="photo to run detection on (default: a noise frame)")
    ap.add_argument("--runs", type=int, default=30, help="timed inferences (default: 30)")
    ap.add_argument("--expect", choices=["person", "vehicle", "animal"],
                    help="exit 1 unless this object type is detected")
    ap.add_argument("-v", "--verbose", action="store_true", help="show backend logs")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    print("backend availability (what 'auto' sees):")
    for name, (ok, why) in detectors.probe(args.model).items():
        print(f"  {name:9} {'yes' if ok else 'no ':3}  {why}")

    t0 = time.monotonic()
    det = detectors.create(args.backend, args.model)
    load_s = time.monotonic() - t0
    where = getattr(det, "device", None) or getattr(det, "provider", None) or "-"
    print(f"\nloaded {det.NAME!r} on {where} in {load_s:.2f}s "
          f"({det.model_path}, input {det.input_size}x{det.input_size})")

    if args.image:
        frame = load_image(args.image, det.input_size)
    else:
        frame = np.random.default_rng(0).integers(0, 256, (det.input_size, det.input_size, 3),
                                                  dtype=np.uint8)
    det.detect(frame)  # warm-up: first GPU/NPU inference includes one-off setup
    times = []
    for _ in range(max(1, args.runs)):
        t = time.perf_counter()
        results = det.detect(frame)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
    print(f"detect() on {frame.shape[1]}x{frame.shape[0]}: mean {sum(times) / len(times):.1f} ms, "
          f"p95 {p95:.1f} ms over {len(times)} runs (resize + inference + decode)")

    print(f"\n{len(results)} detection(s){'' if args.image else ' (noise frame: expect none)'}:")
    for r in results:
        x1, y1, x2, y2 = (round(v) for v in r["box"])
        print(f"  {r['objectType']:8} {r['label']:12} {r['score']:.2f}  ({x1},{y1})-({x2},{y2})")
    if args.expect and not any(r["objectType"] == args.expect for r in results):
        print(f"\nFAIL: no {args.expect} detected")
        return 1
    if args.expect:
        print(f"\nOK: {args.expect} detected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
