#!/usr/bin/env python3
"""Check that a stretch of an edited video is in step with its source.

    python align_check.py <source.mp4> <edited.mp4> <first frame> <frames> [--cuda]

Compares frames first..first+frames-1 of the edited file with the same frames
of the source shifted by -2..+2, and prints the PSNR for each shift. The shift
with clearly the highest PSNR is where the two line up: 0 means in step.

Frames are picked by counting decoded frames from the start of each file, never
by seeking to a time: files joined from raw pieces (xfade_concat outputs) carry
decode-order timestamps inside B-frame stretches, and a seek by time can land
a frame or two off - in either file. Decoding from the start is slower but
exact; --cuda decodes with NVDEC. Only ffmpeg is needed."""
import argparse
import re
import subprocess

ap = argparse.ArgumentParser()
ap.add_argument("source")
ap.add_argument("edited")
ap.add_argument("first", type=int, help="first frame to compare (0-based)")
ap.add_argument("frames", type=int, help="how many frames")
ap.add_argument("--cuda", action="store_true", help="decode with NVDEC")
ap.add_argument("--ffmpeg", default="ffmpeg")
a = ap.parse_args()

shifts = [-2, -1, 0, 1, 2]
pad = max(abs(s) for s in shifts)
first, n = a.first, a.frames
lo = max(0, first - pad)
small = "scale=1920:-2:flags=area,format=yuv420p"
g = [f"[0:v]trim=start_frame={lo}:end_frame={first + n + pad},{small},"
     f"split={len(shifts)}" + "".join(f"[s{i}]" for i in range(len(shifts))),
     f"[1:v]trim=start_frame={first}:end_frame={first + n},{small},"
     f"split={len(shifts)}" + "".join(f"[e{i}]" for i in range(len(shifts)))]
for i, s in enumerate(shifts):
    st = first + s - lo
    if st < 0:
        continue
    g.append(f"[s{i}]trim=start_frame={st}:end_frame={st + n},setpts=N/25/TB[a{i}]")
    g.append(f"[e{i}]setpts=N/25/TB[b{i}]")
    g.append(f"[a{i}][b{i}]psnr[p{i}]")
hw = ["-hwaccel", "cuda"] if a.cuda else []
cmd = [a.ffmpeg, "-hide_banner", "-nostdin", *hw, "-i", a.source, *hw, "-i", a.edited,
       "-filter_complex", ";".join(g)]
used = [i for i, s in enumerate(shifts) if first + s - lo >= 0]
for i in used:
    cmd += ["-map", f"[p{i}]", "-f", "null", "-"]
# unused split outputs must still be consumed
for i in set(range(len(shifts))) - set(used):
    cmd[cmd.index("-filter_complex") + 1] += f";[s{i}]nullsink;[e{i}]nullsink"
res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
found = re.findall(r"Parsed_psnr_(\d+).*?average:([0-9.inf]+)", res.stderr)
if not found:
    print(res.stderr[-2000:])
    raise SystemExit("ffmpeg gave no PSNR - see the log above")
# psnr filters are numbered in graph order, which is the order of `used`
found.sort(key=lambda x: int(x[0]))
vals = [float(v) if v != "inf" else float("inf") for _, v in found]
best = max(range(len(vals)), key=lambda k: vals[k])
for k, i in enumerate(used):
    mark = "   <- best" if k == best else ""
    print(f"source shifted {shifts[i]:+d} frame(s): PSNR {vals[k]:6.2f} dB{mark}")
print("in step" if shifts[used[best]] == 0 else
      f"NOT in step: the edited file matches the source {shifts[used[best]]:+d} frame(s) away")
