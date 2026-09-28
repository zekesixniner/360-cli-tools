#!/bin/bash
# Run xfade_concat.py through seven scenarios and record what it produced.
#   tests/xfade_regression.sh <xfade_concat.py> <clips dir> <out dir>
# Run it for two versions and compare:  diff old/summary.txt new/summary.txt
# plus the *.norm files (plan, commands, logs, timelines, chapters). Piece
# hashes, the tool version and the output folder are normalised away.
set -u
X="python3 $(realpath "$1")"; S=$(realpath "$2"); O=$(realpath -m "$3")
rm -rf "$O"; mkdir -p "$O"; cd "$S"
C="--encoder x265 --x265-preset ultrafast -y"
run() { name=$1; shift
  $X "$@" $C -o "$O/$name.mp4" --dry-run > "$O/$name.dry" 2>&1
  $X "$@" $C -o "$O/$name.mp4" > "$O/$name.log" 2>&1; echo "exit=$?" >> "$O/$name.log"
}
run s1 A.mp4 B.mp4 C.mp4 --fade 1 --head 0.4 --tail 12f
run s2 A.mp4 B.mp4 C.mp4 --fades 1.5,0 --fade-in 1 --fade-out 2
run s3 "GS*.mp4" --fade 1 --head 1 --tail 1
run s4 --list list1.txt --fade 1 --fade-in 0.5 --fade-out 1 --lang sv
run s5 precut.mp4 A.mp4 --fade 0.8
run s6 A.mp4 C.mp4 --mode full --fade 1 --bframes 2
run s7 A.mp4 B.mp4 --fade 1 --transition dissolve --no-audio --min-copy 1
$X --make-list "$O/ml1.txt" A.mp4 B.mp4 -y > /dev/null 2>&1
$X --make-list "$O/ml2.txt" master.mp4 --rows 4 --lang sv -y > /dev/null 2>&1
# error paths
$X A.mp4 B.mp4 --fades 1,2 -o z.mp4 --dry-run $C > "$O/e1.txt" 2>&1
$X --list list1.txt A.mp4 -o z.mp4 $C > "$O/e2.txt" 2>&1
cd "$O"
for f in *.mp4; do
  echo "$f $(md5sum < $f | cut -c1-32) $(ffprobe -v error -count_packets -show_entries stream=nb_read_packets -of csv=p=0 $f | tr '\n' ' ')"
done > summary.txt
# normalise: piece hashes and tool version differ by design
for f in *.dry *.log *.json *.txt; do
  sed -E -e 's/_[0-9a-f]{10}\.hevc/_HASH.hevc/g' -e 's/xfade_concat [0-9.]+/xfade_concat VER/g' \
      -e 's/(xfade_concat|Klipplista för xfade_concat|list for xfade_concat) [0-9]+\.[0-9]+\.[0-9]+/\1 VER/g' \
      -e "s#$O#OUT#g" -e "s#$S#SRC#g" -e "s/\r/\n/g" "$f" | grep -vE "^(frame=|size=)|speed=.*x *$" > "$f.norm" 2>/dev/null
done
