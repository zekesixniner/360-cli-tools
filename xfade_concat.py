#!/usr/bin/env python3
"""xfade_concat.py - join clips with soft transitions, re-encoding only the transitions.

Built for native Windows ffmpeg (run from PowerShell) so NVDEC/NVENC work,
which they do not under WSL1.

Smart mode (default) - the idea from gopro-max-gpx-pipeline's xfade_concat.py,
made frame-exact:
  * the middle of every clip is stream-copied, cut on IDR keyframes only
  * only the short stretches around each transition are re-encoded:
      [last keyframe .. transition] + xfade + [transition .. next keyframe]
  * pieces are written as raw Annex B (.hevc): no container, so no timestamps
    to rebase, no edit lists to inherit and no hvcC to clash - joining them is
    plain byte concatenation, and one final mux re-derives all timing at a
    constant frame rate
  * every piece carries its own VPS/SPS/PPS in-band, repeated at each keyframe,
    so NVENC pieces and the camera's/OVRLEY's own bitstream live in one file
Full mode (--mode full) re-encodes everything (any source codec).

Audio is always rebuilt in one cheap pass (acrossfade at the joins, AAC).
All cut points are whole frames; a timeline JSON maps each source clip onto
the output.

The engine (probing, keyframe scan, Annex B pieces, join) lives in pieces.py,
shared with titles_in_360.py - keep it next to this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path

import pieces as engine
from pieces import (RENUMBER, Encoder, Source, add_encoding_args, add_run_args, clock, die,
                    expand_inputs, fmt_cmd, fsec, hms, join_pieces, parse_clock, parse_time,
                    played_frames, probe_source, render_pieces, resolve_tools, run, scan_spans,
                    secs, t, tokenize, warn)

__version__ = "0.7.0"

GOPRO_RE = re.compile(r"^G([A-Z])(\d{2})(\d{4})", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# messages (EN/SV, --lang or GOPRO_LANG like gopro-max-gpx-pipeline)
# --------------------------------------------------------------------------- #
MESSAGES = {
    "en": {
        "probing": "Probing {n} file(s)...",
        "probing_kf": "Scanning keyframes near the cut points...",
        "encoder_line": "{w}x{h} @ {fps} fps ({fpsf:.3f}), {depth}-bit {codec}, mode={mode}, "
                        "encoder={enc}, decode={dec}",
        "table_head": "{i:>3}  {file:<28} {length:>9} {head:>7} {tail:>7} {fade:>7} {out:>13}",
        "output_line": "Output: {dur} ({frames} frames), {pieces} piece(s): "
                       "copied {copy}, re-encoded {enc}{audio}",
        "with_audio": ", audio",
        "without_audio": ", no audio",
        "audio": "[audio] building crossfaded audio track",
        "join": "[join] concat -> {name}",
        "done": "Done: {out}\n      {timeline}\n      {frames} frames, {dur}",
        "warn_no_audio": "warning: no audio stream {s} in {names} -> output without audio",
        "warn_shorten": "warning: transition {a} -> {b} shortened to {d:.3f}s (clips too short for {want:.3f}s)",
        "warn_fallback_full": "warning: smart mode needs HEVC 4:2:0 8/10-bit sources ({name}: {codec} {pix}) -> --mode full",
        "warn_no_idr": "warning: {name}: no usable IDR keyframes within {w}s of the cuts -> "
                       "the whole clip is re-encoded (open GOP or very long GOP?)",
        "warn_short_gop": "note: {name}: no two keyframes fit in the {span:.1f}s kept from this "
                          "clip, so it is re-encoded (the source's GOP is longer than that)",
        "warn_bframes": "warning: --bframes is ignored in smart mode (re-encoded pieces use 0)",
        "err_no_inputs": "need at least one input clip",
        "err_no_output": "-o/--output is required",
        "yt_title_placeholder": "TITLE",
        "yt_speed": "Speed",
        "yt_len": "{on}",
        "yt_len_sped": "{on} on screen from {rec} recorded",
        "yt_audio_keep": "original audio",
        "yt_audio_mute": "muted",
        "yt_short": "note: chapter {n} ({stamp}) lasts only {s} s - YouTube needs every "
                    "chapter to be 10 s or longer, or it shows none of them",
        "yt_few": "note: only {n} clip(s) - YouTube shows chapters from 3 upwards",
        "yt_written": "YouTube chapters: {file}",
        "err_audio_mode": "{name}: unknown audio mode '{val}' (retime, keep or mute)",
        "err_speed": "{name}: invalid speed '{val}' (e.g. 2, 1.5, 0.5, 1/3)",
        "err_speed_range": "{name}: speed {val} is outside the supported 0.02-50 range",
        "err_speed_short": "{name}: nothing left of this clip at speed {val}",
        "tpl_header": "# Clips list for xfade_concat {v} - edit the rows below, then run:",
        "tpl_run": "#   python xfade_concat.py --list {name} -o {out} --fade-in 1 --fade-out 1",
        "tpl_cols1": "# Columns (everything after the path is optional):",
        "tpl_cols2": "#   in=   start in the file      dur=  how long it runs",
        "tpl_cols3": "#   out=  end point - checked against in+dur before anything is encoded",
        "tpl_cols4": "#   head= trim from the start    tail= trim from the end"
                     "    fade= transition into the NEXT clip",
        "tpl_cols5": "#   speed= 2 plays twice as fast, 0.5 half as fast (1.5, 3, 4, 6, "
                     "0.67, 0.33, 0.25, 0.12 ...)",
        "tpl_cols6": "#   audio= retime (follows speed, default) | keep (normal pitch, "
                     "truncated or looped) | mute",
        "tpl_cols7": "#   title=\"Take off ESMK\"  chapter title in the YouTube text "
                     "written next to the video",
        "tpl_master": "# {name} is {len} long. Replace the times below with your highlights.",
        "tpl_out_note": "# Add out=<end> to a row to have it checked against in+dur before encoding.",
        "tpl_files": "# {n} file(s), full length each. Add in=/dur= to use only part of one.",
        "tpl_done": "Wrote {file} with {n} row(s) - open it, edit, then run the command at the top.",
        "err_list_and_inputs": "use either --list or input files, not both",
        "err_mismatch": "{a}: {wa}x{ha}@{fa} differs from {b}: {wb}x{hb}@{fb}",
        "err_channels": "audio channel counts differ between clips: {ch}",
        "err_fades_count": "--fades needs {n} comma-separated values (one per junction), got {got}",
        "err_trim": "{name}: head+tail ({ht:.3f}s) >= clip length ({length:.3f}s)",
        "err_fade_io": "{name}: --fade-in/--fade-out do not fit in the clip",
        "err_list_kv": "{file}:{line}: expected key=value, got '{tok}'",
        "err_list_key": "{file}:{line}: unknown option '{key}' (in/dur/out/head/tail/fade)",
        "err_out_mismatch": "{file}:{line}: {name}: in={ins} + dur={dur} ends at {implied}, "
                            "but out={stated} - fix the line (nothing has been encoded yet)",
        "err_range": "{name}: the in/dur/out range does not fit in the clip ({length:.3f}s long)",
    },
    "sv": {
        "probing": "Läser in {n} fil(er)...",
        "probing_kf": "Letar keyframes nära klippunkterna...",
        "encoder_line": "{w}x{h} @ {fps} fps ({fpsf:.3f}), {depth}-bit {codec}, läge={mode}, "
                        "kodare={enc}, avkodning={dec}",
        "table_head": "{i:>3}  {file:<28} {length:>9} {head:>7} {tail:>7} {fade:>7} {out:>13}",
        "output_line": "Utdata: {dur} ({frames} rutor), {pieces} bit(ar): "
                       "kopierat {copy}, omkodat {enc}{audio}",
        "with_audio": ", ljud",
        "without_audio": ", utan ljud",
        "audio": "[ljud] bygger ljudspår med crossfades",
        "join": "[skarv] concat -> {name}",
        "done": "Klart: {out}\n       {timeline}\n       {frames} rutor, {dur}",
        "warn_no_audio": "varning: ljudström {s} saknas i {names} -> utdata utan ljud",
        "warn_shorten": "varning: övergång {a} -> {b} kortad till {d:.3f}s (klippen för korta för {want:.3f}s)",
        "warn_fallback_full": "varning: smart-läget kräver HEVC 4:2:0 8/10-bit ({name}: {codec} {pix}) -> --mode full",
        "warn_no_idr": "varning: {name}: inga användbara IDR-keyframes inom {w}s från klippunkterna -> "
                       "hela klippet kodas om (öppen GOP eller mycket lång GOP?)",
        "warn_short_gop": "obs: {name}: det får inte plats två keyframes i de {span:.1f}s som "
                          "behålls ur klippet, så det kodas om (källans GOP är längre än så)",
        "warn_bframes": "varning: --bframes ignoreras i smart-läget (omkodade bitar använder 0)",
        "err_no_inputs": "behöver minst ett klipp",
        "err_no_output": "-o/--output krävs",
        "yt_title_placeholder": "TITEL",
        "yt_speed": "Hastighet",
        "yt_len": "{on}",
        "yt_len_sped": "{on} på skärmen av {rec} inspelat",
        "yt_audio_keep": "originalljud",
        "yt_audio_mute": "utan ljud",
        "yt_short": "obs: kapitel {n} ({stamp}) varar bara {s} s - YouTube kräver att varje "
                    "kapitel är minst 10 s, annars visas inga kapitel alls",
        "yt_few": "obs: bara {n} klipp - YouTube visar kapitel först från 3",
        "yt_written": "YouTube-kapitel: {file}",
        "err_audio_mode": "{name}: okänt ljudläge '{val}' (retime, keep eller mute)",
        "err_speed": "{name}: ogiltig hastighet '{val}' (t.ex. 2, 1.5, 0.5, 1/3)",
        "err_speed_range": "{name}: hastigheten {val} ligger utanför intervallet 0.02-50",
        "err_speed_short": "{name}: inget kvar av klippet vid hastighet {val}",
        "tpl_header": "# Klipplista för xfade_concat {v} - redigera raderna nedan och kör sedan:",
        "tpl_run": "#   python xfade_concat.py --list {name} -o {out} --lang sv --fade-in 1 --fade-out 1",
        "tpl_cols1": "# Kolumner (allt efter sökvägen är valfritt):",
        "tpl_cols2": "#   in=   start i filen          dur=  hur länge det pågår",
        "tpl_cols3": "#   out=  slutpunkt - kontrolleras mot in+dur innan något kodas",
        "tpl_cols4": "#   head= klipp bort i början    tail= klipp bort i slutet"
                     "    fade= övergång till NÄSTA klipp",
        "tpl_cols5": "#   speed= 2 spelar dubbelt så fort, 0.5 hälften så fort (1.5, 3, 4, 6, "
                     "0.67, 0.33, 0.25, 0.12 ...)",
        "tpl_cols6": "#   audio= retime (följer hastigheten, standard) | keep (normal tonhöjd, "
                     "avhugget eller loopat) | mute",
        "tpl_cols7": "#   title=\"Take off ESMK\"  kapitelnamn i YouTube-texten som skrivs "
                     "bredvid videon",
        "tpl_master": "# {name} är {len} lång. Byt ut tiderna nedan mot dina höjdpunkter.",
        "tpl_out_note": "# Lägg till out=<slut> på en rad för att få den kontrollerad mot in+dur.",
        "tpl_files": "# {n} fil(er), hela längden var. Lägg till in=/dur= för att bara ta en del.",
        "tpl_done": "Skrev {file} med {n} rad(er) - öppna den, redigera, kör sedan kommandot högst upp.",
        "err_list_and_inputs": "använd antingen --list eller filnamn, inte båda",
        "err_mismatch": "{a}: {wa}x{ha}@{fa} skiljer sig från {b}: {wb}x{hb}@{fb}",
        "err_channels": "antal ljudkanaler skiljer mellan klippen: {ch}",
        "err_fades_count": "--fades behöver {n} kommaseparerade värden (ett per skarv), fick {got}",
        "err_trim": "{name}: head+tail ({ht:.3f}s) >= klippets längd ({length:.3f}s)",
        "err_fade_io": "{name}: --fade-in/--fade-out får inte plats i klippet",
        "err_list_kv": "{file}:{line}: väntade nyckel=värde, fick '{tok}'",
        "err_list_key": "{file}:{line}: okänt alternativ '{key}' (in/dur/out/head/tail/fade)",
        "err_out_mismatch": "{file}:{line}: {name}: in={ins} + dur={dur} slutar {implied}, "
                            "men out={stated} - rätta raden (inget är kodat än)",
        "err_range": "{name}: intervallet in/dur/out får inte plats i klippet ({length:.3f}s långt)",
    },
}
engine.register(MESSAGES)


# --------------------------------------------------------------------------- #
# probing
# --------------------------------------------------------------------------- #
@dataclass
class Clip(Source):
    """A source file plus how this row of the job uses it."""
    gopro: tuple | None = None  # (letter, chapter, number)
    head: int = 0
    tail: int = 0
    fade: int = 0  # transition into the NEXT clip
    head_raw: str | None = None
    tail_raw: str | None = None
    fade_raw: str | None = None
    in_raw: str | None = None
    dur_raw: str | None = None
    out_raw: str | None = None
    row_file: str = ""
    row_line: str = ""
    speed_raw: str | None = None
    audio_mode: str = ""            # retime | keep | mute
    title: str = ""                 # YouTube chapter title
    speed: Fraction = Fraction(1)   # >1 plays faster, <1 slower
    out_len: int = 0                # frames this clip contributes to the output
    label: str = ""          # file name, plus the range when one was given
    out_start: int = 0

    def src_at(self, o: int) -> int:
        """Source frame for an output-frame offset within this clip's kept range."""
        return self.head + (o if self.speed == 1 else round(o * self.speed))


def probe_clip(ffprobe: str, path: Path) -> Clip:
    c = probe_source(ffprobe, path, Clip)
    m = GOPRO_RE.match(path.name)
    c.gopro = (m.group(1).upper(), int(m.group(2)), int(m.group(3))) if m else None
    return c


# --------------------------------------------------------------------------- #
# list file
# --------------------------------------------------------------------------- #
def read_list_file(list_path: Path) -> list[tuple[Path, dict]]:
    """Lines: <path> [in=..] [dur=..|out=..] [head=..] [tail=..] [fade=..]

    in/dur/out select a stretch of the file, so highlights can be taken
    straight from one long master instead of being pre-cut with
    `ffmpeg -ss .. -t .. -c copy` (which leaves hidden pre-roll behind an edit
    list and destroys most of smart mode's copying). The same file may be
    listed many times with different ranges. '#' starts a comment."""
    rows: list[tuple[Path, dict]] = []
    base = list_path.resolve().parent
    for lineno, raw in enumerate(list_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        parts = tokenize(raw)
        if not parts:
            continue
        p = Path(parts[0])
        if not p.is_absolute():
            p = base / p
        opts: dict = {}
        for tok in parts[1:]:
            if "=" not in tok:
                die("err_list_kv", file=list_path, line=lineno, tok=tok)
            k, val = tok.split("=", 1)
            k = k.lower()
            if k not in ("head", "tail", "fade", "in", "dur", "out", "speed", "audio",
                         "title"):
                die("err_list_key", file=list_path, line=lineno, key=k)
            opts[k] = parse_clock(val) if k in ("in", "dur", "out") else val
        opts["_line"] = str(lineno)
        opts["_file"] = str(list_path)
        rows.append((p, opts))
    return rows


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
@dataclass
class Piece:
    kind: str  # "enc" | "xfade" | "copy"
    clip: int
    src: int  # first source frame in clip
    frames: int
    clip_b: int | None = None
    src_b: int = 0
    fade_in: bool = False  # whole piece fades from black
    fade_out: bool = False  # whole piece fades to black
    file: Path | None = field(default=None, repr=False)


def is_chapter_pair(a: Clip, b: Clip) -> bool:
    return (a.gopro is not None and b.gopro is not None
            and a.gopro[0] == b.gopro[0] and a.gopro[2] == b.gopro[2]
            and b.gopro[1] == a.gopro[1] + 1)


def assign_trims(clips: list[Clip], args, fps: Fraction) -> None:
    head = parse_time(args.head, fps, "--head")
    tail = parse_time(args.tail, fps, "--tail")
    fade = parse_time(args.fade, fps, "--fade")
    for c in clips:
        c.head, c.tail, c.fade = head, tail, fade

    # continuous GoPro chapters (GS01xxxx -> GS02xxxx): seamless cut, no trims inside
    if not args.list and not args.no_group:
        for a, b in zip(clips, clips[1:]):
            if is_chapter_pair(a, b):
                a.fade = a.tail = b.head = 0

    for c in clips:
        # in/dur/out pick a stretch of the file; they are just a friendlier way
        # of saying head/tail, so everything downstream stays the same.
        if c.in_raw is not None:
            c.head = parse_time(c.in_raw, fps, f"in ({c.path.name})")
        if c.dur_raw is not None:
            end = c.head + parse_time(c.dur_raw, fps, f"dur ({c.path.name})")
            if c.out_raw is not None:
                # out= alongside dur= is a typo guard: both must describe the same
                # end point, and a disagreement stops the run before anything is
                # encoded. One frame of slack absorbs clock/frame rounding.
                stated = parse_time(c.out_raw, fps, f"out ({c.path.name})")
                if abs(end - stated) > 1:
                    die("err_out_mismatch", file=c.row_file, line=c.row_line,
                        name=c.path.name, ins=hms(secs(c.head, fps)),
                        dur=hms(secs(end - c.head, fps)),
                        implied=hms(secs(end, fps)), stated=hms(secs(stated, fps)))
            c.tail = c.frames - end
        elif c.out_raw is not None:
            c.tail = c.frames - parse_time(c.out_raw, fps, f"out ({c.path.name})")
        if c.tail < 0:
            die("err_range", name=c.path.name, length=secs(c.frames, fps))
        if c.head_raw is not None:
            c.head = parse_time(c.head_raw, fps, f"head ({c.path.name})")
        if c.tail_raw is not None:
            c.tail = parse_time(c.tail_raw, fps, f"tail ({c.path.name})")
        if c.fade_raw is not None:
            c.fade = parse_time(c.fade_raw, fps, f"fade ({c.path.name})")
        c.label = c.path.name
        if c.in_raw is not None or c.dur_raw is not None or c.out_raw is not None:
            c.label = f"{c.path.name} @{hms(secs(c.head, fps))}"
        if c.speed != 1:
            c.label += f" {float(c.speed):g}x"

    if args.fades:
        vals = args.fades.split(",")
        if len(vals) != len(clips) - 1:
            die("err_fades_count", n=len(clips) - 1, got=len(vals))
        for c, v in zip(clips, vals):
            c.fade = parse_time(v, fps, "--fades")
    clips[-1].fade = 0

    for c in clips:
        if c.frames - c.head - c.tail <= 0:
            die("err_trim", name=c.path.name, ht=secs(c.head + c.tail, fps),
                length=secs(c.frames, fps))
        if c.speed_raw is not None:
            try:
                c.speed = Fraction(str(c.speed_raw).strip())
            except (ValueError, ZeroDivisionError):
                die("err_speed", name=c.path.name, val=c.speed_raw)
            if not (Fraction(1, 50) <= c.speed <= 50):
                die("err_speed_range", name=c.path.name, val=c.speed_raw)
        # everything downstream counts in output frames, which is where speed
        # stops being a special case: a 20 s clip at 2x is simply a 10 s clip.
        # The last output frame must map to a source frame that exists: output
        # frame o reads source frame round(o * speed), and floor(n / speed) can
        # still round past the end (n=250 at 0.5x wants source frame 250).
        # Trim the length until the mapping fits.
        kept = c.frames - c.head - c.tail
        c.out_len = math.floor(Fraction(kept) / c.speed)
        while c.out_len > 0 and round((c.out_len - 1) * c.speed) > kept - 1:
            c.out_len -= 1
        if c.out_len <= 0:
            die("err_speed_short", name=c.path.name, val=str(c.speed))
    # like the original script: a transition may use at most 40 % of either clip
    for a, b in zip(clips, clips[1:]):
        limit = math.floor(0.4 * min(a.out_len, b.out_len))
        if a.fade > limit:
            warn("warn_shorten", a=a.path.name, b=b.path.name, d=secs(limit, fps),
                 want=secs(a.fade, fps))
            a.fade = limit


def build_plan(clips: list[Clip], fps: Fraction, fade_in: int, fade_out: int,
               smart: bool, min_copy: int) -> tuple[list[Piece], int]:
    """Lay the clips out on the output timeline.

    Positions inside a clip are counted in OUTPUT frames, and turned into source
    frames only when a piece is emitted. That is what lets a clip play at a
    different speed without the rest of the planner knowing about it: the fades
    and transitions stay the length the viewer sees."""
    n = len(clips)
    pieces: list[Piece] = []
    cursor = 0
    for i, c in enumerate(clips):
        total = c.out_len
        in_ov = clips[i - 1].fade if i else 0
        c.out_start = cursor - in_ov
        o0, o1 = in_ov, total - c.fade
        fi = fade_in if i == 0 else 0
        fo = fade_out if i == n - 1 else 0
        if o0 + fi > o1 - fo:
            die("err_fade_io", name=c.path.name)

        def add(kind, o, frames, **kw):
            nonlocal cursor
            if frames > 0:
                pieces.append(Piece(kind, i, c.src_at(o), frames, **kw))
                cursor += frames

        add("enc", o0, fi, fade_in=True)
        b0, b1 = o0 + fi, o1 - fo  # body
        ks = ke = None
        if smart and c.speed == 1 and b1 > b0:
            # keyframes are source frame indices; at speed 1 an output offset is
            # just that minus the head trim
            safe = sorted(k - c.head for k, kf in c.keyframes.items() if kf.safe)
            ks = next((k for k in safe if b0 <= k <= b1), None)
            ends = [k for k in safe if k <= b1]
            if c.tail == 0 and b1 == total:
                ends.append(total)          # end of file is a safe cut point too
            ke = max(ends) if ends else None
            if ks is None or ke is None or ke - ks < min_copy:
                ks = ke = None
        if ks is None:
            add("enc", b0, b1 - b0)
        else:
            add("enc", b0, ks - b0)
            add("copy", ks, ke - ks)
            add("enc", ke, b1 - ke)
        add("enc", b1, fo, fade_out=True)
        if i < n - 1 and c.fade > 0:
            add("xfade", o1, c.fade, clip_b=i + 1, src_b=clips[i + 1].src_at(0))
    return pieces, cursor


# --------------------------------------------------------------------------- #
# ffmpeg command builders
# --------------------------------------------------------------------------- #
class Enc(Encoder):
    """xfade_concat's pieces: body re-encodes, fades and transitions."""

    def retime(self, clip: Clip) -> str:
        """Filters that turn source frames into output frames for a sped clip.

        setpts rescales the timeline; the fps filter then resamples to the output
        rate, dropping frames when speeding up and repeating them when slowing
        down (no interpolation - that would cost more than the encode itself at
        8K). Empty at speed 1, so ordinary clips keep exactly the old chain."""
        if clip.speed == 1:
            return ""
        num, den = clip.speed.numerator, clip.speed.denominator
        # round=up makes output frame k land on source frame round(k*speed);
        # the other modes bias the pick by up to half an output frame
        return f",setpts=PTS*{den}/{num},fps={self.fps}:round=up"

    def fades(self, p: Piece) -> list[str]:
        if p.fade_in:
            return [f"fade=t=in:start_frame=0:nb_frames={p.frames}"]
        if p.fade_out:
            return [f"fade=t=out:start_frame=0:nb_frames={p.frames}"]
        return []

    def piece_cmd(self, p: Piece, clips: list[Clip], out: Path) -> list[str]:
        base = self.base()
        a = clips[p.clip]
        if p.kind == "copy":
            return self.copy_cmd(a, p.src, p.frames, out)

        if p.kind == "enc":
            cmd = base + self.input_args(a, p.src)
            fades = self.fades(p)
            retime = self.retime(a)
            if fades or retime or (self.hw and self.args.encoder != "nvenc"):
                chain = (RENUMBER + "," + self.to_cpu() + retime
                         + "".join("," + f for f in fades) + f",format={self.encfmt}")
            else:
                # renumber timestamps to exact frame slots: a seek landing half a frame
                # in could otherwise make the CFR sync duplicate the first frame
                chain = RENUMBER
            cmd += ["-filter_complex", f"[0:v:0]{chain}[v]", "-map", "[v]"]
        else:
            b = clips[p.clip_b]
            cmd = base + self.input_args(a, p.src) + self.input_args(b, p.src_b)
            def prep(clip: Clip) -> str:
                # read enough source frames to cover the overlap at this speed,
                # retime, then cut to exactly the frames the transition needs
                need = math.ceil(p.frames * float(clip.speed)) + 2 if clip.speed != 1 else p.frames
                c = f"trim=end_frame={need},{RENUMBER},{self.to_cpu()}{self.retime(clip)}"
                if clip.speed != 1:
                    c += f",trim=end_frame={p.frames},{RENUMBER}"
                return c + ",settb=AVTB"
            graph = (f"[0:v:0]{prep(a)}[a];[1:v:0]{prep(b)}[b];"
                     f"[a][b]xfade=transition={self.args.transition}:"
                     f"duration={fsec(p.frames, self.fps)}:offset=0,"
                     + ",".join([*self.fades(p), f"format={self.encfmt}"]) + "[v]")
            cmd += ["-filter_complex", graph, "-map", "[v]"]
        return cmd + self.encode_tail(p.frames, out)


def atempo_chain(speed: Fraction) -> str:
    """atempo only accepts a limited factor per instance, so chain them."""
    if speed == 1:
        return ""
    out = []
    left = float(speed)
    while left > 2.0:
        out.append(2.0)
        left /= 2.0
    while left < 0.5:
        out.append(0.5)
        left /= 0.5
    out.append(left)
    return "".join(f",atempo={v:.6f}" for v in out)


def audio_cmd(args, clips: list[Clip], fps: Fraction, total: int, fade_in: int,
              fade_out: int, out: Path) -> list[str]:
    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-stats", "-y"]
    for c in clips:
        cmd += ["-i", str(c.path)]
    layout = clips[0].audio[args.audio_stream].get("channel_layout")
    afmt = "aformat=sample_fmts=fltp" + (f":channel_layouts={layout}" if layout else "")
    parts = []
    for i, c in enumerate(clips):
        s = c.v_start + secs(c.head, fps)
        e = c.v_start + secs(c.frames - c.tail, fps)
        want = secs(c.out_len, fps)          # seconds of audio this clip owes
        have = e - s                         # seconds the source stretch holds
        if c.audio_mode == "mute":
            fit = ",volume=0"
        elif c.audio_mode == "keep":
            # leave pitch and tempo alone: a faster clip simply drops the audio
            # it no longer has room for, a slower one repeats its own audio until
            # the picture is covered. aloop repeats the whole stretch; the atrim
            # below cuts the result to the exact length either way.
            fit = ""
            if want > have + 1e-9:
                fit = f",aloop=loop=-1:size={max(1, round(have * 48000))}"
        else:
            fit = atempo_chain(c.speed)
        parts.append(f"[{i}:a:{args.audio_stream}]aresample=48000:async=1:first_pts=0,{afmt},"
                     f"apad,atrim=start={s:.9f}:end={e:.9f},asetpts=PTS-STARTPTS"
                     f"{fit},apad,"
                     f"atrim=end={fsec(c.out_len, fps)},asetpts=PTS-STARTPTS[a{i}]")
    cur = "a0"
    for i in range(1, len(clips)):
        d = clips[i - 1].fade
        nxt = f"x{i}"
        if d > 0:
            parts.append(f"[{cur}][a{i}]acrossfade=d={fsec(d, fps)}:"
                         f"c1={args.audio_curve}:c2={args.audio_curve}[{nxt}]")
        else:
            parts.append(f"[{cur}][a{i}]concat=n=2:v=0:a=1[{nxt}]")
        cur = nxt
    tail = []
    if fade_in:
        tail.append(f"afade=t=in:st=0:d={fsec(fade_in, fps)}")
    if fade_out:
        tail.append(f"afade=t=out:st={fsec(total - fade_out, fps)}:d={fsec(fade_out, fps)}")
    tail.append(f"atrim=end={fsec(total, fps)}")
    parts.append(f"[{cur}]{','.join(tail)}[aout]")
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[aout]",
                  "-c:a", "aac", "-b:a", args.audio_bitrate, "-f", "mp4", str(out)]


def piece_key(p: Piece, clips: list[Clip], enc: Enc) -> str:
    def src(i):
        c = clips[i]
        st = c.path.stat()
        return [str(c.path), st.st_size, st.st_mtime_ns]
    spec = {"p": [p.kind, p.src, p.frames, p.src_b, p.fade_in, p.fade_out],
            "a": src(p.clip), "b": src(p.clip_b) if p.clip_b is not None else None,
            "tr": enc.args.transition, "enc": enc.video_args, "bsf": enc.enc_bsf,
            "sp": [str(clips[p.clip].speed),
                   str(clips[p.clip_b].speed) if p.clip_b is not None else None],
            "hw": enc.hw, "ts": enc.timescale, "v": __version__}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]


def yt_stamp(seconds: int) -> str:
    """YouTube chapter format: M:SS under an hour, H:MM:SS from there."""
    h, rem = divmod(int(seconds), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def plain_time(seconds: float) -> str:
    """Source position written so YouTube will NOT turn it into a link.

    YouTube linkifies anything shaped like 12:34 in a description, and a stray
    timestamp between the chapters - out of order, or near the end - makes it
    drop the whole chapter list. 1h02m03s / 14m57s is read by people, ignored
    by YouTube."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s"


def plain_len(seconds: float) -> str:
    total = int(round(seconds))
    m, sec = divmod(total, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h} h {m} min"
    return f"{m} min {sec} s" if m else f"{sec} s"


def youtube_chapters(clips: list[Clip], fps: Fraction, total: int) -> tuple[str, list[str]]:
    """Chapter list for the YouTube description, one chapter per clip.

    A chapter starts halfway through the transition into its clip, which is
    where the new picture takes over. Returns the text and any rule YouTube
    would reject it for: chapters are only shown when the first is 0:00, there
    are at least three, and every one lasts 10 seconds or more."""
    starts = []
    for i, c in enumerate(clips):
        if i == 0:
            starts.append(0)
        else:
            # rounded up, so a click lands where the new clip has taken over
            # rather than while the old one still dominates the blend
            mid = c.out_start + Fraction(clips[i - 1].fade, 2)
            starts.append(max(math.ceil(mid / fps), starts[-1] + 1))
    ends = starts[1:] + [int(float(Fraction(total) / fps))]

    lines, problems = [], []
    for i, c in enumerate(clips):
        title = c.title or t("yt_title_placeholder")
        lines.append(f"{yt_stamp(starts[i])} {title}")
        on_screen = secs(c.out_len, fps)
        recorded = secs(c.frames - c.head - c.tail, fps)
        spd = f"{float(c.speed):g}×"
        if c.speed == 1:
            length = t("yt_len", on=plain_len(on_screen))
        else:
            length = t("yt_len_sped", on=plain_len(on_screen), rec=plain_len(recorded))
        src = (f"{c.path.name} {plain_time(secs(c.head, fps))}–"
               f"{plain_time(secs(c.frames - c.tail, fps))}")
        audio = "" if c.speed == 1 or c.audio_mode == "retime" else \
            " · " + t("yt_audio_" + c.audio_mode)
        lines.append(f"   {t('yt_speed')} {spd} · {length} · {src}{audio}")
        if ends[i] - starts[i] < 10:
            problems.append(t("yt_short", n=i + 1, stamp=yt_stamp(starts[i]),
                              s=ends[i] - starts[i]))
    if len(clips) < 3:
        problems.append(t("yt_few", n=len(clips)))
    return "\n".join(lines) + "\n", problems


def make_list(args) -> None:
    """Write a clips list the user can open and edit, instead of typing paths."""
    paths = expand_inputs(args.inputs)
    if not paths:
        die("err_no_inputs")
    dest = Path(args.make_list)
    if dest.exists() and not args.overwrite:
        die("err_exists", out=dest)

    print(t("probing", n=len(paths)))
    clips = [probe_clip(args.ffprobe, p) for p in paths]
    c0 = clips[0]
    ref = dest.resolve().parent

    def rel(path: Path) -> str:
        try:
            return os.path.relpath(path, ref).replace("\\", "/")
        except ValueError:       # different drive on Windows
            return str(path)

    lines = [t("tpl_header", v=__version__),
             t("tpl_run", out="montage.mp4", name=dest.name),
             "#",
             t("tpl_cols1"), t("tpl_cols2"), t("tpl_cols3"), t("tpl_cols4"),
             t("tpl_cols5"), t("tpl_cols6"), t("tpl_cols7"), "#"]

    if args.rows:
        # range rows through one master: evenly spaced starting points, so every
        # row is valid as written and only the numbers need changing
        lines.append(t("tpl_master", name=c0.path.name, len=clock(secs(c0.frames, c0.fps))))
        lines.append(t("tpl_out_note"))
        lines.append("#")
        lines.append("")
        for _ in range(args.rows):
            lines.append(f"{rel(c0.path):<30} in=00:00:00  dur=00:00:12")
    else:
        lines.append(t("tpl_files", n=len(clips)))
        lines.append("#")
        width = max(len(rel(c.path)) for c in clips)
        for c in clips:
            lines.append(f"{rel(c.path):<{width}}   # {secs(c.frames, c.fps):.2f} s")

    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rows = sum(1 for ln in lines if not ln.startswith("#"))
    print(t("tpl_done", file=dest, n=rows))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Join clips with soft transitions (xfade/acrossfade). Smart mode "
                    "stream-copies the clip bodies and re-encodes only the transitions "
                    "with NVENC. Times: seconds (1.5) or frames (45f).")
    ap.add_argument("inputs", nargs="*", help="input clips (wildcards are expanded)")
    ap.add_argument("-o", "--output", help="output .mp4 (not needed with --make-list)")
    ap.add_argument("-y", "--overwrite", action="store_true", help="overwrite output")
    ap.add_argument("--make-list", type=Path, metavar="FILE",
                    help="write a ready-to-edit clips list for the given inputs and exit")
    ap.add_argument("--rows", type=int, default=0, metavar="N",
                    help="--make-list: N range rows through a single master instead of "
                         "one row per file")
    ap.add_argument("--list", type=Path,
                    help="text file: '<path> [head=..] [tail=..] [fade=..]' per line")
    ap.add_argument("--lang", choices=["en", "sv"], default=engine.default_lang(),
                    help="message language (default: $GOPRO_LANG or en)")

    g = ap.add_argument_group("transitions / trimming")
    g.add_argument("--fade", "--duration", default="1.5",
                   help="overlap per junction: this much of clip A's end AND clip B's "
                        "start is blended; 0 = hard cut (default 1.5)")
    g.add_argument("--fades", help="per-junction overlap list, e.g. '1,0,2.5' (overrides)")
    g.add_argument("--head", default="0", help="cut from the start of each clip (default 0)")
    g.add_argument("--tail", default="0", help="cut from the end of each clip (default 0)")
    g.add_argument("--fade-in", default="0", help="fade from black at output start")
    g.add_argument("--fade-out", default="0", help="fade to black at output end")
    g.add_argument("--transition", default="fade",
                   help="xfade transition: fade (default), dissolve, fadeblack, fadewhite, ...")
    g.add_argument("--no-sort", action="store_true",
                   help="keep given order (GoPro names are otherwise sorted by file, chapter)")
    g.add_argument("--no-group", action="store_true",
                   help="treat GoPro chapters as separate clips (fade/trim between them)")

    add_encoding_args(ap, "smart = copy clip bodies, re-encode transitions only (default); "
                          "full = re-encode everything")

    g = ap.add_argument_group("audio")
    g.add_argument("--no-audio", action="store_true")
    g.add_argument("--audio-stream", type=int, default=0, help="audio stream index per clip")
    g.add_argument("--audio-bitrate", default="192k")
    g.add_argument("--audio-curve", default="tri", help="acrossfade curve (tri, qsin, ...)")
    g.add_argument("--clip-audio", choices=["retime", "keep", "mute"], default="retime",
                   help="what a speed= clip does with its audio: retime it (default), "
                        "keep it at normal pitch, or mute it. Per row: audio=keep")

    add_run_args(ap, __version__)
    args = ap.parse_args()
    engine.set_lang(args.lang)
    resolve_tools(args)

    if args.make_list:
        make_list(args)
        return

    if not args.output:
        die("err_no_output")
    output = Path(args.output).resolve()
    if output.exists() and not args.overwrite and not args.dry_run:
        die("err_exists", out=output)

    # ---- inputs
    if args.list:
        if args.inputs:
            die("err_list_and_inputs")
        rows = read_list_file(args.list)
        paths, overrides = [p for p, _ in rows], [o for _, o in rows]
    else:
        paths = expand_inputs(args.inputs)
        overrides = [{} for _ in paths]
    if not paths:
        die("err_no_inputs")

    print(t("probing", n=len(paths)))
    probed: dict[Path, Clip] = {}
    clips = []
    for path in paths:
        key = path.resolve()
        if key not in probed:
            probed[key] = probe_clip(args.ffprobe, path)
        clips.append(replace(probed[key]))   # own trims per row, shared probe result
    for c, o in zip(clips, overrides):
        c.head_raw, c.tail_raw, c.fade_raw = o.get("head"), o.get("tail"), o.get("fade")
        c.in_raw, c.dur_raw, c.out_raw = o.get("in"), o.get("dur"), o.get("out")
        c.row_file, c.row_line = o.get("_file", ""), o.get("_line", "")
        c.speed_raw = o.get("speed")
        c.title = o.get("title", "")
        c.audio_mode = (o.get("audio") or args.clip_audio).lower()
        if c.audio_mode not in ("retime", "keep", "mute"):
            die("err_audio_mode", name=c.path.name, val=c.audio_mode)
    if not args.list and not args.no_sort and all(c.gopro for c in clips):
        clips.sort(key=lambda c: (c.gopro[0], c.gopro[2], c.gopro[1]))

    c0 = clips[0]
    for c in clips[1:]:
        if (c.width, c.height) != (c0.width, c0.height) or c.fps != c0.fps:
            die("err_mismatch", a=c.path.name, wa=c.width, ha=c.height, fa=c.fps,
                b=c0.path.name, wb=c0.width, hb=c0.height, fb=c0.fps)
    fps = c0.fps

    use_audio = not args.no_audio
    if use_audio:
        missing = [c.path.name for c in clips if len(c.audio) <= args.audio_stream]
        if missing:
            warn("warn_no_audio", s=args.audio_stream, names=", ".join(missing))
            use_audio = False
        else:
            chans = {c.audio[args.audio_stream].get("channels") for c in clips}
            if len(chans) > 1:
                die("err_channels", ch=sorted(chans))

    smart = args.mode == "smart"
    if smart:
        for c in clips:
            if not c.smart_ok():
                warn("warn_fallback_full", name=c.path.name, codec=c.codec, pix=c.pix_fmt)
                smart = False
                break
    if smart and args.bframes:
        warn("warn_bframes")

    assign_trims(clips, args, fps)
    fade_in = parse_time(args.fade_in, fps, "--fade-in")
    fade_out = parse_time(args.fade_out, fps, "--fade-out")
    min_copy = parse_time(args.min_copy, fps, "--min-copy")

    # ---- keyframe scan around the cut points (smart mode)
    if smart:
        print(t("probing_kf"))
        w = args.gop_window
        for i, c in enumerate(clips):
            in_ov = clips[i - 1].fade if i else 0
            b0 = secs(c.head + in_ov + (fade_in if i == 0 else 0), fps) + c.v_start
            b1 = secs(c.frames - c.tail - c.fade - (fade_out if i == len(clips) - 1 else 0),
                      fps) + c.v_start
            # look forward from the body start and backward from the body end
            spans = [(max(0.0, b0 - 1.0), b0 + w)]
            if b1 < secs(c.frames, fps) + c.v_start - 1e-6:
                spans.append((max(0.0, b1 - w), b1 + 1.0))
            scan_spans(args.ffmpeg, c, spans)

    pieces, total = build_plan(clips, fps, fade_in, fade_out, smart, min_copy)
    if smart:
        for i, c in enumerate(clips):
            body = [p for p in pieces if p.clip == i and p.kind != "xfade"]
            if any(p.kind == "copy" for p in body) or c.speed != 1:
                continue
            span = sum(p.frames for p in body)
            if span <= 2 * min_copy:
                continue
            # Distinguish "this file has no keyframes we can cut on" from the
            # ordinary case of a stretch shorter than the source's GOP, where
            # there simply is not room for two keyframes to copy between.
            if not any(kf.safe for kf in c.keyframes.values()):
                warn("warn_no_idr", name=c.label or c.path.name, w=args.gop_window)
            else:
                warn("warn_short_gop", name=c.label or c.path.name,
                     span=secs(span, fps))
    enc = Enc(args, clips, smart)

    # ---- plan printout
    copy_f = sum(p.frames for p in pieces if p.kind == "copy")
    print("\n" + t("encoder_line", w=c0.width, h=c0.height, fps=fps, fpsf=float(fps),
                   depth=enc.depth, codec=c0.codec, mode="smart" if smart else "full",
                   enc=args.encoder, dec="NVDEC" if enc.hw else "CPU"))
    print(t("table_head", i="#", file="file", length="length", head="head", tail="tail",
            fade="fade->", out="out start"))
    for i, c in enumerate(clips):
        fade_txt = f"{secs(c.fade, fps):7.3f}" if i < len(clips) - 1 else "      -"
        print(f"{i:>3}  {(c.label or c.path.name):<28} {secs(c.frames, fps):9.3f} {secs(c.head, fps):7.3f} "
              f"{secs(c.tail, fps):7.3f} {fade_txt} {hms(secs(c.out_start, fps)):>13}")
    print(t("output_line", dur=hms(secs(total, fps)), frames=total, pieces=len(pieces),
            copy=hms(secs(copy_f, fps)), enc=hms(secs(total - copy_f, fps)),
            audio=t("with_audio") if use_audio else t("without_audio")) + "\n")

    work = (args.work_dir or output.with_name(output.stem + "_work")).resolve()
    for idx, p in enumerate(pieces):
        p.file = work / f"{idx:03d}_{p.kind}_{piece_key(p, clips, enc)}.hevc"

    timeline = {
        "tool": f"xfade_concat {__version__}",
        "output": str(output),
        "mode": "smart" if smart else "full",
        "fps": str(fps),
        "output_frames": total,
        "output_duration_s": secs(total, fps),
        "copied_s": secs(copy_f, fps),
        "reencoded_s": secs(total - copy_f, fps),
        "mapping": "out_t = out_start_s + (src_t - src_in_s) / speed"
                   "   for src_in_s <= src_t < src_out_s",
        "clips": [{
            "file": str(c.path),
            "src_in_s": secs(c.head, fps),
            "src_out_s": secs(c.frames - c.tail, fps),
            "speed": float(c.speed),
            "title": c.title,
            "out_start_s": secs(c.out_start, fps),
            "out_end_s": secs(c.out_start + c.out_len, fps),
            "fade_from_prev_s": secs(clips[i - 1].fade, fps) if i else 0.0,
            "fade_to_next_s": secs(c.fade, fps),
        } for i, c in enumerate(clips)],
    }

    def label(p: Piece) -> str:
        src = clips[p.clip].label or clips[p.clip].path.name
        if p.clip_b is not None:
            src += f" -> {clips[p.clip_b].label or clips[p.clip_b].path.name}"
        return f"{p.kind:<5} {src}  [{secs(p.src, fps):.3f}s +{secs(p.frames, fps):.3f}s]"

    chapters, yt_problems = youtube_chapters(clips, fps, total)
    for msg in yt_problems:
        print(msg, file=sys.stderr)

    if args.dry_run:
        for p in pieces:
            print(label(p))
            print("  $ " + fmt_cmd(enc.piece_cmd(p, clips, p.file)))
        if use_audio:
            print("[audio]\n  $ " + fmt_cmd(audio_cmd(args, clips, fps, total, fade_in, fade_out,
                                                      work / "audio.m4a")))
        print("\n" + json.dumps(timeline, indent=2))
        print("\n" + chapters)
        return

    # written before encoding starts: it depends only on the plan, so the
    # YouTube text can be prepared while the video is still being rendered
    yt_path = output.with_suffix(".chapters.txt")
    yt_path.write_text(chapters, encoding="utf-8")
    print(t("yt_written", file=yt_path) + "\n")

    work.mkdir(parents=True, exist_ok=True)

    # ---- video pieces
    render_pieces(pieces, lambda p, out: enc.piece_cmd(p, clips, out), label, args.verbose)

    # ---- audio
    audio_file = None
    if use_audio:
        audio_file = work / "audio.m4a"
        print(t("audio"))
        tmp = work / "audio.partial.m4a"
        run(audio_cmd(args, clips, fps, total, fade_in, fade_out, tmp), args.verbose)
        os.replace(tmp, audio_file)

    # ---- join
    print(t("join", name=output.name))
    if audio_file:
        extra, maps = ["-i", str(audio_file)], ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        extra, maps = [], ["-map", "0:v:0"]
    join_pieces(args.ffmpeg, [p.file for p in pieces], fps, enc, output, extra, maps,
                args.verbose)
    got = played_frames(args.ffprobe, output)
    if got != total:
        die("err_frames", name=output.name, want=total, got=got)

    tl_path = output.with_suffix(".timeline.json")
    tl_path.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print("\n" + t("done", out=output, timeline=tl_path, frames=total, dur=hms(secs(total, fps))))
    if c0.width == 2 * c0.height:
        print(t("note_360"))


if __name__ == "__main__":
    main()
