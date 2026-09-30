#!/usr/bin/env python3
"""Check that an mp4's presentation timestamps are right, and that seeking by time is exact.

    python pts_check.py <file.mp4> [--seeks 10] [--cuda] [--ffmpeg ffmpeg] [--ffprobe ffprobe]

Three checks, all on the finished file:

  1. packets   dts strictly increasing, no pts before its dts, and the pts of
               all packets are exactly the frame slots first, first+1, first+2
               ... - each one once. (Decoding order may differ from presentation
               order; nothing may be missing or doubled. `first` is usually 0;
               it is a few ms when the mux delays the video to match the audio's
               encoder priming.)
  2. frames    the decoder hands the frames out with pts first, first+1 ... in
               that order - a frame whose packet carries the wrong pts shows up
               here.
  3. seeking   for frames spread over the file, `-ss` to the frame's own time
               returns that very frame (compared by hash with the same frame
               decoded from the start). A seek by time lands 1-2 frames off in a
               file whose timestamps are in decoding order.

Exits 1 if any check fails. Only ffmpeg and ffprobe are needed. --cuda decodes with
NVDEC, which is what makes this practical on 8K."""
import argparse
import subprocess
import sys
from fractions import Fraction


def probe(ffprobe, path):
    def run(args):
        res = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", *args, str(path)],
                             capture_output=True, text=True)
        if res.returncode:
            sys.exit(res.stderr.strip() or "ffprobe failed")
        return res.stdout
    s = run(["-show_entries", "stream=r_frame_rate,time_base", "-of", "csv=p=0"]).split(",")
    fps, tb = Fraction(s[0]), Fraction(s[1])
    start = run(["-show_entries", "format=start_time", "-of", "csv=p=0"]).strip()
    pk = [ln.split(",") for ln in
          run(["-show_entries", "packet=pts,dts", "-of", "csv=p=0"]).split()]
    return fps, tb, [(int(p), int(d)) for p, d in pk], Fraction(start or 0)


def framemd5(ffmpeg, args, hw):
    res = subprocess.run([ffmpeg, "-v", "error", "-nostdin", *hw, *args, "-map", "0:v:0",
                          "-fps_mode", "passthrough", "-f", "framemd5", "-"],
                         capture_output=True, text=True)
    tb, rows = None, []
    for ln in res.stdout.splitlines():
        if ln.startswith("#tb 0:"):
            tb = Fraction(ln.split(":", 1)[1].strip())
        elif ln and not ln.startswith("#"):
            f = [x.strip() for x in ln.split(",")]
            rows.append((int(f[2]), f[5]))
    return tb, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--seeks", type=int, default=10, help="frames to seek to (default 10)")
    ap.add_argument("--cuda", action="store_true", help="decode with NVDEC")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    a = ap.parse_args()

    fps, tb, packets, file_start = probe(a.ffprobe, a.file)
    n = len(packets)
    slot = (1 / fps) / tb            # ticks per frame, stream time base
    bad = 0

    # 1. packets
    dts = [d for _, d in packets]
    pts = [p for p, _ in packets]
    problems = []
    if any(b <= a_ for a_, b in zip(dts, dts[1:])):
        problems.append("dts is not strictly increasing")
    if any(p < d for p, d in packets):
        problems.append(f"{sum(1 for p, d in packets if p < d)} packets have pts < dts")
    first = min(pts)
    if sorted(pts) != [first + k * slot for k in range(n)]:
        problems.append("pts are not consecutive frame slots, once each")
    print(f"packets: {n}, {sum(1 for p, d in packets if p != d)} with pts != dts"
          f" -> {'FAIL: ' + '; '.join(problems) if problems else 'ok'}")
    bad += bool(problems)

    # 2. decoded frames, in decoder output order
    hw = ["-hwaccel", "cuda"] if a.cuda else []
    ftb, frames = framemd5(a.ffmpeg, ["-i", a.file], hw)
    fslot = (1 / fps) / ftb
    f0 = frames[0][0] if frames else 0
    wrong = [k for k, (p, _) in enumerate(frames) if p != f0 + k * fslot]
    ok = len(frames) == n and not wrong
    print(f"frames: {len(frames)} decoded, "
          + ("pts in order, one frame each -> ok" if ok else
             f"FAIL: {len(wrong)} with the wrong pts, first at frame {wrong[0] if wrong else '-'}"
             + ("" if len(frames) == n else f"; expected {n} frames")))
    bad += not ok

    # 3. seeking by time
    hashes = [h for _, h in frames]
    picks = sorted({min(n - 1, round(i * (n - 1) / max(1, a.seeks - 1)) + i % 3)
                    for i in range(a.seeks)})
    missed = []
    for k in picks:
        # frame k plays at first + k slots; -ss counts from the file's start time
        t = max(0.0, float(first * tb - file_start + (k - Fraction(1, 4)) / fps))
        _, got = framemd5(a.ffmpeg, ["-ss", f"{t:.6f}", "-i", a.file, "-frames:v", "1"], hw)
        if not got or got[0][1] != hashes[k]:
            near = [j for j, h in enumerate(hashes) if got and h == got[0][1]]
            missed.append(f"frame {k} -> got {near[0] if near else '?'}")
    print(f"seeking: {len(picks)} frames tried -> "
          + ("ok" if not missed else f"FAIL: {len(missed)} off ({', '.join(missed[:6])})"))
    bad += bool(missed)
    sys.exit(1 if bad else 0)


main()
