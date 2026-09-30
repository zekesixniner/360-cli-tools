#!/usr/bin/env python3
"""Check pieces.py's picture-order reader against the decoder, on any HEVC stream.

    python order_check.py <piece.hevc | clip.mp4>

The join writes presentation times that come from the picture order counts
(POC) in the slice headers (pieces.PictureScanner). This runs the same reader
over one stream and compares what it says with ffmpeg's own decoder: for every
picture, where does the decoder output it? They must agree exactly. Point it at
a piece kept with --keep-work, or at a camera/OVRLEY file, to vouch for a
bitstream the synthetic test clips do not cover (other encoders, slices,
temporal layers). An .mp4 is read as it is for the decoder's side and copied out
to raw Annex B for the reader's, which is how the pieces are stored. Needs
ffmpeg and ffprobe on PATH; the file is treated as one piece, so it must start
with a keyframe."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pieces  # noqa: E402


def decoder_ranks(path):
    """rank[i] = output position of the i-th picture in decoding order, as the
    decoder sees it. A frame's packet position in the file is its decoding order."""
    res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "frame=pkt_pos", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    pos = [int(x.rstrip(",")) for x in res.stdout.split()]
    decode_index = {p: i for i, p in enumerate(sorted(pos))}
    ranks = [0] * len(pos)
    for out_index, p in enumerate(pos):
        ranks[decode_index[p]] = out_index
    return ranks


def scanner_ranks(path):
    scan = pieces.PictureScanner()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            scan.feed(chunk)
    scan.feed(b"", final=True)
    return pieces.display_ranks(scan.pics), scan


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as tmp:
        raw = src
        if src.suffix.lower() in (".mp4", ".mov", ".m4v"):
            raw = Path(tmp) / "video.hevc"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-i", str(src), "-map", "0:v:0",
                            "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(raw)],
                           check=True)
        truth = decoder_ranks(src)
        try:
            mine, scan = scanner_ranks(raw)
        except ValueError as e:
            sys.exit(f"the reader gave up: {e}")
    delay = max((i - r for i, r in enumerate(mine)), default=0)
    print(f"{len(mine)} pictures, {scan.sequences} coded video sequence(s), reorder delay {delay}")
    if mine == truth:
        print("agrees with the decoder on every picture")
        return
    bad = [i for i, (a, b) in enumerate(zip(mine, truth)) if a != b]
    sys.exit(f"DISAGREES with the decoder on {len(bad)} of {len(truth)} pictures "
             f"(first: picture {bad[0]} in decoding order, reader {mine[bad[0]]}, "
             f"decoder {truth[bad[0]]})" if len(mine) == len(truth)
             else f"DISAGREES: the reader found {len(mine)} pictures, the decoder {len(truth)}")


main()
