---
name: Compatibility report
about: It worked (or did not) on a GPU, OS or ffmpeg that is not in the README's table
title: "Compatibility: <GPU> / <OS>"
labels: compatibility
---

**System**
- GPU (and driver version):
- OS (Windows / Linux distribution / WSL2 on Windows 10 or 11):
- ffmpeg (first line of `ffmpeg -version`, and where it came from):

**Output of `python3 gpu_check.py`** (paste all of it)

```
```

**Did you run the tools?** Which one, what footage (resolution, 8 or 10 bit), and did
it finish without a warning? If something went wrong, paste the message and the
ffmpeg command that was printed with it.

- [ ] `xfade_concat.py`
- [ ] `titles_in_360.py`
- [ ] flags I needed (`--cpu-decode`, `--encoder x265`, ...):
