"""Clip library tests: grouping, covers (lossless, dates kept, safe when the file is busy).

usage: .venv\\Scripts\\python.exe tests\\test_library.py <workdir>
Makes its own synthetic clips in <workdir>; never touches the real replays folder.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipt import library  # noqa: E402
from clipt.engine import Exporter  # noqa: E402

FF = str(ROOT / "bin" / "ffmpeg.exe")
FP = str(ROOT / "bin" / "ffprobe.exe")
WORK = Path(sys.argv[1]) / "library"
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))


def make_clip(path, dur=12):
    """1080p HEVC with three named audio tracks, like an OBS replay."""
    cmd = [FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=60:duration={dur}"]
    for i in range(3):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 + 220 * i}:duration={dur}"]
    cmd += ["-map", "0", "-map", "1", "-map", "2", "-map", "3", "-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "log-level=error", "-c:a", "aac",
            "-metadata:s:a:0", "title=Stream mix", "-metadata:s:a:1", "title=Game audio", "-metadata:s:a:2", "title=Mic",
            str(path)]
    subprocess.run(cmd, check=True)


def streams(path):
    j = json.loads(subprocess.run([FP, "-v", "error", "-show_entries",
                                   "format=duration:stream=codec_type:stream_tags=name:stream_disposition=attached_pic",
                                   "-of", "json", str(path)], capture_output=True, text=True).stdout)
    return float(j["format"]["duration"]), j["streams"]


def packets(path, sel):
    return subprocess.run([FP, "-v", "error", "-select_streams", sel, "-count_packets", "-show_entries",
                           "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.split()


# ---- grouping and labels ----
today = date(2026, 10, 18)
mk = lambda y, m, d, h=12: library.Clip(Path(f"{y}{m}{d}{h}.mp4"), datetime(y, m, d, h).timestamp(), 1)
clips = [mk(2026, 10, 18, 14), mk(2026, 10, 18, 9), mk(2026, 10, 17), mk(2026, 10, 14), mk(2026, 10, 8), mk(2025, 12, 31)]
groups = library.group_by_day(clips, today)
check("days: today, yesterday, weekday, date, other year",
      [g[0] for g in groups] == ["Today", "Yesterday", "Wednesday", "Oct 8", "Dec 31, 2025"], [g[0] for g in groups])
check("two clips today stay in one group", len(groups[0][1]) == 2)
check("clock reads like 2:49 PM", library.clock(datetime(2026, 10, 17, 14, 49)) == "2:49 PM")
check("morning clock", library.clock(datetime(2026, 10, 17, 9, 5)) == "9:05 AM")
check("latest label reads like a card", library.when_label(datetime(2026, 10, 18, 13, 5), today) == "Today 1:05 PM")
check("future-dated file has age 0, not negative", library.age(time.time() + 86400 * 30) == 0.0)

# ---- covers ----
src = WORK / "src.mp4"
make_clip(src)
clip = WORK / "Replay 2026-10-17 14-49-05.mp4"
shutil.copy(src, clip)
old = time.time() - 3600
os.utime(clip, (old, old))
created_before = os.stat(clip).st_birthtime if hasattr(os.stat_result, "st_birthtime") else None
d0, s0 = streams(clip)
p0 = packets(clip, "v:0") + packets(clip, "a")

status, img = library.add_cover(FF, FP, clip)
check("cover added", status == library.ADDED and img and img[:2] == b"\xff\xd8", status)
d1, s1 = streams(clip)
check("one extra stream, marked as a cover", len(s1) == len(s0) + 1 and s1[-1]["disposition"]["attached_pic"] == 1)
check("length unchanged", abs(d1 - d0) < 0.05, f"{d0} → {d1}")
check("video and audio packets identical (lossless)", packets(clip, "v:0") + packets(clip, "a") == p0, p0)
check("audio track names kept", [s.get("tags", {}).get("name") for s in s1[1:4]] == ["Stream mix", "Game audio", "Mic"],
      [s.get("tags", {}).get("name") for s in s1[1:4]])
check("modified date kept", abs(os.stat(clip).st_mtime - old) < 0.01, os.stat(clip).st_mtime - old)
if created_before is not None:
    check("created date kept", abs(os.stat(clip).st_birthtime - created_before) < 0.01)
check("no temp files left", [p.name for p in WORK.iterdir() if p.name.startswith("~")] == [])
check("second run leaves it alone", library.add_cover(FF, FP, clip)[0] == library.HAD)
cov = library.cover_image(FF, clip)
check("cover picture can be read back", cov is not None and cov[:2] == b"\xff\xd8")
check("clip without a cover has none to read", library.cover_image(FF, src) is None)

# busy: a player holding the file open (no delete sharing) must leave it untouched
busy = WORK / "busy.mp4"
shutil.copy(src, busy)
os.utime(busy, (old, old))
size_before = busy.stat().st_size
with open(busy, "rb"):
    st, _ = library.add_cover(FF, FP, busy)
check("file open elsewhere: not changed, worth a retry", st == library.FAILED and busy.stat().st_size == size_before, st)
check("…and no temp files left", [p.name for p in WORK.iterdir() if p.name.startswith("~")] == [])

# another program (browser + PadDeck's --covers) already working on a clip: leave it alone
locked = WORK / "locked.mp4"
shutil.copy(src, locked)
os.utime(locked, (old, old))
lockf = WORK / "~cover locked.mp4.lock"
lockf.write_bytes(b"")
check("clip locked by another coverer is skipped", library.add_cover(FF, FP, locked)[0] == library.FAILED
      and lockf.exists() and streams(locked)[1][-1]["disposition"]["attached_pic"] == 0)
os.utime(lockf, (old, old))
check("a stale lock (crash) doesn't block forever", library.add_cover(FF, FP, locked)[0] == library.ADDED and not lockf.exists())

stale_tmp = WORK / "~cover 99999 gone.mp4"
fresh_tmp = WORK / "~cover 99999 busy.mp4"
stale_tmp.write_bytes(b"x")
fresh_tmp.write_bytes(b"x")
os.utime(stale_tmp, (old, old))
library.sweep_temps(WORK)
check("left-behind temp files are swept, fresh ones kept", not stale_tmp.exists() and fresh_tmp.exists())
fresh_tmp.unlink()

mkv = WORK / "other.mkv"
subprocess.run([FF, "-v", "error", "-y", "-i", str(src), "-map", "0", "-c", "copy", str(mkv)], check=True)
check("mkv is skipped, not rewritten", library.add_cover(FF, FP, mkv)[0] == library.SKIPPED)
check("mkv still gets a card picture", (library.frame_at_end(FF, mkv, 12) or b"")[:2] == b"\xff\xd8")

fresh = WORK / "fresh.mp4"
shutil.copy(src, fresh)
check("folder scan skips a clip OBS may still be writing", library.add_covers(FF, FP, [fresh]) == 0
      and library.add_cover(FF, FP, fresh)[0] == library.ADDED)

just_saved = WORK / "just saved.mp4"
shutil.copy(src, just_saved)
check("a clip named right after saving is waited for, then covered",
      library.add_covers(FF, FP, [just_saved], settle=1, retries=4) == 1)

check("list skips temp files, newest first", [c.path.name for c in library.list_clips(WORK)][:2] == ["just saved.mp4", "fresh.mp4"]
      and all(not c.path.name.startswith("~") for c in library.list_clips(WORK)))
out = WORK / "out"
out.mkdir()
(out / "Replay 2026-10-17 14-49-05_trimmed_1.mp4").write_bytes(b"x")
check("trimmed badge finds exports", library.was_trimmed(out, "Replay 2026-10-17 14-49-05")
      and not library.was_trimmed(out, "Replay 2026-10-17 14-28-36"))

# exports from a clip with a cover use the real video, not the picture
exp = str(WORK / "export.mp4")
Exporter(FF, FP).run(str(clip), [[2.0, 5.0]], 10, exp)
d, s = streams(exp)
check("export of a covered clip: 3 s, one video stream", abs(d - 3.0) < 0.15 and
      [x["codec_type"] for x in s].count("video") == 1, f"{d:.2f}s {[x['codec_type'] for x in s]}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
