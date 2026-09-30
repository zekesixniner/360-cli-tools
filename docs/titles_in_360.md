# titles_in_360.py

Part of [360-cli-tools](../README.md) — installation, requirements and where the
tools fit in the pipeline are described there.

Put titles, signs and logos into an **equirectangular 360° video** so they look
flat and undistorted in the viewer, wherever you place them: ahead, to the side,
behind, above or below. Only the stretches where a sign is on screen are
re-encoded (NVENC); everything else is stream-copied.

```powershell
python $HOME\bin\titles_in_360.py flight.mp4 --titles signs.txt --preview all          # stills to check the look
python $HOME\bin\titles_in_360.py flight.mp4 --titles signs.txt -o flight_titled.mp4   # the real thing
```

## The titles file

One sign per row, styles in `[style]` blocks. `python $HOME\bin\titles_in_360.py --make-titles signs.txt`
writes a commented starting point (`--lang sv` for Swedish comments);
[`examples/titles_en.txt`](../examples/titles_en.txt) and
[`examples/titles_sv.txt`](../examples/titles_sv.txt) are the same files, ready
to copy.

```text
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
1:45      4     0       image=logo.png                     size=15 pitch=-12
```

| column | |
|---|---|
| **start** | `1:35`, `95.5` (seconds), `2375f` (frames) — or a moment in a source clip, see [below](#placing-signs-by-source-clip) |
| **dur** | how long the sign shows, **fades included** |
| **yaw** | direction: `0` ahead, `90` right, `-90` left, `180` behind — or `front`/`right`/`back`/`left` (also `fram`/`höger`/`bak`/`vänster`). Several with commas (`0,180`) show the same sign in each direction |
| **text** | in quotes when it has spaces. `\n` starts a new line. Emoji work (in colour, with Segoe UI Emoji on Windows) |
| **image=** | instead of text: a PNG (transparency kept), e.g. a club logo. Relative to the titles file |

After that, any options from the table below, as `key=value`. `#` starts a
comment — except inside a value (`color=#ffd700`) or in a clip number (`#3@0:05`).

### Styles

Every option can be set in four places; the later one wins:

```
built-in  <  command line (--font, --size, ...)  <  [style default]  <  style=name  <  the row
```

`[style default]` applies to every sign; other styles only where a row says
`style=name`.

### Options

**Lengths** (outline, padding, radius, shadow, glow, extrude) are pixels at the
sign's centre in the output, or a share of the font size: `4%`, `0.1em`. The
share keeps a style looking the same at any size, so it is usually the better
choice. **Colours** are names (`white`, `gold`), `#rrggbb`, or `#rrggbbaa` with
transparency.

| option | default | |
|---|---|---|
| `size` | `5` | font size in **degrees of view**; `120px` for pixels. Images: their height |
| `width` | | images: width in degrees instead of `size` |
| `font` | Segoe UI Bold | a name (`"Segoe UI Bold"`, `"Georgia Bold"`, `Arial`) or a font file (`myfont.ttf`, a full path, or a file in a `fonts` folder next to the titles file). `--list-fonts` shows the names |
| `color` / `opacity` | `white` / `1` | text colour and opacity |
| `outline` / `outline-color` | `3%` / `black` | a thin outline keeps text readable against both sky and ground |
| `align` / `line-spacing` | `center` / `1.15` | for text with several lines |
| `box` / `box-opacity` | `none` / `0.6` | a plate behind the text: its colour and opacity |
| `padding` | `40%,20%` | plate: space around the text (horizontal,vertical) |
| `radius` | `25%` | plate: rounded corners |
| `border` | `none` | plate: `colour,width`, e.g. `#ffd700,3` |
| `shadow` | `4%,4%` | drop shadow offset `dx,dy` (`none` to turn off). Falls behind the plate if there is one, otherwise behind the text |
| `shadow-blur` / `-color` / `-opacity` | `5%` / `black` / `0.6` | |
| `glow` | `none` | a soft halo, its blur radius (e.g. `12%`) |
| `glow-color` / `glow-opacity` | `white` / `0.8` | |
| `extrude` | `none` | 3D depth: `dx,dy` of the extrusion, e.g. `6%,6%` |
| `extrude-color` | text colour, darkened | |
| `pitch` | `0` | degrees above (+) or below (−) the horizon |
| `roll` | `0` | tilt in degrees, clockwise + |
| `fade` | `--fade` (0.5) | fade in and out, seconds; `fade=0` for a hard cut |
| `fade-in` / `fade-out` | `fade` | separately |
| `emoji-font` | Segoe UI Emoji | |
| `style` | | row only: which `[style]` to use |

`size` in degrees is how large the sign looks to the viewer, independent of the
video's resolution. For scale: a phone showing a 360° video covers roughly 90°
across, so `size=5` is a comfortable caption and `8`–`10` a headline.

### Placing signs by source clip

When the video came out of `xfade_concat.py`, its `<video>.timeline.json` sits
next to it, and a sign can be placed at a moment in the **recording** instead of
the output — the right place survives any change to transitions, trims or speeds:

| start | meaning |
|---|---|
| `GS010109@0:12` | 12 s into the file `GS010109…` (name or start of the name) |
| `"Take off"@0:05` | 5 s into the clip with `title="Take off"` in the clips list |
| `#3@0:05` | 5 s into the 3rd clip of the list |

A file listed several times (ranges from one master) is matched by which range
contains the time. Speeds are taken into account. A time that was cut away
stops the run with a message listing the ranges that were used. Pass
`--timeline` if the JSON is somewhere else.

## Checking before encoding

- **`--dry-run`** reads and checks the whole titles file (styles, colours,
  fonts, images), then prints the plan: which pieces are copied, which are
  re-encoded, and how much of the film that is.
- **`--preview TIMES`** renders stills instead of encoding: `--preview 1:35`,
  `--preview 0:35,1:40`, or `--preview all` (one per sign, taken where it is
  fully faded in). For each time it writes the whole equirect frame
  (`…_equirect.jpg`) and a normal 100° view looking at each sign
  (`…_yaw90.jpg`), which is what a viewer sees. The stills land in
  `<output>_preview\` (or `<input>_preview\` without `-o`). A preview takes a
  few seconds, so this is the place to try out fonts and colours.
- **`--srt`** also writes the texts as a subtitle file for YouTube, as a
  fallback for players that do not show 360° — or on its own, without `-o`.

## How it works

1. **Drawing.** Each sign is drawn flat with Pillow at 3× size — text, outline,
   plate, border, shadow, glow, extrusion, emoji — and reduced to 1× with an
   exact box filter, which gives smooth plate corners and shadow edges.
2. **Projection.** The flat sign is treated as a pinhole view whose centre has
   the same pixel density as the equirect frame, rotated to its yaw, pitch and
   roll, and mapped onto the sphere with a fine mesh of small quads (8 px,
   bicubic). Only the area the sign covers is computed. Colours are resampled
   premultiplied by alpha, so edges carry no dark or light fringe. A sign
   behind the viewer straddles the left/right seam of the frame and becomes two
   pieces. The result is cached in the work folder: changing a time or a fade
   does not redraw anything.
3. **Planning (smart mode).** The stretches where any sign shows are merged.
   Every gap between them is stream-copied from its first to its last safe
   keyframe (found with the same header scan as `xfade_concat.py`); the few
   frames between a keyframe and a sign join the re-encoded piece. A gap too
   short to hold two keyframes is re-encoded without overlays — that stays on
   the GPU.
4. **Encoding.** Pieces with signs are decoded with NVDEC, taken to the CPU,
   overlaid in 10-bit (`overlay=format=yuv420p10`), and encoded with NVENC. Each
   sign is its own looped image input, faded with `fade=…:alpha=1` and switched
   on for its exact frames with `enable='between(n,…)'`. Any dropped or
   repeated frame stops the run.

   Two details matter when the source is an `xfade_concat` output, which is the
   usual case. Such a file switches encoders where a re-encoded piece meets a
   copied one, and at every switch ffmpeg would rebuild the filter graph —
   NVDEC starts a new frame context — restarting every frame counter in it. So
   frames leave NVDEC as ordinary frames (they go to the CPU for the overlays
   anyway) and `-reinit_filter 0` keeps the graph from being rebuilt. Second,
   files joined by older versions carry timestamps in decoding order inside
   their B-frame stretches, so a seek by time can land a frame or two off. Each
   re-encoded piece therefore starts decoding at the last safe keyframe before
   it and skips forward by counting frames, which is exact whatever the
   timestamps say.
5. **Joining** is the engine shared with `xfade_concat.py` (`pieces.py`): raw
   Annex B pieces, byte-concatenated into one mux with the source's audio
   copied unchanged, then checked for the exact number of frames that play.

How much is re-encoded depends on the source's keyframe spacing: each sign
costs its own duration plus up to one GOP on either side. `--dry-run` shows the
total before anything is encoded.

## Options

| option | default | |
|---|---|---|
| `input` | | the 360° video |
| `-o` / `-y` | | output, overwrite |
| `--titles FILE` | | the titles file |
| `--make-titles FILE` | | write a commented example titles file and exit |
| `--timeline FILE` | `<input>.timeline.json` | for `clip@time` starts |
| `--fade` | `0.5` | fade in/out for every sign that does not set its own |
| `--font` / `--size` / `--color` / `--outline` / `--box-opacity` | | command-line defaults (the titles file wins) |
| `--list-fonts [FILTER]` | | list usable font names and exit |
| `--preview TIMES` / `--preview-dir` | `<output or input>_preview` | stills instead of encoding |
| `--srt [FILE]` | `<output>.srt` | subtitles too |
| `--mode` | `smart` | `full` re-encodes everything (any source codec) |
| `--gop-window` / `--min-copy` | `30` / `2` | as in `xfade_concat.py` |
| `--encoder` / `--cq` / `--preset` / `--bit-depth` / `--cpu-decode` / `--encode-extra` | `auto` / `15` / `p7` | as in `xfade_concat.py` |
| `--work-dir` / `--keep-work` | `<output>_work` | pieces and drawn signs are cached there; an interrupted run resumes |
| `--dry-run` / `-v` | | plan and ffmpeg commands |
| `--lang en\|sv` | `$GOPRO_LANG` | message language |

## Limitations

- The source must be 2:1 equirectangular (a warning says so otherwise).
- Signs are still images: no moving text, no signs that follow the horizon
  through a turn. `roll` tilts a sign once, for its whole duration.
- Emoji are recognised by their code points (most pictographs, flags, skin
  tones, joined sequences). Symbols from the older Unicode blocks, such as ✈,
  come from the text font unless followed by the emoji selector (✈️).
- A sign wider than about 120° starts to look stretched at its edges (a warning
  says so); more than 150° is refused.
- Files joined by an older `xfade_concat.py` or `titles_in_360.py` (before
  `pieces.py` 1.1) carry decoding-order timestamps inside B-frame stretches of
  copied footage: the frames are in the right order, but their presentation
  times are not reordered. Current versions write correct presentation times
  into everything they join; this tool still counts frames instead of seeking
  by time, so it is exact on old files too.
- Only video and audio are kept; other streams (GPMF data, timecode) are
  dropped. Spherical metadata is lost as always: run `inject360-inplace` last.

## Testing

Verified with synthetic clips (`tests/make_clips.sh`) and
`tests/check_titles.py`, which compares the output with its source frame by
frame:

- stream-copied stretches are **bit-identical** to the source
- re-encoded stretches stay in step with the source, frame for frame, also
  with seven overlapping signs in one piece, on an `xfade_concat` output (old
  files have timestamps in decoding order; new ones carry presentation times —
  the titled frames are identical either way), in full mode where pieces
  start between keyframes, and across a mid-stream change that makes current
  ffmpeg rebuild the filter graph
- every sign is visible in exactly its frames, including one across the seam
  behind the viewer and one at each end of the file
- fades ramp linearly over exactly `fade` seconds; `fade=0` switches on and off
  within one frame
- full mode, caching and resume (a rerun produces an identical file)
- `clip@time` placement against a real `xfade_concat` timeline with speed changes
- direction checks: a sign placed at a yaw/pitch/roll is found centred in a
  view looking that way, the right way up and turned the right way

The NVENC/NVDEC path itself needs testing on the GPU machine.
