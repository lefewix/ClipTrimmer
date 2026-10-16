"""Export engine + section model tests.

usage: .venv\\Scripts\\python.exe tests\\test_engine.py <workdir>
Makes its own synthetic test clips (test pattern + tones) in <workdir>.
"""
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipt.engine import ExportCancelled, ExportError, Exporter  # noqa: E402
from clipt.model import Sections  # noqa: E402

FF = str(ROOT / "bin" / "ffmpeg.exe")
FP = str(ROOT / "bin" / "ffprobe.exe")
WORK = Path(sys.argv[1])
WORK.mkdir(parents=True, exist_ok=True)
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""))


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def make_source(path, fps, vcodec, tracks, dur=30):
    if path.exists():
        return
    cmd = [FF, "-y", "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate={fps}:duration={dur}"]
    for i in range(tracks):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 + 220 * i}:duration={dur}"]
    cmd += ["-map", "0:v"] + sum((["-map", f"{i + 1}:a"] for i in range(tracks)), [])
    cmd += ["-c:v", vcodec, "-g", str(fps * 2), "-pix_fmt", "yuv420p"]
    if vcodec == "libx265":
        cmd += ["-x265-params", "log-level=error"]
    if tracks:
        cmd += ["-c:a", "aac"]
    r = run(cmd + [str(path)])
    assert r.returncode == 0, r.stderr[-1500:]


def probe(path):
    r = run([FP, "-v", "error", "-count_frames", "-show_entries",
             "stream=codec_type,width,height,nb_read_frames:format=duration", "-of", "json", str(path)])
    j = json.loads(r.stdout)
    v = [s for s in j["streams"] if s["codec_type"] == "video"][0]
    return {"w": v["width"], "h": v["height"], "frames": int(v["nb_read_frames"]),
            "audio": sum(s["codec_type"] == "audio" for s in j["streams"]),
            "dur": float(j["format"]["duration"]), "mb": os.path.getsize(path) / 1048576}


def psnr(out, t_out, src, t_src):
    g = (f"[0:v]trim=start={t_out},setpts=PTS-STARTPTS,select=eq(n\\,0)[a];"
         f"[1:v]trim=start={t_src},setpts=PTS-STARTPTS,select=eq(n\\,0)[b];[a][b]psnr")
    r = run([FF, "-i", str(out), "-i", str(src), "-filter_complex", g, "-frames:v", "1", "-f", "null", "-"])
    m = re.search(r"average:([\d.]+|inf)", r.stderr)
    return float("inf") if m and m.group(1) == "inf" else (float(m.group(1)) if m else -1)


ex = Exporter(FF, FP)


def export(name, src, segs, mb=10, res=0, audio=True):
    out = WORK / f"{name}.mp4"
    out.unlink(missing_ok=True)
    seen = []
    try:
        size = ex.run(str(src), segs, mb, str(out), res, audio, progress=lambda p, l: seen.append(p))
    except ExportError as e:
        check(f"{name}: export finished", False, str(e)[-400:])
        return None
    check(f"{name}: export finished", out.exists(), f"{size:.2f} MB")
    check(f"{name}: progress reached 100", seen and seen[-1] == 100.0)
    return out


src60 = WORK / "src_h264_60.mp4"
src120 = WORK / "src_hevc_120_3tracks.mp4"
mute = WORK / "src_noaudio.mp4"
make_source(src60, 60, "libx264", 1)
make_source(src120, 120, "libx265", 3)
make_source(mute, 60, "libx264", 0)

o = export("single", src60, [(5, 12)])
if o:
    p = probe(o)
    check("single: 7 s, 420 frames", abs(p["dur"] - 7) < 0.15 and abs(p["frames"] - 420) <= 2, f'{p["dur"]:.3f}s {p["frames"]}')
    check("single: under 10 MB", p["mb"] <= 10, f'{p["mb"]:.2f}')
    check("single: starts at source 5.0s", psnr(o, 0, src60, 5) > 30)

o = export("two", src60, [(5, 8), (20, 25)])
if o:
    p = probe(o)
    check("two: 8 s, 480 frames", abs(p["dur"] - 8) < 0.15 and abs(p["frames"] - 480) <= 2, f'{p["dur"]:.3f}s {p["frames"]}')
    check("two: 4.0s in output = source 21.0s", psnr(o, 4.0, src60, 21.0) > 30)

o = export("overlap", src60, [(20, 22), (5, 10), (8, 12)])
if o:
    check("overlap: merged to 9 s", abs(probe(o)["dur"] - 9) < 0.15)

o = export("obs_like", src120, [(1, 3), (10, 11.5), (25, 29)])
if o:
    p = probe(o)
    check("obs_like: 7.5 s, 900 frames, 1 audio", abs(p["dur"] - 7.5) < 0.15 and abs(p["frames"] - 900) <= 3 and p["audio"] == 1,
          f'{p["dur"]:.3f}s {p["frames"]} a={p["audio"]}')

o = export("res480", src60, [(5, 8), (20, 25)], res=480)
if o:
    p = probe(o)
    check("res480: 854x480 and under 10 MB", (p["w"], p["h"]) == (854, 480) and p["mb"] <= 10, f'{p["w"]}x{p["h"]} {p["mb"]:.2f}')

o = export("noaudio", src60, [(5, 8)], audio=False)
if o:
    check("noaudio: no audio track", probe(o)["audio"] == 0)
o = export("silent_src", mute, [(5, 8), (10, 12)])
if o:
    check("silent source: 5 s", abs(probe(o)["dur"] - 5) < 0.15)

try:
    ex.run(str(src60), [(0, 30)], 0.1, str(WORK / "tiny.mp4"))
    check("tiny target errors", False)
except ExportError as e:
    check("tiny target: friendly error", "too small" in str(e))

# cancel mid-export: no partial file left behind
out = WORK / "cancel.mp4"
out.unlink(missing_ok=True)
threading.Timer(0.6, ex.cancel).start()
try:
    ex.run(str(src120), [(0, 30)], 50, str(out))
    check("cancel stops the export", False, "finished anyway")
except ExportCancelled:
    check("cancel stops the export", True)
check("cancel leaves no partial file", not out.exists())

# ---- model ----
m = Sections()
m.set_in(5.0); m.set_out(8.0)
m.set_in(20.0)
check("model: I after a finished section starts a new one", len(m.items) == 2 and m.active == 1)
check("model: open section exports to the end", m.ranges(30) == [[5.0, 8.0], [20.0, 30]])
m.set_out(25.0)
m.new_at(6.0); m.set_out(9.0)
check("model: overlaps merge", m.ranges(30) == [[5.0, 9.0], [20.0, 25.0]], m.ranges(30))
t = m.move_edge(0, "in", 50.0, 30)
check("model: IN can't pass OUT", abs(t - 7.9) < 1e-9, t)
m.undo()   # undoes the last action: Out at 9.0 (edge drags checkpoint in the app)
check("model: undo", m.items == [[5.0, 8.0], [20.0, 25.0], [6.0, None]], m.items)
m.redo()
check("model: redo", m.items[2] == [6.0, 9.0], m.items)
m.remove(0)
check("model: remove keeps active valid", m.active == 1 and len(m.items) == 2)
m.clear()
check("model: clear", m.items == [] and m.ranges(30) == [])
m.undo()
check("model: undo clear", len(m.items) == 2)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
