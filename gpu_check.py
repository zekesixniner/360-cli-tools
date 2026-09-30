#!/usr/bin/env python3
"""gpu_check.py - what can this machine's NVIDIA GPU and ffmpeg do for the 360 tools?

    python gpu_check.py [--size 7680x3840] [--depth 8|10|both] [--ffmpeg ffmpeg]
                        [--lang en|sv] [-v] [--keep]

xfade_concat.py and titles_in_360.py decode with NVDEC and encode with NVENC.
Whether that works depends on the card generation, the driver and the ffmpeg
build - and a card that cannot do 8K or 10-bit only says so in the middle of a
long run. This makes the same calls the tools make, on a few synthetic frames
(about half a minute), and tells you which switches to use if something fails:

  * NVENC HEVC, 8-bit and 10-bit, with the tools' own settings (preset p7, vbr)
  * NVDEC decode of what NVENC wrote, and the copy of the frames to the CPU
  * the whole re-encode chain: NVDEC -> CPU -> NVENC
  * when 8K fails, the same again at half size, to tell "no 8K" from "no NVENC"

Nothing is written outside a temporary folder. Needs only ffmpeg (and
nvidia-smi, if it exists, to name the card). Standalone: it does not import
pieces.py, so it can live anywhere. Exit code 0 = everything tested works."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

__version__ = "1.0.0"

MESSAGES = {
    "en": {
        "ffmpeg": "ffmpeg:      {ver}",
        "build": "in this build: hevc_nvenc {nvenc}, cuda decoding {cuda}, libx265 {x265}",
        "gpu": "GPU:         {gpu}",
        "no_smi": "GPU:         (nvidia-smi not found - no NVIDIA driver here, or not on PATH)",
        "plan": "Testing {size}, {n} frames per test.",
        "yes": "yes", "no": "NO",
        "ok": "ok", "fail": "FAIL", "skip": "skip",
        "enc": "NVENC HEVC {d}-bit", "dec": "NVDEC decode + copy to CPU, {d}-bit",
        "chain": "NVDEC -> CPU -> NVENC, {d}-bit",
        "half": "  ...same at {size}",
        "src_x265": "source made with libx265 instead",
        "no_source": "no {d}-bit source to decode (NVENC failed and libx265 is missing)",
        "needs_enc": "needs a working NVENC (see above)",
        "needs_dec": "needs NVDEC decoding to work (see above)",
        "timeout": "timed out",
        "h_nonvenc": "this ffmpeg has no hevc_nvenc - install a build with NVENC (Windows: gyan.dev, Linux: the distribution's ffmpeg usually has it)",
        "h_driver": "no usable NVIDIA driver/device - check that nvidia-smi works (WSL2: the driver is the one in Windows)",
        "h_api": "the driver is too old for this ffmpeg's NVENC API - update the NVIDIA driver (or use an older ffmpeg)",
        "h_10bit": "this card or ffmpeg cannot encode HEVC 10-bit",
        "h_size": "the card refuses this frame size - NVENC has a maximum resolution per generation",
        "h_preset": "NVENC preset/tune not supported - ffmpeg 4.4 or newer and a recent driver are needed for p1-p7",
        "h_hwaccel": "CUDA decoding could not start - NVDEC is missing or unsupported for this format/size",
        "sum_head": "Result",
        "sum_ok": "Everything tested works. The tools' defaults are right for this machine.",
        "sum_enc8": "8-bit NVENC fails: use --encoder x265 (all on the CPU, slow).",
        "sum_enc10": "10-bit NVENC fails but 8-bit works: for 10-bit footage use --encoder x265 (slow).",
        "sum_dec": "NVDEC decoding fails: add --cpu-decode (CPU decodes, NVENC still encodes).",
        "sum_chain": "NVDEC works alone but the decode -> encode chain fails: use --cpu-decode.",
        "sum_size": "NVENC works at {half} but not at {size}: this card cannot encode 8K - use --encoder x265 for 8K footage.",
        "sum_nogpu": "No NVIDIA encoder is usable here. The tools still run on the CPU with --encoder x265 (they do this by themselves when ffmpeg has no hevc_nvenc).",
        "report": "Working on a card or system that is not in the README's table? The output above is exactly what a compatibility report needs.",
        "err_ffmpeg": "'{exe}' not found (put ffmpeg on PATH or pass --ffmpeg)",
        "err_size": "--size must look like 7680x3840",
    },
    "sv": {
        "ffmpeg": "ffmpeg:      {ver}",
        "build": "i bygget:     hevc_nvenc {nvenc}, CUDA-avkodning {cuda}, libx265 {x265}",
        "gpu": "Grafikkort:  {gpu}",
        "no_smi": "Grafikkort:  (nvidia-smi saknas - ingen NVIDIA-drivrutin här, eller inte i PATH)",
        "plan": "Testar {size}, {n} rutor per test.",
        "yes": "ja", "no": "NEJ",
        "ok": "ok", "fail": "FEL", "skip": "hopp",
        "enc": "NVENC HEVC {d}-bit", "dec": "NVDEC-avkodning + kopia till CPU, {d}-bit",
        "chain": "NVDEC -> CPU -> NVENC, {d}-bit",
        "half": "  ...samma vid {size}",
        "src_x265": "källa gjord med libx265 i stället",
        "no_source": "ingen {d}-bitarskälla att avkoda (NVENC misslyckades och libx265 saknas)",
        "needs_enc": "kräver att NVENC fungerar (se ovan)",
        "needs_dec": "kräver att NVDEC-avkodning fungerar (se ovan)",
        "timeout": "tog för lång tid",
        "h_nonvenc": "det här ffmpeg saknar hevc_nvenc - installera ett bygge med NVENC (Windows: gyan.dev, Linux: distributionens ffmpeg brukar ha det)",
        "h_driver": "ingen användbar NVIDIA-drivrutin/enhet - kolla att nvidia-smi fungerar (WSL2: drivrutinen är den i Windows)",
        "h_api": "drivrutinen är för gammal för det här ffmpegs NVENC-API - uppdatera NVIDIA-drivrutinen (eller använd ett äldre ffmpeg)",
        "h_10bit": "kortet eller ffmpeg kan inte koda HEVC 10-bit",
        "h_size": "kortet nekar den här bildstorleken - NVENC har en högsta upplösning per generation",
        "h_preset": "NVENC-preset/tune stöds inte - ffmpeg 4.4 eller nyare och en ny drivrutin krävs för p1-p7",
        "h_hwaccel": "CUDA-avkodning kunde inte startas - NVDEC saknas eller stöder inte det här formatet/den här storleken",
        "sum_head": "Resultat",
        "sum_ok": "Allt som testades fungerar. Verktygens standardinställningar passar den här datorn.",
        "sum_enc8": "8-bitars NVENC misslyckas: använd --encoder x265 (allt på CPU, långsamt).",
        "sum_enc10": "10-bitars NVENC misslyckas men 8-bit fungerar: för 10-bitsfilm, använd --encoder x265 (långsamt).",
        "sum_dec": "NVDEC-avkodning misslyckas: lägg till --cpu-decode (CPU avkodar, NVENC kodar fortfarande).",
        "sum_chain": "NVDEC fungerar ensamt men kedjan avkodning -> kodning misslyckas: använd --cpu-decode.",
        "sum_size": "NVENC fungerar vid {half} men inte vid {size}: kortet kan inte koda 8K - använd --encoder x265 för 8K-film.",
        "sum_nogpu": "Ingen NVIDIA-kodare går att använda här. Verktygen går ändå att köra på CPU med --encoder x265 (de gör det själva när ffmpeg saknar hevc_nvenc).",
        "report": "Kör du på ett kort eller system som inte finns i READMEs tabell? Utskriften ovan är precis vad en kompatibilitetsrapport behöver.",
        "err_ffmpeg": "hittar inte '{exe}' (lägg ffmpeg i PATH eller ange --ffmpeg)",
        "err_size": "--size ska se ut som 7680x3840",
    },
}
LANG = "en"
FRAMES = 25

# what the tools pass to hevc_nvenc (pieces.Encoder), so a pass here means a pass there
NVENC = ["-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "15",
         "-b:v", "0", "-bf", "0", "-g", "50"]
DEPTH = {  # bit depth -> (hardware frame format, planar format, NVENC profile)
    8: ("nv12", "yuv420p", "main"),
    10: ("p010le", "yuv420p10le", "main10"),
}

NO_DEVICE = {"h_nonvenc", "h_driver", "h_api"}   # failures that say nothing about size or bit depth

# (substring in ffmpeg's error output, hint key) - first match wins
HINTS = [
    ("unknown encoder", "h_nonvenc"),
    ("encoder not found", "h_nonvenc"),
    ("api version", "h_api"),
    ("10 bit encode not supported", "h_10bit"),
    ("dimension", "h_size"),
    ("frame width", "h_size"),
    ("frame height", "h_size"),
    ("cannot load", "h_driver"),
    ("could not dynamically load", "h_driver"),
    ("no capable devices", "h_driver"),
    ("openencodesessionex failed", "h_driver"),
    ("invalid preset", "h_preset"),
    ("unable to parse option value", "h_preset"),
    ("unrecognized option 'preset'", "h_preset"),
    ("failed setup for format cuda", "h_hwaccel"),
    ("hwaccel initialisation returned error", "h_hwaccel"),
    ("device creation failed", "h_hwaccel"),
    ("cuda", "h_hwaccel"),
]


def t(key: str, **kw) -> str:
    return MESSAGES.get(LANG, MESSAGES["en"]).get(key, MESSAGES["en"][key]).format(**kw)


class Outcome:
    def __init__(self, ok: bool, secs: float = 0.0, log: str = "", timed_out: bool = False):
        self.ok, self.secs, self.log, self.timed_out = ok, secs, log, timed_out


class Checker:
    def __init__(self, ffmpeg: str, workdir: Path, verbose: bool):
        self.ffmpeg, self.dir, self.verbose = ffmpeg, workdir, verbose

    def run(self, args: list[str]) -> Outcome:
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
        if self.verbose:
            print("    $ " + (subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)))
        start = time.time()
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=600)
        except subprocess.TimeoutExpired:
            return Outcome(False, time.time() - start, "", timed_out=True)
        log = (res.stderr or "").strip()
        return Outcome(res.returncode == 0, time.time() - start, log)

    # ---- the individual tests; each returns (Outcome, path of what it wrote or None)
    def encode(self, depth: int, size: str) -> tuple[Outcome, Path]:
        hw, planar, profile = DEPTH[depth]
        out = self.dir / f"enc{depth}_{size}.mp4"
        src = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r=25", "-frames:v", str(FRAMES)]
        return self.run(src + ["-pix_fmt", hw, *NVENC, "-profile:v", profile, str(out)]), out

    def x265_source(self, depth: int, size: str) -> tuple[Outcome, Path]:
        _, planar, _ = DEPTH[depth]
        out = self.dir / f"x265_{depth}_{size}.mp4"
        src = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r=25", "-frames:v", "10"]
        return self.run(src + ["-pix_fmt", planar, "-c:v", "libx265", "-preset", "ultrafast",
                               "-x265-params", "keyint=10:log-level=error", str(out)]), out

    def decode(self, depth: int, source: Path) -> Outcome:
        hw, _, _ = DEPTH[depth]
        # hwdownload only accepts frames that really are in GPU memory, so a
        # silent fall-back to software decoding cannot pass this test
        return self.run(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(source),
                         "-vf", f"hwdownload,format={hw}", "-f", "null", "-"])

    def chain(self, depth: int, source: Path) -> Outcome:
        hw, planar, profile = DEPTH[depth]
        out = self.dir / f"chain{depth}.mp4"
        return self.run(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", str(source),
                         "-vf", f"hwdownload,format={hw},format={planar}",
                         *NVENC, "-profile:v", profile, str(out)])


def hint_key(log: str) -> str | None:
    low = log.lower()
    for needle, key in HINTS:
        if needle in low:
            return key
    return None


def show(label: str, o: Outcome, note: str = "") -> None:
    tag = t("ok") if o.ok else t("fail")
    extra = f"  {o.secs:.1f} s" if o.ok else ""
    print(f"  [{tag:>4}] {label}{extra}{note}")
    if not o.ok:
        if o.timed_out:
            print("         " + t("timeout"))
            return
        key = hint_key(o.log)
        if key:
            print("         -> " + t(key))
        lines = [ln for ln in o.log.splitlines() if ln.strip()]
        for ln in lines[-3:]:
            print("         | " + ln[:150])


def skip(label: str, why: str) -> None:
    print(f"  [{t('skip'):>4}] {label}")
    print("         " + why)


def probe_ffmpeg(ffmpeg: str) -> tuple[str, bool, bool, bool]:
    def out(args):
        try:
            return subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True, text=True,
                                  errors="replace").stdout
        except OSError:
            return ""
    ver = (subprocess.run([ffmpeg, "-version"], capture_output=True, text=True,
                          errors="replace").stdout.splitlines() or ["?"])[0]
    enc, hwa = out(["-encoders"]), out(["-hwaccels"])
    return (ver, "hevc_nvenc" in enc, bool(re.search(r"^\s*cuda\s*$", hwa, re.M)), "libx265" in enc)


def gpu_name() -> str | None:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        res = subprocess.run([smi, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    return "; ".join(f"{ln.split(',')[0].strip()}, driver {ln.split(',')[-1].strip()}"
                     for ln in lines) if res.returncode == 0 and lines else None


def half_of(size: str) -> str:
    w, h = (int(x) for x in size.split("x"))
    return f"{max(w // 2 // 2 * 2, 64)}x{max(h // 2 // 2 * 2, 64)}"


def main() -> int:
    global LANG
    env = os.environ.get("GOPRO_LANG", "en").lower()
    ap = argparse.ArgumentParser(description="Check what this GPU and ffmpeg can do for the 360 tools.")
    ap.add_argument("--size", default="7680x3840", help="frame size to test (default 7680x3840)")
    ap.add_argument("--depth", choices=["8", "10", "both"], default="both",
                    help="bit depth to test (default both)")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--lang", choices=["en", "sv"], default=env if env in MESSAGES else "en")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the ffmpeg commands")
    ap.add_argument("--keep", action="store_true", help="keep the temporary files")
    ap.add_argument("--version", action="version", version=f"gpu_check {__version__}")
    args = ap.parse_args()
    LANG = args.lang
    if not re.fullmatch(r"\d+x\d+", args.size):
        sys.exit(t("err_size"))
    if not shutil.which(args.ffmpeg) and not Path(args.ffmpeg).is_file():
        sys.exit(t("err_ffmpeg", exe=args.ffmpeg))

    ver, has_nvenc, has_cuda, has_x265 = probe_ffmpeg(args.ffmpeg)
    yn = lambda b: t("yes") if b else t("no")  # noqa: E731
    print(t("ffmpeg", ver=ver))
    print(t("build", nvenc=yn(has_nvenc), cuda=yn(has_cuda), x265=yn(has_x265)))
    gpu = gpu_name()
    print(t("gpu", gpu=gpu) if gpu else t("no_smi"))
    print("\n" + t("plan", size=args.size, n=FRAMES) + "\n")

    depths = [8, 10] if args.depth == "both" else [int(args.depth)]
    tmp = tempfile.mkdtemp(prefix="gpu_check_")
    failed: dict[str, bool] = {}
    half = half_of(args.size)
    half_ok = False
    no_device = False       # NVENC fails for a reason that has nothing to do with size or bit depth
    try:
        chk = Checker(args.ffmpeg, Path(tmp), args.verbose)
        for d in depths:
            enc_out, enc_file = chk.encode(d, args.size)
            show(t("enc", d=d), enc_out)
            failed[f"enc{d}"] = not enc_out.ok
            if not enc_out.ok:
                key = hint_key(enc_out.log)
                if key in NO_DEVICE:
                    no_device = True
                elif args.size != half:          # is it the size, or NVENC as such?
                    o2, _ = chk.encode(d, half)
                    show(t("enc", d=d) + t("half", size=half), o2)
                    half_ok = half_ok or o2.ok

            source, note = None, ""
            if enc_out.ok:
                source = enc_file
            elif has_x265:
                o3, f3 = chk.x265_source(d, args.size)
                if o3.ok:
                    source, note = f3, "  (" + t("src_x265") + ")"
            if source is None:
                skip(t("dec", d=d), t("no_source", d=d))
                skip(t("chain", d=d), t("no_source", d=d))
                continue
            dec_out = chk.decode(d, source)
            show(t("dec", d=d), dec_out, note)
            failed[f"dec{d}"] = not dec_out.ok
            if not dec_out.ok:
                skip(t("chain", d=d), t("needs_dec"))
            elif not enc_out.ok:
                skip(t("chain", d=d), t("needs_enc"))
            else:
                ch = chk.chain(d, source)
                show(t("chain", d=d), ch)
                failed[f"chain{d}"] = not ch.ok
    finally:
        if args.keep:
            print(f"\n(temporary files kept in {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---- advice
    print("\n" + t("sum_head"))
    lines: list[str] = []
    enc_all_failed = all(failed.get(f"enc{d}", True) for d in depths)
    if enc_all_failed and no_device:
        lines.append(t("sum_nogpu"))            # nothing else is worth saying without a device
    elif enc_all_failed:
        lines.append(t("sum_size", half=half, size=args.size) if half_ok else t("sum_enc8"))
    else:
        if failed.get("enc8"):
            lines.append(t("sum_enc8"))
        if failed.get("enc10"):
            lines.append(t("sum_enc10"))
    if not (enc_all_failed and no_device):
        if any(failed.get(f"dec{d}") for d in depths):
            lines.append(t("sum_dec"))
        elif any(failed.get(f"chain{d}") for d in depths):
            lines.append(t("sum_chain"))
    if not lines:
        lines.append(t("sum_ok"))
    for ln in lines:
        print("  " + ln)
    print("\n" + t("report"))
    return 0 if failed and not any(failed.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
