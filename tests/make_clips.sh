#!/bin/bash
# Synthetic test material for the regression suite and the titles checks.
#   tests/make_clips.sh <dir>
# 2:1 HEVC 10-bit with B-frames and audio: closed and open GOP, GoPro chapter
# names, a 40 s master, a clip pre-cut mid-GOP (hidden pre-roll behind an edit
# list), a 1920x960 film for titles and an 8K sky still for previews.
set -e
D=${1:?usage: make_clips.sh <dir>}
mkdir -p "$D" && cd "$D"
FONT=$(fc-match -f '%{file}' 'DejaVu Sans:bold' 2>/dev/null || echo /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf)
mk() { # name seconds x265-params
  ffmpeg -v error -y -f lavfi -i "testsrc2=s=384x192:r=25:d=$2" -f lavfi -i "sine=f=440:d=$2:sample_rate=48000" \
    -vf "drawtext=fontfile=$FONT:text='$1 %{n}':fontsize=40:x=20:y=70:fontcolor=white:box=1:boxcolor=black" \
    -c:v libx265 -pix_fmt yuv420p10le -x265-params "keyint=25:min-keyint=25:bframes=4:log-level=error:$3" \
    -c:a aac -b:a 96k -shortest "$1.mp4"
}
mk A 8 "open-gop=0"
mk B 7 "open-gop=1"
mk C 9 "open-gop=0"
mk GS010101 6 "open-gop=0"
mk GS020101 5 "open-gop=0"
mk GS010102 6 "open-gop=0"
mk master 40 "open-gop=0:keyint=50:min-keyint=50"
ffmpeg -v error -y -ss 3.3 -i master.mp4 -t 6 -c copy precut.mp4
cat > list1.txt <<'L'
# master ranges, speed, audio, titles, per-row fades
master.mp4   in=00:00:02  dur=6          title="Take off"
master.mp4   in=0:10      dur=8   speed=2  fade=0.5
master.mp4   in=00:20.5   out=00:26.5  speed=0.5 audio=keep title="Landing"
precut.mp4   head=0.2 tail=8f
B.mp4        audio=mute speed=1/3 tail=4
L
ffmpeg -v error -y -f lavfi -i "testsrc2=s=1920x960:r=25:d=12" -f lavfi -i "sine=f=300:d=12:sample_rate=48000" \
  -c:v libx265 -pix_fmt yuv420p10le -x265-params "keyint=25:min-keyint=25:bframes=4:open-gop=0:log-level=error" \
  -c:a aac -b:a 96k film.mp4
ffmpeg -v error -y -f lavfi -i "nullsrc=s=7680x3840:d=1" \
  -vf "geq=r='if(lt(Y,H/2),60+100*Y/(H/2),70+40*sin(X/40))':g='if(lt(Y,H/2),120+100*Y/(H/2),110+30*sin(Y/25))':b='if(lt(Y,H/2),200+50*Y/(H/2),60)'" \
  -frames:v 1 -c:v libx265 -pix_fmt yuv420p10le -x265-params log-level=error sky8k.mp4
echo "clips written to $D"
