"""pieces.py - shared engine for the 360-cli-tools scripts.

xfade_concat.py and titles_in_360.py both edit long 8K HEVC videos without
re-encoding all of them. They share one idea, implemented here once:

  * probe a source without decoding it (frame count, edit-list shift, colour)
  * find keyframes near the cut points by streaming headers through
    trace_headers - a keyframe is a safe, frame-exact cut point only if no
    leading pictures (RADL/RASL) follow it
  * cut the video into pieces: stretches between two safe keyframes are
    stream-copied, everything else is re-encoded with NVENC (or libx265)
  * write every piece as raw Annex B (.hevc), carrying its own parameter sets
    in-band - no container, so no timestamps to rebase and no edit lists to
    inherit
  * join the pieces by plain byte concatenation into one final mux that
    re-derives all timing at a constant frame rate, then count the frames that
    actually play

This module is not run on its own. Keep it next to the scripts that import it.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

__version__ = "1.0.0"

RENUMBER = "setpts=N/FRAME_RATE/TB"  # hw-frame safe (touches timestamps only)

# HEVC NAL unit types
NAL_LEADING = {6, 7, 8, 9}  # RADL/RASL: decoded after an IRAP but displayed before it
NAL_IRAP = {16, 17, 18, 19, 20, 21}  # BLA, IDR, CRA

# H.273 code points for the hevc_metadata bitstream filter
PRIMARIES = {"bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
             "film": 8, "bt2020": 9, "smpte428": 10, "smpte431": 11, "smpte432": 12}
TRANSFER = {"bt709": 1, "gamma22": 4, "gamma28": 5, "smpte170m": 6, "smpte240m": 7,
            "linear": 8, "iec61966-2-4": 11, "bt1361e": 12, "iec61966-2-1": 13,
            "bt2020-10": 14, "bt2020-12": 15, "smpte2084": 16, "arib-std-b67": 18}
MATRIX = {"rgb": 0, "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
          "ycgco": 8, "bt2020nc": 9, "bt2020c": 10}

# --------------------------------------------------------------------------- #
# messages (EN/SV, --lang or GOPRO_LANG like gopro-max-gpx-pipeline)
#
# The keys below are the ones the engine itself uses. Each script adds its own
# with register(); a script's key wins over one defined here.
# --------------------------------------------------------------------------- #
MESSAGES = {
    "en": {
        "cached": "  (cached)",
        "note_360": "Note: 2:1 frame - run inject360-inplace on the output as the last step.",
        "warn_vfr": "warning: {name} looks VFR (r={r}, avg={a}); cuts may drift",
        "warn_no_nvenc": "warning: ffmpeg has no hevc_nvenc -> using libx265 (CPU, slow)",
        "warn_nb_frames": "warning: {name}: container claims {tag} frames but only {real} are "
                          "actually shown - likely cut mid-GOP from a master (hidden pre-roll "
                          "behind an edit list); using the real, playable count",
        "warn_dts": "warning: the join reported timestamp problems:\n{log}",
        "err_ffmpeg": "ffmpeg failed (exit {code}):\n  {cmd}",
        "err_ffprobe": "ffprobe failed on {path}:\n{err}",
        "err_not_found": "'{exe}' not found (add C:\\ffmpeg\\bin to PATH or pass --ffmpeg/--ffprobe)",
        "err_exists": "{out} exists (use -y to overwrite)",
        "err_no_match": "no files match '{pat}'",
        "err_not_file": "not a file: {path}",
        "err_no_video": "no video stream in {path}",
        "err_time": "invalid {what} value '{val}' (use seconds like 1.5 or frames like 45f)",
        "err_frames": "{name}: expected {want} frames, got {got}",
    },
    "sv": {
        "cached": "  (cachad)",
        "note_360": "Obs: 2:1-bild - kör inject360-inplace på resultatet som sista steg.",
        "warn_vfr": "varning: {name} verkar ha variabel bildfrekvens (r={r}, avg={a}); klipp kan glida",
        "warn_no_nvenc": "varning: ffmpeg saknar hevc_nvenc -> använder libx265 (CPU, långsamt)",
        "warn_nb_frames": "varning: {name}: behållaren uppger {tag} rutor men bara {real} visas "
                          "faktiskt - troligen klippt mitt i en GOP från ett master (dolt förspel "
                          "bakom en edit-list); använder det riktiga, spelbara antalet",
        "warn_dts": "varning: skarvningen rapporterade tidsstämpelproblem:\n{log}",
        "err_ffmpeg": "ffmpeg misslyckades (felkod {code}):\n  {cmd}",
        "err_ffprobe": "ffprobe misslyckades för {path}:\n{err}",
        "err_not_found": "hittar inte '{exe}' (lägg C:\\ffmpeg\\bin i PATH eller ange --ffmpeg/--ffprobe)",
        "err_exists": "{out} finns redan (använd -y för att skriva över)",
        "err_no_match": "inga filer matchar '{pat}'",
        "err_not_file": "ingen fil: {path}",
        "err_no_video": "ingen videoström i {path}",
        "err_time": "ogiltigt värde för {what}: '{val}' (sekunder som 1.5 eller rutor som 45f)",
        "err_frames": "{name}: väntade {want} rutor, fick {got}",
    },
}
LANG = "en"


def register(messages: dict) -> None:
    """Add a script's own messages (same {'en': {...}, 'sv': {...}} shape)."""
    for lang, table in messages.items():
        MESSAGES.setdefault(lang, {}).update(table)


def default_lang() -> str:
    env = os.environ.get("GOPRO_LANG", "en").lower()
    return env if env in MESSAGES else "en"


def set_lang(lang: str) -> None:
    global LANG
    LANG = lang


def t(key: str, **kw) -> str:
    return MESSAGES.get(LANG, MESSAGES["en"]).get(key, MESSAGES["en"][key]).format(**kw)


def warn(key: str, **kw) -> None:
    print(t(key, **kw), file=sys.stderr)


def die(key: str, **kw) -> None:
    prefix = "fel" if LANG == "sv" else "error"
    print(f"{prefix}: {t(key, **kw)}", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def fmt_cmd(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def run(cmd: list[str], verbose: bool) -> None:
    if verbose:
        print("  $ " + fmt_cmd(cmd))
    res = subprocess.run(cmd)
    if res.returncode != 0:
        die("err_ffmpeg", code=res.returncode, cmd=fmt_cmd(cmd))


def secs(frames: int, fps: Fraction) -> float:
    return float(Fraction(frames) / fps)


def fsec(frames: int, fps: Fraction) -> str:
    return f"{secs(frames, fps):.9f}"


def hms(seconds: float) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:06.3f}"


def clock(seconds: float) -> str:
    """HH:MM:SS for list files, with decimals only when they matter."""
    h, rem = divmod(float(seconds), 3600)
    m, sec = divmod(rem, 60)
    if abs(sec - round(sec)) < 5e-4:
        return f"{int(h):02d}:{int(m):02d}:{int(round(sec)):02d}"
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}".rstrip("0")


def parse_time(value: str, fps: Fraction, what: str) -> int:
    """'1.5' = seconds, '45f' = frames. Returns frames."""
    v = str(value).strip().lower()
    try:
        n = int(v[:-1]) if v.endswith("f") else round(Fraction(v) * fps)
    except (ValueError, ZeroDivisionError):
        die("err_time", what=what, val=value)
    if n < 0:
        die("err_time", what=what, val=value)
    return n


def parse_clock(value: str) -> str:
    """Accept 12, 1:30, 00:04:30.5 - returns plain seconds as a string."""
    v = str(value).strip()
    if ":" not in v:
        return v
    total = Fraction(0)
    for part in v.split(":"):
        total = total * 60 + Fraction(part or "0")
    return str(float(total))


def tokenize(line: str, keep_quoted: bool = False, hash_in_word: bool = False) -> list:
    """Split a list-file line on whitespace, honouring quotes.

    A quote opens only at the start of a token or straight after '=', so
    title="Take off ESMK" is one token while an apostrophe inside a word
    (Peter's) is just a character. Backslashes are literal, for Windows paths.
    '#' outside quotes starts a comment.

    With keep_quoted=True every token comes back as (text, quoted), where
    quoted tells whether the token started with a quote - which is how a
    quoted "text" is told apart from a bare key=value option.

    With hash_in_word=True a '#' only starts a comment at the start of a token
    and when no digit follows it, so colours (color=#ffd700) and clip numbers
    (#3@0:05) survive."""
    toks: list = []
    cur, quote, quoted = "", None, False
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "\"'" and (cur == "" or cur.endswith("=")):
            quote = ch
            quoted = quoted or cur == ""
        elif ch == "#" and not (hash_in_word and (cur or quoted)):
            if hash_in_word and line[i + 1:i + 2].isdigit():
                cur += ch
                continue
            break
        elif ch.isspace():
            if cur or (quoted and keep_quoted):
                toks.append((cur, quoted) if keep_quoted else cur)
            cur, quoted = "", False
        else:
            cur += ch
    if cur or (quoted and keep_quoted):
        toks.append((cur, quoted) if keep_quoted else cur)
    return toks


def expand_inputs(items: list[str]) -> list[Path]:
    """PowerShell does not expand wildcards for native programs - do it here."""
    out: list[Path] = []
    for item in items:
        if any(ch in item for ch in "*?["):
            matches = sorted(glob.glob(item))
            if not matches:
                die("err_no_match", pat=item)
            out.extend(Path(m) for m in matches)
        else:
            out.append(Path(item))
    return out


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #
@dataclass
class Keyframe:
    idx: int  # frame index (presentation order)
    pts: int  # ticks, source stream time base
    delay: int  # pts - dts in frames
    safe: bool = True  # no leading pictures -> frame-exact cut point


@dataclass
class Source:
    """What probing learns about one video file."""
    path: Path
    frames: int
    fps: Fraction
    tb: Fraction
    width: int
    height: int
    pix_fmt: str
    codec: str
    v_start: float
    f_start: float
    audio: list[dict]
    color: dict
    raw_offset: float = 0.0  # raw_time = edited_time + raw_offset (edit-list shift)
    keyframes: dict = field(default_factory=dict)  # idx -> Keyframe

    @property
    def fdur_ticks(self) -> Fraction:
        return (1 / self.fps) / self.tb

    def smart_ok(self) -> bool:
        """Can pieces of this file be stream-copied and joined with our own?"""
        return self.codec == "hevc" and self.pix_fmt in ("yuv420p", "yuv420p10le", "yuvj420p")


def ffprobe_json(ffprobe: str, args: list[str], path: Path) -> dict:
    cmd = [ffprobe, "-v", "error", *args, "-of", "json", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        die("err_ffprobe", path=path, err=res.stderr.strip())
    return json.loads(res.stdout)


def probe_source(ffprobe: str, path: Path, cls=Source):
    """Probe a file without decoding it. `cls` may be a subclass of Source."""
    if not path.is_file():
        die("err_not_file", path=path)
    info = ffprobe_json(
        ffprobe,
        ["-show_entries",
         "format=start_time,duration:stream=index,codec_type,codec_name,width,height,pix_fmt,"
         "r_frame_rate,avg_frame_rate,time_base,nb_frames,start_time,color_range,"
         "color_space,color_transfer,color_primaries,channels,channel_layout"],
        path)
    streams = info.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video:
        die("err_no_video", path=path)
    v = video[0]
    fps = Fraction(v["r_frame_rate"])
    avg = v.get("avg_frame_rate", "0/0")
    if avg not in ("0/0", v["r_frame_rate"]) and abs(float(Fraction(avg) - fps)) > 0.01:
        warn("warn_vfr", name=path.name, r=fps, a=Fraction(avg))
    # An edit list shifts the timeline the demuxer reports. Two things follow
    # from it, and both matter:
    #   * the container's frame tag counts the hidden pre-roll as well, so it
    #     overstates how many frames actually play;
    #   * a plain -ss becomes ambiguous, because the same timestamp can resolve
    #     to the pre-roll keyframe instead of the intended one.
    # The first packet's timestamp gives the shift directly (it is negative by
    # exactly the hidden part), so both fall out of one cheap probe - no need to
    # decode the clip to count it, which on 8K footage takes minutes per file.
    def first_pts(extra: list[str]) -> float | None:
        j = ffprobe_json(ffprobe, [*extra, "-select_streams", "v:0",
                                   "-read_intervals", "%+#1",
                                   "-show_entries", "packet=pts_time"], path)
        pk = (j.get("packets") or [{}])[0].get("pts_time")
        return float(pk) if pk not in (None, "N/A") else None

    edited_first = first_pts([])
    raw_offset = 0.0
    if edited_first is not None and edited_first < 0:
        # Only an edit list can push the first timestamp negative, and only then
        # is a second probe worth its process start-up.
        raw_first = first_pts(["-ignore_editlist", "1"])
        if raw_first is not None:
            raw_offset = raw_first - edited_first

    tag = int(v.get("nb_frames") or 0)
    preroll = round(-edited_first * fps) if edited_first and edited_first < 0 else 0
    frames = tag - preroll
    if frames <= 0:
        # No usable tag (or something unexpected): count packets the slow-but-sure
        # way. Still demux-only - the pre-roll is exactly the run of packets the
        # edit list pushes to a negative timestamp.
        res = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True)
        frames = sum(1 for x in res.stdout.split()
                     if x and x[0] != "N" and float(x.rstrip(",")) >= -1e-6)
    if preroll:
        warn("warn_nb_frames", name=path.name, tag=tag, real=frames)

    color = {k: v[k] for k in ("color_range", "color_space", "color_transfer",
                               "color_primaries") if v.get(k) and v[k] != "unknown"}
    return cls(
        path=path.resolve(), frames=frames, fps=fps, tb=Fraction(v["time_base"]),
        width=int(v["width"]), height=int(v["height"]), pix_fmt=v.get("pix_fmt", ""),
        codec=v.get("codec_name", ""),
        v_start=float(v.get("start_time") or 0.0),
        f_start=float(info.get("format", {}).get("start_time") or 0.0),
        audio=[s for s in streams if s.get("codec_type") == "audio"],
        color=color, raw_offset=raw_offset)


def scan_keyframes(ffmpeg: str, clip: Source, start_s: float, dur_s: float) -> None:
    """Stream-copy a window through trace_headers (no decoding) and record keyframes.

    A keyframe (IRAP) is a safe, frame-exact cut point only if no leading pictures
    (RADL/RASL) follow it: those are decoded after it but belong to the previous
    stretch in display order (open GOP, IDR_W_RADL with leading pictures)."""
    seek = max(0.0, start_s - clip.f_start)
    # -to, not -t: with -copyts the timestamps stay absolute, so a duration limit
    # would be measured against them and cut the scan short (or drop it entirely)
    # for any window that does not start near the beginning of the file.
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "debug", "-ss", f"{seek:.6f}", "-copyts",
           "-i", str(clip.path), "-to", f"{seek + dur_s:.6f}", "-map", "0:v:0", "-c", "copy",
           "-bsf:v", "trace_headers", "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    pkt_re = re.compile(r"Packet: \d+ bytes, (key frame, )?pts (-?\d+|NOPTS), dts (-?\d+|NOPTS)")
    nal_re = re.compile(r"nal_unit_type: (\d+)\(")
    cur = None  # [key, pts, dts, first_vcl_type]
    tracking: Keyframe | None = None
    for line in res.stderr.splitlines():
        if "trace_headers" not in line:
            continue
        m = pkt_re.search(line)
        if m:
            key = bool(m.group(1))
            if m.group(2) == "NOPTS" or m.group(3) == "NOPTS":
                cur = None
                continue
            cur = [key, int(m.group(2)), int(m.group(3)), None]
            continue
        m = nal_re.search(line)
        if not m or cur is None or cur[3] is not None:
            continue
        nal = int(m.group(1))
        if nal >= 32:  # parameter sets / SEI
            continue
        cur[3] = nal
        key, pts, dts = cur[0], cur[1], cur[2]
        if nal in NAL_LEADING:
            if tracking is not None:
                tracking.safe = False
        elif nal not in NAL_IRAP:  # first trailing picture ends the leading run
            tracking = None
        if key and nal in NAL_IRAP:
            idx = round((float(pts * clip.tb) - clip.v_start) * clip.fps)
            kf = Keyframe(idx=idx, pts=pts, delay=round((pts - dts) / clip.fdur_ticks))
            clip.keyframes[idx] = kf
            tracking = kf


def scan_spans(ffmpeg: str, clip: Source, spans: list[tuple[float, float]]) -> None:
    """Scan several (start_s, end_s) windows, merging the ones that overlap."""
    merged: list[list[float]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    for lo, hi in merged:
        scan_keyframes(ffmpeg, clip, lo, hi - lo)


def safe_keyframes(clip: Source) -> list[int]:
    return sorted(k for k, kf in clip.keyframes.items() if kf.safe)


# --------------------------------------------------------------------------- #
# encoding
# --------------------------------------------------------------------------- #
class Encoder:
    """Encoder settings shared by every re-encoded piece, plus the commands that
    are the same in every tool: seeking into a source, getting frames from the
    GPU to the CPU, copying a keyframe-to-keyframe stretch, and writing a piece."""

    def __init__(self, args, clips: list[Source], smart: bool):
        c0 = clips[0]
        self.args = args
        self.fps = c0.fps
        self.smart = smart
        depth = args.bit_depth
        if depth == "auto":
            depth = "10" if ("10" in c0.pix_fmt or "p010" in c0.pix_fmt) else "8"
        self.depth = int(depth)
        self.hw = not args.cpu_decode
        self.planar = "yuv420p10le" if self.depth == 10 else "yuv420p"
        self.hwfmt = "p010le" if self.depth == 10 else "nv12"
        self.encfmt = self.hwfmt if args.encoder == "nvenc" else self.planar

        # MP4 timescale: source time base in smart mode (exact copy), frame-exact always
        num, den = self.fps.numerator, self.fps.denominator
        ts = 1
        for c in clips:
            ts = math.lcm(ts, c.tb.denominator)
        if (ts * den) % num:
            ts = math.lcm(ts, num)
        if ts < 10000:
            ts *= math.ceil(10000 / ts)
        self.timescale = ts
        bframes = 0 if smart else args.bframes
        gop = args.gop if args.gop else round(2 * self.fps)

        if args.encoder == "nvenc":
            a = ["-c:v", "hevc_nvenc", "-preset", args.preset, "-tune", "hq",
                 "-rc", "vbr", "-cq", str(args.cq), "-b:v", "0", "-bf", str(bframes),
                 "-profile:v", "main10" if self.depth == 10 else "main"]
            if not self.hw:
                a += ["-pix_fmt", self.encfmt]
        else:
            a = ["-c:v", "libx265", "-preset", args.x265_preset, "-crf", str(args.cq),
                 "-x265-params", f"bframes={bframes}:log-level=error",
                 "-pix_fmt", self.encfmt]
        a += ["-g", str(gop)]
        for k, flag in {"color_range": "-color_range", "color_space": "-colorspace",
                        "color_transfer": "-color_trc",
                        "color_primaries": "-color_primaries"}.items():
            if k in c0.color:
                a += [flag, c0.color[k]]
        if args.encode_extra:
            a += shlex.split(args.encode_extra, posix=(os.name != "nt"))
        self.video_args = a

        # pin colour VUI so frame props cannot make pieces' SPS differ (2 = unspecified)
        col = c0.color
        vui = ":".join(f"{k}={v}" for k, v in {
            "colour_primaries": PRIMARIES.get(col.get("color_primaries"), 2),
            "transfer_characteristics": TRANSFER.get(col.get("color_transfer"), 2),
            "matrix_coefficients": MATRIX.get(col.get("color_space"), 2),
            "video_full_range_flag": 1 if col.get("color_range") == "pc" else 0}.items())
        # Pieces are written as raw Annex B, which has no container timestamps and
        # no edit lists at all - so parameter sets must travel in-band, repeated at
        # every keyframe, and the final mux re-derives all timing from the stream.
        self.enc_bsf = f"hevc_metadata={vui},dump_extra=freq=keyframe"
        self.tag = "hev1"  # parameter sets change mid-stream at the joins

    def base(self) -> list[str]:
        return [self.args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-stats", "-y"]

    def input_args(self, clip: Source, frame: int) -> list[str]:
        a: list[str] = []
        if self.hw:
            a += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        if frame > 0:
            # half a frame early: the accurate-seek trim then keeps exactly `frame`
            t0 = (clip.v_start - clip.f_start) + (frame - 0.5) / float(clip.fps)
            a += ["-ss", f"{t0:.6f}"]
        return a + ["-i", str(clip.path)]

    def to_cpu(self) -> str:
        if self.hw:
            return f"hwdownload,format={self.hwfmt},format={self.planar}"
        return f"format={self.planar}"

    def copy_cmd(self, clip: Source, frame: int, frames: int, out: Path) -> list[str]:
        """Straight bitstream copy of `frames` frames from the safe keyframe at
        `frame` into raw Annex B: no container means no timestamps to rebase and
        no edit list to inherit, so the frames come through exactly as they are
        in the source."""
        kf = clip.keyframes.get(frame)
        pre = ["-ignore_editlist", "1"]
        if frame and kf is not None:
            # half a frame past the keyframe, in raw time: unambiguous even
            # when a hidden pre-roll keyframe sits earlier in the timeline
            seek = float(kf.pts * clip.tb) + clip.raw_offset + 0.5 / float(clip.fps)
            pre += ["-ss", f"{seek:.6f}"]
        cmd = self.base() + pre + [
            "-i", str(clip.path), "-map", "0:v:0", "-frames:v", str(frames),
            "-c", "copy", "-bsf:v", "hevc_mp4toannexb"]
        return cmd + ["-an", "-sn", "-dn", "-f", "hevc", str(out)]

    def encode_tail(self, frames: int, out: Path) -> list[str]:
        """Output half of a re-encode command: exactly `frames` frames as Annex B."""
        return ["-frames:v", str(frames), "-an", "-sn", "-dn", *self.video_args,
                "-bsf:v", self.enc_bsf, "-f", "hevc", str(out)]


def annexb_frames(path: Path) -> int:
    """Count frames in a raw Annex B piece, exactly and in one pass.

    A picture starts at the VCL NAL unit whose first_slice_segment_in_pic_flag
    is set - the top bit of the first byte after the 2-byte NAL header. Scanning
    for start codes with bytes.find runs at memchr speed; letting ffprobe count
    packets instead parses every NAL and is ~20x slower, which on 8K pieces is
    minutes rather than seconds across a whole job."""
    count = 0
    carry = b""
    base = 0   # absolute offset of carry[0]
    done = 0   # absolute offset already accounted for
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 23)
            if not chunk:
                break
            buf = carry + chunk
            i = 0
            while True:
                i = buf.find(b"\x00\x00\x01", i)
                if i < 0 or i + 6 > len(buf):
                    break
                if base + i >= done:
                    if ((buf[i + 3] >> 1) & 0x3F) < 32 and (buf[i + 5] & 0x80):
                        count += 1
                    done = base + i + 1
                i += 3
            keep = min(len(buf), 6)
            carry = buf[-keep:]
            base += len(buf) - keep
    return count


def render_pieces(pieces: list, make_cmd, label, verbose: bool) -> None:
    """Render pieces serially (one NVDEC/NVENC chip; parallel jobs corrupt frames).

    Each piece needs .file (final path) and .frames. A piece whose file already
    holds the right number of frames is reused, so an interrupted run resumes
    where it stopped."""
    for idx, p in enumerate(pieces):
        head = f"[{idx + 1}/{len(pieces)}] {label(p)}"
        if p.file.exists() and annexb_frames(p.file) == p.frames:
            print(head + t("cached"))
            continue
        print(head)
        tmp = p.file.with_name(p.file.stem + ".partial.hevc")
        run(make_cmd(p, tmp), verbose)
        got = annexb_frames(tmp)
        if got != p.frames:
            print(t("err_frames", name=tmp.name, want=p.frames, got=got), file=sys.stderr)
            sys.exit(1)
        os.replace(tmp, p.file)


def join_pieces(ffmpeg: str, files: list[Path], fps: Fraction, enc: Encoder, output: Path,
                extra_inputs: list[str], maps: list[str], verbose: bool) -> None:
    """Join raw Annex B pieces by plain byte concatenation.

    Streaming them into ffmpeg's stdin avoids writing a second copy of the whole
    video to disk, and avoids a command line with one argument per piece. ffmpeg
    re-derives every timestamp from the bitstream at a constant frame rate,
    which is what makes the result frame-exact regardless of how the individual
    pieces were produced. `extra_inputs` are further inputs (e.g. audio), and
    `maps` the -map arguments for the whole output."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y",
           "-fflags", "+genpts", "-f", "hevc", "-r", str(fps), "-i", "pipe:0"]
    cmd += extra_inputs + maps
    # avoid_negative_ts make_zero: genpts can hand the first packet a negative
    # timestamp, and the mp4 muxer would then write an edit list that hides that
    # frame from playback - the file would claim the right frame count while
    # presenting one fewer.
    cmd += ["-c", "copy", "-tag:v", enc.tag, "-avoid_negative_ts", "make_zero",
            "-video_track_timescale", str(enc.timescale), "-f", "mp4", str(output)]
    if verbose:
        print("  $ cat <pieces> | " + fmt_cmd(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for f in files:
            with open(f, "rb") as fh:
                shutil.copyfileobj(fh, proc.stdin, 1 << 20)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode("utf-8", "replace")
    if proc.wait() != 0:
        print(err, file=sys.stderr)
        die("err_ffmpeg", code=proc.returncode, cmd=fmt_cmd(cmd))
    ts_issues = [ln for ln in err.splitlines() if "monoton" in ln.lower()]
    if ts_issues:
        warn("warn_dts", log="\n".join(ts_issues[:10]))


def played_frames(ffprobe: str, path: Path) -> int:
    """Count what actually plays, not what the container claims: a frame hidden
    behind an edit list still shows up in nb_frames."""
    res = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True)
    return sum(1 for x in res.stdout.split()
               if x and x[0] != "N" and float(x.rstrip(",")) >= -1e-6)


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
def add_encoding_args(ap, mode_help: str):
    """The 'mode / video encoding' option group, identical in every tool."""
    g = ap.add_argument_group("mode / video encoding")
    g.add_argument("--mode", choices=["smart", "full"], default="smart", help=mode_help)
    g.add_argument("--gop-window", type=float, default=30.0,
                   help="seconds to search for an IDR keyframe near each cut (default 30)")
    g.add_argument("--min-copy", default="2",
                   help="shortest stretch worth stream-copying (default 2 s)")
    g.add_argument("--encoder", choices=["auto", "nvenc", "x265"], default="auto",
                   help="auto = hevc_nvenc if available, else libx265 (CPU)")
    g.add_argument("--cq", type=int, default=15, help="NVENC -cq / x265 -crf (default 15)")
    g.add_argument("--preset", default="p7", help="NVENC preset p1..p7 (default p7)")
    g.add_argument("--x265-preset", default="medium")
    g.add_argument("--bframes", type=int, default=0, help="B-frames, full mode only (default 0)")
    g.add_argument("--gop", type=int, default=0, help="GOP length (default 2 x fps)")
    g.add_argument("--bit-depth", choices=["auto", "8", "10"], default="auto")
    g.add_argument("--cpu-decode", action="store_true", help="decode on CPU instead of NVDEC")
    g.add_argument("--encode-extra", default="", help="extra encoder args, e.g. \"-temporal-aq 1\"")
    return g


def add_run_args(ap, version: str):
    """The 'run' option group, identical in every tool."""
    g = ap.add_argument_group("run")
    g.add_argument("--work-dir", type=Path, help="default: <output>_work next to the output")
    g.add_argument("--keep-work", "--keep-temp", action="store_true",
                   help="keep pieces after success")
    g.add_argument("--dry-run", action="store_true", help="print plan and commands only")
    g.add_argument("-v", "--verbose", action="store_true", help="print ffmpeg commands")
    g.add_argument("--ffmpeg", default="ffmpeg")
    g.add_argument("--ffprobe", default="ffprobe")
    g.add_argument("--version", action="version", version=version)
    return g


def resolve_tools(args) -> None:
    """Check ffmpeg/ffprobe and settle --encoder auto."""
    for exe in (args.ffmpeg, args.ffprobe):
        if not shutil.which(exe) and not Path(exe).is_file():
            die("err_not_found", exe=exe)
    if args.encoder == "auto":
        encs = subprocess.run([args.ffmpeg, "-hide_banner", "-encoders"],
                              capture_output=True, text=True).stdout
        args.encoder = "nvenc" if "hevc_nvenc" in encs else "x265"
        if args.encoder == "x265":
            warn("warn_no_nvenc")
    if args.encoder == "x265":
        args.cpu_decode = True
