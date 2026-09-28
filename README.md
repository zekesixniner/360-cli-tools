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

```powershell
xfade --list clips.txt -o flight.mp4 --fade-in 1 --fade-out 1
titles360 flight.mp4 --titles signs.txt --preview all        # check the look
titles360 flight.mp4 --titles signs.txt -o flight_titled.mp4
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
constant frame rate. That is what lets the camera's own bitstream and NVENC's
re-encodes share one file with no timestamp problems, and it is checked at the
end by counting the frames that actually play. Keyframes are found by reading
bitstream headers, never by decoding, so probing an hour of 8K takes seconds.
The details are in [docs/xfade_concat.md](docs/xfade_concat.md#how-it-works).

Pieces are rendered one at a time: the RTX 2080 Ti has a single NVDEC/NVENC
chip, and parallel GPU jobs (OVRLEY at the same time, say) have produced
corrupt frames before. They are cached in `<output>_work\`, so an interrupted
run resumes where it stopped.

## Requirements

- Windows 10/11, NVIDIA GPU with HEVC NVENC (10-bit needs Turing / RTX 20 or newer)
- ffmpeg with `hevc_nvenc` on PATH, e.g. the gyan.dev build in `C:\ffmpeg\bin`.
  Without NVENC, libx265 is used automatically (CPU, slow)
- Python 3.9+ for Windows
- [Pillow](https://pypi.org/project/pillow/) for `titles_in_360.py`
  (`python -m pip install pillow`); `xfade_concat.py` needs only the standard library

The tools run on native Windows, from PowerShell: NVDEC/NVENC do not work under
WSL1. Under Linux or WSL they still run, on the CPU (`--encoder x265`), which is
how they are tested.

## Installation

Developed in WSL, run on Windows. Clone in WSL:

```bash
git clone https://github.com/zekesixniner/360-cli-tools.git ~/dev/360-cli-tools
```

Then, in PowerShell, run the installer from the repository (replace `Ubuntu`
with your distribution's name from `wsl -l`):

```powershell
powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\<you>\dev\360-cli-tools\install.ps1 -Pillow
```

It copies the three scripts to `$HOME\bin` (`-Dest` to change that) and adds
two commands to your PowerShell profile, `xfade` and `titles360`. Run it again
after every update: the scripts are replaced, the profile block too (never
duplicated), and nothing else in the folder or the profile is touched.
`-Pillow` also installs or upgrades Pillow.

`pieces.py` must stay next to the two scripts — they import it from there.

Without the installer, copy the three `.py` files anywhere and run
`python <folder>\xfade_concat.py ...`.

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
diff /tmp/old/summary.txt /tmp/new/summary.txt       # output MP4 checksums
for f in /tmp/old/*.norm; do diff -q $f /tmp/new/${f##*/}; done   # plans, commands, logs

# titles: frame-by-frame comparison with the source
cd /tmp/clips && python3 ~/dev/360-cli-tools/titles_in_360.py film.mp4 \
    --titles ~/dev/360-cli-tools/tests/titles_check.txt -o titled.mp4 --encoder x265 -y
python3 ~/dev/360-cli-tools/tests/check_titles.py film.mp4 titled.mp4
```

The regression scenarios cover smart and full mode, closed and open GOP, GoPro
chapters, ranges from a master with speed changes and all three audio modes, a
clip pre-cut mid-GOP (hidden pre-roll behind an edit list), and error paths.
`tests/titles_check.txt` lists what `check_titles.py` should report.

Moving the engine into `pieces.py` was verified this way: `xfade_concat.py`
before and after produced bit-identical MP4s, identical plans and ffmpeg
commands, timelines, chapter files and list templates.

The NVENC/NVDEC path needs testing on the GPU machine.

## History

This repository started as `xfade-concat` (renamed in September 2026; GitHub
redirects the old URL). Version tags up to `v0.5.1` are `xfade_concat.py` alone.
