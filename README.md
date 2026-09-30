# 360-cli-tools

Command-line tools for editing long **8K equirectangular 360° video** (GoPro
MAX/MAX2) without re-encoding all of it. Each tool re-encodes only the few
seconds it has to change, with NVIDIA **NVENC (HEVC)**, and stream-copies the
rest, frame-exact.

| tool | what it does | docs |
|---|---|---|
| `xfade_concat.py` | joins clips with soft transitions; ranges straight from a master, speed changes, YouTube chapters | [docs/xfade_concat.md](docs/xfade_concat.md) |
| `titles_in_360.py` | puts titles, signs and logos into the 360° picture, undistorted, in any direction | [docs/titles_in_360.md](docs/titles_in_360.md) |
| `pieces.py` | the engine both share — not run on its own | |
| `gpu_check.py` | tests what your GPU and ffmpeg can do, and which switches to use | [below](#will-it-work-on-my-gpu) |

```powershell
python $HOME\bin\xfade_concat.py --list clips.txt -o flight.mp4 --fade-in 1 --fade-out 1
python $HOME\bin\titles_in_360.py flight.mp4 --titles signs.txt --preview all   # check the look
python $HOME\bin\titles_in_360.py flight.mp4 --titles signs.txt -o flight_titled.mp4
```

## Where they fit in the pipeline

```
overlay_map.py → OVRLEY (per clip) → xfade_concat.py → titles_in_360.py → inject360-inplace (always last)
```

- **xfade after OVRLEY**: the telemetry is already burned into each clip, so
  shortening the timeline does not affect sync. If you join **before** OVRLEY,
  shift the telemetry with the timeline JSON xfade writes.
- **Titles after joining**: sign times are then times in the finished film. A
  sign can also be tied to a moment in a source clip (`GS010109@0:12`) through
  xfade's timeline JSON, so it stays in place when transitions or trims change.
- **`inject360-inplace` last**: ffmpeg drops the spherical metadata, so every
  file these tools write needs it re-injected before YouTube sees it as 360°.

## How the re-encoding is kept small

Both tools cut the video into **pieces**. Stretches between two safe keyframes
are copied byte for byte; everything else — a transition, a sign, the frames
up to the nearest keyframe — is re-encoded. Every piece is written as a raw
HEVC (Annex B) bitstream with its own parameter sets, and the pieces are joined
by plain byte concatenation into one final mux that re-derives all timing at a
constant frame rate, and the picture order read from the slice headers on the
way through is written into the finished file as presentation times. That is
what lets the camera's own bitstream and NVENC's re-encodes share one file with
no timestamp problems, and leaves every frame playing at its proper time. Every re-encoded piece is
checked for its exact frame count and for frames ffmpeg dropped or repeated
along the way (which would show as a jump in the picture; the run stops with
the command that caused it), and the finished file for the frames that
actually play. Keyframes are found by reading
bitstream headers, never by decoding, so probing an hour of 8K takes seconds.
The details are in [docs/xfade_concat.md](docs/xfade_concat.md#how-it-works).

Pieces are rendered one at a time: the RTX 2080 Ti has a single NVDEC/NVENC
chip, and parallel GPU jobs (OVRLEY at the same time, say) have produced
corrupt frames before. They are cached in `<output>_work\`, so an interrupted
run resumes where it stopped.

## Requirements

- An NVIDIA GPU with HEVC NVENC, for speed. Whether a card can also do 10-bit
  and 8K depends on its generation — [`gpu_check.py`](#will-it-work-on-my-gpu)
  tells you in half a minute. Without one, libx265 is used (CPU, slow)
- ffmpeg with `hevc_nvenc` on PATH, e.g. the gyan.dev build in `C:\ffmpeg\bin` on
  Windows, or the distribution's ffmpeg on Linux
- Python 3.9+
- [Pillow](https://pypi.org/project/pillow/) for `titles_in_360.py`
  (`python -m pip install pillow`); `xfade_concat.py` needs only the standard library
- Windows 10/11 is what the maintainer runs. Linux and WSL2 are supported too,
  see [Linux and WSL2](#linux-and-wsl2). NVDEC/NVENC do not work under WSL1

## Installation

Developed in WSL, run on Windows. Clone in WSL:

```bash
git clone https://github.com/zekesixniner/360-cli-tools.git ~/dev/360-cli-tools
```

Copy the three scripts to a folder on Windows, from WSL (again after every
update):

```bash
mkdir -p /mnt/c/Users/<you>/bin
cp ~/dev/360-cli-tools/{xfade_concat.py,titles_in_360.py,pieces.py} /mnt/c/Users/<you>/bin/
```

`pieces.py` must stay next to the two scripts — they import it from there.
Once, in PowerShell, install Pillow for `titles_in_360.py`:

```powershell
python -m pip install pillow
```

Then run the scripts with Python, as in all examples here:

```powershell
python $HOME\bin\xfade_concat.py --help
python $HOME\bin\titles_in_360.py --help
```

Nothing is changed in PowerShell's settings, so this works under the default
execution policy. (A shorter command — a function in your PowerShell profile,
say — needs a policy that lets the profile run; that is your call to make.)

## Linux and WSL2

The scripts are plain Python and ffmpeg, so they run the same way on Linux — no
copying to another folder, just run them from the clone. Only NVIDIA is
supported for hardware encoding (`hevc_nvenc`); AMD and Intel (VAAPI/QSV) would
need changes in `pieces.py`. Everything else falls back to libx265.

```bash
# Debian / Ubuntu (also inside WSL2)
sudo apt install ffmpeg python3 python3-pil fonts-dejavu fonts-noto-color-emoji
git clone https://github.com/zekesixniner/360-cli-tools.git ~/dev/360-cli-tools
cd ~/dev/360-cli-tools
python3 gpu_check.py                       # what can this machine do?
python3 xfade_concat.py --help
```

(`python3-pil` is Pillow, needed only for `titles_in_360.py`. If you prefer a
virtual environment: `python3 -m venv ~/.venvs/360 && ~/.venvs/360/bin/pip install pillow`.)

**The GPU.** On a normal Linux machine you need the NVIDIA driver (`nvidia-smi`
must work) and an ffmpeg built with NVENC; the distribution's usually is. Under
**WSL2** the driver is the ordinary Windows driver — do not install one inside
WSL — and `nvidia-smi` inside WSL should list the card. NVENC is reported to
work under WSL2, but the maintainer has not tried it: run `gpu_check.py` and
tell us what it says ([see below](#will-it-work-on-my-gpu)).

**Fonts.** Without `font=` / `--font`, `titles_in_360.py` uses the first it finds
of Segoe UI, Arial, Helvetica, DejaVu Sans, Liberation Sans and Noto Sans (bold),
so on Linux you get DejaVu Sans Bold. Asking for a font that is not installed is
an error, so **the examples' `font="Georgia Bold"` needs changing on Linux**:

```bash
python3 titles_in_360.py --list-fonts            # names that can be used
python3 titles_in_360.py --list-fonts dejavu     # filter
#   in a titles file:  font="DejaVu Serif Bold"   or   font=/path/to/file.ttf
```

Colour emoji need `fonts-noto-color-emoji`. Fonts are read from `/usr/share/fonts`,
`/usr/local/share/fonts`, `~/.fonts` and `~/.local/share/fonts`; under WSL2
Windows' own fonts can be reached by copying them into `~/.local/share/fonts`
(mind their licences).

**Where the files live.** Work in WSL's own file system (`~/...`), not on
`/mnt/c/...`: the pieces are big, and access across that boundary is many times
slower. Put the clips and the output there, or keep them on Windows and move
only the cache: `--work-dir ~/work/flight_work`. The default is `<output>_work`
next to the output.

## Will it work on my GPU?

```bash
python3 gpu_check.py            # Windows: python gpu_check.py
```

It makes the same calls the tools make — NVENC HEVC with the tools' own settings,
NVDEC decoding, and the whole NVDEC → CPU → NVENC chain — at 8K, for 8-bit and
10-bit, on a few synthetic frames (about half a minute). When something fails it
says which switch to use, and whether the trouble is 8K as such or NVENC as such
(it repeats a failed 8K test at half size). Exit code 0 means everything tested
works. `--size`, `--depth`, `--ffmpeg`, `--lang sv` and `-v` (shows the ffmpeg
commands) are available.

| the check says | what to do |
|---|---|
| everything ok | nothing, the defaults are right |
| NVDEC fails | `--cpu-decode` (CPU decodes, NVENC still encodes) |
| 10-bit NVENC fails, 8-bit works | use `--encoder x265` for 10-bit footage |
| 8K fails but half size works | the card cannot encode 8K: `--encoder x265` |
| no driver / no `hevc_nvenc` | `--encoder x265` (this is also what `auto` does without `hevc_nvenc`) |

The same test by hand, without the script (8-bit: `-pix_fmt nv12 -profile:v main`):

```bash
ffmpeg -f lavfi -i testsrc2=s=7680x3840:r=25 -frames:v 25 -pix_fmt p010le \
       -c:v hevc_nvenc -profile:v main10 t.mp4
```

If that writes `t.mp4`, NVENC can do 8K 10-bit on this machine. `Frame Dimension`
or `not supported` in the error means the card cannot; `Cannot load libcuda` or
`No capable devices` means the driver is missing or not visible.

### Tested on

| system | what works | by |
|---|---|---|
| RTX 2080 Ti (driver 616.92), Windows, gyan.dev ffmpeg 9.0.1 | `gpu_check.py`: all ok — NVENC 8- and 10-bit, NVDEC and the whole chain at 8K. Real GoPro MAX2 films (xfade and titles) | maintainer |
| Ubuntu in WSL1, CPU only (`--encoder x265`) | everything, on synthetic clips (the test suite below) | maintainer |
| Ubuntu 26.04 in WSL1, ffmpeg 8.0.1 | no GPU there, as expected for WSL1 (`nvidia-smi` missing): `gpu_check.py` says so and points to `--encoder x265` | maintainer |
| any other card, native Linux, WSL2 with a GPU | **not tested yet** | |

Got it running on something else — or not? Open an issue with the output of
`python3 gpu_check.py` (the **compatibility report** template asks for exactly
that) and it goes in this table.

## Language

Messages are English or Swedish: `--lang en|sv`, or `$env:GOPRO_LANG = "sv"`
for all tools (same as gopro-max-gpx-pipeline). `--make-list` and
`--make-titles` write their templates in that language too.

## Testing

Everything can be exercised on the CPU with synthetic clips (needs numpy for
the titles check):

```bash
tests/make_clips.sh /tmp/clips                       # 2:1 HEVC 10-bit test material

# xfade: record seven scenarios for two versions, then compare
tests/xfade_regression.sh old/xfade_concat.py /tmp/clips /tmp/old
tests/xfade_regression.sh xfade_concat.py     /tmp/clips /tmp/new
diff /tmp/old/summary.txt /tmp/new/summary.txt       # file checksums, packet counts, decoded-frame hash
cat /tmp/new/pts.txt                                 # presentation times and seeking, per output
for f in /tmp/old/*.norm; do diff -q $f /tmp/new/${f##*/}; done   # plans, commands, logs

# titles: frame-by-frame comparison with the source
cd /tmp/clips && python3 ~/dev/360-cli-tools/titles_in_360.py film.mp4 \
    --titles ~/dev/360-cli-tools/tests/titles_check.txt -o titled.mp4 --encoder x265 -y
python3 ~/dev/360-cli-tools/tests/check_titles.py film.mp4 titled.mp4
```

To check on the GPU machine that re-encoded stretches are in step with their
source, frame for frame (it counts frames instead of seeking, so it works on
joined files too):

```powershell
python tests\align_check.py new.mp4 new_titled.mp4 2272 516 --cuda
```

The regression scenarios cover smart and full mode, closed and open GOP, GoPro
chapters, ranges from a master with speed changes and all three audio modes, a
clip pre-cut mid-GOP (hidden pre-roll behind an edit list), and error paths.
`tests/titles_check.txt` lists what `check_titles.py` should report.

Moving the engine into `pieces.py` was verified this way: `xfade_concat.py`
before and after produced bit-identical MP4s, identical plans and ffmpeg
commands, timelines, chapter files and list templates.

`tests/pts_check.py file.mp4` checks that a finished file has proper presentation
times (packets, decoded frames, and ten seeks by time that must each return the
frame they ask for). `tests/order_check.py piece.hevc` (or an .mp4) checks the
picture-order reader that feeds them against the decoder on any HEVC stream —
worth running on a piece from the GPU machine (`--keep-work`) and on camera
footage. Since `pieces.py` 1.1 the output MP4s are no longer bit-identical to
before, by design (new `ctts`/edit list in moov); the `frames=` hash in
`summary.txt` — the decoded frames, timestamps ignored — is what must not change.

`gpu_check.py` is tested against stub ffmpegs that fail in each of the ways it
reports (8K refused, no 10-bit, no NVDEC, broken chain, no device), and on a
machine without a GPU. The NVENC/NVDEC path itself needs testing on GPU
machines.

## History

This repository started as `xfade-concat` (renamed in September 2026; GitHub
redirects the old URL). Version tags up to `v0.5.1` are `xfade_concat.py` alone.
