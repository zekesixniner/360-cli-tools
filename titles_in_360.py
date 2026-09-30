#!/usr/bin/env python3
"""titles_in_360.py - put titles, signs and logos into a 360° video.

Each sign is drawn flat with Pillow (font, outline, plate, border, shadow, glow,
3D extrusion, emoji, images), then projected with ffmpeg's v360 filter onto the
equirectangular frame at the direction you give it (yaw/pitch/roll), so it
looks undistorted in the viewer.

Smart mode (default) re-encodes only the stretches where a sign is on screen,
widened to the nearest keyframes; everything else is stream-copied. The engine
is the same as xfade_concat.py's (pieces.py): raw Annex B pieces, one final mux,
frame-exact. Full mode (--mode full) re-encodes everything.

Built for native Windows ffmpeg (run from PowerShell) so NVDEC/NVENC work.
Needs Pillow:  python -m pip install pillow
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import shutil
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import pieces as engine
from pieces import (RENUMBER, Encoder, add_encoding_args, add_run_args, die, fmt_cmd, hms,
                    join_pieces, parse_clock, parse_time, played_frames, probe_source,
                    render_pieces, resolve_tools, safe_keyframes, scan_spans, secs, t,
                    tokenize, warn)

try:
    from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont
except ImportError:  # reported properly in main(), after --lang is known
    Image = None

__version__ = "0.1.4"

# --------------------------------------------------------------------------- #
# messages
# --------------------------------------------------------------------------- #
MESSAGES = {
    "en": {
        "probing": "Probing {name}...",
        "probing_kf": "Scanning keyframes around the signs...",
        "rendering": "Drawing {n} sign(s)...",
        "n_signs": "{n} sign(s)",
        "render_one": "  [{i}/{n}] {label}",
        "encoder_line": "{w}x{h} @ {fps} fps, {depth}-bit {codec}, mode={mode}, encoder={enc}, "
                        "decode={dec}",
        "sign_head": "{i:>3}  {start:>12} {dur:>7}  {yaw:<12} {what}",
        "plan_line": "Output: {dur} ({frames} frames), {pieces} piece(s): copied {copy}, "
                     "re-encoded {enc} ({pct:.1f} %){audio}",
        "with_audio": ", audio copied",
        "without_audio": ", no audio",
        "join": "[join] -> {name}",
        "done": "Done: {out}\n      {frames} frames, {dur}",
        "srt_written": "Subtitles: {file}",
        "preview_written": "Preview: {file}",
        "tpl_done": "Wrote {file} - open it, edit the rows, then run the command at the top.",
        "fonts_head": "{n} font(s){filt}:",
        "warn_fallback_full": "warning: smart mode needs HEVC 4:2:0 8/10-bit ({codec} {pix}) "
                              "-> --mode full",
        "warn_bframes": "warning: --bframes is ignored in smart mode (re-encoded pieces use 0)",
        "warn_not_360": "warning: {w}x{h} is not 2:1 - signs are placed as if it were "
                        "equirectangular",
        "warn_clamped": "{file}:{line}: warning: sign runs past the end of the video - shortened",
        "warn_fade_long": "{file}:{line}: warning: fade-in + fade-out longer than the sign - "
                          "shortened to fit",
        "warn_no_idr": "warning: no usable keyframes near {at} -> re-encoding that stretch "
                       "(open GOP or very long GOP?)",
        "warn_wide": "{file}:{line}: warning: the sign is {deg:.0f}° wide - it will look "
                     "stretched at the edges; consider a smaller size",
        "warn_emoji_font": "warning: no colour emoji font found - emoji drawn with the text font",
        "warn_no_signs": "warning: the titles file has no signs",
        "err_pillow": "Pillow is missing. Install it with:  python -m pip install pillow",
        "err_exists": "{out} exists (use -y to overwrite)",
        "err_no_output": "-o/--output is required (or use --preview / --srt / --make-titles)",
        "err_no_input": "an input video is required",
        "err_no_titles": "--titles is required",
        "err_same_file": "the output must not be the input file",
        "err_syntax": "{file}:{line}: {msg}",
        "err_columns": "expected: <start> <dur> <yaw> \"text\" [key=value ...]",
        "err_no_text": "a sign needs \"text\" or image=...",
        "err_two_texts": "two texts on one line - quote text that has spaces: \"like this\"",
        "err_unknown_key": "unknown option '{opt}'",
        "err_row_only": "'{opt}' belongs on a sign row, not in a style",
        "err_bad_value": "{opt}: invalid value '{val}' ({why})",
        "err_bad_yaw": "invalid yaw '{val}' (degrees like 0, 90, -90, 180, or front/right/back/left)",
        "err_style": "unknown style '{name}' (defined: {known})",
        "err_style_head": "style header must look like [style name]",
        "err_start": "start {at} is outside the video (0 - {len})",
        "err_zero": "the sign has no duration",
        "err_ref_no_timeline": "'{ref}' refers to a source clip, but there is no timeline JSON "
                               "(expected {path}, or pass --timeline)",
        "err_ref_unknown": "'{ref}' matches no clip in the timeline (files: {files})",
        "err_ref_ambiguous": "'{ref}' matches clips {nums}; use #<number>@<time> instead",
        "err_ref_outside": "'{ref}' is not in the video - that part of the clip was cut "
                           "(used: {ranges})",
        "err_font": "font '{name}' not found{hint}",
        "err_font_hint": " - did you mean: {alts}?",
        "err_image": "cannot read image {path}: {err}",
        "err_too_wide": "the sign is {deg:.0f}° wide - more than a flat sign can cover (max 150°)",
        "err_preview_time": "--preview: invalid time '{val}'",
        "err_timeline": "cannot read timeline {path}: {err}",
        "not_found": "not found",
        "why_number": "a number",
        "why_length": "pixels like 4, or a share of the font size like 5%",
        "why_pair": "two lengths like 4,4 - or none",
        "why_color": "a colour like white, #ffd700 or #00000080",
        "why_align": "left, center or right",
        "why_angle": "degrees like 6, or pixels like 120px",
    },
    "sv": {
        "probing": "Läser in {name}...",
        "probing_kf": "Letar keyframes runt skyltarna...",
        "rendering": "Ritar {n} skylt(ar)...",
        "n_signs": "{n} skylt(ar)",
        "render_one": "  [{i}/{n}] {label}",
        "encoder_line": "{w}x{h} @ {fps} fps, {depth}-bit {codec}, läge={mode}, kodare={enc}, "
                        "avkodning={dec}",
        "sign_head": "{i:>3}  {start:>12} {dur:>7}  {yaw:<12} {what}",
        "plan_line": "Utdata: {dur} ({frames} rutor), {pieces} bit(ar): kopierat {copy}, "
                     "omkodat {enc} ({pct:.1f} %){audio}",
        "with_audio": ", ljud kopierat",
        "without_audio": ", utan ljud",
        "join": "[skarv] -> {name}",
        "done": "Klart: {out}\n       {frames} rutor, {dur}",
        "srt_written": "Undertexter: {file}",
        "preview_written": "Förhandsvisning: {file}",
        "tpl_done": "Skrev {file} - öppna den, redigera raderna och kör sedan kommandot högst upp.",
        "fonts_head": "{n} typsnitt{filt}:",
        "warn_fallback_full": "varning: smart-läget kräver HEVC 4:2:0 8/10-bit ({codec} {pix}) "
                              "-> --mode full",
        "warn_bframes": "varning: --bframes ignoreras i smart-läget (omkodade bitar använder 0)",
        "warn_not_360": "varning: {w}x{h} är inte 2:1 - skyltarna placeras som om bilden vore "
                        "equirectangular",
        "warn_clamped": "{file}:{line}: varning: skylten går förbi videons slut - förkortad",
        "warn_fade_long": "{file}:{line}: varning: fade-in + fade-out är längre än skylten - "
                          "förkortade",
        "warn_no_idr": "varning: inga användbara keyframes nära {at} -> den sträckan kodas om "
                       "(öppen GOP eller mycket lång GOP?)",
        "warn_wide": "{file}:{line}: varning: skylten är {deg:.0f}° bred - den ser utdragen ut "
                     "i kanterna; prova en mindre storlek",
        "warn_emoji_font": "varning: hittar inget emoji-typsnitt i färg - emoji ritas med "
                           "texttypsnittet",
        "warn_no_signs": "varning: titelfilen har inga skyltar",
        "err_pillow": "Pillow saknas. Installera med:  python -m pip install pillow",
        "err_exists": "{out} finns redan (använd -y för att skriva över)",
        "err_no_output": "-o/--output krävs (eller använd --preview / --srt / --make-titles)",
        "err_no_input": "en indatavideo krävs",
        "err_no_titles": "--titles krävs",
        "err_same_file": "utdata får inte vara samma fil som indata",
        "err_syntax": "{file}:{line}: {msg}",
        "err_columns": "väntade: <start> <längd> <yaw> \"text\" [nyckel=värde ...]",
        "err_no_text": "en skylt behöver \"text\" eller image=...",
        "err_two_texts": "två texter på en rad - sätt citattecken runt text med mellanslag: "
                         "\"så här\"",
        "err_unknown_key": "okänt alternativ '{opt}'",
        "err_row_only": "'{opt}' hör hemma på en skyltrad, inte i en stil",
        "err_bad_value": "{opt}: ogiltigt värde '{val}' ({why})",
        "err_bad_yaw": "ogiltig yaw '{val}' (grader som 0, 90, -90, 180, eller "
                       "fram/höger/bak/vänster)",
        "err_style": "okänd stil '{name}' (definierade: {known})",
        "err_style_head": "en stilrubrik ska se ut så här: [style namn]",
        "err_start": "starten {at} ligger utanför videon (0 - {len})",
        "err_zero": "skylten har ingen längd",
        "err_ref_no_timeline": "'{ref}' pekar på ett källklipp, men det finns ingen tidslinje-"
                               "JSON (väntade {path}, eller ange --timeline)",
        "err_ref_unknown": "'{ref}' matchar inget klipp i tidslinjen (filer: {files})",
        "err_ref_ambiguous": "'{ref}' matchar klippen {nums}; använd #<nummer>@<tid> i stället",
        "err_ref_outside": "'{ref}' finns inte i videon - den delen av klippet är bortklippt "
                           "(använt: {ranges})",
        "err_font": "hittar inte typsnittet '{name}'{hint}",
        "err_font_hint": " - menade du: {alts}?",
        "err_image": "kan inte läsa bilden {path}: {err}",
        "err_too_wide": "skylten är {deg:.0f}° bred - mer än en platt skylt kan täcka (max 150°)",
        "err_preview_time": "--preview: ogiltig tid '{val}'",
        "err_timeline": "kan inte läsa tidslinjen {path}: {err}",
        "not_found": "finns inte",
        "why_number": "ett tal",
        "why_length": "pixlar som 4, eller andel av teckenstorleken som 5%",
        "why_pair": "två längder som 4,4 - eller none",
        "why_color": "en färg som white, #ffd700 eller #00000080",
        "why_align": "left, center eller right",
        "why_angle": "grader som 6, eller pixlar som 120px",
    },
}
engine.register(MESSAGES)

# --------------------------------------------------------------------------- #
# style keys
#
# Every key can go in a [style] block, on a sign row, or (the few with a flag)
# on the command line. Later wins:
#   built-in  <  command line  <  [style default]  <  style=name  <  the row
# Lengths are output pixels at the sign's centre, or a share of the font size
# ("5%"), so a style keeps its look when the size changes.
# --------------------------------------------------------------------------- #
BUILTIN = {
    "font": "",                  # "" = first available of DEFAULT_FONTS
    "size": "5",                 # font size in degrees of view (or 120px)
    "color": "white",
    "opacity": "1",
    "outline": "3%",
    "outline-color": "black",
    "align": "center",
    "line-spacing": "1.15",
    "box": "none",               # plate colour, or none
    "box-opacity": "0.6",
    "padding": "40%,20%",        # horizontal,vertical inside the plate
    "radius": "25%",
    "border": "none",            # colour[,width]
    "shadow": "4%,4%",           # dx,dy - or none
    "shadow-blur": "5%",
    "shadow-color": "black",
    "shadow-opacity": "0.6",
    "glow": "none",              # blur radius - or none
    "glow-color": "white",
    "glow-opacity": "0.8",
    "extrude": "none",           # dx,dy of the 3D depth - or none
    "extrude-color": "",         # "" = the text colour, darkened
    "pitch": "0",
    "roll": "0",
    "fade": "",                  # "" = --fade
    "fade-in": "",
    "fade-out": "",
    "width": "",                 # images: width in degrees instead of size
    "emoji-font": "",
}
ROW_ONLY = {"style", "image"}
YAW_WORDS = {"front": 0, "fram": 0, "forward": 0, "right": 90, "höger": 90, "hoger": 90,
             "back": 180, "bak": 180, "behind": 180, "left": -90, "vänster": -90,
             "vanster": -90}
DEFAULT_FONTS = ["Segoe UI Bold", "Arial Bold", "Helvetica Bold", "DejaVu Sans Bold",
                 "Liberation Sans Bold", "Noto Sans Bold"]
EMOJI_FONTS = ["Segoe UI Emoji", "Noto Color Emoji", "Apple Color Emoji"]
SUPERSAMPLE = 3  # signs are drawn at 3x and reduced: smooth plates, shadows and edges


# --------------------------------------------------------------------------- #
# titles file
# --------------------------------------------------------------------------- #
@dataclass
class Sign:
    line: int
    start: int              # first frame
    frames: int             # how long it shows, fades included
    yaws: list[float]
    text: str
    image: Path | None
    style: dict             # fully resolved key -> string
    fade_in: int = 0
    fade_out: int = 0
    placements: list = field(default_factory=list)

    @property
    def end(self) -> int:
        return self.start + self.frames

    def what(self) -> str:
        if self.image:
            return f"[{self.image.name}]"
        s = self.text.replace("\n", " / ")
        return f'"{s[:40]}…"' if len(s) > 41 else f'"{s}"'

    def alpha_at(self, f: int) -> float:
        """Sign opacity at frame f, the same curve the fade filters draw."""
        if not self.start <= f < self.end:
            return 0.0
        a = 1.0
        if self.fade_in:
            sf = max(self.start - 1, 0)
            a = min(a, (f - sf) / self.fade_in)
        if self.fade_out:
            a = min(a, (self.end - f) / self.fade_out)
        return max(0.0, min(1.0, a))


@dataclass
class Crop:
    png: Path
    x: int
    y: int
    w: int
    h: int


@dataclass
class Placement:
    sign: Sign
    yaw: float
    pitch: float
    roll: float
    key: str = ""
    crops: list = field(default_factory=list)


def parse_yaws(val: str) -> list[float]:
    out = []
    for part in val.split(","):
        p = part.strip().lower()
        if p in YAW_WORDS:
            out.append(float(YAW_WORDS[p]))
            continue
        try:
            y = float(p.rstrip("°").replace("deg", ""))
        except ValueError:
            raise ValueError(t("err_bad_yaw", val=val))
        y = (y + 180.0) % 360.0 - 180.0
        out.append(180.0 if y == -180.0 else y)
    return out


class Timeline:
    """Maps 'clip@time' to the output of xfade_concat, via its timeline JSON."""

    def __init__(self, path: Path | None):
        self.path = path
        self.clips = []
        if path and path.is_file():
            try:
                self.clips = json.loads(path.read_text(encoding="utf-8"))["clips"]
            except (OSError, ValueError, KeyError) as e:
                die("err_timeline", path=path, err=e)

    def resolve(self, name: str, t_s: float) -> float:
        ref = f"{name}@{hms(t_s)}"
        if not self.clips:
            raise ValueError(t("err_ref_no_timeline", ref=ref, path=self.path))
        key = name.strip().lower()
        relative = False
        if key.startswith("#") and key[1:].isdigit():
            n = int(key[1:])
            cands = [n - 1] if 1 <= n <= len(self.clips) else []
            relative = True
        else:
            by_title = [i for i, c in enumerate(self.clips)
                        if c.get("title", "").strip().lower() == key]
            if by_title:
                cands, relative = by_title, True
            else:
                cands = [i for i, c in enumerate(self.clips)
                         if Path(c["file"]).name.lower() == key
                         or Path(c["file"]).stem.lower().startswith(key)]
        if not cands:
            files = sorted({Path(c["file"]).name for c in self.clips})
            raise ValueError(t("err_ref_unknown", ref=ref, files=", ".join(files)))
        if relative:
            if len(cands) > 1:
                raise ValueError(t("err_ref_ambiguous", ref=ref,
                                   nums=", ".join(f"#{i + 1}" for i in cands)))
            c = self.clips[cands[0]]
            src = c["src_in_s"] + t_s
            if not c["src_in_s"] - 1e-6 <= src <= c["src_out_s"] + 1e-6:
                raise ValueError(t("err_ref_outside", ref=ref,
                                   ranges=f"0-{hms(c['src_out_s'] - c['src_in_s'])}"))
            return c["out_start_s"] + (src - c["src_in_s"]) / c.get("speed", 1.0)
        for i in cands:
            c = self.clips[i]
            if c["src_in_s"] - 1e-6 <= t_s < c["src_out_s"] + 1e-6:
                return c["out_start_s"] + (t_s - c["src_in_s"]) / c.get("speed", 1.0)
        ranges = ", ".join(f"#{i + 1} {hms(self.clips[i]['src_in_s'])}-"
                           f"{hms(self.clips[i]['src_out_s'])}" for i in cands)
        raise ValueError(t("err_ref_outside", ref=ref, ranges=ranges))


def to_frames(val: str, fps: Fraction, timeline: Timeline, what: str) -> int:
    """'1:35', '95.5', '2375f' or 'clip@0:12' -> output frame."""
    v = val.strip()
    if "@" in v:
        ref, _, tm = v.rpartition("@")
        try:
            secs_in = float(Fraction(parse_clock(tm)))
        except (ValueError, ZeroDivisionError):
            raise ValueError(t("err_time", what=what, val=val))
        return round(Fraction(timeline.resolve(ref, secs_in)) * fps)
    return parse_time(parse_clock(v), fps, what)


def read_titles(path: Path, fps: Fraction, total: int, timeline: Timeline, cli: dict,
                default_fade: str) -> tuple[list[Sign], dict]:
    styles: dict[str, dict] = {}
    rows = []
    current: str | None = None
    fname = path.name

    def fail(line, msg):
        die("err_syntax", file=fname, line=line, msg=msg)

    def kv(tok: str, line: int) -> tuple[str, str]:
        k, _, v = tok.partition("=")
        k = k.strip().lower()
        if k not in BUILTIN and k not in ROW_ONLY:
            fail(line, t("err_unknown_key", opt=k))
        return k, v

    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith("["):
            inner = stripped.split("]", 1)[0].strip("[ ").split()
            if len(inner) != 2 or inner[0].lower() != "style":
                fail(lineno, t("err_style_head"))
            current = inner[1].lower()
            styles.setdefault(current, {})
            continue
        toks = tokenize(raw, keep_quoted=True, hash_in_word=True)
        if not toks:
            continue
        first, first_q = toks[0]
        if current is not None and "=" in first and not first_q:
            for tok, _ in toks:
                if "=" not in tok:
                    fail(lineno, t("err_columns"))
                k, v = kv(tok, lineno)
                if k in ROW_ONLY:
                    fail(lineno, t("err_row_only", opt=k))
                styles[current][k] = v
            continue
        current = None
        if len(toks) < 4:
            fail(lineno, t("err_columns"))
        rows.append((lineno, toks))

    signs = []
    for lineno, toks in rows:
        (start_s, _), (dur_s, _), (yaw_s, _) = toks[:3]
        text, opts = None, {}
        for tok, quoted in toks[3:]:
            if "=" in tok and not quoted:
                k, v = kv(tok, lineno)
                opts[k] = v
            else:
                if text is not None:
                    fail(lineno, t("err_two_texts"))
                text = tok.replace("\\n", "\n")
        if text is None and not opts.get("image"):
            fail(lineno, t("err_no_text"))
        style = dict(BUILTIN)
        style.update(cli)
        style.update(styles.get("default", {}))
        sname = opts.pop("style", "").lower()
        if sname:
            if sname not in styles:
                known = ", ".join(sorted(styles)) or "-"
                fail(lineno, t("err_style", name=sname, known=known))
            style.update(styles[sname])
        image = opts.pop("image", "")
        style.update(opts)
        try:
            yaws = parse_yaws(yaw_s)
            start = to_frames(start_s, fps, timeline, "start")
            frames = parse_time(parse_clock(dur_s), fps, "dur")
            fade = style["fade"] or default_fade
            fi = parse_time(style["fade-in"] or fade, fps, "fade-in")
            fo = parse_time(style["fade-out"] or fade, fps, "fade-out")
        except ValueError as e:
            fail(lineno, str(e))
        if not 0 <= start < total:
            fail(lineno, t("err_start", at=hms(secs(start, fps)), len=hms(secs(total, fps))))
        if frames <= 0:
            fail(lineno, t("err_zero"))
        if start + frames > total:
            warn("warn_clamped", file=fname, line=lineno)
            frames = total - start
        if fi + fo > frames:
            warn("warn_fade_long", file=fname, line=lineno)
            scale = frames / (fi + fo)
            fi, fo = int(fi * scale), int(fo * scale)
        img_path = None
        if image:
            img_path = Path(image)
            if not img_path.is_absolute():
                img_path = path.resolve().parent / img_path
        signs.append(Sign(line=lineno, start=start, frames=frames, yaws=yaws,
                          text=text or "", image=img_path, style=style,
                          fade_in=fi, fade_out=fo))
    signs.sort(key=lambda s: (s.start, s.line))
    return signs, styles


# --------------------------------------------------------------------------- #
# fonts
# --------------------------------------------------------------------------- #
def font_dirs() -> list[Path]:
    home = Path.home()
    if os.name == "nt":
        dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
                / "Microsoft/Windows/Fonts"]
    elif sys.platform == "darwin":
        dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), home / "Library/Fonts"]
    else:
        dirs = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".fonts",
                home / ".local/share/fonts"]
    return [d for d in dirs if d.is_dir()]


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


class FontBook:
    """Finds fonts by file, by path, or by name ('Segoe UI Bold', 'Georgia:italic')."""

    def __init__(self, extra_dirs: list[Path]):
        self.extra = [d for d in extra_dirs if d.is_dir()]
        self._index: list[tuple[str, str, str, int]] | None = None  # family, style, path, idx
        self._cache: dict = {}

    @staticmethod
    def _cache_file() -> Path:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
            or str(Path.home() / ".cache")
        return Path(base) / "360-cli-tools" / "fonts.json"

    def index(self):
        if self._index is not None:
            return self._index
        dirs = self.extra + font_dirs()
        # the font folders' own timestamps change whenever a font is added or
        # removed, so they tell when the cached name list is stale
        stamp = [[str(d), d.stat().st_mtime_ns] for d in dirs]
        cache = self._cache_file()
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("stamp") == stamp and data.get("v") == __version__:
                self._index = [tuple(x) for x in data["fonts"]]
                return self._index
        except (OSError, ValueError):
            pass
        found = []
        for d in dirs:
            for p in sorted(d.rglob("*")):
                if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                    continue
                for idx in range(8 if p.suffix.lower() == ".ttc" else 1):
                    try:
                        f = ImageFont.truetype(str(p), 12, index=idx)
                    except OSError:
                        try:  # bitmap colour fonts exist at one size only
                            f = ImageFont.truetype(str(p), 109, index=idx)
                        except OSError:
                            break
                    fam, sty = f.getname()
                    found.append((fam or p.stem, sty or "Regular", str(p), idx))
        self._index = found
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"v": __version__, "stamp": stamp,
                                         "fonts": found}), encoding="utf-8")
        except OSError:
            pass
        return self._index

    def names(self) -> list[str]:
        return sorted({f"{fam} {sty}" for fam, sty, _, _ in self.index()})

    def locate(self, spec: str) -> tuple[str, int] | None:
        spec = spec.strip()
        if spec in self._cache:
            return self._cache[spec]
        hit = None
        p = Path(spec)
        if p.is_file():
            hit = (str(p), 0)
        elif p.suffix.lower() in (".ttf", ".otf", ".ttc"):
            for d in self.extra + font_dirs():
                if (d / p.name).is_file():
                    hit = (str(d / p.name), 0)
                    break
        else:
            fam, _, sty = spec.partition(":")
            want = _norm(fam + sty)
            idx = self.index()
            exact = [(pth, i) for f, s, pth, i in idx if _norm(f + s) == want]
            if not exact and not sty:
                # 'Georgia' alone -> its regular face
                exact = [(pth, i) for f, s, pth, i in idx
                         if _norm(f) == want and _norm(s) in ("regular", "book", "normal",
                                                               "roman", "medium")]
            if exact:
                hit = exact[0]
        self._cache[spec] = hit
        return hit

    def load(self, spec: str, px: int):
        if not spec:  # no usable system font at all: Pillow's own
            return ImageFont.load_default(max(1, px))
        hit = self.locate(spec)
        if hit is None:
            alts = difflib.get_close_matches(spec, self.names(), n=4, cutoff=0.5)
            hint = t("err_font_hint", alts=", ".join(alts)) if alts else ""
            die("err_font", name=spec, hint=hint)
        return ImageFont.truetype(hit[0], max(1, px), index=hit[1])

    def default_text(self) -> str:
        for name in DEFAULT_FONTS:
            if self.locate(name):
                return name
        return ""

    def emoji(self, spec: str) -> str | None:
        for name in ([spec] if spec else []) + EMOJI_FONTS:
            if self.locate(name):
                return name
        return None


def is_emoji(ch: str, nxt: str) -> bool:
    cp = ord(ch)
    return (0x1F000 <= cp <= 0x1FAFF or 0x1F1E6 <= cp <= 0x1F1FF
            or (0x2300 <= cp <= 0x2BFF and nxt == "\ufe0f"))


def split_runs(line: str) -> list[tuple[str, bool]]:
    """Split a line into (text, is_emoji) runs; joiners and modifiers stay with
    the emoji they belong to."""
    runs: list[tuple[str, bool]] = []
    i = 0
    while i < len(line):
        ch = line[i]
        nxt = line[i + 1] if i + 1 < len(line) else ""
        prev_emo = bool(runs) and runs[-1][1]
        emo = is_emoji(ch, nxt) or (prev_emo and ch in "\ufe0f\u200d\U0001f3fb\U0001f3fc"
                                                     "\U0001f3fd\U0001f3fe\U0001f3ff")
        if prev_emo and i and line[i - 1] == "\u200d":
            emo = True   # the emoji after a zero-width joiner
        if runs and runs[-1][1] == emo:
            runs[-1] = (runs[-1][0] + ch, emo)
        else:
            runs.append((ch, emo))
        i += 1
    return runs


# --------------------------------------------------------------------------- #
# drawing a sign (flat)
# --------------------------------------------------------------------------- #
class Look:
    """A resolved style: every value parsed, lengths in supersampled pixels."""

    def __init__(self, st: dict, px_per_deg: float, where: tuple[str, int]):
        self.where = where
        ss = SUPERSAMPLE
        self.px_per_deg = px_per_deg
        self.font_px = self.angle(st["size"], "size") * ss
        F = self.font_px
        self.font = st["font"]
        self.color = self.colour(st["color"], "color")
        self.opacity = self.number(st["opacity"], "opacity")
        self.outline = self.length(st["outline"], F, "outline")
        self.outline_color = self.colour(st["outline-color"], "outline-color")
        self.align = st["align"].lower()
        if self.align not in ("left", "center", "centre", "right"):
            self.bad("align", st["align"], "why_align")
        self.line_spacing = self.number(st["line-spacing"], "line-spacing")
        self.box = self.colour(st["box"], "box", allow_none=True)
        self.box_opacity = self.number(st["box-opacity"], "box-opacity")
        pad = self.pair(st["padding"], F, "padding")
        self.pad = pad if pad else (0.0, 0.0)
        self.radius = self.length(st["radius"], F, "radius")
        self.border, self.border_w = None, 0.0
        if st["border"].strip().lower() not in ("", "none", "off"):
            parts = st["border"].split(",")
            self.border = self.colour(parts[0], "border")
            self.border_w = self.length(parts[1], F, "border") if len(parts) > 1 else 0.04 * F
        self.shadow = self.pair(st["shadow"], F, "shadow")
        self.shadow_blur = self.length(st["shadow-blur"], F, "shadow-blur")
        self.shadow_color = self.colour(st["shadow-color"], "shadow-color")
        self.shadow_opacity = self.number(st["shadow-opacity"], "shadow-opacity")
        g = st["glow"].strip().lower()
        self.glow = None if g in ("", "none", "off", "0") else self.length(g, F, "glow")
        self.glow_color = self.colour(st["glow-color"], "glow-color")
        self.glow_opacity = self.number(st["glow-opacity"], "glow-opacity")
        self.extrude = self.pair(st["extrude"], F, "extrude")
        ec = st["extrude-color"].strip()
        self.extrude_color = (self.colour(ec, "extrude-color") if ec else
                              tuple(int(c * 0.45) for c in self.color[:3]) + (255,))
        self.pitch = self.number(st["pitch"], "pitch")
        self.roll = self.number(st["roll"], "roll")
        w = st["width"].strip()
        self.width_px = self.angle(w, "width") * ss if w else None
        self.emoji_font = st["emoji-font"]

    # -- value parsing; errors point at the titles file line
    def bad(self, key, val, why):
        die("err_syntax", file=self.where[0], line=self.where[1],
            msg=t("err_bad_value", opt=key, val=val, why=t(why)))

    def number(self, v, key) -> float:
        try:
            return float(v)
        except ValueError:
            self.bad(key, v, "why_number")

    def length(self, v, font_px, key) -> float:
        s = str(v).strip().lower()
        if s in ("", "none", "off"):
            return 0.0
        try:
            if s.endswith("%"):
                return float(s[:-1]) / 100.0 * font_px
            if s.endswith("em"):
                return float(s[:-2]) * font_px
            return float(s.removesuffix("px")) * SUPERSAMPLE
        except ValueError:
            self.bad(key, v, "why_length")

    def pair(self, v, font_px, key):
        s = str(v).strip().lower()
        if s in ("", "none", "off"):
            return None
        parts = s.split(",")
        if len(parts) == 1:
            parts = parts * 2
        if len(parts) != 2:
            self.bad(key, v, "why_pair")
        return (self.length(parts[0], font_px, key), self.length(parts[1], font_px, key))

    def colour(self, v, key, allow_none=False):
        s = str(v).strip()
        if allow_none and s.lower() in ("", "none", "off"):
            return None
        try:
            c = ImageColor.getrgb(s)
        except ValueError:
            self.bad(key, v, "why_color")
        return c if len(c) == 4 else (*c, 255)

    def angle(self, v, key) -> float:
        """Degrees of view -> output pixels (at 1x)."""
        s = str(v).strip().lower().replace("°", "").replace("deg", "")
        try:
            if s.endswith("px"):
                return float(s[:-2])
            return float(s) * self.px_per_deg
        except ValueError:
            self.bad(key, v, "why_angle")


def _alpha_scaled(im, factor: float):
    """Multiply an RGBA image's alpha by factor (0..1)."""
    if factor >= 0.999:
        return im
    r, g, b, a = im.split()
    a = a.point(lambda v: int(v * factor + 0.5))
    return Image.merge("RGBA", (r, g, b, a))


def _solid(size, colour, mask):
    """An RGBA layer of one colour, shaped by mask (L) and the colour's own alpha."""
    layer = Image.new("RGBA", size, colour[:3] + (0,))
    a = mask
    if colour[3] < 255:
        a = mask.point(lambda v: v * colour[3] // 255)
    layer.putalpha(a)
    return layer


def draw_content(sign: Sign, look: Look, fonts: FontBook, emoji_state: dict):
    """The sign's content (text lines or image) at supersampled size.

    Returns (rgba, block_w, block_h, x_off, y_off): the content drawn on a
    canvas with room for the outline, and where the layout block sits in it."""
    F = look.font_px
    if sign.image:
        try:
            src = Image.open(sign.image).convert("RGBA")
        except OSError as e:
            die("err_image", path=sign.image, err=e)
        if look.width_px:
            w = round(look.width_px)
            h = max(1, round(src.height * w / src.width))
        else:
            h = round(F)
            w = max(1, round(src.width * h / src.height))
        img = src.resize((w, h), Image.LANCZOS)
        return img, w, h, 0, 0

    font = fonts.load(look.font, round(F))
    asc, desc = font.getmetrics()
    line_h = (asc + desc) * look.line_spacing
    efont_name = None
    if any(e for ln in sign.text.split("\n") for _, e in split_runs(ln)):
        efont_name = fonts.emoji(look.emoji_font)
        if efont_name is None and not emoji_state.get("warned"):
            warn("warn_emoji_font")
            emoji_state["warned"] = True

    def emoji_img(run: str):
        """Colour emoji, scaled to the text size. Bitmap emoji fonts (Noto)
        only exist at one pixel size, so draw at that and scale."""
        path, idx = fonts.locate(efont_name)
        try:
            ef = ImageFont.truetype(path, round(F), index=idx)
            ef.getbbox(run, mode="RGBA")
        except OSError:
            ef = ImageFont.truetype(path, 109, index=idx)
        bb = ef.getbbox(run, anchor="ls", mode="RGBA")
        im = Image.new("RGBA", (max(1, bb[2] - bb[0]), max(1, bb[3] - bb[1])), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((-bb[0], -bb[1]), run, font=ef, anchor="ls",
                                embedded_color=True)
        scale = F / ef.size
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       Image.LANCZOS)
        adv = ef.getlength(run) * scale
        return im, round(-bb[1] * scale), adv

    lines = []
    for ln in sign.text.split("\n"):
        runs, width = [], 0.0
        for run, emo in split_runs(ln):
            if emo and efont_name:
                im, base, adv = emoji_img(run)
                runs.append(("img", im, base, width))
                width += adv
            else:
                runs.append(("txt", run, 0, width))
                width += font.getlength(run)
        lines.append((runs, width))
    block_w = max((w for _, w in lines), default=0.0)
    block_h = (len(lines) - 1) * line_h + asc + desc
    o = math.ceil(look.outline) + 2
    cw, ch = math.ceil(block_w) + 2 * o, math.ceil(block_h) + 2 * o
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    fill = look.color
    stroke = round(look.outline)
    for i, (runs, width) in enumerate(lines):
        if look.align == "left":
            x0 = o
        elif look.align == "right":
            x0 = o + block_w - width
        else:
            x0 = o + (block_w - width) / 2
        base_y = o + asc + i * line_h
        for kind, what, base, dx in runs:
            if kind == "txt":
                draw.text((x0 + dx, base_y), what, font=font, anchor="ls", fill=fill,
                          stroke_width=stroke, stroke_fill=look.outline_color)
            else:
                canvas.alpha_composite(what, (round(x0 + dx), round(base_y - base)))
    return canvas, block_w, block_h, o, o


def render_flat(sign: Sign, look: Look, fonts: FontBook, emoji_state: dict):
    """Draw the whole sign flat, centred on its plate/text block, and return it
    premultiplied (RGBa) at output resolution."""
    content, bw, bh, cx0, cy0 = draw_content(sign, look, fonts, emoji_state)
    content = _alpha_scaled(content, look.opacity)
    plate = look.box is not None or look.border is not None
    px, py = look.pad if plate else (0.0, 0.0)
    pw, ph = bw + 2 * px, bh + 2 * py          # plate (or text block) size

    # room around the plate for everything that reaches outside it
    reach = [look.outline + 2]
    if look.shadow:
        reach.append(max(abs(look.shadow[0]), abs(look.shadow[1])) + 3 * look.shadow_blur)
    if look.glow:
        reach.append(3 * look.glow)
    if look.extrude:
        reach.append(max(abs(look.extrude[0]), abs(look.extrude[1])) + look.outline)
    m = math.ceil(max(reach)) + 4
    W = math.ceil(pw) + 2 * m
    H = math.ceil(ph) + 2 * m
    W += W % 2
    H += H % 2
    cxp, cyp = W / 2, H / 2                     # plate centre = sign centre
    # where the content canvas goes so its layout block is centred
    ox = round(cxp - bw / 2 - cx0)
    oy = round(cyp - bh / 2 - cy0)

    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text_layer.alpha_composite(content, (ox, oy))
    text_alpha = text_layer.getchannel("A")

    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    plate_mask = None
    if plate:
        plate_mask = Image.new("L", (W, H), 0)
        box = (cxp - pw / 2, cyp - ph / 2, cxp + pw / 2, cyp + ph / 2)
        ImageDraw.Draw(plate_mask).rounded_rectangle(box, radius=look.radius, fill=255)

    if look.shadow:
        src = plate_mask if plate_mask is not None else text_alpha
        dx, dy = look.shadow
        sh = ImageChops.offset(src, round(dx), round(dy))
        if look.shadow_blur > 0:
            sh = sh.filter(ImageFilter.GaussianBlur(look.shadow_blur))
        out.alpha_composite(_alpha_scaled(_solid((W, H), look.shadow_color, sh),
                                          look.shadow_opacity))
    if plate_mask is not None:
        if look.box is not None:
            out.alpha_composite(_alpha_scaled(_solid((W, H), look.box, plate_mask),
                                              look.box_opacity))
        if look.border is not None and look.border_w > 0:
            ring = Image.new("L", (W, H), 0)
            box = (cxp - pw / 2, cyp - ph / 2, cxp + pw / 2, cyp + ph / 2)
            ImageDraw.Draw(ring).rounded_rectangle(box, radius=look.radius, outline=255,
                                                   width=max(1, round(look.border_w)))
            out.alpha_composite(_solid((W, H), look.border, ring))
    if look.glow:
        g = text_alpha.filter(ImageFilter.GaussianBlur(look.glow))
        g = g.point(lambda v: min(255, v * 2))
        out.alpha_composite(_alpha_scaled(_solid((W, H), look.glow_color, g),
                                          look.glow_opacity))
    if look.extrude:
        dx, dy = look.extrude
        steps = max(1, math.ceil(max(abs(dx), abs(dy))))
        depth = Image.new("L", (W, H), 0)
        for k in range(1, steps + 1):
            depth = ImageChops.lighter(depth, ImageChops.offset(
                text_alpha, round(dx * k / steps), round(dy * k / steps)))
        out.alpha_composite(_alpha_scaled(_solid((W, H), look.extrude_color, depth),
                                          look.opacity))
    out.alpha_composite(text_layer)

    # reduce: an exact box filter over premultiplied colour
    small = out.convert("RGBa").reduce(SUPERSAMPLE)
    return small


# --------------------------------------------------------------------------- #
# projection onto the sphere
# --------------------------------------------------------------------------- #
def _rot(yaw: float, pitch: float, roll: float):
    """3x3 rotation taking the sign's own frame (x right, y up, z forward) to the
    world's: roll about the view axis (clockwise +), then pitch (up +), then yaw
    (right +)."""
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    cr, sr = math.cos(math.radians(roll)), math.sin(math.radians(roll))
    Ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    Rp = [[1, 0, 0], [0, cp, sp], [0, -sp, cp]]
    Rr = [[cr, sr, 0], [-sr, cr, 0], [0, 0, 1]]

    def mul(A, B):
        return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mul(Ry, mul(Rp, Rr))


def project(flat, eq_w: int, eq_h: int, yaw: float, pitch: float, roll: float,
            where: tuple[str, int]) -> list[tuple]:
    """Wrap a flat, premultiplied sign onto the equirectangular frame.

    The flat image is treated as a pinhole (gnomonic) view whose centre pixel
    density matches the equirect's, so the sign keeps its drawn size at its
    centre. Only the area the sign covers is computed: its outline is traced
    onto the sphere to find that area, and a fine mesh of small quads (Pillow's
    MESH transform, bicubic) maps it from the flat image. Colours are resampled
    premultiplied, which makes the edges exact.

    Returns [(RGBA image, x, y)] - two pieces when the sign straddles the
    left/right seam behind the viewer."""
    fw, fh = flat.size
    f = eq_w / (2 * math.pi)                  # equirect pixels per radian
    h_fov = 2 * math.degrees(math.atan(fw / 2 / f))
    v_fov = 2 * math.degrees(math.atan(fh / 2 / f))
    if h_fov > 150 or v_fov > 150:
        die("err_syntax", file=where[0], line=where[1],
            msg=t("err_too_wide", deg=max(h_fov, v_fov)))
    R = _rot(yaw, pitch, roll)
    Rt = [[R[j][i] for j in range(3)] for i in range(3)]   # inverse = transpose
    cx, cy = fw / 2, fh / 2

    def flat_to_eq(px, py):
        lx, ly, lz = px - cx, -(py - cy), f
        wx = R[0][0] * lx + R[0][1] * ly + R[0][2] * lz
        wy = R[1][0] * lx + R[1][1] * ly + R[1][2] * lz
        wz = R[2][0] * lx + R[2][1] * ly + R[2][2] * lz
        lon = math.degrees(math.atan2(wx, wz))
        lat = math.degrees(math.atan2(wy, math.hypot(wx, wz)))
        dl = (lon - yaw + 180) % 360 - 180             # unwrapped around the sign
        return (yaw + dl + 180) / 360 * eq_w, (90 - lat) / 180 * eq_h

    def eq_to_flat(x, y):
        lon = math.radians(x / eq_w * 360 - 180)
        lat = math.radians(90 - y / eq_h * 180)
        wx, wy, wz = math.cos(lat) * math.sin(lon), math.sin(lat), math.cos(lat) * math.cos(lon)
        lx = Rt[0][0] * wx + Rt[0][1] * wy + Rt[0][2] * wz
        ly = Rt[1][0] * wx + Rt[1][1] * wy + Rt[1][2] * wz
        lz = Rt[2][0] * wx + Rt[2][1] * wy + Rt[2][2] * wz
        if lz <= 1e-6:
            return None
        return cx + f * lx / lz, cy - f * ly / lz

    # trace the outline to find the covered area
    n = 64
    edge = ([(fw * k / n, 0) for k in range(n + 1)] + [(fw * k / n, fh) for k in range(n + 1)]
            + [(0, fh * k / n) for k in range(n + 1)] + [(fw, fh * k / n) for k in range(n + 1)])
    pts = [flat_to_eq(px, py) for px, py in edge]
    x0, x1 = min(p[0] for p in pts) - 4, max(p[0] for p in pts) + 4
    y0, y1 = min(p[1] for p in pts) - 4, max(p[1] for p in pts) + 4
    for pole_y, pole in ((0.0, (0, 1, 0)), (float(eq_h), (0, -1, 0))):
        # a sign covering a pole spans the whole width
        lx = sum(Rt[0][k] * pole[k] for k in range(3))
        ly = sum(Rt[1][k] * pole[k] for k in range(3))
        lz = sum(Rt[2][k] * pole[k] for k in range(3))
        if lz > 0 and 0 <= cx + f * lx / lz <= fw and 0 <= cy - f * ly / lz <= fh:
            x0, x1 = 0, eq_w
            y0, y1 = min(y0, pole_y), max(y1, pole_y)
    y0, y1 = max(0, math.floor(y0)), min(eq_h, math.ceil(y1))
    x0, x1 = math.floor(x0), math.ceil(x1)
    if x1 - x0 >= eq_w:
        x0, x1 = 0, eq_w
    spans = []   # (x from, x to) in real pixel columns
    if x0 < 0:
        spans += [(x0 + eq_w, eq_w), (0, x1)]
    elif x1 > eq_w:
        spans += [(x0, eq_w), (0, x1 - eq_w)]
    else:
        spans.append((x0, x1))

    cell = 8
    out = []
    for sx0, sx1 in spans:
        sx0 -= sx0 % 2
        sx1 = min(eq_w, sx1 + sx1 % 2)
        ry0, ry1 = y0 - y0 % 2, min(eq_h, y1 + y1 % 2)
        w, h = sx1 - sx0, ry1 - ry0
        if w <= 0 or h <= 0:
            continue
        xs = list(range(0, w, cell)) + [w]
        ys = list(range(0, h, cell)) + [h]
        grid = [[eq_to_flat(sx0 + x, ry0 + y) for x in xs] for y in ys]
        mesh = []
        for j in range(len(ys) - 1):
            for i in range(len(xs) - 1):
                q = (grid[j][i], grid[j + 1][i], grid[j + 1][i + 1], grid[j][i + 1])
                if None in q:
                    continue
                # skip cells that miss the flat image entirely
                if max(p[0] for p in q) < -2 or min(p[0] for p in q) > fw + 2 or \
                        max(p[1] for p in q) < -2 or min(p[1] for p in q) > fh + 2:
                    continue
                mesh.append(((xs[i], ys[j], xs[i + 1], ys[j + 1]),
                             tuple(c for p in q for c in p)))
        if not mesh:
            continue
        img = flat.transform((w, h), Image.MESH, mesh, resample=Image.BICUBIC)
        img = img.convert("RGBA")
        bb = img.getchannel(3).getbbox()
        if not bb:
            continue
        bx0, by0 = bb[0] - bb[0] % 2, bb[1] - bb[1] % 2
        bx1, by1 = min(w, bb[2] + bb[2] % 2), min(h, bb[3] + bb[3] % 2)
        out.append((img.crop((bx0, by0, bx1, by1)), sx0 + bx0, ry0 + by0))
    return out


def sign_key(sign: Sign, yaw: float, eq_w: int, eq_h: int) -> str:
    img = None
    if sign.image:
        st = sign.image.stat() if sign.image.is_file() else None
        img = [str(sign.image), st.st_size if st else 0, st.st_mtime_ns if st else 0]
    spec = {"style": {k: v for k, v in sign.style.items()
                      if k not in ("fade", "fade-in", "fade-out")},
            "text": sign.text, "img": img, "yaw": yaw, "eq": [eq_w, eq_h],
            "ss": SUPERSAMPLE, "v": __version__}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]


def check_signs(signs: list[Sign], fonts: FontBook, eq_w: int, titles_name: str) -> list:
    """Parse every sign's style and find its font and image, so that a mistake in
    the titles file stops the run before anything is drawn or encoded."""
    looks = []
    for s in signs:
        if not s.style["font"]:
            s.style["font"] = fonts.default_text()
        look = Look(s.style, eq_w / 360.0, (titles_name, s.line))
        if s.image:
            if not s.image.is_file():
                die("err_image", path=s.image, err=t("not_found"))
        else:
            fonts.load(look.font, 10)
        looks.append(look)
    return looks


def build_placements(signs: list[Sign], args, fonts: FontBook, eq_w: int, eq_h: int,
                     work: Path, titles_name: str, verbose: bool) -> None:
    """Draw and project every sign in every direction it is shown (cached)."""
    emoji_state: dict = {}
    todo = []
    for s, look in zip(signs, check_signs(signs, fonts, eq_w, titles_name)):
        for yaw in s.yaws:
            p = Placement(sign=s, yaw=yaw, pitch=look.pitch, roll=look.roll,
                          key=sign_key(s, yaw, eq_w, eq_h))
            s.placements.append(p)
            todo.append((p, look))
    work.mkdir(parents=True, exist_ok=True)
    print(t("rendering", n=len(todo)))
    for i, (p, look) in enumerate(todo, 1):
        meta = work / f"sign_{p.key}.json"
        if meta.is_file():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                p.crops = [Crop(work / c["png"], c["x"], c["y"], c["w"], c["h"]) for c in data]
                if all(c.png.is_file() for c in p.crops):
                    continue
            except (OSError, ValueError, KeyError):
                pass
        label = f"{p.sign.what()} yaw {p.yaw:g}"
        print(t("render_one", i=i, n=len(todo), label=label) + ("" if not verbose else
                                                                  f" [{p.key}]"))
        flat = render_flat(p.sign, look, fonts, emoji_state)
        deg = 2 * math.degrees(math.atan(math.pi * flat.width / eq_w))
        if deg > 120:
            warn("warn_wide", file=titles_name, line=p.sign.line, deg=deg)
        p.crops = []
        for j, (img, x, y) in enumerate(project(flat, eq_w, eq_h, p.yaw, p.pitch, p.roll,
                                                (titles_name, p.sign.line))):
            png = work / f"sign_{p.key}_{j}.png"
            img.save(png, compress_level=1)
            p.crops.append(Crop(png, x, y, img.width, img.height))
        meta.write_text(json.dumps([{"png": c.png.name, "x": c.x, "y": c.y, "w": c.w,
                                     "h": c.h} for c in p.crops]), encoding="utf-8")


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
@dataclass
class Piece:
    kind: str               # "copy" | "plain" | "titled"
    start: int
    frames: int
    file: Path | None = field(default=None, repr=False)

    @property
    def end(self) -> int:
        return self.start + self.frames


def windows(signs: list[Sign]) -> list[list[int]]:
    """Frames where any sign shows, merged into disjoint [start, end) stretches."""
    out: list[list[int]] = []
    for s in sorted(signs, key=lambda s: s.start):
        if out and s.start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], s.end)
        else:
            out.append([s.start, s.end])
    return out


def keyframe_spans(src, wins, total: int, fps: Fraction, w: float) -> list:
    """Where to look for keyframes: forward from the start of every gap between
    signs, and backward from its end."""
    spans = []
    edges = [0] + [x for win in wins for x in win] + [total]
    for a, b in zip(edges[::2], edges[1::2]):
        if b <= a:
            continue
        ta, tb = secs(a, fps) + src.v_start, secs(b, fps) + src.v_start
        spans.append((max(0.0, ta - 1.0), ta + w))
        if b < total:
            spans.append((max(0.0, tb - w), tb + 1.0))
    return spans


def build_plan(src, signs: list[Sign], total: int, smart: bool, min_copy: int,
               fps: Fraction) -> list[Piece]:
    """Lay the video out as copy / plain / titled pieces.

    Stretches with a sign are 'titled'. Every gap between them is stream-copied
    from its first to its last safe keyframe; the few frames between a sign and
    the nearest keyframe join the titled piece next to them. A gap with no two
    usable keyframes is re-encoded as 'plain' (no overlays - stays on the GPU)."""
    wins = windows(signs)
    safe = safe_keyframes(src) if smart else []
    segs: list[list] = []   # [kind, start, end]

    def add(kind, a, b):
        if b <= a:
            return
        if segs and segs[-1][0] == kind == "titled" and segs[-1][2] == a:
            segs[-1][2] = b
        else:
            segs.append([kind, a, b])

    edges = [0] + [x for win in wins for x in win] + [total]
    gaps = list(zip(edges[::2], edges[1::2]))
    for gi, (a, b) in enumerate(gaps):
        if gi:
            add("titled", *wins[gi - 1])
        if b <= a:
            continue
        ks = next((k for k in safe if a <= k < b), None)
        ends = [k for k in safe if a < k <= b]
        if b == total:
            ends.append(total)            # end of file is a safe cut point too
        ke = max(ends) if ends else None
        if smart and ks is not None and ke is not None and ke - ks >= min_copy:
            add("titled" if a else "plain", a, ks)
            add("copy", ks, ke)
            add("titled" if b < total else "plain", ke, b)
        else:
            # a gap shorter than the source's GOP simply has no room to copy;
            # only a video without any usable keyframe is worth a warning
            if smart and not safe and b - a > 2 * min_copy:
                warn("warn_no_idr", at=hms(secs(a, fps)))
                safe = [-1]  # warn once
            add("plain", a, b)
    pieces = [Piece(k, a, b - a) for k, a, b in segs]
    # 'titled' pieces that hold no sign after all (lead-ins at the file edges)
    for p in pieces:
        if p.kind == "titled" and not any(s.start < p.end and s.end > p.start for s in signs):
            p.kind = "plain"
    # merge neighbouring plain pieces
    merged: list[Piece] = []
    for p in pieces:
        if merged and merged[-1].kind == p.kind == "plain":
            merged[-1].frames += p.frames
        else:
            merged.append(p)
    return merged


# --------------------------------------------------------------------------- #
# ffmpeg commands
# --------------------------------------------------------------------------- #
class TEnc(Encoder):
    """Pieces for titles_in_360.

    The video these pieces come from is usually an xfade_concat output, which
    switches parameter sets where a re-encoded piece meets a copied one. At
    such a switch ffmpeg rebuilds the whole filter graph - always when NVDEC
    starts a new hardware frame context, and on a colour-metadata change too -
    and every counter in the graph starts again from 0: setpts=N, enable=n, the
    fades. The picture then jumps. So frames leave NVDEC as ordinary frames
    (they go to the CPU for the overlays anyway), and -reinit_filter 0 keeps
    the graph from being rebuilt for any other change."""

    def input_args(self, clip, frame: int) -> list[str]:
        """Start decoding at the last safe keyframe at or before `frame`; the
        chain then skips the frames up to `frame` by count (see skip()).

        Seeking by time is not reliable here: a file joined from raw pieces
        (an xfade_concat output) carries decode-order timestamps inside its
        B-frame stretches, so an accurate seek lands a frame or two off. A
        keyframe's own timestamp is always right, and frames always come out of
        the decoder in the right order - so keyframe plus a frame count is
        exact. The seek is made in the file's raw timeline, like a copy piece's."""
        a = ["-hwaccel", "cuda"] if self.hw else []
        a += ["-reinit_filter", "0"]
        k = self.start_keyframe(clip, frame)
        if k is not None and k > 0:
            kf = clip.keyframes[k]
            seek = float(kf.pts * clip.tb) + clip.raw_offset + 0.5 / float(clip.fps)
            a += ["-ignore_editlist", "1", "-noaccurate_seek", "-ss", f"{seek:.6f}"]
        elif k is None and frame > 0:
            # no keyframe known: fall back to an accurate seek by time
            t0 = (clip.v_start - clip.f_start) + (frame - 0.5) / float(clip.fps)
            a += ["-ss", f"{t0:.6f}"]
        return a + ["-i", str(clip.path)]

    @staticmethod
    def start_keyframe(clip, frame: int) -> int | None:
        if frame <= 0:
            return 0
        ks = [k for k, kf in clip.keyframes.items() if kf.safe and 0 < k <= frame]
        return max(ks) if ks else None

    def skip(self, clip, frame: int) -> str:
        """Filters dropping the frames between the keyframe and `frame`."""
        k = self.start_keyframe(clip, frame)
        if k is None or frame - k <= 0:
            return ""
        return f"trim=start_frame={frame - k},"

    def to_cpu(self) -> str:
        return f"format={self.planar}"

    def overlays(self, p: Piece, signs: list[Sign]):
        """(crop, first frame, frames, fade_in, fade_out) for every sign in piece p,
        frames relative to the piece."""
        out = []
        for s in signs:
            if s.start < p.end and s.end > p.start:
                rel = s.start - p.start
                for pl in s.placements:
                    for c in pl.crops:
                        out.append((c, rel, s.frames, s.fade_in, s.fade_out))
        return out

    def piece_cmd(self, p: Piece, src, signs: list[Sign], out: Path) -> list[str]:
        if p.kind == "copy":
            return self.copy_cmd(src, p.start, p.frames, out)
        cmd = self.base() + self.input_args(src, p.start)
        skip = self.skip(src, p.start)
        if p.kind == "plain":
            return cmd + ["-filter_complex", f"[0:v:0]{skip}{RENUMBER}[v]", "-map", "[v]"] + \
                self.encode_tail(p.frames, out)
        ovs = self.overlays(p, signs)
        fps = str(self.fps)
        graph = [f"[0:v:0]{skip}{RENUMBER},{self.to_cpu()}[b0]"]
        ofmt = "yuv420p10" if self.depth == 10 else "yuv420"
        for i, (c, rel, n, fi, fo) in enumerate(ovs, 1):
            cmd += ["-loop", "1", "-framerate", fps, "-i", str(c.png)]
            chain = [f"[{i}:v]format=rgba"]
            if fi:
                chain.append(f"fade=t=in:start_frame={max(rel - 1, 0)}:nb_frames={fi}:alpha=1")
            if fo:
                chain.append(f"fade=t=out:start_frame={rel + n - fo}:nb_frames={fo}:alpha=1")
            graph.append(",".join(chain) + f"[o{i}]")
            graph.append(f"[b{i - 1}][o{i}]overlay=x={c.x}:y={c.y}:format={ofmt}:"
                         f"eof_action=pass:enable='between(n,{rel},{rel + n - 1})'[b{i}]")
        # exact frame slots on the way out as well, so nothing downstream
        # depends on how the overlays' frame sync stamps its output
        graph.append(f"[b{len(ovs)}]{RENUMBER},format={self.encfmt}[v]")
        return cmd + ["-filter_complex", ";".join(graph), "-map", "[v]"] + \
            self.encode_tail(p.frames, out)


def piece_key(p: Piece, src, enc: TEnc, signs: list[Sign]) -> str:
    st = src.path.stat()
    ovs = [[c.png.name, c.x, c.y, rel, n, fi, fo]
           for c, rel, n, fi, fo in (enc.overlays(p, signs) if p.kind == "titled" else [])]
    spec = {"p": [p.kind, p.start, p.frames], "src": [str(src.path), st.st_size,
                                                        st.st_mtime_ns],
            "ov": ovs, "enc": enc.video_args, "bsf": enc.enc_bsf, "hw": enc.hw,
            "v": __version__}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# preview, subtitles, template
# --------------------------------------------------------------------------- #
def write_previews(args, src, signs: list[Sign], fps: Fraction, total: int,
                   dest_dir: Path) -> None:
    """One still per requested time: the whole equirect frame (small), plus a
    flat view of what a viewer sees looking at each sign shown then."""
    frames = []
    for v in args.preview.split(","):
        v = v.strip()
        if v.lower() in ("all", "alla"):
            for s in signs:
                steady = s.start + s.fade_in + (s.frames - s.fade_in - s.fade_out) // 2
                frames.append(min(steady, s.end - 1))
            continue
        try:
            frames.append(to_frames(v, fps, args.timeline_obj, "--preview"))
        except ValueError:
            die("err_preview_time", val=v)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(set(min(max(0, x), total - 1) for x in frames)):
        active = [s for s in signs if s.alpha_at(f) > 0]
        stamp = hms(secs(f, fps)).replace(":", "-")
        # half a frame early: the accurate seek then starts exactly at frame f
        t0 = (src.v_start - src.f_start) + (f - 0.5) / float(fps)
        cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
               "-ss", f"{max(0.0, t0):.6f}", "-i", str(src.path)]
        graph = ["[0:v:0]format=rgb24[b0]"]
        n = 0
        for s in active:
            for pl in s.placements:
                for c in pl.crops:
                    n += 1
                    cmd += ["-i", str(c.png)]
                    graph.append(f"[{n}:v]format=rgba,colorchannelmixer=aa={s.alpha_at(f):.4f}"
                                 f"[o{n}]")
                    graph.append(f"[b{n - 1}][o{n}]overlay=x={c.x}:y={c.y}:format=rgb[b{n}]")
        views = []
        for s in active:
            for pl in s.placements:
                views.append((pl.yaw, pl.pitch))
        if not views:
            views = [(0.0, 0.0)]
        views = list(dict.fromkeys(views))
        graph.append(f"[b{n}]split={len(views) + 1}" + "".join(f"[s{i}]"
                                                               for i in range(len(views) + 1)))
        eq_w = min(src.width, 3840)
        graph.append(f"[s0]scale={eq_w}:-2:flags=area[eq]")
        outs = ["-map", "[eq]", "-frames:v", "1", "-q:v", "3",
                str(dest_dir / f"preview_{stamp}_equirect.jpg")]
        for i, (yaw, pitch) in enumerate(views, 1):
            graph.append(f"[s{i}]v360=input=e:output=flat:h_fov=100:v_fov=65:w=1920:h=1080:"
                         f"yaw={yaw:g}:pitch={pitch:g}:interp=cubic[v{i}]")
            name = f"preview_{stamp}_yaw{yaw:g}" + (f"_pitch{pitch:g}" if pitch else "") + ".jpg"
            outs += ["-map", f"[v{i}]", "-frames:v", "1", "-q:v", "3", str(dest_dir / name)]
        engine.run(cmd + ["-filter_complex", ";".join(graph)] + outs, args.verbose)
        print(t("preview_written", file=dest_dir / f"preview_{stamp}_*.jpg"))


def srt_time(frames: int, fps: Fraction) -> str:
    ms = round(secs(frames, fps) * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path: Path, signs: list[Sign], fps: Fraction) -> None:
    rows = [s for s in signs if s.text and not s.image]
    out = []
    for i, s in enumerate(rows, 1):
        out.append(f"{i}\n{srt_time(s.start, fps)} --> {srt_time(s.end, fps)}\n{s.text}\n")
    path.write_text("\n".join(out), encoding="utf-8")
    print(t("srt_written", file=path))


TEMPLATE = {
    "en": '''# Titles for titles_in_360 {v}. Run:
#   python titles_in_360.py <video.mp4> --titles {name} -o <out.mp4>
#   python titles_in_360.py <video.mp4> --titles {name} --preview all   (stills only)
#
# One sign per row:   <start>  <dur>  <yaw>  "text" or image=file.png   [options]
#   start  1:35, 95.5, 2375f - or a place in a source clip: GS010109@0:12,
#          "Take off"@0:05 (clip title) or #3@0:05 (clip number), read from the
#          <video>.timeline.json that xfade_concat writes
#   dur    how long it shows, fades included
#   yaw    0 = ahead, 90 = right, -90 = left, 180 = behind (or front/right/back/left);
#          several with commas: 0,180
# Options (on a row, or in a [style] block; later wins:
#   built-in < command line < [style default] < style=name < the row):
#   size=5 (degrees; or 120px)  font="Segoe UI Bold" (or a .ttf file)  color=white
#   opacity=1  outline=3%  outline-color=black  align=center  line-spacing=1.15
#   box=#003366 (or none)  box-opacity=0.6  padding=40%,20%  radius=25%  border=#ffd700,3
#   shadow=4%,4% (or none)  shadow-blur=5%  shadow-color=black  shadow-opacity=0.6
#   glow=10% (or none)  glow-color=white  glow-opacity=0.8
#   extrude=6%,6% (3D depth, or none)  extrude-color=#555555
#   pitch=0 (up +)  roll=0  fade=0.5  fade-in=  fade-out=  width= (images)  emoji-font=
# Lengths are pixels at 8K scale, or a share of the font size (5%). \\n in text = new line.

[style default]
size=5 color=white outline=3% shadow=4%,4% shadow-blur=5%

[style club]
font="Georgia Bold" color=#ffd700 box=#003366 box-opacity=0.7 border=#ffd700,3 radius=20%

# start   dur   yaw     text                              options
0:05      4     0       "I was only going to try flying..."
0:35      3     0       "Right. We're flying."             fade=1
1:00      4     90      "Look left."
1:10      3     -90     "No, your other left."
1:35      3     180     "You didn't look back, did you?"   pitch=-10
1:40      5     0,180   "Kristianstads Flygklubb"          style=club size=7
''',
    "sv": '''# Titlar för titles_in_360 {v}. Kör:
#   python titles_in_360.py <video.mp4> --titles {name} -o <ut.mp4> --lang sv
#   python titles_in_360.py <video.mp4> --titles {name} --preview all   (bara stillbilder)
#
# En skylt per rad:   <start>  <längd>  <yaw>  "text" eller image=fil.png   [alternativ]
#   start  1:35, 95.5, 2375f - eller en plats i ett källklipp: GS010109@0:12,
#          "Take off"@0:05 (klippets titel) eller #3@0:05 (klippnummer), läses ur
#          <video>.timeline.json som xfade_concat skriver
#   längd  hur länge skylten syns, fade inräknad
#   yaw    0 = rakt fram, 90 = höger, -90 = vänster, 180 = bakom (eller fram/höger/bak/vänster);
#          flera med komma: 0,180
# Alternativ (på en rad, eller i ett [style]-block; senare vinner:
#   inbyggt < kommandoraden < [style default] < style=namn < raden):
#   size=5 (grader; eller 120px)  font="Segoe UI Bold" (eller en .ttf-fil)  color=white
#   opacity=1  outline=3%  outline-color=black  align=center  line-spacing=1.15
#   box=#003366 (eller none)  box-opacity=0.6  padding=40%,20%  radius=25%  border=#ffd700,3
#   shadow=4%,4% (eller none)  shadow-blur=5%  shadow-color=black  shadow-opacity=0.6
#   glow=10% (eller none)  glow-color=white  glow-opacity=0.8
#   extrude=6%,6% (3D-djup, eller none)  extrude-color=#555555
#   pitch=0 (upp +)  roll=0  fade=0.5  fade-in=  fade-out=  width= (bilder)  emoji-font=
# Längder är pixlar i 8K-skala, eller andel av teckenstorleken (5%). \\n i text = ny rad.

[style default]
size=5 color=white outline=3% shadow=4%,4% shadow-blur=5%

[style klubb]
font="Georgia Bold" color=#ffd700 box=#003366 box-opacity=0.7 border=#ffd700,3 radius=20%

# start   längd  yaw    text                              alternativ
0:05      4      0      "Jag skulle bara provflyga lite..."
0:35      3      0      "Jaha. Vi flyger."                 fade=1
1:00      4      90     "Titta åt vänster."
1:10      3      -90    "Nej, din andra vänster."
1:35      3      180    "Du tittade inte bakåt, va?"       pitch=-10
1:40      5      0,180  "Kristianstads Flygklubb"          style=klubb size=7
''',
}


def make_titles(dest: Path, overwrite: bool) -> None:
    if dest.exists() and not overwrite:
        die("err_exists", out=dest)
    text = TEMPLATE.get(engine.LANG, TEMPLATE["en"]).format(v=__version__, name=dest.name)
    dest.write_text(text, encoding="utf-8")
    print(t("tpl_done", file=dest))


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Put titles, signs and logos into a 360° (equirectangular) video. Smart "
                    "mode re-encodes only where a sign shows; the rest is stream-copied. "
                    "Times: 1:35, 95.5 (seconds) or 2375f (frames).")
    ap.add_argument("input", nargs="?", help="the 360° video")
    ap.add_argument("-o", "--output", help="output .mp4")
    ap.add_argument("-y", "--overwrite", action="store_true", help="overwrite output")
    ap.add_argument("--titles", type=Path, help="titles file (see --make-titles)")
    ap.add_argument("--make-titles", type=Path, metavar="FILE",
                    help="write a commented example titles file and exit")
    ap.add_argument("--timeline", type=Path,
                    help="xfade_concat timeline JSON for clip@time starts "
                         "(default: <input>.timeline.json when it exists)")
    ap.add_argument("--lang", choices=["en", "sv"], default=engine.default_lang(),
                    help="message language (default: $GOPRO_LANG or en)")

    g = ap.add_argument_group("look (defaults for every sign; the titles file wins)")
    g.add_argument("--fade", default="0.5", help="fade in and out per sign (default 0.5)")
    g.add_argument("--font", help="font name or file, e.g. \"Segoe UI Bold\"")
    g.add_argument("--size", help="font size in degrees (default 5)")
    g.add_argument("--color", help="text colour (default white)")
    g.add_argument("--outline", help="outline width (default 3%%)")
    g.add_argument("--box-opacity", help="plate opacity (default 0.6)")
    g.add_argument("--list-fonts", nargs="?", const="", metavar="FILTER",
                   help="list the fonts that can be used by name, and exit")

    g = ap.add_argument_group("check without encoding")
    g.add_argument("--preview", metavar="TIMES",
                   help="write stills instead of encoding: '1:35', '0:35,1:40' or 'all' "
                        "(one per sign)")
    g.add_argument("--preview-dir", type=Path, help="default: <output or input>_preview")
    g.add_argument("--srt", nargs="?", const="", metavar="FILE",
                   help="also write the texts as YouTube subtitles (default <output>.srt)")

    add_encoding_args(ap, "smart = copy everything but the signs (default); "
                          "full = re-encode everything")
    add_run_args(ap, __version__)
    args = ap.parse_args()
    engine.set_lang(args.lang)

    if Image is None:
        die("err_pillow")
    if args.make_titles:
        make_titles(args.make_titles, args.overwrite)
        return
    extra_font_dirs = [args.titles.resolve().parent / "fonts"] if args.titles else []
    fonts = FontBook(extra_font_dirs)
    if args.list_fonts is not None:
        names = [n for n in fonts.names() if args.list_fonts.lower() in n.lower()]
        filt = f" '{args.list_fonts}'" if args.list_fonts else ""
        print(t("fonts_head", n=len(names), filt=filt))
        for n in names:
            print("  " + n)
        return
    resolve_tools(args)
    if not args.input:
        die("err_no_input")
    if not args.titles:
        die("err_no_titles")
    only_checks = args.preview is not None or (args.srt is not None and not args.output)
    if not args.output and not only_checks:
        die("err_no_output")
    output = Path(args.output).resolve() if args.output else None
    if output and not args.preview and output.exists() and not args.overwrite \
            and not args.dry_run:
        die("err_exists", out=output)

    src_path = Path(args.input)
    print(t("probing", name=src_path.name))
    src = probe_source(args.ffprobe, src_path)
    if output and output == src.path:
        die("err_same_file")
    fps, total = src.fps, src.frames
    if src.width != 2 * src.height:
        warn("warn_not_360", w=src.width, h=src.height)

    tl = args.timeline or src.path.with_suffix(".timeline.json")
    args.timeline_obj = Timeline(tl)
    cli = {k: v for k, v in {"font": args.font, "size": args.size, "color": args.color,
                             "outline": args.outline, "box-opacity": args.box_opacity}.items()
           if v is not None}
    signs, _ = read_titles(args.titles, fps, total, args.timeline_obj, cli, args.fade)
    if not signs:
        warn("warn_no_signs")

    base = output or src.path
    work = (args.work_dir or base.with_name(base.stem + "_work")).resolve()

    # ---- the sign table
    print()
    for i, s in enumerate(signs, 1):
        print(t("sign_head", i=i, start=hms(secs(s.start, fps)), dur=f"{secs(s.frames, fps):.2f}s",
                yaw=",".join(f"{y:g}" for y in s.yaws), what=s.what()))

    if args.srt is not None:
        srt = Path(args.srt) if args.srt else (base.with_suffix(".srt"))
        write_srt(srt, signs, fps)
        if not output and args.preview is None:
            return

    if args.dry_run and not args.preview:
        check_signs(signs, fonts, src.width, args.titles.name)
        # plan without drawing: crops are unknown yet, so commands show placeholders
        for s in signs:
            for y in s.yaws:
                s.placements.append(Placement(s, y, 0, 0, crops=[
                    Crop(work / "sign_<key>_0.png", 0, 0, 0, 0)]))
    elif signs:
        build_placements(signs, args, fonts, src.width, src.height, work,
                         args.titles.name, args.verbose)

    if args.preview is not None:
        dest = args.preview_dir or base.with_name(base.stem + "_preview")
        write_previews(args, src, signs, fps, total, dest.resolve())
        return
    # ---- plan
    smart = args.mode == "smart"
    if smart and not src.smart_ok():
        warn("warn_fallback_full", codec=src.codec, pix=src.pix_fmt)
        smart = False
    if smart and args.bframes:
        warn("warn_bframes")
    min_copy = parse_time(args.min_copy, fps, "--min-copy")
    if smart:
        print(t("probing_kf"))
        scan_spans(args.ffmpeg, src, keyframe_spans(src, windows(signs), total, fps,
                                                    args.gop_window))
    plan = build_plan(src, signs, total, smart, min_copy, fps)
    # every re-encoded piece starts decoding at a safe keyframe before it
    need = [p.start for p in plan if p.kind != "copy" and p.start > 0
            and TEnc.start_keyframe(src, p.start) is None]
    if need:
        if not smart:
            print(t("probing_kf"))
        scan_spans(args.ffmpeg, src, [(max(0.0, secs(f, fps) + src.v_start - args.gop_window),
                                       secs(f, fps) + src.v_start + 0.1) for f in need])
    enc = TEnc(args, [src], smart)
    use_audio = bool(src.audio)
    copy_f = sum(p.frames for p in plan if p.kind == "copy")
    print("\n" + t("encoder_line", w=src.width, h=src.height, fps=fps, depth=enc.depth,
                   codec=src.codec, mode="smart" if smart else "full", enc=args.encoder,
                   dec="NVDEC" if enc.hw else "CPU"))
    print(t("plan_line", dur=hms(secs(total, fps)), frames=total, pieces=len(plan),
            copy=hms(secs(copy_f, fps)), enc=hms(secs(total - copy_f, fps)),
            pct=100.0 * (total - copy_f) / max(1, total),
            audio=t("with_audio") if use_audio else t("without_audio")) + "\n")
    for idx, p in enumerate(plan):
        p.file = work / f"{idx:03d}_{p.kind}_{piece_key(p, src, enc, signs)}.hevc"

    def label(p: Piece) -> str:
        n = sum(1 for s in signs if s.start < p.end and s.end > p.start)
        extra = "  " + t("n_signs", n=n) if p.kind == "titled" else ""
        return (f"{p.kind:<6} {hms(secs(p.start, fps))} +{secs(p.frames, fps):.3f}s"
                f"{extra}")

    if args.dry_run:
        for p in plan:
            print(label(p))
            print("  $ " + fmt_cmd(enc.piece_cmd(p, src, signs, p.file)))
        return

    work.mkdir(parents=True, exist_ok=True)
    render_pieces(plan, lambda p, out: enc.piece_cmd(p, src, signs, out), label, args.verbose)

    print(t("join", name=output.name))
    if use_audio:
        extra = []
        if abs(src.v_start) > 1e-6:
            extra += ["-itsoffset", f"{-src.v_start:.6f}"]
        extra += ["-i", str(src.path)]
        maps = ["-map", "0:v:0", "-map", "1:a?"]
    else:
        extra, maps = [], ["-map", "0:v:0"]
    join_pieces(args.ffmpeg, [p.file for p in plan], fps, enc, output, extra, maps,
                args.verbose)
    got = played_frames(args.ffprobe, output)
    if got != total:
        die("err_frames", name=output.name, want=total, got=got)
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print("\n" + t("done", out=output, frames=total, dur=hms(secs(total, fps))))
    if src.width == 2 * src.height:
        print(t("note_360"))


if __name__ == "__main__":
    main()
