#!/usr/bin/env python3
"""
ClipTrimmer - A simple desktop app to trim game clips and compress them to a target size.

HOW TO USE:
  1. Double-click ClipTrimmer.pyw  (the .pyw extension hides the console window)
  2. A browser window opens with the editor
  3. Drag an MP4 onto the window (or click to browse)
  4. Use the player + scrubber to find your trim points
  5. Click "Set In" and "Set Out" while playing/paused at the right spots
  6. Enter a target size (e.g. 10 MB for Discord) and click Export
  7. The trimmed + compressed file is saved next to your original

REQUIREMENTS:
  - Python 3.8+  (you have this if you can run the file)
  - FFmpeg       (the app downloads it automatically on first run, Windows only)

If FFmpeg auto-download fails, install it manually from https://ffmpeg.org
and make sure ffmpeg.exe is on your PATH, then restart the app.
"""

import os
import sys
import json
import time
import shutil
import hashlib
import secrets
import zipfile
import tempfile
import threading
import webbrowser
import subprocess
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
import socket


def resource_dir():
    """
    Directory where bundled resources (like ffmpeg) live.
    - When frozen by PyInstaller --onedir: the folder containing the exe.
    - When frozen by PyInstaller --onefile: the temp unpack dir (sys._MEIPASS).
    - When running as a plain script: the folder containing this file.
    """
    if getattr(sys, "frozen", False):
        # onefile sets _MEIPASS; onedir keeps files next to the exe
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def pick_free_port(preferred=8731, attempts=40):
    """
    Return an available localhost TCP port. Tries the preferred port first,
    then scans upward. This stops the app from failing when another program
    is already using the default port.
    """
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    # last resort: let the OS choose any free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


RESOURCE_DIR = resource_dir()
# PORT is finalized in _start_server(), which binds directly (avoiding the
# check-then-bind race of probing a port first). This is only a placeholder.
PORT = 8731

# Per-session secret. The page embeds this and sends it on every request; the
# server rejects requests without it. Combined with the 127.0.0.1-only bind this
# stops other local processes, other websites (CSRF), and DNS-rebinding attacks
# from driving the app or reading files through it.
TOKEN = secrets.token_urlsafe(24)

# Per-user working area for temp files and (in script mode) downloaded ffmpeg.
APP_DIR = Path.home() / ".cliptrimmer"
APP_DIR.mkdir(exist_ok=True)
FFMPEG_DIR = APP_DIR / "ffmpeg"

# Job state shared between the export thread and status polling
JOBS = {}
JOB_COUNTER = [0]
# Live ffmpeg Popen objects per job (kept out of JOBS so status stays
# JSON-serializable) and the set of jobs the user asked to cancel.
JOB_PROCS = {}
JOB_CANCELS = set()

# Ceiling for the video bitrate. Beyond this the encoder just wastes bits
# (e.g. a 512 MB target on a 10 s clip would request ~400,000 kbps).
MAX_VIDEO_KBPS = 40000

# First-run FFmpeg download state, polled by the UI via /ffstatus.
FF_STATE = {"status": "idle", "progress": 0, "message": ""}


def compute_bitrates(target_mb, duration, keep_audio):
    """
    Pure helper: return (video_kbps, audio_kbps, capped) for a target size in
    MB and a clip duration in seconds. video_kbps is capped at MAX_VIDEO_KBPS.
    """
    duration = max(0.1, duration)
    total_kbits = target_mb * 8 * 1024
    if not keep_audio:
        audio_kbps = 0
    elif target_mb <= 4:
        audio_kbps = 96
    elif target_mb <= 8:
        audio_kbps = 128
    else:
        audio_kbps = 160
    video_kbps = int((total_kbits / duration - audio_kbps) * 0.97)
    capped = video_kbps > MAX_VIDEO_KBPS
    return (min(video_kbps, MAX_VIDEO_KBPS), audio_kbps, capped)


def parse_range_header(range_hdr, size):
    """
    Pure helper: parse an HTTP Range header against a resource of `size` bytes.
    Returns:
      None              - no usable range (absent or malformed): serve whole file
      (start, end)      - a satisfiable byte range (inclusive)
    Raises ValueError   - syntactically valid but unsatisfiable (=> 416).
    """
    if not range_hdr or not range_hdr.startswith("bytes="):
        return None
    spec = range_hdr.split("=", 1)[1].split(",")[0].strip()
    if "-" not in spec:
        return None
    first, last = spec.split("-", 1)
    try:
        if first == "" and last != "":
            # suffix range: last N bytes
            n = int(last)
            if n <= 0:
                raise ValueError("unsatisfiable")
            return (max(0, size - n), size - 1)
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError as e:
        if str(e) == "unsatisfiable":
            raise
        return None
    if start < 0:
        return None
    # Check satisfiability before well-formedness: "bytes=5000-" on a 1000-byte
    # file resolves end to 999, which would otherwise look like end < start.
    if start >= size:
        raise ValueError("unsatisfiable")
    if end < start:
        return None
    return (start, min(end, size - 1))


def validate_export_request(data):
    """
    Pure helper: validate the /export JSON payload (everything except the
    path check, which needs the filesystem). Returns (params, None) on
    success or (None, error_message) on failure.
    """
    if not isinstance(data, dict):
        return (None, "Request body must be a JSON object.")
    try:
        start = float(data["start"])
        end = float(data["end"])
        target_mb = float(data["target_mb"])
    except (KeyError, TypeError, ValueError):
        return (None, "start, end and target_mb must be numbers.")
    if not all(x == x and abs(x) != float("inf") for x in (start, end, target_mb)):
        return (None, "start, end and target_mb must be finite numbers.")
    if start < 0:
        return (None, "Start time cannot be negative.")
    if end - start < 0.1:
        return (None, "Selection is too short - it must be at least 0.1 seconds.")
    if not (1 <= target_mb <= 4096):
        return (None, "Target size must be between 1 and 4096 MB.")
    try:
        resolution = int(data.get("resolution", 0) or 0)
    except (TypeError, ValueError):
        return (None, "resolution must be a number.")
    if resolution < 0 or resolution > 4320:
        return (None, "resolution out of range.")
    return ({"start": start, "end": end, "target_mb": target_mb,
             "resolution": resolution,
             "keep_audio": bool(data.get("keep_audio", True))}, None)


def path_inside(path, base_dir):
    """
    Pure helper: resolve `path` and return its real path if it lands inside
    `base_dir` (or is base_dir itself), else None.

    Every endpoint that takes a path from the client runs it through here, so
    a crafted path can only ever name a file in the app's own temp dir. The
    realpath comes first so symlinks are resolved before the comparison
    rather than after it.
    """
    try:
        real = os.path.realpath(path) if path else ""
        base = os.path.realpath(str(base_dir))
    except Exception:
        return None
    if not real:
        return None
    return real if real == base or real.startswith(base + os.sep) else None


def prune_jobs(jobs, keep=8):
    """
    Pure helper: drop the oldest finished jobs, keeping at most `keep`
    finished entries (running/queued jobs and non-job keys are untouched).
    """
    finished = [k for k, v in jobs.items()
                if k.isdigit() and isinstance(v, dict)
                and v.get("status") in ("done", "error", "cancelled")]
    finished.sort(key=int)
    for k in finished[:max(0, len(finished) - keep)]:
        del jobs[k]


def export_active(jobs):
    """Pure helper: True if any export job is queued or encoding."""
    return any(isinstance(v, dict) and v.get("status") in ("queued", "encoding")
               for v in jobs.values())


def parse_fps_fraction(s, default=30.0):
    """
    Pure helper: parse ffprobe's avg_frame_rate (e.g. '30000/1001', '60/1')
    into a float fps. Returns `default` for junk or degenerate values.
    """
    try:
        s = (s or "").strip()
        if "/" in s:
            num, den = s.split("/", 1)
            num, den = float(num), float(den)
            if den == 0:
                return default
            fps = num / den
        else:
            fps = float(s)
        if not (1.0 <= fps <= 1000.0):
            return default
        return fps
    except (TypeError, ValueError):
        return default


def guess_video_mime(path):
    """Pure helper: Content-Type for a video file based on its extension."""
    ext = os.path.splitext(str(path))[1].lower()
    return {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo", ".ts": "video/mp2t",
    }.get(ext, "application/octet-stream")

# App lifetime / heartbeat state. The UI window pings /alive every couple of
# seconds; when the window is closed the pings stop and a watchdog thread shuts
# the whole app down, so nothing lingers in the background.
LIFE = {
    "last_ping": time.time(),   # timestamp of the most recent /alive
    "got_ping": False,          # has the page pinged at least once?
    "start": time.time(),       # process start time (for a startup grace period)
    "bye": False,               # set true by /bye for an immediate quit
    "hidden": False,            # is the window currently minimized/hidden?
}

# Where exported clips are saved. Defaults to Desktop (or home if no Desktop).
_default_out = Path.home() / "Desktop"
if not _default_out.exists():
    _default_out = Path.home()
OUTPUT_DIR = [str(_default_out)]   # list so it's mutable from handlers


# tkinter is not thread-safe and the HTTP server handles each request on its
# own thread, so the dialog runs in a single dedicated thread per request,
# serialized by this lock. A second click while a dialog is open returns
# immediately with busy=True instead of spawning a second Tk root.
_PICKER_LOCK = threading.Lock()


def _run_folder_dialog(result):
    """Create a Tk root, show the folder dialog, store the result. Runs in
    its own dedicated thread so Tk objects live and die on one thread."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()                       # hide the empty root window
        root.attributes("-topmost", True)     # bring dialog to front
        chosen = filedialog.askdirectory(
            initialdir=OUTPUT_DIR[0],
            title="Choose where to save exported clips"
        )
        root.destroy()
        result["folder"] = chosen or None
    except Exception as e:
        print(f"Folder picker unavailable: {e}")
        result["folder"] = None


def pick_output_folder():
    """
    Open a native OS folder-picker dialog. Returns (chosen_path_or_None, busy).
    busy=True means a dialog is already open and no new one was shown.
    """
    if not _PICKER_LOCK.acquire(blocking=False):
        return (None, True)
    try:
        result = {}
        t = threading.Thread(target=_run_folder_dialog, args=(result,))
        t.start()
        t.join()
        return (result.get("folder"), False)
    finally:
        _PICKER_LOCK.release()


# ----------------------------------------------------------------------------
# FFmpeg discovery / auto-install
# ----------------------------------------------------------------------------
def find_ffmpeg():
    """
    Return (ffmpeg_path, ffprobe_path) or (None, None) if not found.
    Search order, most-trusted first:
      1. Bundled alongside the app (PyInstaller build, or next to the script)
      2. A copy previously auto-downloaded into the per-user app folder
      3. The system PATH
    """
    exe = ".exe" if os.name == "nt" else ""

    # 1. Bundled with the app. PyInstaller is configured to drop ffmpeg into
    #    a "ffmpeg" subfolder of the resource dir; also check the dir itself.
    for base in (RESOURCE_DIR / "ffmpeg", RESOURCE_DIR):
        ff = base / f"ffmpeg{exe}"
        fp = base / f"ffprobe{exe}"
        if ff.is_file() and fp.is_file():
            return (str(ff), str(fp))

    # 2. Previously auto-downloaded copy (script mode only)
    if FFMPEG_DIR.exists():
        for root, _dirs, files in os.walk(FFMPEG_DIR):
            names = {f.lower() for f in files}
            if "ffmpeg.exe" in names and "ffprobe.exe" in names:
                return (str(Path(root) / "ffmpeg.exe"),
                        str(Path(root) / "ffprobe.exe"))
            if "ffmpeg" in names and "ffprobe" in names:
                return (str(Path(root) / "ffmpeg"),
                        str(Path(root) / "ffprobe"))

    # 3. System PATH
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if ff and fp:
        return (ff, fp)
    return (None, None)


def download_ffmpeg():
    """
    Download a static FFmpeg build for Windows, updating FF_STATE so the UI
    can show live progress. Returns True on success.
    """
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    zip_path = APP_DIR / "ffmpeg.zip"

    def _hook(blocks, block_size, total_size):
        if total_size > 0:
            pct = min(99, int(blocks * block_size * 100 / total_size))
            FF_STATE["progress"] = pct
            FF_STATE["message"] = f"Downloading FFmpeg… {pct}%"

    try:
        FF_STATE.update(status="downloading", progress=0,
                        message="Downloading FFmpeg… 0%")
        print("Downloading FFmpeg (one-time setup, ~25 MB)...")
        urllib.request.urlretrieve(url, zip_path, reporthook=_hook)
        FF_STATE.update(status="extracting", progress=99,
                        message="Extracting FFmpeg…")
        print("Extracting...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(FFMPEG_DIR)
        zip_path.unlink(missing_ok=True)
        FF_STATE.update(status="ready", progress=100, message="FFmpeg ready")
        return True
    except Exception as e:
        FF_STATE.update(status="error",
                        message=f"FFmpeg auto-download failed: {e}")
        print(f"FFmpeg auto-download failed: {e}")
        return False


def parse_probe_fields(text):
    """
    Pure helper: turn ffprobe's 'key=value' output into a dict. Malformed
    lines are skipped and ffprobe's 'N/A' becomes an empty string.

    We ask for keys rather than ffprobe's nokey format on purpose: with
    -show_entries the fields come back in ffprobe's own internal order, not
    the order they were requested, so positional parsing silently swaps
    width and frame rate depending on the file.
    """
    fields = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        fields[key.strip()] = "" if value == "N/A" else value
    return fields


def _probe(src, stream_entries=None, format_entries=None):
    """Run ffprobe against `src` and return its key=value output as a dict."""
    if not FFPROBE:
        return {}
    cmd = [FFPROBE, "-v", "error"]
    if stream_entries:
        cmd += ["-select_streams", "v:0", "-show_entries",
                "stream=" + stream_entries]
    if format_entries:
        cmd += ["-show_entries", "format=" + format_entries]
    cmd += ["-of", "default=noprint_wrappers=1", str(src)]
    try:
        return parse_probe_fields(subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ).stdout)
    except Exception:
        return {}


def probe_media(src):
    """
    Ask ffprobe for the frame rate and pixel size of a clip. Returns
    (fps, width, height); fps falls back to 30.0 and the dimensions to 0 when
    ffprobe is missing or the probe fails. Zero dimensions mean "unknown", and
    the UI simply omits the source-resolution readout rather than guessing.
    """
    fields = _probe(src, stream_entries="avg_frame_rate,width,height")
    fps = parse_fps_fraction(fields.get("avg_frame_rate", ""))

    def as_int(key):
        try:
            return max(0, int(fields.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return (fps, as_int("width"), as_int("height"))


def probe_duration(src):
    """Clip length in seconds, or 0.0 if ffprobe can't tell us."""
    try:
        return max(0.0, float(_probe(
            src, format_entries="duration").get("duration") or 0))
    except (TypeError, ValueError):
        return 0.0


# ---- timeline filmstrip -----------------------------------------------------
# Evenly-spaced frames tiled into one JPEG and stretched across the timeline
# track, so a cut can be found by eye instead of by scrubbing a blank bar.
# Rendered once per source and cached on disk next to the upload.
FILMSTRIP_FRAMES = 20
FILMSTRIP_HEIGHT = 58          # matches .tl-track's height in the stylesheet
FILMSTRIP_LOCK = threading.Lock()


def filmstrip_path(src):
    """Cache location for a clip's strip. The name is a digest of the source
    path so it is filesystem-safe whatever the clip was called."""
    digest = hashlib.sha1(str(src).encode("utf-8", "replace")).hexdigest()
    return APP_DIR / f"strip_{digest[:16]}.jpg"


def filmstrip_filter(duration, frames=FILMSTRIP_FRAMES,
                     height=FILMSTRIP_HEIGHT):
    """
    Pure helper: the -vf chain that samples `frames` frames across `duration`
    seconds and tiles them into one horizontal strip.

    The sample rate asks for two extra frames on purpose. `fps` rounds, and
    `tile` only emits once it has a full row - undershooting by a single frame
    means no strip at all rather than a short one.
    """
    rate = (frames + 2) / max(0.1, duration)
    return (f"fps={rate:.6f},scale=-2:{height},tile={frames}x1")


def build_filmstrip(src, duration=None):
    """
    Render the strip for `src`, returning its cached path or None.

    None is a normal outcome, not an error: no ffmpeg, an unreadable clip or a
    render that overruns all end up here, and the timeline just stays blank.
    The lock means a second caller waits for the first render instead of
    starting a duplicate one.
    """
    if not FFMPEG:
        return None
    out = filmstrip_path(src)
    with FILMSTRIP_LOCK:
        if out.is_file() and out.stat().st_size:
            return out
        if duration is None:
            duration = probe_duration(src)
        if duration <= 0:
            return None
        cmd = [FFMPEG, "-y", "-i", str(src), "-an", "-sn",
               "-vf", filmstrip_filter(duration),
               "-frames:v", "1", "-q:v", "5", str(out)]
        try:
            subprocess.run(
                cmd, capture_output=True, timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            return None
        return out if out.is_file() and out.stat().st_size else None


FFMPEG, FFPROBE = find_ffmpeg()


# ----------------------------------------------------------------------------
# Video helpers
# ----------------------------------------------------------------------------
def _run_ffmpeg_pass(cmd, job, duration, pass_label, pass_base, pass_span):
    """
    Run one FFmpeg pass via Popen, parsing '-progress pipe:1' output to update
    job['progress'] (0-100 overall). pass_base/pass_span map this pass onto a
    slice of the overall bar (e.g. pass 1 = 0..40, pass 2 = 40..100).
    Returns (returncode, stderr_tail).
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    # Register the live process so /cancel can terminate it.
    JOB_PROCS[job["id"]] = proc

    # FFmpeg writes progress key=value lines to stdout; stderr holds errors.
    # Drain stderr on a side thread so a full pipe can't deadlock the process.
    stderr_lines = []

    def _drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
            if len(stderr_lines) > 200:
                del stderr_lines[0]

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_ms="):
            try:
                # out_time_ms is microseconds despite the name
                done_sec = int(line.split("=", 1)[1]) / 1_000_000
                frac = min(1.0, max(0.0, done_sec / duration))
                job["progress"] = round(pass_base + frac * pass_span, 1)
                job["message"] = f"{pass_label} - {job['progress']:.0f}%"
            except (ValueError, ZeroDivisionError):
                pass
        elif line == "progress=end":
            job["progress"] = round(pass_base + pass_span, 1)

    proc.wait()
    t.join(timeout=1)
    JOB_PROCS.pop(job["id"], None)
    return proc.returncode, "".join(stderr_lines[-12:])


def cancel_export(job_id):
    """Mark a job cancelled and terminate its ffmpeg process if running."""
    job = JOBS.get(job_id)
    if not isinstance(job, dict) or job.get("status") not in ("queued", "encoding"):
        return False
    JOB_CANCELS.add(job_id)
    proc = JOB_PROCS.get(job_id)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return True


def run_export(job_id, src, start, end, target_mb, output_path,
               resolution, keep_audio=True):
    """
    Trim [start, end] from src and compress to ~target_mb, two-pass.
    resolution: 0 = keep original, otherwise the target height (e.g. 720).
    keep_audio: if False, the clip is exported with no audio track and the
                whole size budget goes to video.
    """
    job = JOBS[job_id]
    try:
        duration = max(0.1, end - start)

        # Calculate target bitrate (capped at MAX_VIDEO_KBPS - beyond that
        # extra bits are wasted and encoding just gets slower).
        video_kbps, audio_kbps, capped = compute_bitrates(
            target_mb, duration, keep_audio)
        if capped:
            job["note"] = (f"Bitrate capped at {MAX_VIDEO_KBPS:,} kbps - "
                           "the file will be smaller than the target.")

        if video_kbps < 50:
            job["status"] = "error"
            job["message"] = ("Target size is too small for this clip length. "
                              "Try a larger size or a shorter trim.")
            return

        # Optional downscale. -2 keeps the width even and preserves aspect
        # ratio; the scale filter only shrinks (won't upscale a smaller clip).
        scale_args = []
        if resolution and resolution > 0:
            scale_args = ["-vf", f"scale=-2:'min({resolution},ih)'"]

        # Pass-2 audio args depend on the toggle.
        if keep_audio:
            audio_args = ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
        else:
            audio_args = ["-an"]

        passlog = str(APP_DIR / f"passlog_{job_id}")
        null_out = "NUL" if os.name == "nt" else "/dev/null"

        job["status"] = "encoding"
        job["progress"] = 0.0

        # ---- Pass 1 (no audio, analysis only) -> bar 0..40 ----
        cmd1 = [FFMPEG, "-y", "-ss", str(start), "-to", str(end), "-i", src,
                "-c:v", "libx264", "-b:v", f"{video_kbps}k",
                *scale_args,
                "-pass", "1", "-passlogfile", passlog,
                "-an", "-f", "mp4",
                "-progress", "pipe:1", "-nostats", null_out]
        rc, err = _run_ffmpeg_pass(cmd1, job, duration,
                                   "Pass 1 of 2 (analyzing)", 0, 40)
        if job_id in JOB_CANCELS:
            job["status"] = "cancelled"
            job["message"] = "Export cancelled."
            return
        if rc != 0:
            job["status"] = "error"
            job["message"] = "Encoding failed during pass 1.\n" + err
            return

        # ---- Pass 2 (final) -> bar 40..100 ----
        cmd2 = [FFMPEG, "-y", "-ss", str(start), "-to", str(end), "-i", src,
                "-c:v", "libx264", "-b:v", f"{video_kbps}k",
                *scale_args,
                "-pass", "2", "-passlogfile", passlog,
                *audio_args,
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", output_path]
        rc, err = _run_ffmpeg_pass(cmd2, job, duration,
                                   "Pass 2 of 2 (encoding)", 40, 60)
        if job_id in JOB_CANCELS:
            job["status"] = "cancelled"
            job["message"] = "Export cancelled."
            return
        if rc != 0:
            job["status"] = "error"
            job["message"] = "Encoding failed during pass 2.\n" + err
            return

        final_mb = os.path.getsize(output_path) / (1024 * 1024)
        job["status"] = "done"
        job["progress"] = 100.0
        job["message"] = (f"Done! Saved to:\n{output_path}\n"
                           f"Final size: {final_mb:.2f} MB")
        if job.get("note"):
            job["message"] += "\n" + job["note"]
        job["output"] = output_path
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Unexpected error: {e}"
    finally:
        JOB_PROCS.pop(job_id, None)
        JOB_CANCELS.discard(job_id)
        # Remove the two-pass log files, even if a pass failed, plus any
        # partial output left behind by a cancelled/failed encode.
        for f in APP_DIR.glob(f"passlog_{job_id}*"):
            try:
                f.unlink()
            except OSError:
                pass
        if job.get("status") in ("cancelled", "error"):
            try:
                if os.path.exists(output_path):
                    os.unlink(output_path)
            except OSError:
                pass


# ----------------------------------------------------------------------------
# Fonts
# ----------------------------------------------------------------------------
# Outfit 400 + 600 - the family's only two weights - inlined as woff2 so the
# page needs no network at runtime and the app stays a single file that
# PyInstaller can freeze without a data-file manifest. Byte-identical to the
# files served from my web server's static fonts folder.
OUTFIT_400_WOFF2_B64 = (
    "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"
)

OUTFIT_600_WOFF2_B64 = (
    "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"
    "9SyULq0IZ7+oMr0L"
)


# ----------------------------------------------------------------------------
# HTML / JS front-end
# ----------------------------------------------------------------------------
def build_page():
    ff_ready = "true" if (FFMPEG and FFPROBE) else "false"
    return (PAGE_HTML
            .replace("__FFMPEG_READY__", ff_ready)
            .replace("__TOKEN__", TOKEN)
            .replace("__OUTFIT400__", OUTFIT_400_WOFF2_B64)
            .replace("__OUTFIT600__", OUTFIT_600_WOFF2_B64))


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#ffffff">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#17181a">
<title>clip. — trim and compress</title>
<!-- Resolve the sheet BEFORE first paint, and always write it out both ways.
     Writing only 'dark' would leave a stored light choice on a dark-system
     machine with no attribute at all, so the palette went light while
     color-scheme stayed dark: dark scrollbars and selects on the white sheet. -->
<script>try{var t=localStorage.getItem('clip_theme');
var d=t==='dark'||(!t&&matchMedia('(prefers-color-scheme: dark)').matches);
document.documentElement.dataset.theme=d?'dark':'light'}catch(e){}</script>
<style>html{background:#fff}html[data-theme=dark]{background:#17181a}</style>
<style>
  /* ===================================================== CANON, PLAYED STRAIGHT
     Ported 2026-08-05 from my web apps — the family's
     shipped system, token for token. Replaces the dark-mauve world entirely:
     no dot grid, no panel stack, no tinted boxes, no drop shadows on content.

     THE CONTRACT (inherited, unedited):
       · Components use ONLY these tokens. No raw colors below :root.
       · One accent (--ac) + one ambient (--ac2); every other accent shade is
         color-mix-derived here. Never hand-pick a shade downstream.
       · Light is the flagship (white sheet); dark is neutral graphite #17181a
         — never blue-gray, never black. Both always emitted; the orb flips
         data-theme on <html> instantly.
       · Outfit 400 + 600 only, self-hosted, NO MONOSPACE EVER (Felix-pinned)
         — timecodes align via tabular-nums, which is on the body.
       · Sentence case everywhere; nothing tracks positive.
       · Type is three rungs: 26/600 title, 17/600 data, 13/400 body; tracking
         scales with size (-.021em / -.012em / 0). A new size does not create
         a new rung.
       · THE BARE-CONTENT RULE: content sits bare on the sheet — hairlines,
         no boxes, no fills, no tints, no shadows. A "card" is a run of the
         sheet fenced by a top hairline.
       · THE OPT-IN FILL RULE: buttons are NOT filled by default. The bare
         `button` element is a neutral reset; the filled accent capsule is
         opt-in via .btn, and there is exactly ONE per view (Export).
       · Liquid Glass is CHROME ONLY — the topbar and the theme orb. Content
         never gets glass.
     ======================================================================== */

  /* Self-hosted so the app is a single file with no network at runtime; the
     family serves these two weights from /static/fonts and nothing else. */
  @font-face{font-family:'Outfit';font-style:normal;font-weight:400;
    font-display:swap;src:url(data:font/woff2;base64,__OUTFIT400__) format('woff2')}
  @font-face{font-family:'Outfit';font-style:normal;font-weight:600;
    font-display:swap;src:url(data:font/woff2;base64,__OUTFIT600__) format('woff2')}

  :root {
    /* the accent pair + derivations. --ac is painted raw only on bar fills,
       focus rings and markers; --ac2's only job is the ambient ground. Text
       shades flip per theme; the FILL does not — it is always darkened so its
       white label never drops under 4.5:1, and hover deepens rather than
       brightens. */
    --ac: #3a6fd8;
    --ac2: #5b8def;
    --ac-fill: color-mix(in srgb, var(--ac) 86%, #000000);
    --ac-fill-hot: color-mix(in srgb, var(--ac) 78%, #000000);
    --on-fill: #ffffff;        /* label ink on --ac-fill — the ONLY white-on-accent */

    --sans: 'Outfit', ui-rounded, 'SF Pro Rounded', -apple-system,
      BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;

    /* type: three rungs + the caption tier hanging off body */
    --fs-title: 26px;  --tr-title: -0.021em;
    --fs-data: 17px;   --tr-data: -0.012em;
    --fs-ui: 15px;     /* fields, control reset */
    --fs-b: 13px;      /* body — the default */
    --fs-s: 12px;
    --fs-xs: 11.5px;

    /* corner language: content squares off, controls are capsules. The
       family's --r-xl/--r-xxl (popover and toolbar-glass radii) have no
       consumer here: this app's one glass element is a full-width bar. */
    --r-sm: 6px;    /* kbd, ticks, small furniture */
    --r-md: 8px;    /* revealed hit areas, preset tiles */
    --r-lg: 12px;   /* presented images — the video stage, the timeline track */
    --r-cap: 999px;

    /* motion */
    --ease-settle: cubic-bezier(.2,.7,.3,1);
    --ease-runout: cubic-bezier(.23,1,.32,1);
    --t-press: 140ms;
    --t-quiet: 150ms;
    --t-state: 180ms;
    --t-lift: 250ms;
    --t-rise: 380ms;

    /* the transport bar rides ON the video, which is black in both palettes,
       so its ink is fixed rather than themed — the one place values are not
       drawn from the sheet tokens, because the ground is not the sheet. */
    --onvid: #ffffff;
    --onvid-m: rgba(255,255,255,.72);
    --onvid-track: rgba(255,255,255,.24);

    color-scheme: light dark;
  }

  /* ---- light: the flagship white sheet ------------------------------------ */
  :root, :root[data-theme="light"] {
    --ac-pale: color-mix(in srgb, var(--ac) 92%, #000000);  /* accent as TEXT */

    --bg: #ffffff;              /* the sheet; lives on <html> */
    --surface: #ffffff;
    --surface3: #f2f2f4;        /* quiet fill: inputs, revealed hit areas */
    --hovrow: rgba(0,0,0,.05);  /* the only tint content ever receives */
    --txt: #1d1d1f;
    --m1: #6e6e73;              /* secondary text */
    --tert: #86868b;            /* non-text furniture (ticks, glyphs) */
    --hair: rgba(0,0,0,.1);
    --hairh: rgba(0,0,0,.28);
    --hair2: rgba(0,0,0,.07);
    --track: #ececef;           /* unfilled bar portion */
    --tagmix: 12%;              /* accent-wash strength for chips */

    /* Liquid Glass chrome: translucent ground + dual rim (specular top catch,
       bottom falloff — the pair is what makes a lens, not a frosted card) */
    --glass: rgba(255,255,255,.72);
    --glasssolid: #ffffff;      /* reduced-transparency / no-backdrop-filter fallback */
    --rim: rgba(255,255,255,.85);
    --rimb: rgba(15,20,30,.06);
    --segon: #ffffff;
    --shc: rgba(20,25,35,.1);   /* glass cast shadow */
    --shc3: rgba(20,25,35,.08); /* whisper cast: at-rest chrome */
    /* filled portion of a data bar. Raw accent clears 3:1 against the light
       track but only 2.5:1 against the dark one, so dark lifts it. */
    --barfill: var(--ac);

    /* ambient ground (body::before): two very wide, very soft radials pooled
       in the top corners, derived ONLY from --ac/--ac2 — the blue tint the
       glass needs to lens. If you can name where it stops, it is too strong. */
    --amb: radial-gradient(120% 62% at 84% -22%, color-mix(in srgb, var(--ac2) 6%, transparent), transparent 60%),
           radial-gradient(96% 52% at -14% -6%, color-mix(in srgb, var(--ac) 4%, transparent), transparent 56%);

    /* status voices: green is good/save, red is errors only, amber is the
       quiet watch voice (a missing dependency), never an alert. */
    --ok: #218054;
    --err: #b03a2e;
    --warn: #a8641f;
  }

  /* ---- dark: neutral graphite — never blue-gray, never black -------------- */
  :root[data-theme="dark"] {
    --ac-pale: color-mix(in srgb, var(--ac) 45%, #ffffff);

    --bg: #17181a;
    --surface: #1c1d20;
    --surface3: rgba(255,255,255,.08);
    --hovrow: rgba(255,255,255,.07);
    --txt: #f2f2f4;
    --m1: #98989f;
    --tert: #8a8a92;
    --hair: rgba(255,255,255,.12);
    --hairh: rgba(255,255,255,.3);
    --hair2: rgba(255,255,255,.08);
    --track: rgba(255,255,255,.14);
    --tagmix: 18%;              /* dark grounds swallow a low-alpha wash */

    --glass: rgba(30,31,35,.62);
    --glasssolid: #212226;
    --rim: rgba(255,255,255,.16);
    --rimb: rgba(0,0,0,.35);    /* heavier: a light-alpha edge vanishes on dark */
    --segon: rgba(255,255,255,.1);
    --shc: rgba(0,0,0,.34);
    --shc3: rgba(0,0,0,.28);
    --barfill: var(--ac-pale);

    /* dark needs more alpha (11/8) because #17181a eats it, but the ellipses
       are shorter so the tint stays a halo at the top */
    --amb: radial-gradient(110% 54% at 84% -20%, color-mix(in srgb, var(--ac2) 11%, transparent), transparent 55%),
           radial-gradient(88% 46% at -14% -6%, color-mix(in srgb, var(--ac) 8%, transparent), transparent 52%);

    --ok: #43c383;
    --err: #ff8a7a;
    --warn: #cf9a62;
  }
  :root[data-theme="dark"] { color-scheme: dark; }
  :root[data-theme="light"] { color-scheme: light; }

  @media (prefers-contrast: more) {
    :root, :root[data-theme="light"] {
      --hair: rgba(0,0,0,.32); --hairh: rgba(0,0,0,.5);
      --m1: #4a4a4f; --tert: #5c5c61;
    }
    :root[data-theme="dark"] {
      --hair: rgba(255,255,255,.36); --hairh: rgba(255,255,255,.55);
      --m1: #c0c0c8; --tert: #b0b0b8;
    }
  }

  /* ---- ground ------------------------------------------------------------- */
  * { box-sizing: border-box; margin: 0; padding: 0; }
  /* --bg lives on <html> so the canvas is always the flat ground colour; body
     is transparent over it and carries only the ambient radials. */
  html {
    height: 100%;
    background: var(--bg);
    -webkit-text-size-adjust: 100%;
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
  }
  body {
    height: 100%;
    background: transparent;
    color: var(--txt);
    font: 400 var(--fs-b)/1.45 var(--sans);
    font-variant-numeric: tabular-nums;   /* every timecode in the app aligns here */
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
    display: flex; flex-direction: column;
  }
  /* position:fixed on a PSEUDO, not background-attachment — mobile Safari
     repaints an attached background every scroll frame. */
  body::before {
    content: ''; position: fixed; inset: 0; z-index: -1;
    pointer-events: none; background: var(--amb);
  }

  /* bare button element = NEUTRAL RESET ONLY (The Opt-In Fill Rule: filled
     identity lives on .btn and nowhere else; no button:hover may paint).
     Typography stays here on purpose — it is a reset, not an identity. */
  button {
    background: transparent; border: 0; color: inherit;
    font-family: inherit; font-size: var(--fs-ui); font-weight: 600;
    cursor: pointer;
  }
  /* press-scale is element-level feedback: it sets no color, so it cannot leak */
  button:active { transform: scale(.97); }
  button:disabled { opacity: .45; cursor: default; }
  button:disabled:active { transform: none; }
  :focus-visible { outline: 2px solid var(--ac); outline-offset: 2px;
    border-radius: 4px; }

  /* the app's only keyboard furniture: a quiet fill, never a bordered key */
  kbd {
    background: var(--surface3); border: 0; border-radius: var(--r-sm);
    padding: 2px 6px; font: 600 var(--fs-xs)/1 var(--sans);
    color: var(--tert); min-width: 19px; text-align: center;
    display: inline-block;
  }

  @keyframes rise { from { opacity: 0; transform: translateY(8px); } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
      animation-duration: 0s !important; transition-duration: 0s !important;
    }
  }

  /* ---- Liquid Glass: chrome only, never content --------------------------- */
  /* The dual rim is what makes these read as a LENS rather than a frosted
     card: light catches the top edge (--rim) and falls off at the bottom
     (--rimb). Both are sub-threshold on purpose — felt as curvature, never
     seen as an outline. The cast shadow is separate and carries elevation. */
  .topbar, #themeflip {
    background: var(--glass);
    -webkit-backdrop-filter: blur(16px) saturate(170%);
    backdrop-filter: blur(16px) saturate(170%);
    box-shadow: inset 0 1px 1px var(--rim), inset 0 -1px 1px var(--rimb),
      0 10px 26px var(--shc);
  }

  /* ---------- masthead ---------- */
  .topbar {
    position: relative; z-index: 10; flex-shrink: 0;
    height: 58px; display: flex; align-items: center; gap: 14px;
    padding: 0 18px; border-bottom: 1px solid var(--hair);
  }
  .wordmark {
    font-size: var(--fs-data); font-weight: 600;
    letter-spacing: var(--tr-data); color: var(--txt); white-space: nowrap;
  }
  .wordmark-dot { color: var(--ac-pale); }
  .topbar .sub {
    color: var(--m1); font-size: var(--fs-s); white-space: nowrap;
  }
  .mast-sep { width: 3px; height: 3px; border-radius: 50%;
    background: var(--hair); flex-shrink: 0; }

  /* the loaded clip, stated as a line — not a chip, not a box */
  .file-chip {
    display: flex; align-items: baseline; gap: 9px; min-width: 0;
    font-size: var(--fs-s); color: var(--m1);
  }
  .file-chip .nm {
    font-size: var(--fs-b); font-weight: 600; color: var(--txt);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .file-chip .meta { white-space: nowrap; }

  .topbar .spacer { flex: 1; }
  .hotkey-legend {
    display: flex; align-items: center; gap: 6px;
    color: var(--tert); font-size: var(--fs-xs);
  }
  .hotkey-legend span { margin-right: 4px; }

  /* the theme orb: a 38px glass lens docked in the masthead */
  #themeflip {
    width: 38px; height: 38px; flex-shrink: 0; border-radius: 50%;
    color: var(--m1); display: inline-flex; align-items: center;
    justify-content: center; padding: 0;
    border: 1px solid var(--hair);
    transition: color var(--t-quiet), transform var(--t-press) ease-out;
  }
  #themeflip:active { transform: scale(.94); }
  @media (hover: hover) { #themeflip:hover { color: var(--txt); } }

  /* ---------- layout ---------- */
  /* flex:1 + min-height:0 (instead of a hardcoded 100vh calc) means the
     warning banner can appear above without pushing the sidebar's export
     button below the viewport. */
  .layout {
    display: grid; grid-template-columns: 1fr 348px;
    flex: 1; min-height: 0;
  }
  .main {
    display: flex; flex-direction: column;
    padding: 18px; gap: 16px; min-width: 0; overflow: hidden;
  }
  /* the sidebar is a run of the sheet fenced by a hairline — not a panel */
  .sidebar {
    border-left: 1px solid var(--hair);
    padding: 4px 20px 20px; overflow-y: auto;
    display: flex; flex-direction: column;
    scrollbar-width: thin; scrollbar-color: var(--hair) transparent;
  }
  .sidebar::-webkit-scrollbar { width: 8px; }
  .sidebar::-webkit-scrollbar-thumb {
    background: var(--hair); border-radius: 4px; }

  /* ---------- drop zone ---------- */
  /* The one dashed edge in the system, and it earns it: a drop target has to
     read as an opening rather than a surface. Everything else stays hairline. */
  .dropzone {
    flex: 1; border: 1px dashed var(--hairh);
    border-radius: var(--r-lg); display: flex;
    flex-direction: column; align-items: center; justify-content: center;
    gap: 6px; cursor: pointer; background: transparent;
    transition: border-color var(--t-state), background var(--t-state);
  }
  .dropzone:hover, .dropzone.over {
    border-color: var(--ac); background: var(--hovrow); }
  .dropzone .dz-ico { font-size: 30px; opacity: .9; margin-bottom: 2px; }
  .dropzone .dz-big {
    font-size: var(--fs-title); font-weight: 600;
    letter-spacing: var(--tr-title); color: var(--txt); }
  .dropzone .dz-small { color: var(--m1); font-size: var(--fs-b); }

  /* ---------- video stage ---------- */
  /* A presented image: --r-lg, hairline, no shadow (The Bare-Content Rule). */
  .stage {
    position: relative; background: #000; border-radius: var(--r-lg);
    overflow: hidden; flex: 1; min-height: 0;
    border: 1px solid var(--hair);
  }
  video { width: 100%; height: 100%; object-fit: contain;
    display: block; background: #000; }

  /* transport overlay along the bottom of the video. Its ground is the clip,
     not the sheet, so it uses the fixed --onvid ink in both palettes. */
  .transport {
    position: absolute; left: 0; right: 0; bottom: 0;
    display: flex; align-items: center; gap: 14px;
    padding: 22px 16px 14px;
    background: linear-gradient(0deg, rgba(0,0,0,.78) 0%, transparent 100%);
    color: var(--onvid-m);
  }
  /* on-video controls are bare glyphs — the filled capsule is spoken for */
  .play-btn, .vol-btn {
    color: var(--onvid); font-size: 15px; padding: 4px;
    display: flex; align-items: center; justify-content: center;
    transition: opacity var(--t-quiet);
  }
  .play-btn { font-size: 18px; width: 30px; flex-shrink: 0; }
  @media (hover: hover) {
    .play-btn:hover, .vol-btn:hover { opacity: .7; }
  }
  .ts { font-size: var(--fs-s); white-space: nowrap; }
  .ts .cur { color: var(--onvid); font-weight: 600; }
  .seekbar {
    flex: 1; height: 4px; border-radius: var(--r-cap); position: relative;
    background: var(--onvid-track); cursor: pointer;
  }
  .seekbar .filled {
    position: absolute; left: 0; top: 0; bottom: 0;
    background: var(--onvid); border-radius: var(--r-cap);
  }
  .seekbar .knob {
    position: absolute; top: 50%; width: 12px; height: 12px;
    border-radius: 50%; background: var(--onvid);
    transform: translate(-50%,-50%);
  }

  /* progress overlay (during export) */
  .export-overlay {
    position: absolute; inset: 0; display: none;
    flex-direction: column; align-items: center; justify-content: center;
    gap: 14px; background: rgba(0,0,0,.72);
    -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px);
    color: var(--onvid);
  }
  .export-overlay.show { display: flex; animation: rise var(--t-lift) var(--ease-settle); }
  .export-overlay .eo-label { font-size: var(--fs-b); color: var(--onvid-m); }
  .eo-pct {
    font-size: var(--fs-title); font-weight: 600;
    letter-spacing: var(--tr-title); color: var(--onvid);
  }
  .eo-bar {
    width: 64%; max-width: 420px; height: 4px;
    background: var(--onvid-track); border-radius: var(--r-cap);
    overflow: hidden;
  }
  .eo-fill {
    height: 100%; width: 0%; border-radius: var(--r-cap);
    background: var(--onvid); transition: width var(--t-lift) var(--ease-runout);
  }
  .eo-sub { font-size: var(--fs-s); color: var(--onvid-m); }
  .eo-cancel {
    margin-top: 2px; font-size: var(--fs-b); color: var(--onvid-m);
    transition: color var(--t-quiet);
  }
  @media (hover: hover) { .eo-cancel:hover { color: var(--onvid); } }

  /* ---------- transport row under the stage ---------- */
  .controls-row {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    justify-content: center;
  }
  /* the quiet capsule — grey fill, no border, ink label. Every secondary
     action in the app is one of these; only Export is filled. */
  .pill {
    font: 600 var(--fs-b) var(--sans); color: var(--txt);
    background: var(--surface3); border-radius: var(--r-cap);
    padding: 9px 16px; display: inline-flex; align-items: center; gap: 8px;
    transition: background var(--t-state);
  }
  @media (hover: hover) { .pill:hover { background: var(--hovrow); } }
  .pill kbd { background: transparent; padding: 0 1px; min-width: 0;
    color: var(--m1); }

  /* ---------- timeline ---------- */
  /* a run of the sheet fenced by a top hairline — not a panel */
  .timeline { border-top: 1px solid var(--hair); padding: 12px 0 2px; }
  .tl-ruler { position: relative; height: 20px; margin-bottom: 8px; }
  .tl-ruler .tk {
    position: absolute; bottom: 0; width: 1px; background: var(--tert);
  }
  .tl-ruler .tk.min { height: 4px; opacity: .35; }
  .tl-ruler .tk.maj { height: 8px; opacity: .6; }
  .tl-ruler .lb {
    position: absolute; bottom: 10px; transform: translateX(-50%);
    font-size: var(--fs-xs); color: var(--tert); white-space: nowrap;
  }
  /* The filmstrip is painted as the track's background by the JS once it
     arrives, stretched edge to edge — the strip samples evenly across the
     clip, so its frames line up with the ruler above by construction. */
  .tl-track {
    position: relative; height: 58px; border-radius: var(--r-lg);
    background: var(--surface3) center / 100% 100% no-repeat;
    cursor: pointer; overflow: hidden;
  }
  /* what is NOT selected, veiled back */
  .tl-dim {
    position: absolute; top: 0; bottom: 0; pointer-events: none; z-index: 2;
    background: color-mix(in srgb, var(--bg) 62%, transparent);
  }
  .tl-grid {
    position: absolute; top: 0; bottom: 0; width: 1px;
    background: var(--hair); pointer-events: none;
  }
  /* the selection is the accent wash — the same chip wash used everywhere */
  .tl-selection {
    position: absolute; top: 0; bottom: 0; pointer-events: none;
    background: color-mix(in srgb, var(--ac) var(--tagmix), transparent);
  }
  /* In / out markers are BOTH the accent, told apart by which way the flag
     points — the one-accent rule holds, and red stays reserved for errors. */
  .tl-marker {
    position: absolute; top: 0; bottom: 0; width: 2px;
    background: var(--ac); pointer-events: none; z-index: 4;
  }
  /* The grab strip. The visible marker is 2px, which is nowhere near a
     pointer target, so an invisible 16px band rides with it and takes the
     drag. pointer-events is re-enabled here only: the marker line itself
     stays transparent to clicks so scrubbing the track still works. */
  .tl-marker::before {
    content: ''; position: absolute; top: 0; bottom: 0;
    left: -7px; width: 16px;
    pointer-events: auto; cursor: ew-resize;
  }
  .tl-marker .flag {
    position: absolute; top: 0; width: 0; height: 0;
    border-top: 9px solid var(--ac); border-bottom: 9px solid transparent;
  }
  .tl-marker.in  .flag { left: 0;  border-right: 8px solid transparent; }
  .tl-marker.out .flag { right: 0; border-left: 8px solid transparent; }
  /* while a marker is being dragged the whole track takes the resize cursor,
     so it does not flicker back to the scrub pointer between frames */
  .tl-track.dragging { cursor: ew-resize; }
  /* the playhead is ink: the strongest mark on the track, and never accent */
  .tl-playhead {
    position: absolute; top: 0; bottom: 0; width: 2px;
    background: var(--txt); pointer-events: none; z-index: 5;
  }
  .tl-playhead .ph {
    position: absolute; top: 0; left: 50%; transform: translateX(-50%);
    width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 7px solid var(--txt);
  }
  .tl-foot {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-top: 8px; font-size: var(--fs-s); color: var(--m1);
  }
  .tl-foot b {
    font-size: var(--fs-b); font-weight: 600; color: var(--txt);
    margin-left: 5px;
  }

  /* ---------- sidebar sections ---------- */
  /* a section is a run fenced by a top hairline; the first one is unfenced
     because there is nothing above it to divide from */
  .sec {
    border-top: 1px solid var(--hair);
    padding: 16px 0 18px;
    display: flex; flex-direction: column; gap: 10px;
  }
  .sec:first-child { border-top: 0; }
  .sec-title {
    font-size: var(--fs-data); font-weight: 600;
    letter-spacing: var(--tr-data); line-height: 1.3; color: var(--txt);
    display: flex; justify-content: space-between; align-items: baseline;
  }
  .sec-title .hint { font-size: var(--fs-s); font-weight: 400; color: var(--m1);
    letter-spacing: 0; }

  /* preset tiles: quiet fills that take the accent wash when chosen */
  .preset-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .preset {
    background: var(--surface3); border: 1px solid transparent;
    border-radius: var(--r-md); padding: 9px 11px;
    text-align: left; color: var(--txt); font-weight: 400;
    display: flex; flex-direction: column; gap: 2px;
    transition: background var(--t-state), border-color var(--t-state);
  }
  @media (hover: hover) { .preset:hover { background: var(--hovrow); } }
  .preset.active {
    background: color-mix(in srgb, var(--ac) var(--tagmix), transparent);
    border-color: color-mix(in srgb, var(--ac) 35%, transparent);
  }
  .preset .p-name { font-size: var(--fs-b); font-weight: 600; }
  .preset.active .p-name { color: var(--ac-pale); }
  .preset .p-sub { font-size: var(--fs-s); color: var(--m1); }

  /* every single-line control is a capsule; the extra side padding buys the
     curve back so text never crowds the round end */
  .size-input-wrap { position: relative; }
  .size-input-wrap .unit {
    position: absolute; right: 16px; top: 50%; transform: translateY(-50%);
    color: var(--m1); font-size: var(--fs-s); pointer-events: none;
  }
  input.field {
    width: 100%; background: var(--surface3); color: var(--txt);
    border: 0; border-radius: var(--r-cap);
    padding: 10px 16px; font: 400 var(--fs-ui) var(--sans);
    outline: none;
  }
  input.field:focus, input.field:focus-visible {
    outline: 2px solid var(--ac); outline-offset: 0; }

  /* trim point rows: hairline-fenced runs, no fill, no border box */
  .trim-row {
    display: flex; align-items: center; gap: 8px;
    padding: 7px 0; border-top: 1px solid var(--hair2);
  }
  .trim-row .tag {
    font-size: var(--fs-s); color: var(--m1); width: 26px; flex-shrink: 0;
  }
  .trim-row .tval {
    flex: 1; font-size: var(--fs-data); font-weight: 600;
    letter-spacing: var(--tr-data); color: var(--txt);
  }
  /* revealed hit areas: quiet fill, squared-off, never a bordered key */
  .nudge {
    width: 28px; height: 28px; border-radius: var(--r-md);
    background: var(--surface3); color: var(--m1);
    font-size: var(--fs-b); font-weight: 600;
    display: flex; align-items: center; justify-content: center;
    transition: background var(--t-state), color var(--t-state);
  }
  @media (hover: hover) {
    .nudge:hover { background: var(--hovrow); color: var(--txt); } }
  .nudge.set { width: auto; padding: 0 11px; }
  /* clearing is a destructive-ish step, so it stays quieter than its
     neighbours until pointed at, and disappears when there is nothing to
     clear rather than sitting there dead */
  .nudge.clear { background: transparent; color: var(--tert); }
  @media (hover: hover) {
    .nudge.clear:hover { background: var(--hovrow); color: var(--txt); } }
  .nudge.clear[hidden] { display: none; }

  .length-row {
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 7px 0; border-top: 1px solid var(--hair2);
    color: var(--m1); font-size: var(--fs-b);
  }
  .length-row b {
    color: var(--txt); font-size: var(--fs-data); font-weight: 600;
    letter-spacing: var(--tr-data);
  }

  /* output rows */
  .out-row {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; min-height: 34px;
  }
  .out-row > label { color: var(--m1); font-size: var(--fs-b); }
  /* Chevron drawn inline so there is no asset dependency. */
  select.field {
    background-color: var(--surface3); color: var(--txt);
    background-image:
      linear-gradient(45deg, transparent 50%, var(--m1) 50%),
      linear-gradient(135deg, var(--m1) 50%, transparent 50%);
    background-position: right 16px center, right 11px center;
    background-size: 5px 5px, 5px 5px;
    background-repeat: no-repeat;
    border: 0; border-radius: var(--r-cap);
    padding: 8px 32px 8px 14px; font: 400 var(--fs-b) var(--sans);
    min-width: 152px; appearance: none; cursor: pointer; outline: none;
  }
  select.field:focus, select.field:focus-visible {
    outline: 2px solid var(--ac); outline-offset: 0; }
  select.field option { background: var(--surface); color: var(--txt); }

  /* the switch: a segmented capsule that fills with the accent when on */
  .toggle {
    width: 40px; height: 24px; border-radius: var(--r-cap); flex-shrink: 0;
    background: var(--track); position: relative; cursor: pointer;
    transition: background var(--t-state);
  }
  .toggle .knob {
    position: absolute; top: 3px; left: 3px;
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--segon);
    box-shadow: 0 1px 3px var(--shc3);
    transition: left var(--t-state) var(--ease-settle);
  }
  .toggle.on { background: var(--ac-fill); }
  .toggle.on .knob { left: 19px; background: #ffffff; }

  .pathbox {
    flex: 1; min-width: 0; font-size: var(--fs-s); color: var(--m1);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    direction: rtl; text-align: left;
  }
  .folder-btn {
    width: 30px; height: 30px; border-radius: var(--r-md); flex-shrink: 0;
    background: var(--surface3); color: var(--m1); font-size: var(--fs-b);
    display: flex; align-items: center; justify-content: center;
    transition: background var(--t-state), color var(--t-state);
  }
  @media (hover: hover) {
    .folder-btn:hover { background: var(--hovrow); color: var(--txt); } }

  /* ---------- bitrate meter ---------- */
  /* a plain accent bar on the track — the same 4px capsule bar the family
     uses for every quantity. The old red→amber→green gradient is gone: red
     is errors only, and a gradient was decoration doing a number's job. */
  .bitrate { border-top: 1px solid var(--hair); padding: 16px 0 18px; }
  .br-top {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 9px;
  }
  .br-top .br-label { font-size: var(--fs-b); color: var(--m1); }
  .br-top .br-val {
    font-size: var(--fs-data); font-weight: 600;
    letter-spacing: var(--tr-data); color: var(--txt);
  }
  .br-meter {
    height: 4px; border-radius: var(--r-cap); overflow: hidden;
    background: var(--track);
  }
  .br-fill {
    height: 100%; width: 0%; border-radius: var(--r-cap);
    background: var(--barfill);
    transition: width var(--t-lift) var(--ease-runout);
  }
  .br-scale {
    display: flex; justify-content: space-between;
    font-size: var(--fs-s); color: var(--tert); margin-top: 6px;
  }
  /* what you will actually get, stated before you commit to it */
  .br-out {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--hair2);
    font-size: var(--fs-b); color: var(--m1);
  }
  .br-out b {
    font-size: var(--fs-data); font-weight: 600;
    letter-spacing: var(--tr-data); color: var(--txt);
  }

  /* ---------- the one filled element ---------- */
  /* an accent capsule with a white label. The fill is always darkened
     (--ac-fill) so the label clears 4.5:1, and hover deepens it. */
  .actions { margin-top: auto; padding-top: 18px;
    display: flex; flex-direction: column; gap: 9px; }
  /* the reason the primary action is unavailable. Amber, because it is a
     watch — the target is reachable again as soon as a number changes. */
  .gate-note {
    display: none; font-size: var(--fs-s); color: var(--warn);
    line-height: 1.4;
  }
  .gate-note.show { display: block; }
  .btn {
    width: 100%; display: inline-flex; align-items: center;
    justify-content: center; gap: .5rem;
    font: 600 var(--fs-ui) var(--sans);
    color: var(--on-fill); background: var(--ac-fill);
    border: none; border-radius: var(--r-cap); padding: 12px 18px;
    transition: background var(--t-state), transform var(--t-press) ease-out;
  }
  @media (hover: hover) { .btn:hover { background: var(--ac-fill-hot); } }
  .btn kbd { background: rgba(255,255,255,.2); color: var(--on-fill); }
  /* secondary: the quiet capsule — grey fill, no border, ink label */
  .btn.secondary { background: var(--surface3); color: var(--txt); }
  @media (hover: hover) {
    .btn.secondary:hover { background: var(--hovrow); } }

  /* ---------- messages ---------- */
  /* a hairline-fenced run, not a tinted box; the voice is in the ink */
  .toast {
    display: none; margin-top: 14px; padding-top: 12px;
    border-top: 1px solid var(--hair);
    font-size: var(--fs-b); color: var(--m1); white-space: pre-wrap;
    line-height: 1.5;
  }
  .toast.show { display: block; animation: rise var(--t-rise) var(--ease-settle); }
  .toast.ok  { color: var(--ok); }
  .toast.err { color: var(--err); }
  .toast-action {
    display: inline-flex; margin-top: 10px; padding: 8px 14px;
    background: var(--surface3); color: var(--txt);
    border-radius: var(--r-cap); font: 600 var(--fs-b) var(--sans);
    transition: background var(--t-state);
  }
  @media (hover: hover) {
    .toast-action:hover { background: var(--hovrow); } }

  /* a missing dependency is amber across the family — a watch, never an alert */
  .warn-banner {
    display: none; flex-shrink: 0; margin: 0 18px;
    padding: 11px 0; border-bottom: 1px solid var(--hair);
    color: var(--warn); font-size: var(--fs-b);
  }
  .warn-banner.show { display: block; animation: rise var(--t-rise) var(--ease-settle); }
  .warn-banner .dl-bar {
    display: none; height: 4px; border-radius: var(--r-cap);
    background: var(--track); margin-top: 9px; overflow: hidden;
  }
  .warn-banner .dl-bar.show { display: block; }
  .warn-banner .dl-fill {
    height: 100%; width: 0%; background: var(--warn);
    border-radius: var(--r-cap);
    transition: width var(--t-lift) var(--ease-runout);
  }

  /* ---------- fallbacks ---------- */
  /* No backdrop-filter (or the user asked for less transparency): the glass
     becomes an opaque surface. The rims go with it — a rim without refraction
     is just an outline. */
  @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
    .topbar, #themeflip {
      background: var(--glasssolid); box-shadow: 0 1px 0 var(--hair2);
    }
  }
  @media (prefers-reduced-transparency: reduce) {
    .topbar, #themeflip {
      background: var(--glasssolid);
      -webkit-backdrop-filter: none; backdrop-filter: none;
      box-shadow: 0 1px 0 var(--hair2);
    }
    .export-overlay {
      -webkit-backdrop-filter: none; backdrop-filter: none;
      background: rgba(0,0,0,.88);
    }
    body::before { background: none; }
  }
  /* color-mix carries the accent wash and every derived shade; without it the
     washes resolve to nothing, so the tinted states fall back to the fill. */
  @supports not (color: color-mix(in srgb, red 50%, blue)) {
    body::before { background: none; }
    .preset.active, .tl-selection { background: var(--surface3); }
  }

</style>
</head>
<body>

<div class="topbar">
  <span class="wordmark">clip<span class="wordmark-dot">.</span></span>
  <span class="sub">Trim and compress</span>
  <span class="mast-sep"></span>
  <div class="file-chip" id="fileChip" style="display:none;">
    <span class="nm" id="fileName">—</span>
    <span class="meta" id="fileMeta">—</span>
  </div>
  <div class="spacer"></div>
  <!-- hidden until a clip is loaded: it is the busiest thing in the masthead
       and every key in it is inert on the empty state -->
  <div class="hotkey-legend" id="hotkeyLegend" style="display:none;">
    <kbd>I</kbd><kbd>O</kbd><span>in/out</span>
    <kbd>[</kbd><kbd>]</kbd><span>nudge</span>
    <kbd>←</kbd><kbd>→</kbd><span>frame</span>
    <kbd>J</kbd><kbd>K</kbd><kbd>L</kbd><span>shuttle</span>
    <kbd>Space</kbd><span>play</span>
    <kbd>↵</kbd><span>export</span>
  </div>
  <button id="themeflip" type="button"></button>
</div>

<div class="warn-banner" id="ffwarn">
  <span id="ffwarnText">FFmpeg was not found. Trimming will not work until it
  is installed.</span>
  <div class="dl-bar" id="ffdlBar"><div class="dl-fill" id="ffdlFill"></div></div>
</div>

<div class="layout">
  <!-- ============ LEFT: player + timeline ============ -->
  <div class="main">

    <div class="dropzone" id="dropzone">
      <div class="dz-ico"><svg width="30" height="30" viewBox="0 0 24 24"
        fill="none" stroke="currentColor" stroke-width="1.6"
        stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="5"
        width="19" height="14" rx="3"/><path d="M2.5 9.5h19M7.5 5v4.5M16.5 5v4.5"
        /><path d="M10.4 12.6v3.8l3.4-1.9z" fill="currentColor"
        stroke="none"/></svg></div>
      <div class="dz-big">Drop an MP4 clip here</div>
      <div class="dz-small">or click to browse</div>
      <input type="file" id="fileInput" accept="video/mp4,video/*" hidden>
    </div>

    <div class="stage" id="stage" style="display:none;">
      <video id="video"></video>
      <div class="transport">
        <button class="play-btn" id="playBtn" aria-label="Play"></button>
        <div class="ts"><span class="cur" id="curTime">0:00.00</span>
          <span> / </span><span id="totTime">0:00.00</span></div>
        <div class="seekbar" id="seekbar">
          <div class="filled" id="seekFilled"></div>
          <div class="knob" id="seekKnob"></div>
        </div>
        <button class="vol-btn" id="muteBtn" aria-label="Mute"></button>
      </div>
      <div class="export-overlay" id="exportOverlay">
        <div class="eo-label" id="eoLabel">Encoding…</div>
        <div class="eo-pct" id="eoPct">0%</div>
        <div class="eo-bar"><div class="eo-fill" id="eoFill"></div></div>
        <div class="eo-sub" id="eoSub">This can take a moment</div>
        <button class="eo-cancel" id="eoCancel">Cancel export</button>
      </div>
    </div>

    <div class="controls-row" id="controlsRow" style="display:none;">
      <button class="pill" id="setIn">Set in <kbd>I</kbd></button>
      <button class="pill" id="setOut">Set out <kbd>O</kbd></button>
      <button class="pill" id="previewBtn" title="Play the selection">
        Preview</button>
    </div>

    <div class="timeline" id="timeline" style="display:none;">
      <div class="tl-ruler" id="tlRuler"></div>
      <div class="tl-track" id="tlTrack">
        <!-- the two veils outside the selection. Once the track carries a
             filmstrip a 12% accent wash is invisible over the imagery, so the
             selection is shown by dimming everything that is NOT selected. -->
        <div class="tl-dim" id="tlDimL" style="display:none;"></div>
        <div class="tl-dim" id="tlDimR" style="display:none;"></div>
        <div class="tl-selection" id="tlSelection" style="display:none;"></div>
        <div class="tl-marker in"  id="tlMarkIn"  style="display:none;">
          <span class="flag"></span></div>
        <div class="tl-marker out" id="tlMarkOut" style="display:none;">
          <span class="flag"></span></div>
        <div class="tl-playhead" id="tlPlayhead" style="left:0;">
          <span class="ph"></span></div>
      </div>
      <div class="tl-foot">
        <span>In <b id="tlInVal">—</b></span>
        <span>Selection <b id="tlLenVal">—</b></span>
        <span>Out <b id="tlOutVal">—</b></span>
      </div>
    </div>

  </div>

  <!-- ============ RIGHT: sidebar ============ -->
  <div class="sidebar" id="sidebar">

    <div class="sec">
      <div class="sec-title">Target size
        <span class="hint" id="sizeHint">~10 MB</span></div>
      <div class="preset-grid">
        <button class="preset active" data-size="10">
          <span class="p-name">Discord</span>
          <span class="p-sub">10 MB</span></button>
        <button class="preset" data-size="25">
          <span class="p-name">Messenger</span>
          <span class="p-sub">25 MB</span></button>
        <button class="preset" data-size="50">
          <span class="p-name">Nitro</span>
          <span class="p-sub">50 MB</span></button>
        <button class="preset" data-size="0" id="presetCustom">
          <span class="p-name">Custom</span>
          <span class="p-sub">—</span></button>
      </div>
      <div class="size-input-wrap">
        <input type="number" class="field" id="sizeInput"
               value="10" min="1" max="4096" step="1">
        <span class="unit">MB</span>
      </div>
    </div>

    <div class="sec">
      <div class="sec-title">Trim points</div>
      <div class="trim-row in">
        <span class="tag">In</span>
        <span class="tval" id="inVal">—</span>
        <button class="nudge" data-nudge="in" data-d="-1"
          aria-label="Nudge in point back one frame">−</button>
        <button class="nudge" data-nudge="in" data-d="1"
          aria-label="Nudge in point forward one frame">+</button>
        <button class="nudge set" id="setInSide">Set</button>
        <button class="nudge clear" data-clear="in" id="clearIn"
          title="Clear the in point"
          aria-label="Clear the in point">×</button>
      </div>
      <div class="trim-row out">
        <span class="tag">Out</span>
        <span class="tval" id="outVal">—</span>
        <button class="nudge" data-nudge="out" data-d="-1"
          aria-label="Nudge out point back one frame">−</button>
        <button class="nudge" data-nudge="out" data-d="1"
          aria-label="Nudge out point forward one frame">+</button>
        <button class="nudge set" id="setOutSide">Set</button>
        <button class="nudge clear" data-clear="out" id="clearOut"
          title="Clear the out point"
          aria-label="Clear the out point">×</button>
      </div>
      <div class="length-row">
        <span>Selection length</span><b id="lenVal">—</b>
      </div>
    </div>

    <div class="sec">
      <div class="sec-title">Output</div>
      <div class="out-row">
        <label for="resSelect">Resolution</label>
        <select class="field" id="resSelect">
          <option value="0">Keep original</option>
          <option value="1080">1080p</option>
          <option value="720">720p</option>
          <option value="480">480p</option>
        </select>
      </div>
      <div class="out-row">
        <label>Audio</label>
        <div style="display:flex;align-items:center;gap:10px;">
          <span id="audioLabel" style="font-size:var(--fs-b);">Keep audio</span>
          <div class="toggle on" id="audioToggle" role="switch"
            tabindex="0" aria-checked="true" aria-label="Keep audio">
            <div class="knob"></div></div>
        </div>
      </div>
      <div class="out-row">
        <label>Save to</label>
        <span class="pathbox" id="outFolder" title="">—</span>
        <button class="folder-btn" id="chooseFolder"
          title="Choose folder" aria-label="Choose folder"></button>
      </div>
    </div>

    <div class="bitrate">
      <div class="br-top">
        <span class="br-label">Estimated bitrate</span>
        <span class="br-val" id="brVal">— kbps</span>
      </div>
      <div class="br-meter"><div class="br-fill" id="brFill"
        style="width:0%;"></div></div>
      <div class="br-scale"><span>Lower quality</span>
        <span>Higher</span></div>
      <div class="br-out">
        <span>Output</span><b id="brOut">—</b>
      </div>
    </div>

    <div class="actions">
      <!-- Why the button is off, said out loud. The encoder already refuses an
           impossible target cleanly, but only after a two-pass wait; the same
           number is known here before anything is committed to. -->
      <div class="gate-note" id="gateNote"></div>
      <button class="btn" id="exportBtn" disabled>
        Trim and compress <kbd>↵</kbd>
      </button>

      <button class="btn secondary" id="resetBtn" style="display:none;">
        Reset and load a new clip
      </button>
    </div>

    <div class="toast" id="toast"></div>

  </div>
</div>

<script>
// ---------- the theme orb ----------
// Chrome only: the orb is one of the two glass elements in the app. Choosing
// the system's current value returns the page to auto-follow rather than
// pinning the opposite sheet forever.
(function () {
  var d = document.documentElement, b = document.getElementById('themeflip');
  var cur = d.dataset.theme || 'light';
  var SUN = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
    '<circle cx="12" cy="12" r="4.4"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5' +
    'M19.5 12H22M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M4.6 19.4l1.8-1.8' +
    'M17.6 6.4l1.8-1.8"/></svg>';
  var MOON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 ' +
    '9.8 9.8Z"/></svg>';
  var mq = matchMedia('(prefers-color-scheme: dark)');
  function sys() { return mq.matches ? 'dark' : 'light'; }
  function render() {
    b.innerHTML = (cur === 'dark') ? SUN : MOON;
    b.title = b.ariaLabel =
      (cur === 'dark') ? 'Switch to light mode' : 'Switch to dark mode';
  }
  b.addEventListener('click', function () {
    cur = (cur === 'dark') ? 'light' : 'dark';
    try {
      if (cur === sys()) localStorage.removeItem('clip_theme');
      else localStorage.setItem('clip_theme', cur);
    } catch (e) {}
    d.dataset.theme = cur;
    render();
  });
  mq.addEventListener('change', function (e) {
    var stored = null;
    try { stored = localStorage.getItem('clip_theme'); } catch (x) {}
    if (!stored) { cur = e.matches ? 'dark' : 'light';
      d.dataset.theme = cur; render(); }
  });
  render();
})();

// ---------- icons ----------
// Line icons at the family's weight; the two transport glyphs are filled
// because play/pause are shapes, not outlines.
const ICON = {
  play: '<svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor">' +
    '<path d="M8 5.14v13.72a1 1 0 0 0 1.54.84l10.3-6.86a1 1 0 0 0 0-1.68' +
    'L9.54 4.3A1 1 0 0 0 8 5.14Z"/></svg>',
  pause: '<svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor">' +
    '<rect x="6.5" y="5" width="4" height="14" rx="1.4"/>' +
    '<rect x="13.5" y="5" width="4" height="14" rx="1.4"/></svg>',
  vol: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4Z"/>' +
    '<path d="M15.8 9.4a3.6 3.6 0 0 1 0 5.2M18.4 6.8a7.3 7.3 0 0 1 0 10.4"/>' +
    '</svg>',
  mute: '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M4 9.5v5h3.5L12 18.5v-13L7.5 9.5H4Z"/>' +
    '<path d="M16 10l4.5 4.5M20.5 10L16 14.5"/></svg>',
  folder: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M3 7.5a2 2 0 0 1 2-2h3.8l2 2.4H19a2 2 ' +
    '0 0 1 2 2v7.6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7.5Z"/></svg>'
};

let FFMPEG_READY = __FFMPEG_READY__;
const TOKEN = "__TOKEN__";

// Append the per-session token to a request path so the server accepts it.
// Only used where a URL is unavoidable (the <video> src); fetch requests
// send the token as a header instead so it stays out of URLs and logs.
function api(path) {
  return path + (path.includes('?') ? '&' : '?') + 'token=' + TOKEN;
}
function jfetch(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'X-ClipTrimmer-Token': TOKEN},
    opts.headers || {});
  return fetch(path, opts);
}

// ---------- element refs ----------
const $ = id => document.getElementById(id);
const video = $('video');
let serverPath = null;
let inPoint = null, outPoint = null;
let pollTimer = null;
let exporting = false;
let currentJob = null;
let FRAME = 1 / 30;   // one frame; replaced by the real fps after upload
let srcHeight = 0;    // source pixel height; 0 = ffprobe could not tell us

// ---------- remembered settings ----------
// Target size, resolution and the audio switch survive a restart; trim points
// deliberately do not, since they belong to a clip rather than to the app.
const PREFS_KEY = 'clip_prefs';
function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; }
  catch (e) { return {}; }
}
function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      size: activeSize, res: $('resSelect').value, audio: keepAudio
    }));
  } catch (e) { /* private mode / quota — the app works without it */ }
}

// ---------- first-run FFmpeg download progress ----------
if (!FFMPEG_READY) {
  $('ffwarn').classList.add('show');
  const ffPoll = setInterval(() => {
    jfetch('/ffstatus').then(r => r.json()).then(j => {
      if (j.ready) {
        clearInterval(ffPoll);
        FFMPEG_READY = true;
        $('ffwarn').classList.remove('show');
        updateBitrate();   // re-runs the export gate now that ffmpeg is here
        return;
      }
      if (j.status === 'downloading' || j.status === 'extracting') {
        $('ffwarnText').textContent =
          j.message || 'Setting up FFmpeg (one-time, ~25 MB)…';
        $('ffdlBar').classList.add('show');
        $('ffdlFill').style.width = (j.progress || 0) + '%';
      } else if (j.status === 'error') {
        clearInterval(ffPoll);
        $('ffdlBar').classList.remove('show');
        $('ffwarnText').textContent = j.message ||
          'FFmpeg setup failed. Install it from ffmpeg.org and restart.';
      }
    }).catch(() => {});
  }, 700);
}

// ---------- helpers ----------
function fmt(t) {
  if (t == null || isNaN(t)) return '—';
  // round to centiseconds FIRST so 59.999 doesn't render as "0:60.00"
  const cs = Math.round(t * 100);
  const m = Math.floor(cs / 6000);
  const s = ((cs % 6000) / 100).toFixed(2).padStart(5, '0');
  return m + ':' + s;
}
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// ---------- file load ----------
const dz = $('dropzone');
dz.addEventListener('click', () => $('fileInput').click());
dz.addEventListener('dragover', e => { e.preventDefault();
  dz.classList.add('over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('over'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('over');
  if (e.dataTransfer.files.length) loadFile(e.dataTransfer.files[0]);
});
$('fileInput').addEventListener('change', e => {
  if (e.target.files.length) loadFile(e.target.files[0]);
});

function uploadWithProgress(file) {
  // XHR (not fetch) so we get upload progress events for big clips.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload?name=' + encodeURIComponent(file.name));
    xhr.setRequestHeader('X-ClipTrimmer-Token', TOKEN);
    xhr.upload.onprogress = e => {
      if (e.lengthComputable)
        showToast('Loading video… ' +
          Math.round(e.loaded / e.total * 100) + '%', '');
    };
    xhr.onload = () => {
      try {
        if (xhr.status !== 200) throw new Error('HTTP ' + xhr.status);
        resolve(JSON.parse(xhr.responseText));
      } catch (err) { reject(err); }
    };
    xhr.onerror = () => reject(new Error('network error'));
    xhr.send(file);
  });
}

function loadFile(file) {
  showToast('Loading video… 0%', '');
  uploadWithProgress(file)
    .then(info => {
      serverPath = info.path;
      FRAME = 1 / (info.fps || 30);
      srcHeight = info.height || 0;
      video.src = api('/video?path=' + encodeURIComponent(info.path));
      $('fileName').textContent = file.name;
      const mb = (file.size / 1048576).toFixed(1);
      $('fileChip').style.display = 'flex';
      $('dropzone').style.display = 'none';
      $('stage').style.display = 'block';
      $('controlsRow').style.display = 'flex';
      $('timeline').style.display = 'block';
      $('resetBtn').style.display = 'block';
      $('hotkeyLegend').style.display = 'flex';
      markResolutionOptions();
      loadFilmstrip(info.path);
      hideToast();
      video.addEventListener('loadedmetadata', () => {
        const size = (info.width && info.height)
          ? info.width + '×' + info.height : null;
        $('fileMeta').textContent = [
          fmt(video.duration), mb + ' MB',
          Math.round(info.fps || 30) + ' fps', size
        ].filter(Boolean).join('  ·  ');
        $('totTime').textContent = fmt(video.duration);
        buildTicks();
        refreshMarks();
      }, { once: true });
    })
    .catch(err => showToast('Could not load file: ' + err, 'err'));
}

// The filmstrip is best-effort: if the render failed or ffmpeg is missing the
// track just stays blank, which is exactly how it looked before.
function loadFilmstrip(path) {
  const url = api('/filmstrip?path=' + encodeURIComponent(path));
  const img = new Image();
  img.onload = () => {
    // A late strip for a clip that has since been replaced must not paint
    // over the new one.
    if (serverPath === path) $('tlTrack').style.backgroundImage =
      'url("' + url + '")';
  };
  img.src = url;
}

// Downscaling to a height the source never had is a no-op (the encoder's
// scale filter refuses to upscale), so say which options would do nothing
// rather than letting them look like choices.
function markResolutionOptions() {
  Array.from($('resSelect').options).forEach(o => {
    const h = parseInt(o.value, 10) || 0;
    const noop = h > 0 && srcHeight > 0 && h >= srcHeight;
    o.textContent = o.textContent.replace(/ \(no change\)$/, '') +
      (noop ? ' (no change)' : '');
  });
}

// ---------- playback ----------
$('playBtn').addEventListener('click', togglePlay);
video.addEventListener('click', togglePlay);
function togglePlay() {
  if (video.paused) video.play(); else video.pause();
}
function paintPlayBtn() {
  const b = $('playBtn');
  b.innerHTML = video.paused ? ICON.play : ICON.pause;
  b.ariaLabel = video.paused ? 'Play' : 'Pause';
}
video.addEventListener('play', paintPlayBtn);
video.addEventListener('pause', paintPlayBtn);
paintPlayBtn();
video.addEventListener('timeupdate', syncTime);

function syncTime() {
  if (!video.duration || scrubbing) return;   // don't fight an active drag
  setVisualPos(video.currentTime / video.duration);
}

// ---------- scrubbing (smooth + fast) ----------
// Strategy: move the playhead/labels instantly on every mouse move (cheap DOM
// work), but only actually seek the decoder at most once per animation frame,
// using video.fastSeek() which snaps to the nearest keyframe (near-instant) so
// dragging stays fluid on long-GOP game footage. On release we do one exact
// seek to land on the precise frame.
let scrubbing = false;
let dragEl = null;
let pendingTime = null;
let scrubRaf = 0;

function setVisualPos(frac) {
  const pct = (frac * 100) + '%';
  $('seekFilled').style.width = pct;
  $('seekKnob').style.left = pct;
  $('tlPlayhead').style.left = pct;
  if (video.duration) $('curTime').textContent = fmt(frac * video.duration);
}
function flushScrub() {
  scrubRaf = 0;
  if (pendingTime == null || !video.duration) return;
  if (typeof video.fastSeek === 'function') video.fastSeek(pendingTime);
  else video.currentTime = pendingTime;
}
function scrubTo(frac, commit) {
  frac = clamp(frac, 0, 1);
  setVisualPos(frac);
  if (!video.duration) return;
  pendingTime = frac * video.duration;
  if (commit) {
    if (scrubRaf) { cancelAnimationFrame(scrubRaf); scrubRaf = 0; }
    video.currentTime = pendingTime;     // exact final frame
    pendingTime = null;
  } else if (!scrubRaf) {
    scrubRaf = requestAnimationFrame(flushScrub);
  }
}
function fracFrom(el, e) {
  const r = el.getBoundingClientRect();
  return clamp((e.clientX - r.left) / r.width, 0, 1);
}

const seekbar = $('seekbar');
const tlTrack = $('tlTrack');
seekbar.addEventListener('mousedown', e => {
  dragEl = seekbar; scrubbing = true; scrubTo(fracFrom(seekbar, e), false);
});
tlTrack.addEventListener('mousedown', e => {
  dragEl = tlTrack; scrubbing = true; scrubTo(fracFrom(tlTrack, e), false);
});
window.addEventListener('mousemove', e => {
  if (draggingMark) { dragMarkTo(e); return; }
  if (scrubbing && dragEl) scrubTo(fracFrom(dragEl, e), false);
});
window.addEventListener('mouseup', e => {
  if (draggingMark) { dragMarkTo(e); endMarkDrag(); return; }
  if (scrubbing && dragEl) scrubTo(fracFrom(dragEl, e), true);
  scrubbing = false; dragEl = null;
});

// ---------- dragging the in / out markers ----------
// The markers own the drag from mousedown, and stopPropagation keeps the same
// press from also starting a track scrub underneath them — grabbing a handle
// must not fling the playhead to wherever you grabbed it.
let draggingMark = null;   // 'in' | 'out' | null

['in', 'out'].forEach(which => {
  $(which === 'in' ? 'tlMarkIn' : 'tlMarkOut')
    .addEventListener('mousedown', e => {
      if (!video.duration) return;
      e.preventDefault();
      e.stopPropagation();
      draggingMark = which;
      tlTrack.classList.add('dragging');
    });
});

function dragMarkTo(e) {
  if (!video.duration) return;
  let t = fracFrom(tlTrack, e) * video.duration;
  // The two points may not cross. One frame of clearance keeps the selection
  // from collapsing to zero length, which the encoder would reject anyway.
  if (draggingMark === 'in') {
    inPoint = clamp(t, 0, (outPoint != null ? outPoint - FRAME
                                            : video.duration));
  } else {
    outPoint = clamp(t, (inPoint != null ? inPoint + FRAME : 0),
                     video.duration);
  }
  refreshMarks();
}

function endMarkDrag() {
  // Land the playhead on the point just dropped, so the frame you chose is
  // the frame on screen.
  const t = draggingMark === 'in' ? inPoint : outPoint;
  draggingMark = null;
  tlTrack.classList.remove('dragging');
  if (t != null && video.duration) video.currentTime = t;
}

function paintMuteBtn() {
  const b = $('muteBtn');
  b.innerHTML = video.muted ? ICON.mute : ICON.vol;
  b.ariaLabel = video.muted ? 'Unmute' : 'Mute';
}
paintMuteBtn();
$('muteBtn').addEventListener('click', () => {
  video.muted = !video.muted;
  paintMuteBtn();
});

// ---------- timeline ruler (ticks + labels + gridlines) ----------
function buildTicks() {
  const d = video.duration || 0;
  const ruler = $('tlRuler'); ruler.innerHTML = '';
  const track = $('tlTrack');
  track.querySelectorAll('.tl-grid').forEach(g => g.remove());
  if (!d) return;

  // pick a "nice" major interval so we land on ~8 labels
  const raw = d / 8;
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900];
  let major = steps[steps.length - 1];
  for (const s of steps) { if (s >= raw) { major = s; break; } }
  const minor = major / 5;

  for (let t = 0; t <= d + 1e-6; t += minor) {
    const pct = (t / d) * 100;
    const isMaj = Math.abs(t / major - Math.round(t / major)) < 1e-6;
    const tk = document.createElement('div');
    tk.className = 'tk ' + (isMaj ? 'maj' : 'min');
    tk.style.left = pct + '%';
    ruler.appendChild(tk);
    if (isMaj) {
      const lb = document.createElement('div');
      lb.className = 'lb';
      lb.style.left = pct + '%';
      if (pct < 3) lb.style.transform = 'translateX(0)';
      else if (pct > 97) lb.style.transform = 'translateX(-100%)';
      lb.textContent = fmt(t).replace('.00', '');
      ruler.appendChild(lb);
      if (pct > 0.5 && pct < 99.5) {
        const g = document.createElement('div');
        g.className = 'tl-grid';
        g.style.left = pct + '%';
        track.insertBefore(g, track.firstChild);
      }
    }
  }
}

// ---------- in / out points ----------
$('setIn').addEventListener('click', setInPoint);
$('setInSide').addEventListener('click', setInPoint);
$('setOut').addEventListener('click', setOutPoint);
$('setOutSide').addEventListener('click', setOutPoint);

function setInPoint() {
  inPoint = video.currentTime;
  if (outPoint != null && inPoint >= outPoint) {
    outPoint = null;
    showToast('Out point cleared — it was before the new in point.', '');
  }
  refreshMarks();
}
function setOutPoint() {
  outPoint = video.currentTime;
  if (inPoint != null && outPoint <= inPoint) {
    inPoint = null;
    showToast('In point cleared — it was after the new out point.', '');
  }
  refreshMarks();
}
// One mover for both the sidebar buttons and the bracket keys, so they can
// never drift apart on clamping.
function nudgePoint(which, frames) {
  const d = frames * FRAME;
  if (which === 'in' && inPoint != null)
    inPoint = clamp(inPoint + d, 0, (outPoint ?? video.duration));
  if (which === 'out' && outPoint != null)
    outPoint = clamp(outPoint + d, (inPoint ?? 0), video.duration);
  refreshMarks();
}
document.querySelectorAll('[data-nudge]').forEach(b => {
  b.addEventListener('click', () =>
    nudgePoint(b.dataset.nudge, parseFloat(b.dataset.d)));   // data-d in frames
});

function refreshMarks() {
  $('inVal').textContent  = fmt(inPoint);
  $('outVal').textContent = fmt(outPoint);
  $('tlInVal').textContent  = fmt(inPoint);
  $('tlOutVal').textContent = fmt(outPoint);
  const len = (inPoint != null && outPoint != null)
    ? outPoint - inPoint : null;
  $('lenVal').textContent   = fmt(len);
  $('tlLenVal').textContent = fmt(len);

  const d = video.duration || 0;
  const markIn = $('tlMarkIn'), markOut = $('tlMarkOut'), sel = $('tlSelection');

  // Each marker shows independently the moment its point is set.
  if (inPoint != null && d) {
    markIn.style.display = 'block';
    markIn.style.left = (inPoint / d * 100) + '%';
  } else markIn.style.display = 'none';

  if (outPoint != null && d) {
    markOut.style.display = 'block';
    markOut.style.left = (outPoint / d * 100) + '%';
  } else markOut.style.display = 'none';

  // Shaded band appears once both ends exist.
  if (inPoint != null && outPoint != null && d) {
    sel.style.display = 'block';
    sel.style.left  = (inPoint / d * 100) + '%';
    sel.style.width = ((outPoint - inPoint) / d * 100) + '%';
  } else {
    sel.style.display = 'none';
  }

  // Veil whatever falls outside the selection. Each side is independent, so a
  // single point set on its own already shows what it is excluding.
  const dimL = $('tlDimL'), dimR = $('tlDimR');
  if (inPoint != null && d && inPoint > 0) {
    dimL.style.display = 'block';
    dimL.style.left = '0';
    dimL.style.width = (inPoint / d * 100) + '%';
  } else dimL.style.display = 'none';

  if (outPoint != null && d && outPoint < d) {
    dimR.style.display = 'block';
    dimR.style.left = (outPoint / d * 100) + '%';
    dimR.style.width = ((d - outPoint) / d * 100) + '%';
  } else dimR.style.display = 'none';

  // Nothing to clear means no clear button.
  $('clearIn').hidden  = inPoint == null;
  $('clearOut').hidden = outPoint == null;

  updateBitrate();
}

// ---------- clearing a trim point ----------
// Before this a point could only ever be moved, never removed: getting back to
// "whole clip" meant reloading the file.
document.querySelectorAll('[data-clear]').forEach(b => {
  b.addEventListener('click', () => {
    if (b.dataset.clear === 'in') inPoint = null; else outPoint = null;
    previewLoop = false;
    refreshMarks();
  });
});

// ---------- target size presets ----------
let activeSize = 10;
document.querySelectorAll('.preset').forEach(p => {
  p.addEventListener('click', () => {
    document.querySelectorAll('.preset').forEach(x =>
      x.classList.remove('active'));
    p.classList.add('active');
    const s = parseInt(p.dataset.size, 10);
    if (s > 0) { activeSize = s; $('sizeInput').value = s; }
    else { $('sizeInput').focus(); }
    updateBitrate();
    savePrefs();
  });
});
$('sizeInput').addEventListener('input', () => {
  activeSize = parseFloat($('sizeInput').value) || 0;
  // typing a custom number selects the Custom preset unless it matches one.
  // The match is read off data-size, so adding or renaming a preset tile above
  // needs no change here.
  document.querySelectorAll('.preset').forEach(x =>
    x.classList.remove('active'));
  const known = document.querySelector(
    `.preset[data-size="${activeSize}"]`);
  (known || $('presetCustom')).classList.add('active');
  updateBitrate();
  savePrefs();
});
$('resSelect').addEventListener('change', savePrefs);

// ---------- audio toggle ----------
let keepAudio = true;
function toggleAudio() {
  setAudio(!keepAudio);
  savePrefs();
}
function setAudio(on) {
  keepAudio = !!on;
  const t = $('audioToggle');
  t.classList.toggle('on', keepAudio);
  t.setAttribute('aria-checked', keepAudio ? 'true' : 'false');
  $('audioLabel').textContent = keepAudio ? 'Keep audio' : 'No audio';
  updateBitrate();
}
$('audioToggle').addEventListener('click', toggleAudio);
$('audioToggle').addEventListener('keydown', e => {
  if (e.code === 'Space' || e.code === 'Enter') { e.preventDefault(); toggleAudio(); }
});

// Restore last session's choices. Runs after the controls above are defined
// so it can reuse their own setters rather than half-updating the DOM.
(function applyPrefs() {
  const p = loadPrefs();
  if (typeof p.size === 'number' && p.size >= 1 && p.size <= 4096) {
    activeSize = p.size;
    $('sizeInput').value = p.size;
    document.querySelectorAll('.preset').forEach(x =>
      x.classList.remove('active'));
    (document.querySelector(`.preset[data-size="${p.size}"]`) ||
      $('presetCustom')).classList.add('active');
  }
  if (typeof p.res === 'string' &&
      Array.from($('resSelect').options).some(o => o.value === p.res)) {
    $('resSelect').value = p.res;
  }
  setAudio(p.audio === undefined ? true : p.audio);
})();

// ---------- bitrate estimate ----------
function updateBitrate() {
  $('sizeHint').textContent = '~' + (activeSize || 0) + ' MB';
  const len = (inPoint != null && outPoint != null)
    ? (outPoint - inPoint)
    : (video.duration || 0);
  if (!len || !activeSize) {
    $('brVal').textContent = '— kbps';
    $('brFill').style.width = '0%';
    $('brOut').textContent = '—';
    refreshGate(null);
    return;
  }
  // mirrors the backend math
  const totalKbits = activeSize * 8 * 1024;
  let audioKbps = 0;
  if (keepAudio) audioKbps = activeSize <= 4 ? 96
    : activeSize <= 8 ? 128 : 160;
  let videoKbps = Math.floor((totalKbits / len - audioKbps) * 0.97);
  const MAX_KBPS = 40000;   // mirrors the backend cap
  const capped = videoKbps > MAX_KBPS;
  if (capped) videoKbps = MAX_KBPS;
  $('brVal').textContent = videoKbps > 0
    ? videoKbps.toLocaleString() + (capped ? ' kbps (max)' : ' kbps')
    : 'too small';
  // map bitrate onto the meter: 0..8000 kbps -> 0..100% of the bar
  const q = clamp(videoKbps / 8000, 0, 1);
  $('brFill').style.width = (q * 100) + '%';

  // What the export will actually produce. When the bitrate is capped the
  // file lands under the target, so the size is recomputed from the real
  // bitrate rather than repeating the number that was asked for.
  const outMb = capped
    ? ((videoKbps + audioKbps) * len) / (8 * 1024)
    : activeSize;
  $('brOut').textContent = outMb.toFixed(outMb < 10 ? 1 : 0) + ' MB  ·  ' +
    fmt(len);

  refreshGate(videoKbps);
}

// ---------- the export gate ----------
// MIN_KBPS mirrors the encoder's own floor. It refuses an impossible target
// cleanly, but only after a full two-pass wait — and the number is knowable
// here the moment a size or a trim point changes, so it is said up front.
const MIN_KBPS = 50;
function refreshGate(videoKbps) {
  const note = $('gateNote');
  let reason = '';
  if (!FFMPEG_READY) reason = 'Waiting for FFmpeg.';
  else if (!serverPath) reason = '';
  else if (videoKbps != null && videoKbps < MIN_KBPS)
    reason = 'This target is too small for a selection this long. ' +
      'Raise the size, or trim tighter.';
  note.textContent = reason;
  note.classList.toggle('show', !!reason);
  $('exportBtn').disabled = exporting || !FFMPEG_READY || !serverPath ||
    !!reason;
}

// ---------- folder picker ----------
function setFolder(p) {
  $('outFolder').textContent = p;
  $('outFolder').title = p;
}
jfetch('/outdir').then(r => r.json()).then(j => setFolder(j.folder))
  .catch(() => {});
$('chooseFolder').innerHTML = ICON.folder;
$('chooseFolder').addEventListener('click', () => {
  const b = $('chooseFolder');
  b.disabled = true; b.textContent = '…';
  jfetch('/pickfolder').then(r => r.json())
    .then(j => { if (!j.busy) setFolder(j.folder); })
    .catch(() => {})
    .finally(() => { b.disabled = false; b.innerHTML = ICON.folder; });
});

// ---------- export ----------
$('exportBtn').addEventListener('click', startExport);
function startExport() {
  if (exporting || !FFMPEG_READY || !serverPath) return;
  previewLoop = false;
  const start = inPoint != null ? inPoint : 0;
  const end   = outPoint != null ? outPoint : video.duration;
  if (!end || isNaN(end)) {
    showToast('Wait for the video to finish loading first.', 'err'); return;
  }
  if (end - start < 0.1) {
    showToast('Selection is too short — it must be at least 0.1 seconds. ' +
      'Adjust your in and out points.', 'err');
    return;
  }
  if (!activeSize || activeSize < 1 || activeSize > 4096) {
    showToast('Enter a target size between 1 and 4096 MB.', 'err'); return;
  }
  exporting = true;
  $('exportBtn').disabled = true;
  hideToast();
  showOverlay(0, 'Starting…');

  jfetch('/export', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ path: serverPath, start, end,
      target_mb: activeSize, resolution:
        parseInt($('resSelect').value, 10) || 0,
      keep_audio: keepAudio })
  })
  .then(r => r.json())
  .then(j => { if (j.job) { currentJob = j.job; poll(j.job); }
    else throw new Error(j.error || 'failed'); })
  .catch(err => { endExport(); showToast('Export failed: ' + err, 'err'); });
}

// Cancel a running export from the overlay.
$('eoCancel').addEventListener('click', () => {
  if (!currentJob) return;
  $('eoCancel').disabled = true;
  jfetch('/cancel?job=' + currentJob, { method: 'POST' }).catch(() => {});
});

// Warn before closing the window while an export is running (the server
// also refuses to die mid-encode, but this stops the accidental close).
window.addEventListener('beforeunload', e => {
  if (exporting) { e.preventDefault(); e.returnValue = ''; }
});

function poll(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    jfetch('/status?job=' + jobId).then(r => r.json()).then(j => {
      if (typeof j.progress === 'number')
        showOverlay(j.progress, j.message || 'Encoding…');
      if (j.status === 'done') {
        clearInterval(pollTimer);
        showOverlay(100, 'Done');
        setTimeout(() => { endExport();
          showToast(j.message, 'ok', jobId); }, 600);
      } else if (j.status === 'error') {
        clearInterval(pollTimer);
        endExport();
        showToast(j.message, 'err');
      } else if (j.status === 'cancelled') {
        clearInterval(pollTimer);
        endExport();
        showToast('Export cancelled.', '');
      }
    }).catch(() => { /* transient network blip - keep polling */ });
  }, 500);
}

function showOverlay(pct, label) {
  $('exportOverlay').classList.add('show');
  const p = clamp(pct || 0, 0, 100);
  $('eoFill').style.width = p + '%';
  $('eoPct').textContent = Math.round(p) + '%';
  // strip a trailing percentage the backend may add to the label
  $('eoLabel').textContent = (label || 'Encoding…').split(' - ')[0];
}
function endExport() {
  exporting = false;
  currentJob = null;
  $('eoCancel').disabled = false;
  $('exportOverlay').classList.remove('show');
  updateBitrate();   // re-runs the gate, which owns the button's state
}

// ---------- toast ----------
// showToast(msg, kind, doneJobId): when doneJobId is given, the toast gets
// an "Open folder" action that reveals the exported file in Explorer.
function showToast(msg, kind, doneJobId) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (kind ? ' ' + kind : '');
  if (doneJobId) {
    const btn = document.createElement('button');
    btn.className = 'toast-action';
    btn.textContent = 'Open folder';
    btn.addEventListener('click', () => {
      jfetch('/openfolder?job=' + doneJobId, { method: 'POST' })
        .catch(() => {});
    });
    t.appendChild(btn);
  }
}
function hideToast() { $('toast').className = 'toast'; }

// ---------- reset / load a new clip ----------
$('resetBtn').addEventListener('click', resetApp);
function resetApp() {
  if (exporting) {
    showToast('An export is running — cancel it before resetting.', 'err');
    return;
  }
  // Trim points can't be recovered after a reset, so confirm first.
  if ((inPoint != null || outPoint != null) &&
      !confirm('Load a new clip? Your current trim points will be lost.'))
    return;
  try { video.pause(); } catch (e) {}
  video.removeAttribute('src');
  video.load();
  serverPath = null;
  inPoint = null;
  outPoint = null;
  FRAME = 1 / 30;
  srcHeight = 0;
  previewStop = null;
  previewLoop = false;
  $('fileInput').value = '';
  $('fileChip').style.display = 'none';
  $('stage').style.display = 'none';
  $('controlsRow').style.display = 'none';
  $('timeline').style.display = 'none';
  $('resetBtn').style.display = 'none';
  $('hotkeyLegend').style.display = 'none';
  $('dropzone').style.display = 'flex';
  $('tlTrack').style.backgroundImage = '';
  setPreviewBtn();
  hideToast();
  refreshMarks();
}

// ---------- selection preview (loops IN -> OUT until stopped) ----------
// Looping is the point: dialling in a cut means watching the same two seconds
// over and over while nudging an end, and re-arming the preview by hand after
// every nudge is most of that work.
let previewStop = null;   // out-time to loop at, or null
let previewLoop = false;
let previewStart = 0;
$('previewBtn').addEventListener('click', playPreview);
function playPreview() {
  if (!video.duration) return;
  if (previewLoop) { previewLoop = false; video.pause(); return; }
  const start = inPoint != null ? inPoint : 0;
  const end = outPoint != null ? outPoint : video.duration;
  if (end - start < 0.05) {
    showToast('Set in and out points to preview the selection.', '');
    return;
  }
  previewStart = start;
  previewStop = end;
  previewLoop = true;
  video.currentTime = start;
  video.play();
  setPreviewBtn();
}
function setPreviewBtn() {
  $('previewBtn').textContent = previewLoop ? 'Stop preview' : 'Preview';
}
video.addEventListener('timeupdate', () => {
  if (previewStop == null || video.currentTime < previewStop) return;
  if (previewLoop) {
    // Loop from the in point rather than pausing. The bounds are re-read
    // every lap, so nudging an end mid-preview takes effect on the next one.
    previewStart = inPoint != null ? inPoint : 0;
    previewStop = outPoint != null ? outPoint : video.duration;
    video.currentTime = previewStart;
  } else {
    video.pause();
    video.currentTime = previewStop;
    previewStop = null;
  }
});
// any manual pause ends preview mode
video.addEventListener('pause', () => {
  previewStop = null;
  previewLoop = false;
  setPreviewBtn();
});

// ---------- keyboard shortcuts ----------
// Space play/pause · I/O set in/out · [ ] nudge in/out a frame (Shift = back)
// · arrows frame-step the playhead (Shift = 1s) · J/K/L shuttle · Enter export
let shuttleRate = 1;
function frameStep(dir, big) {
  if (!video.duration) return;
  video.pause();
  const step = big ? 1 : FRAME;
  video.currentTime = clamp(video.currentTime + dir * step, 0, video.duration);
}
document.addEventListener('keydown', e => {
  const t = e.target.tagName;
  if (t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA') return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const hasVideo = !!video.src;
  switch (e.code) {
    case 'Space':
      e.preventDefault();
      if (hasVideo) { shuttleRate = 1; video.playbackRate = 1; togglePlay(); }
      break;
    case 'KeyI':
      if (hasVideo) { e.preventDefault(); setInPoint(); }
      break;
    case 'KeyO':
      if (hasVideo) { e.preventDefault(); setOutPoint(); }
      break;
    // Shift is already "the other direction" on the arrow keys, so it means
    // the same here rather than inventing a second pair of keys.
    case 'BracketLeft':
      if (hasVideo && inPoint != null) {
        e.preventDefault(); nudgePoint('in', e.shiftKey ? -1 : 1); }
      break;
    case 'BracketRight':
      if (hasVideo && outPoint != null) {
        e.preventDefault(); nudgePoint('out', e.shiftKey ? -1 : 1); }
      break;
    case 'ArrowLeft':
      if (hasVideo) { e.preventDefault(); frameStep(-1, e.shiftKey); }
      break;
    case 'ArrowRight':
      if (hasVideo) { e.preventDefault(); frameStep(1, e.shiftKey); }
      break;
    case 'KeyJ':   // step/jump back; repeated presses jump further back
      if (hasVideo) { e.preventDefault();
        shuttleRate = 1; video.playbackRate = 1;
        video.currentTime = clamp(video.currentTime - 1, 0, video.duration); }
      break;
    case 'KeyK':   // pause + reset speed
      if (hasVideo) { e.preventDefault();
        shuttleRate = 1; video.playbackRate = 1; video.pause(); }
      break;
    case 'KeyL':   // play; repeated presses speed up 1x -> 2x -> 4x
      if (hasVideo) { e.preventDefault();
        if (video.paused) { shuttleRate = 1; }
        else { shuttleRate = Math.min(shuttleRate * 2, 4); }
        video.playbackRate = shuttleRate;
        video.play(); }
      break;
    case 'Enter':
      if (hasVideo && !exporting && !$('exportBtn').disabled) {
        e.preventDefault(); startExport();
      }
      break;
  }
});

// ---------- keep-alive heartbeat ----------
// Tell the local app we're still open. We also report whether the window is
// currently hidden/minimized, so the watchdog won't quit during the time
// background timers are throttled by the browser. A real close fires pagehide
// -> /bye for an instant, reliable shutdown.
function ping() {
  fetch(api('/alive?hidden=' + (document.hidden ? 1 : 0))).catch(() => {});
}
ping();
setInterval(ping, 2000);
document.addEventListener('visibilitychange', ping);  // report show/hide promptly
window.addEventListener('pagehide', () => {
  try { navigator.sendBeacon(api('/bye')); } catch (e) {}
});
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default logging

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client went away mid-response; nothing to do

    def _authed(self, parsed):
        """Every endpoint except the entry page requires the session token,
        via header (preferred - keeps it out of URLs) or query string."""
        if self.headers.get("X-ClipTrimmer-Token") == TOKEN:
            return True
        return parse_qs(parsed.query).get("token", [""])[0] == TOKEN

    def _host_ok(self):
        """
        Only accept requests addressed to localhost by name/IP. This blocks
        DNS-rebinding, where a malicious site points its own hostname at
        127.0.0.1 to talk to us - its requests carry that foreign Host header.
        """
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def do_GET(self):
        if not self._host_ok():
            self._send(403, json.dumps({"error": "bad host"}))
            return
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._send(200, build_page(), "text/html; charset=utf-8")
            return

        if not self._authed(parsed):
            self._send(403, json.dumps({"error": "forbidden"}))
            return

        if parsed.path == "/video":
            qs = parse_qs(parsed.query)
            path = qs.get("path", [""])[0]
            self._serve_video(path)
            return

        if parsed.path == "/filmstrip":
            qs = parse_qs(parsed.query)
            self._serve_filmstrip(qs.get("path", [""])[0])
            return

        if parsed.path == "/status":
            qs = parse_qs(parsed.query)
            job_id = qs.get("job", [""])[0]
            job = JOBS.get(job_id)
            if not isinstance(job, dict):
                job = {"status": "error", "message": "Unknown job"}
            self._send(200, json.dumps(job))
            return

        if parsed.path == "/ffstatus":
            # First-run FFmpeg download progress for the UI banner.
            self._send(200, json.dumps({
                "ready": bool(FFMPEG and FFPROBE), **FF_STATE}))
            return

        if parsed.path == "/outdir":
            # Report the current output folder so the UI can show it on load.
            self._send(200, json.dumps({"folder": OUTPUT_DIR[0]}))
            return

        if parsed.path == "/pickfolder":
            # Open a native folder dialog (blocks until the user picks/cancels).
            # A second request while one is open returns busy immediately.
            chosen, busy = pick_output_folder()
            if chosen:
                OUTPUT_DIR[0] = chosen
            self._send(200, json.dumps({"folder": OUTPUT_DIR[0],
                                        "busy": busy}))
            return

        if parsed.path == "/alive":
            # Heartbeat from the UI window. While these keep arriving the app
            # stays open; when they stop (window closed) the watchdog quits.
            # 'hidden' tells the watchdog the window is minimized so it won't
            # quit while the browser throttles background timers.
            qs = parse_qs(parsed.query)
            LIFE["last_ping"] = time.time()
            LIFE["got_ping"] = True
            LIFE["hidden"] = qs.get("hidden", ["0"])[0] == "1"
            self._send(200, json.dumps({"ok": True}))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not self._host_ok():
            self._send(403, json.dumps({"error": "bad host"}))
            return
        parsed = urlparse(self.path)

        if not self._authed(parsed):
            self._send(403, json.dumps({"error": "forbidden"}))
            return

        if parsed.path == "/bye":
            # Sent via navigator.sendBeacon when the window is closing -> quit now.
            LIFE["bye"] = True
            self._send(200, json.dumps({"ok": True}))
            return

        if parsed.path == "/upload":
            self._handle_upload(parsed)
            return

        if parsed.path == "/export":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"")
            except (ValueError, json.JSONDecodeError):
                self._send(400, json.dumps(
                    {"error": "Request body must be valid JSON."}))
                return
            self._handle_export(data)
            return

        if parsed.path == "/cancel":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            ok = cancel_export(job_id)
            self._send(200 if ok else 404,
                       json.dumps({"ok": ok} if ok
                                  else {"error": "No running job to cancel."}))
            return

        if parsed.path == "/openfolder":
            # Reveal the finished export in Explorer. The path comes from our
            # own job record, never from the client.
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            job = JOBS.get(job_id)
            out = job.get("output") if isinstance(job, dict) else None
            if out and os.path.isfile(out) and os.name == "nt":
                try:
                    subprocess.Popen(["explorer", "/select,", out])
                    self._send(200, json.dumps({"ok": True}))
                    return
                except Exception:
                    pass
            self._send(404, json.dumps({"error": "Output file not found."}))
            return

        self._send(404, json.dumps({"error": "not found"}))

    # -- serve video with HTTP range support so scrubbing works --
    def _serve_video(self, path):
        # Only ever serve files from inside the app's own temp dir (the uploaded
        # clip). This prevents the endpoint from being used to read arbitrary
        # files off the machine.
        real = path_inside(path, APP_DIR)
        if not real or not os.path.isfile(real):
            self._send(403, json.dumps({"error": "forbidden"}))
            return
        path = real
        size = os.path.getsize(path)
        range_hdr = self.headers.get("Range")
        try:
            rng = parse_range_header(range_hdr, size)
        except ValueError:
            # Syntactically valid but unsatisfiable range -> proper 416.
            try:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except OSError:
                pass
            return
        partial = rng is not None
        start, end = rng if partial else (0, size - 1)
        length = end - start + 1

        try:
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", guess_video_mime(path))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
        except OSError:
            return

        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            # Larger chunks => fewer Python-side loop iterations and syscalls,
            # which noticeably improves streaming/seek throughput from this
            # lightweight server. The browser aborts the connection when it has
            # buffered enough, so over-reading isn't a concern.
            chunk = 1024 * 1024
            while remaining > 0:
                buf = f.read(min(chunk, remaining))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(buf)

    # -- receive an uploaded clip, store a temp copy, return its path --
    def _handle_upload(self, parsed):
        # The client streams the raw file as the request body and passes the
        # name in the query string, so we copy it to disk in chunks instead of
        # buffering the whole (potentially huge) clip in memory.
        raw_name = parse_qs(parsed.query).get("name", ["clip.mp4"])[0]
        safe_name = os.path.basename(unquote(raw_name)) or "clip.mp4"
        length = int(self.headers.get("Content-Length", 0))

        # Drop previous sources and their filmstrips so the temp dir doesn't
        # accumulate clips.
        for pattern in ("source_*", "strip_*"):
            for old in APP_DIR.glob(pattern):
                try:
                    old.unlink()
                except OSError:
                    pass

        temp_path = APP_DIR / ("source_" + safe_name)
        remaining = length
        try:
            with open(temp_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception as e:
            self._send(500, json.dumps({"error": f"upload failed: {e}"}))
            return

        # remember the original name so we can name the output sensibly
        JOBS["_last_name"] = safe_name
        # Detect the real frame rate so frame-step nudges are accurate for
        # 60/120/144 fps captures, and the pixel size so the Resolution menu
        # can say which options would actually do anything. Falls back to
        # 30 fps / unknown size if ffprobe is missing.
        fps, width, height = probe_media(temp_path)
        # Start the filmstrip now rather than on first request: it takes a few
        # seconds on a long clip, and this way it is usually on disk by the
        # time the timeline asks for it.
        threading.Thread(target=build_filmstrip, args=(temp_path,),
                         daemon=True).start()
        self._send(200, json.dumps({"path": str(temp_path), "fps": fps,
                                    "width": width, "height": height}))

    # -- serve the timeline filmstrip, building it on first request --
    def _serve_filmstrip(self, path):
        """
        The strip is normally already being rendered by the upload handler, so
        this usually just reads the cache; if not, build_filmstrip blocks on
        its lock until that render finishes. A 404 here is not an error worth
        surfacing - the timeline simply stays blank.
        """
        real = path_inside(path, APP_DIR)
        if not real or not os.path.isfile(real):
            self._send(403, json.dumps({"error": "forbidden"}))
            return
        strip = build_filmstrip(real)
        try:
            body = strip.read_bytes() if strip else b""
        except OSError:
            body = b""
        if not body:
            self._send(404, json.dumps({"error": "no filmstrip"}))
            return
        self._send(200, body, "image/jpeg")

    # -- kick off a background export job --
    def _handle_export(self, data):
        if not (FFMPEG and FFPROBE):
            self._send(503, json.dumps({"error": "FFmpeg not available"}))
            return
        params, err = validate_export_request(data)
        if err:
            self._send(400, json.dumps({"error": err}))
            return
        # Only transcode files from our own temp dir (the uploaded clip), never
        # an arbitrary path supplied in the request.
        src = data.get("path", "") if isinstance(data.get("path"), str) else ""
        real_src = path_inside(src, APP_DIR) or ""
        inside = bool(real_src)
        if not inside or not os.path.isfile(real_src):
            self._send(400, json.dumps({"error": "source file missing"}))
            return
        src = real_src

        start = params["start"]
        end = params["end"]
        target_mb = params["target_mb"]
        resolution = params["resolution"]
        keep_audio = params["keep_audio"]

        # Build an output path inside the user-chosen output folder. Falls back
        # to the default if that folder somehow no longer exists.
        base_name = JOBS.get("_last_name", "clip.mp4")
        stem = Path(base_name).stem
        out_dir = Path(OUTPUT_DIR[0])
        if not out_dir.is_dir():
            out_dir = Path.home()
        output_path = str(out_dir / f"{stem}_trimmed.mp4")

        # avoid overwriting
        n = 1
        while os.path.exists(output_path):
            output_path = str(out_dir / f"{stem}_trimmed_{n}.mp4")
            n += 1

        JOB_COUNTER[0] += 1
        job_id = str(JOB_COUNTER[0])
        prune_jobs(JOBS)   # keep the finished-job history bounded
        JOBS[job_id] = {"id": job_id, "status": "queued",
                        "message": "Queued...", "progress": 0.0}

        threading.Thread(
            target=run_export,
            args=(job_id, src, start, end, target_mb, output_path,
                  resolution, keep_audio),
            daemon=True
        ).start()

        self._send(200, json.dumps({"job": job_id}))


# ----------------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------------
def _start_server():
    """
    Start the local HTTP server in a background thread and return it.
    Binds directly, retrying on successive ports (no probe-then-bind race).
    """
    global PORT
    server = None
    last_err = None
    for offset in range(40):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 8731 + offset), Handler)
            PORT = 8731 + offset
            break
        except OSError as e:
            last_err = e
    if server is None:
        # last resort: let the OS choose any free port
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            PORT = server.server_address[1]
        except OSError:
            raise last_err
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def _find_chromium():
    """
    Locate an installed Chromium-family browser (Edge, Chrome, or Brave) so we
    can open the UI as a standalone app window. Returns a path or None.
    Edge ships with Windows 10/11, so this almost always succeeds there.
    """
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    candidates = [
        Path(pf) / "Microsoft/Edge/Application/msedge.exe",
        Path(pfx86) / "Microsoft/Edge/Application/msedge.exe",
        Path(pf) / "Google/Chrome/Application/chrome.exe",
        Path(pfx86) / "Google/Chrome/Application/chrome.exe",
        Path(local) / "Google/Chrome/Application/chrome.exe",
        Path(pf) / "BraveSoftware/Brave-Browser/Application/brave.exe",
        Path(pfx86) / "BraveSoftware/Brave-Browser/Application/brave.exe",
        Path(local) / "BraveSoftware/Brave-Browser/Application/brave.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    # Last resort: anything named like a chromium browser on PATH.
    for name in ("msedge", "chrome", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _open_app_window(url):
    """
    Open the UI as a chromeless standalone window using a Chromium browser in
    --app mode, with its own throwaway profile so it spawns a distinct window
    we don't share with the user's normal browsing. Returns True if launched.
    Falls back to the default browser (still quits via the heartbeat) if no
    Chromium browser is found.
    """
    browser = _find_chromium()
    if not browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        return False
    # A dedicated, persistent profile keeps this window separate from the user's
    # normal browsing AND remembers dismissed first-run notices, so banners like
    # Brave's analytics notice don't reappear on every launch.
    profile = APP_DIR / "win_profile"
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=1280,820",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    try:
        subprocess.Popen(
            args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return True
    except Exception:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        return False


def _watchdog(server):
    """
    Quit the whole app when its window is closed. The UI pings /alive every 2s;
    a real close fires /bye for an instant shutdown, and if pings simply stop we
    quit after a timeout. The timeout is generous while the window is minimized,
    because browsers throttle background timers then - otherwise minimizing the
    app for a few seconds could make it quit on its own.
    """
    VISIBLE_TIMEOUT = 12      # window visible but silent -> it was closed
    HIDDEN_TIMEOUT = 1800     # minimized: only quit after a long safety cap
    STARTUP_TIMEOUT = 120     # window never finished loading
    while True:
        time.sleep(1.5)
        now = time.time()
        if LIFE["bye"]:
            break
        if not LIFE["got_ping"]:
            if now - LIFE["start"] > STARTUP_TIMEOUT:
                break           # window never opened; don't linger
            continue
        limit = HIDDEN_TIMEOUT if LIFE["hidden"] else VISIBLE_TIMEOUT
        if now - LIFE["last_ping"] > limit:
            break
    # Never kill a running export: if an encode is still in flight when the
    # window goes away, let it finish (with a generous safety cap) so the
    # user doesn't get a truncated _trimmed.mp4 and stale passlog files.
    EXPORT_GRACE = 30 * 60
    deadline = time.time() + EXPORT_GRACE
    while export_active(JOBS) and time.time() < deadline:
        print("Window closed but an export is still running - waiting...")
        time.sleep(2)
    try:
        server.shutdown()
    except Exception:
        pass
    os._exit(0)


def _ffmpeg_setup_thread():
    """First-run FFmpeg download, run in the background AFTER the server is
    up so the UI can show live progress instead of a silent blocked launch."""
    global FFMPEG, FFPROBE
    if FFMPEG and FFPROBE:
        FF_STATE.update(status="ready", progress=100)
        return
    if os.name == "nt":
        if download_ffmpeg():
            FFMPEG, FFPROBE = find_ffmpeg()
    if not (FFMPEG and FFPROBE):
        if FF_STATE["status"] != "error":
            FF_STATE.update(status="error",
                            message="FFmpeg not found. Install it from "
                                    "https://ffmpeg.org and restart.")
        print("WARNING: FFmpeg not found. Trimming/compression will not work.")
        print("Install FFmpeg from https://ffmpeg.org and restart.")


def main():
    # Start the server and open the window immediately; the (possibly slow)
    # first-run FFmpeg download happens in the background with UI progress.
    server = _start_server()
    threading.Thread(target=_ffmpeg_setup_thread, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 52)
    print("  Clip Trimmer is running.")
    print(f"  Local server: {url}")
    print("=" * 52)

    # Watchdog quits the app when its window closes (see _watchdog).
    LIFE["start"] = time.time()
    LIFE["last_ping"] = time.time()
    threading.Thread(target=_watchdog, args=(server,), daemon=True).start()

    # Open the UI as a standalone, chromeless app window (Chromium --app mode).
    # This renders with the full Chromium engine, so the interface looks exactly
    # like it does in a browser, while presenting as its own window.
    _open_app_window(url)

    # Idle on the main thread; the watchdog calls os._exit when the window is
    # closed, which tears everything down cleanly.
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
