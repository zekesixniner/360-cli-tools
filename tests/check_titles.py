#!/usr/bin/env python3
"""Compare a titles_in_360 output with its source, frame by frame.

    python tests/check_titles.py film.mp4 out.mp4

Prints the runs of frames that are bit-identical to the source (the copied
pieces) and, for four regions (ahead, right, left, behind across the seam),
the frames where a sign is visible and how its opacity ramps up. Made for
tests/titles_check.txt on film.mp4 from tests/make_clips.sh; needs numpy."""
import json
import subprocess
import sys

import numpy as np


def frames(path):
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", path], capture_output=True, text=True).stdout)
    w, h = info["streams"][0]["width"], info["streams"][0]["height"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-fps_mode", "passthrough",
                          "-f", "rawvideo", "-pix_fmt", "gray16le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.uint16).reshape(-1, h, w), w, h


def runs(ix):
    out = []
    for i in ix:
        if out and i == out[-1][1] + 1:
            out[-1][1] = i
        else:
            out.append([i, i])
    return out


a, w, h = frames(sys.argv[1])
b, _, _ = frames(sys.argv[2])
print("frames", len(a), len(b))
band = slice(h * 440 // 960, h * 520 // 960)
regions = {"ahead (yaw 0)": (band, slice(w * 760 // 1920, w * 1160 // 1920)),
           "right (yaw 90)": (band, slice(w * 1300 // 1920, w * 1580 // 1920)),
           "left (yaw -90)": (band, slice(w * 340 // 1920, w * 620 // 1920)),
           "behind (yaw 180, seam)": (band, slice(0, w * 150 // 1920))}
exact = [i for i in range(min(len(a), len(b))) if np.array_equal(a[i], b[i])]
print("bit-identical frames:", runs(exact))
for name, (ys, xs) in regions.items():
    d = [float(np.abs(a[i][ys, xs].astype(int) - b[i][ys, xs].astype(int)).mean()) / 64
         for i in range(min(len(a), len(b)))]
    on = [i for i, v in enumerate(d) if v > 5]
    ramp = [round(d[i], 1) for i in range(max(0, on[0] - 1), on[0] + 12)] if on else []
    print(f"{name}: sign visible in frames {runs(on)}; ramp {ramp}")
