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
PORT = pick_free_port(8731)

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


def pick_output_folder():
    """
    Open a native OS folder-picker dialog and return the chosen path, or None
    if cancelled. Uses tkinter, which ships with Python. Runs the dialog on the
    calling thread; the HTTP handler calls this synchronously.
    """
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
        return chosen or None
    except Exception as e:
        print(f"Folder picker unavailable: {e}")
        return None


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
    """Download a static FFmpeg build for Windows. Returns True on success."""
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    zip_path = APP_DIR / "ffmpeg.zip"
    try:
        print("Downloading FFmpeg (one-time setup, ~25 MB)...")
        urllib.request.urlretrieve(url, zip_path)
        print("Extracting...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(FFMPEG_DIR)
        zip_path.unlink(missing_ok=True)
        return True
    except Exception as e:
        print(f"FFmpeg auto-download failed: {e}")
        return False


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
    return proc.returncode, "".join(stderr_lines[-12:])


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

        # Calculate target bitrate.
        # total budget (bits) = target_mb * 8 * 1024 * 1024
        # Pick an audio bitrate that scales down for very small targets so it
        # doesn't eat the whole budget; reserve ~3% headroom for container
        # overhead. With audio disabled, audio gets 0 of the budget.
        total_kbits = target_mb * 8 * 1024
        if not keep_audio:
            audio_kbps = 0
        elif target_mb <= 4:
            audio_kbps = 96
        elif target_mb <= 8:
            audio_kbps = 128
        else:
            audio_kbps = 160
        video_kbps = (total_kbits / duration) - audio_kbps
        video_kbps = int(video_kbps * 0.97)

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
        if rc != 0:
            job["status"] = "error"
            job["message"] = "Encoding failed during pass 2.\n" + err
            return

        final_mb = os.path.getsize(output_path) / (1024 * 1024)
        job["status"] = "done"
        job["progress"] = 100.0
        job["message"] = (f"Done! Saved to:\n{output_path}\n"
                           f"Final size: {final_mb:.2f} MB")
        job["output"] = output_path
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Unexpected error: {e}"
    finally:
        # Always remove the two-pass log files, even if a pass failed.
        for f in APP_DIR.glob(f"passlog_{job_id}*"):
            try:
                f.unlink()
            except OSError:
                pass


# ----------------------------------------------------------------------------
# HTML / JS front-end
# ----------------------------------------------------------------------------
def build_page():
    ff_ready = "true" if (FFMPEG and FFPROBE) else "false"
    return (PAGE_HTML
            .replace("__FFMPEG_READY__", ff_ready)
            .replace("__TOKEN__", TOKEN))


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ClipTrimmer Studio</title>
<style>
  :root {
    /* Dark mauve family: same near-black base and mauve accent as the extensions. */
    --bg: #1c1d21;
    --panel: #222327;
    --panel-2: #28292e;
    --panel-3: #313237;
    --line: #3a3b41;
    --line-soft: #2e2f34;
    --text: #f1e3e4;
    --muted: #b1a4a7;
    --muted-2: #8b8286;
    --accent: #a288a6;
    --accent-dim: #6b5c6e;
    --green: #85b79a;
    --green-dim: #3f5c4b;
    --red: #d98a8a;
    --red-dim: #6b3a3a;
    --amber: #d9aa5e;
    --radius: 12px;
    --radius-sm: 9px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    background:
      radial-gradient(900px 500px at 12% -5%, #26222a 0%, transparent 60%),
      var(--bg);
    color: var(--text);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    font-size: 14px;
    overflow: hidden;
  }
  button { font-family: inherit; cursor: pointer; }

  /* ---------- top bar ---------- */
  .topbar {
    height: 56px; display: flex; align-items: center; gap: 14px;
    padding: 0 18px; border-bottom: 1px solid var(--line-soft);
    background: linear-gradient(180deg, var(--panel) 0%, var(--bg) 100%);
  }
  .logo {
    width: 32px; height: 32px; border-radius: 9px; flex-shrink: 0;
    background: linear-gradient(135deg, var(--accent), #bb9bb0);
    color: #1c1d21;
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; box-shadow: 0 2px 10px rgba(162,136,166,.28);
  }
  .brand { font-size: 15px; font-weight: 700; letter-spacing: .2px; }
  .brand span { color: var(--muted-2); font-weight: 500; margin-left: 5px; }

  .file-chip {
    display: flex; align-items: center; gap: 9px;
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 9px; padding: 7px 12px; font-size: 12.5px;
    max-width: 420px;
  }
  .file-chip .ico { color: var(--muted-2); }
  .file-chip .nm { font-weight: 600; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .file-chip .meta { color: var(--muted-2);
    border-left: 1px solid var(--line); padding-left: 9px;
    font-variant-numeric: tabular-nums; white-space: nowrap; }

  .topbar .spacer { flex: 1; }
  .hotkey-legend { display: flex; align-items: center; gap: 6px;
    color: var(--muted-2); font-size: 11.5px; }
  kbd {
    background: var(--panel-3); border: 1px solid var(--line);
    border-bottom-width: 2px; border-radius: 5px;
    padding: 2px 6px; font-size: 11px; font-family: inherit;
    color: var(--text); min-width: 20px; text-align: center;
    display: inline-block;
  }
  .icon-btn {
    width: 30px; height: 30px; border-radius: 8px;
    background: var(--panel-2); border: 1px solid var(--line);
    color: var(--muted); font-size: 14px;
    display: flex; align-items: center; justify-content: center;
  }
  .icon-btn:hover { color: var(--text); border-color: var(--accent-dim); }

  /* ---------- layout ---------- */
  .layout {
    display: grid; grid-template-columns: 1fr 340px;
    height: calc(100vh - 56px);
  }
  .main {
    display: flex; flex-direction: column;
    padding: 18px; gap: 14px; min-width: 0; overflow: hidden;
  }
  .sidebar {
    border-left: 1px solid var(--line-soft);
    background: var(--panel);
    padding: 18px; overflow-y: auto;
    display: flex; flex-direction: column; gap: 20px;
  }
  .sidebar::-webkit-scrollbar { width: 8px; }
  .sidebar::-webkit-scrollbar-thumb {
    background: var(--panel-3); border-radius: 4px; }

  /* ---------- drop zone ---------- */
  .dropzone {
    flex: 1; border: 2px dashed var(--line);
    border-radius: var(--radius); display: flex;
    flex-direction: column; align-items: center; justify-content: center;
    gap: 8px; cursor: pointer; background: var(--panel);
    transition: border-color .15s, background .15s;
  }
  .dropzone:hover, .dropzone.over {
    border-color: var(--accent); background: var(--panel-2); }
  .dropzone .dz-ico { font-size: 38px; filter: grayscale(.3); }
  .dropzone .dz-big { font-size: 15px; font-weight: 600; }
  .dropzone .dz-small { color: var(--muted-2); font-size: 12.5px; }

  /* ---------- video stage ---------- */
  .stage {
    position: relative; background: #000; border-radius: var(--radius);
    overflow: hidden; flex: 1; min-height: 0;
    border: 1px solid var(--line-soft);
  }
  video { width: 100%; height: 100%; object-fit: contain;
    display: block; background: #000; }

  /* transport overlay along the bottom of the video */
  .transport {
    position: absolute; left: 0; right: 0; bottom: 0;
    display: flex; align-items: center; gap: 12px;
    padding: 14px 16px;
    background: linear-gradient(0deg, rgba(0,0,0,.78) 0%, transparent 100%);
  }
  .play-btn {
    width: 42px; height: 42px; border-radius: 50%; flex-shrink: 0;
    background: rgba(255,255,255,.95); border: none; color: #111;
    font-size: 15px; display: flex; align-items: center;
    justify-content: center; transition: transform .1s;
  }
  .play-btn:hover { transform: scale(1.06); }
  .ts {
    font-size: 12.5px; font-variant-numeric: tabular-nums;
    color: #d8dbe2; white-space: nowrap;
  }
  .ts .cur { color: #fff; font-weight: 600; }
  .seekbar {
    flex: 1; height: 5px; border-radius: 3px; position: relative;
    background: rgba(255,255,255,.22); cursor: pointer;
  }
  .seekbar .filled {
    position: absolute; left: 0; top: 0; bottom: 0;
    background: var(--accent); border-radius: 3px;
  }
  .seekbar .knob {
    position: absolute; top: 50%; width: 13px; height: 13px;
    border-radius: 50%; background: #fff; transform: translate(-50%,-50%);
    box-shadow: 0 1px 4px rgba(0,0,0,.5);
  }
  .vol-btn { color: #d8dbe2; background: none; border: none;
    font-size: 15px; padding: 4px; }

  /* progress overlay (during export) */
  .export-overlay {
    position: absolute; inset: 0; display: none;
    flex-direction: column; align-items: center; justify-content: center;
    gap: 16px; background: rgba(6,7,10,.82);
    backdrop-filter: blur(2px);
  }
  .export-overlay.show { display: flex; }
  .export-overlay .eo-label {
    font-size: 13px; color: var(--text); font-weight: 600;
    letter-spacing: .3px;
  }
  .eo-bar {
    width: 64%; max-width: 420px; height: 10px;
    background: rgba(255,255,255,.13); border-radius: 6px;
    overflow: hidden;
  }
  .eo-fill {
    height: 100%; width: 0%; border-radius: 6px;
    background: linear-gradient(90deg, var(--accent), var(--green));
    transition: width .35s ease;
  }
  .eo-pct { font-size: 26px; font-weight: 700;
    font-variant-numeric: tabular-nums; }
  .eo-sub { font-size: 12px; color: var(--muted); }

  /* ---------- timeline strip ---------- */
  .controls-row {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .seg {
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 8px; color: var(--text); font-size: 12px;
    padding: 8px 11px; display: flex; align-items: center; gap: 7px;
    font-variant-numeric: tabular-nums;
  }
  .seg:hover { border-color: var(--accent-dim); background: var(--panel-3); }
  .seg kbd { padding: 1px 5px; min-width: 17px; }
  .controls-row .grow { flex: 1; }
  .mark-btn {
    border: 1px solid; border-radius: 8px; font-size: 12.5px;
    font-weight: 700; padding: 9px 16px;
    display: flex; align-items: center; gap: 8px; letter-spacing: .3px;
  }
  .mark-in {
    background: rgba(133,183,154,.14); border-color: var(--green-dim);
    color: var(--green);
  }
  .mark-in:hover { background: rgba(133,183,154,.22); }
  .mark-out {
    background: rgba(217,138,138,.14); border-color: var(--red-dim);
    color: var(--red);
  }
  .mark-out:hover { background: rgba(217,138,138,.22); }
  .mark-btn kbd { background: rgba(0,0,0,.3); }

  .timeline {
    background: var(--panel); border: 1px solid var(--line-soft);
    border-radius: var(--radius); padding: 12px 14px 10px;
  }
  /* NLE-style ruler: minor + major ticks with timecode labels */
  .tl-ruler { position: relative; height: 22px; margin-bottom: 9px; }
  .tl-ruler .tk {
    position: absolute; bottom: 0; width: 1px; background: var(--muted-2);
  }
  .tl-ruler .tk.min { height: 5px; opacity: .35; }
  .tl-ruler .tk.maj { height: 10px; opacity: .7; }
  .tl-ruler .lb {
    position: absolute; bottom: 12px; transform: translateX(-50%);
    font-size: 10px; color: var(--muted-2);
    font-variant-numeric: tabular-nums; white-space: nowrap;
  }
  .tl-track {
    position: relative; height: 60px; border-radius: 7px;
    background: var(--panel-2);
    border: 1px solid var(--line); cursor: pointer;
  }
  /* faint vertical gridlines aligned with the ruler's major ticks */
  .tl-grid {
    position: absolute; top: 0; bottom: 0; width: 1px;
    background: var(--line); opacity: .55; pointer-events: none;
  }
  .tl-selection {
    position: absolute; top: 0; bottom: 0;
    background: rgba(162,136,166,.16); pointer-events: none;
  }
  /* in / out markers: a colored line with a triangle flag on top */
  .tl-marker {
    position: absolute; top: 0; bottom: 0; width: 2px;
    pointer-events: none; z-index: 4;
  }
  .tl-marker.in  { background: var(--green); }
  .tl-marker.out { background: var(--red); }
  .tl-marker .flag {
    position: absolute; top: -8px; left: 50%; transform: translateX(-50%);
    width: 0; height: 0;
    border-left: 6px solid transparent; border-right: 6px solid transparent;
  }
  .tl-marker.in  .flag { border-top: 9px solid var(--green); }
  .tl-marker.out .flag { border-top: 9px solid var(--red); }
  .tl-playhead {
    position: absolute; top: 0; bottom: 0; width: 2px;
    background: #fff; box-shadow: 0 0 6px rgba(255,255,255,.7);
    pointer-events: none; z-index: 5;
  }
  .tl-playhead .ph {
    position: absolute; top: -7px; left: 50%; transform: translateX(-50%);
    width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 7px solid #fff;
  }
  .tl-foot {
    display: flex; justify-content: space-between; align-items: center;
    margin-top: 7px; font-size: 11px; color: var(--muted-2);
    font-variant-numeric: tabular-nums;
  }
  .tl-foot .dot { display: inline-block; width: 7px; height: 7px;
    border-radius: 50%; margin-right: 5px; vertical-align: middle; }
  .dot-in { background: var(--green); }
  .dot-out { background: var(--red); }

  /* ---------- sidebar sections ---------- */
  .sec-title {
    font-size: 10.5px; font-weight: 700; letter-spacing: 1.2px;
    color: var(--muted-2); text-transform: uppercase;
    display: flex; justify-content: space-between; align-items: baseline;
  }
  .sec-title .hint { color: var(--muted-2); font-weight: 600;
    letter-spacing: 0; font-size: 11px; text-transform: none; }
  .sec { display: flex; flex-direction: column; gap: 10px; }

  .preset-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  }
  .preset {
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: 10px 11px;
    text-align: left; color: var(--text);
    display: flex; flex-direction: column; gap: 3px;
    transition: border-color .12s, background .12s;
  }
  .preset:hover { border-color: var(--accent-dim); }
  .preset.active {
    border-color: var(--accent); background: rgba(162,136,166,.14);
  }
  .preset .p-name { font-size: 12.5px; font-weight: 600;
    display: flex; align-items: center; gap: 6px; }
  .preset .p-sub { font-size: 11px; color: var(--muted-2);
    font-variant-numeric: tabular-nums; }

  .size-input-wrap { position: relative; }
  .size-input-wrap .unit {
    position: absolute; right: 12px; top: 50%;
    transform: translateY(-50%); color: var(--muted-2);
    font-size: 12px; pointer-events: none;
  }
  input.field {
    width: 100%; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    padding: 10px 12px; font-size: 13px; font-family: inherit;
    font-variant-numeric: tabular-nums;
  }
  input.field:focus { outline: none; border-color: var(--accent); }

  /* trim point rows */
  .trim-row {
    display: flex; align-items: center; gap: 9px;
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: 9px 10px;
  }
  .trim-row .tag {
    font-size: 10px; font-weight: 700; letter-spacing: .6px;
    padding: 3px 7px; border-radius: 5px;
  }
  .trim-row.in  { border-left: 3px solid var(--green); }
  .trim-row.out { border-left: 3px solid var(--red); }
  .trim-row.in  .tag { background: rgba(133,183,154,.16); color: var(--green); }
  .trim-row.out .tag { background: rgba(217,138,138,.16); color: var(--red); }
  .trim-row .tval {
    flex: 1; font-size: 15px; font-weight: 600;
    font-variant-numeric: tabular-nums; letter-spacing: .5px;
  }
  .nudge {
    width: 26px; height: 26px; border-radius: 6px;
    background: var(--panel-3); border: 1px solid var(--line);
    color: var(--text); font-size: 13px;
    display: flex; align-items: center; justify-content: center;
  }
  .nudge:hover { border-color: var(--accent-dim); }
  .nudge.set { width: auto; padding: 0 7px; }
  .nudge.set kbd { padding: 1px 4px; min-width: 15px; font-size: 10px;
    border-bottom-width: 1px; }

  .length-row {
    display: flex; justify-content: space-between; align-items: center;
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: 9px 12px;
    color: var(--muted); font-size: 12px;
  }
  .length-row b { color: var(--text); font-size: 14px;
    font-variant-numeric: tabular-nums; }

  /* output rows */
  .out-row {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px;
  }
  .out-row > label { color: var(--muted); font-size: 12.5px; }
  select.field {
    background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: var(--radius-sm);
    padding: 8px 10px; font-size: 12.5px; font-family: inherit;
    min-width: 150px;
  }
  select.field:focus { outline: none; border-color: var(--accent); }

  .toggle {
    width: 40px; height: 22px; border-radius: 12px; flex-shrink: 0;
    background: var(--panel-3); border: 1px solid var(--line);
    position: relative; transition: background .15s, border-color .15s;
  }
  .toggle .knob {
    position: absolute; top: 2px; left: 2px;
    width: 16px; height: 16px; border-radius: 50%;
    background: var(--muted); transition: left .15s, background .15s;
  }
  .toggle.on { background: rgba(162,136,166,.25);
    border-color: var(--accent); }
  .toggle.on .knob { left: 20px; background: var(--accent); }

  .pathbox {
    flex: 1; background: var(--panel-2); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: 8px 10px; font-size: 11.5px;
    color: var(--text); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; direction: rtl; text-align: left;
  }
  .folder-btn {
    width: 34px; height: 34px; border-radius: var(--radius-sm);
    background: var(--panel-3); border: 1px solid var(--line);
    color: var(--muted); font-size: 14px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
  }
  .folder-btn:hover { color: var(--text); border-color: var(--accent-dim); }

  /* bitrate meter */
  .bitrate {
    background: var(--panel-2); border: 1px solid var(--line);
    border-radius: var(--radius-sm); padding: 11px 12px;
  }
  .bitrate .br-top {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 8px;
  }
  .br-top .br-label { font-size: 11.5px; color: var(--muted); }
  .br-top .br-val { font-size: 13px; font-weight: 700;
    font-variant-numeric: tabular-nums; }
  .br-meter {
    height: 7px; border-radius: 4px; overflow: hidden;
    background: linear-gradient(90deg,
      var(--red) 0%, var(--amber) 48%, var(--green) 100%);
    position: relative;
  }
  .br-mask {
    position: absolute; top: 0; right: 0; bottom: 0;
    background: var(--panel-2);
    border-left: 2px solid var(--text);
  }
  .br-scale {
    display: flex; justify-content: space-between;
    font-size: 10px; color: var(--muted-2); margin-top: 5px;
  }

  /* main action */
  .go-btn {
    width: 100%; border: none; border-radius: var(--radius);
    padding: 15px; font-size: 14.5px; font-weight: 700;
    color: #1c1d21; letter-spacing: .3px;
    background: linear-gradient(135deg, var(--accent), #bb9bb0);
    display: flex; align-items: center; justify-content: center; gap: 9px;
    box-shadow: 0 4px 16px rgba(162,136,166,.25);
    transition: transform .1s, box-shadow .15s, filter .15s;
  }
  .go-btn:hover { filter: brightness(1.05);
    box-shadow: 0 6px 22px rgba(162,136,166,.34); }
  .go-btn:active { transform: translateY(1px); }
  .go-btn:disabled { filter: grayscale(.7) brightness(.7);
    box-shadow: none; cursor: not-allowed; }
  .go-btn kbd { background: rgba(28,29,33,.18); border-color: rgba(28,29,33,.32);
    color: #1c1d21; }

  /* secondary action - deliberately neutral so it doesn't compete with
     the primary Trim & Compress button */
  .reset-btn {
    width: 100%; border-radius: var(--radius);
    padding: 12px; font-size: 13px; font-weight: 600;
    background: var(--panel-2); border: 1px solid var(--line);
    color: var(--muted); letter-spacing: .2px;
    display: flex; align-items: center; justify-content: center; gap: 8px;
    transition: border-color .15s, color .15s, background .15s;
  }
  .reset-btn:hover {
    color: var(--text); border-color: var(--muted-2);
    background: var(--panel-3);
  }

  .toast {
    border-radius: var(--radius-sm); padding: 10px 12px; font-size: 12px;
    white-space: pre-wrap; display: none; line-height: 1.5;
    font-variant-numeric: tabular-nums;
  }
  .toast.show { display: block; }
  .toast.ok  { background: rgba(133,183,154,.12);
    border: 1px solid var(--green-dim); color: #cfe3d5; }
  .toast.err { background: rgba(217,138,138,.12);
    border: 1px solid var(--red-dim); color: #eed3d3; }

  .warn-banner {
    margin: 12px 18px 0; background: rgba(217,170,94,.10);
    border: 1px solid #6b5631; color: #e8d3a8;
    padding: 10px 13px; border-radius: var(--radius-sm);
    font-size: 12.5px; display: none;
  }
  .warn-banner.show { display: block; }

  .disabled-veil { opacity: .4; pointer-events: none; }
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">✂</div>
  <div class="brand">ClipTrimmer<span>Studio</span></div>
  <div class="file-chip" id="fileChip" style="display:none;">
    <span class="ico">▦</span>
    <span class="nm" id="fileName">—</span>
    <span class="meta" id="fileMeta">—</span>
  </div>
  <div class="spacer"></div>
</div>

<div class="warn-banner" id="ffwarn">
  FFmpeg was not found. Trimming will not work until it is installed.
</div>

<div class="layout">
  <!-- ============ LEFT: player + timeline ============ -->
  <div class="main">

    <div class="dropzone" id="dropzone">
      <div class="dz-ico">🎬</div>
      <div class="dz-big">Drop an MP4 clip here</div>
      <div class="dz-small">or click to browse</div>
      <input type="file" id="fileInput" accept="video/mp4,video/*" hidden>
    </div>

    <div class="stage" id="stage" style="display:none;">
      <video id="video"></video>
      <div class="transport">
        <button class="play-btn" id="playBtn">▶</button>
        <div class="ts"><span class="cur" id="curTime">0:00.00</span>
          <span> / </span><span id="totTime">0:00.00</span></div>
        <div class="seekbar" id="seekbar">
          <div class="filled" id="seekFilled"></div>
          <div class="knob" id="seekKnob"></div>
        </div>
        <button class="vol-btn" id="muteBtn">🔊</button>
      </div>
      <div class="export-overlay" id="exportOverlay">
        <div class="eo-label" id="eoLabel">Encoding…</div>
        <div class="eo-pct" id="eoPct">0%</div>
        <div class="eo-bar"><div class="eo-fill" id="eoFill"></div></div>
        <div class="eo-sub" id="eoSub">This can take a moment</div>
      </div>
    </div>

    <div class="controls-row" id="controlsRow" style="display:none;">
      <div class="grow"></div>
      <button class="mark-btn mark-in" id="setIn">⊢ SET IN</button>
      <button class="mark-btn mark-out" id="setOut">SET OUT ⊣</button>
      <div class="grow"></div>
    </div>

    <div class="timeline" id="timeline" style="display:none;">
      <div class="tl-ruler" id="tlRuler"></div>
      <div class="tl-track" id="tlTrack">
        <div class="tl-selection" id="tlSelection" style="display:none;"></div>
        <div class="tl-marker in"  id="tlMarkIn"  style="display:none;">
          <span class="flag"></span></div>
        <div class="tl-marker out" id="tlMarkOut" style="display:none;">
          <span class="flag"></span></div>
        <div class="tl-playhead" id="tlPlayhead" style="left:0;">
          <span class="ph"></span></div>
      </div>
      <div class="tl-foot">
        <span><span class="dot dot-in"></span>IN
          <b id="tlInVal">—</b></span>
        <span>Selection <b id="tlLenVal">—</b></span>
        <span>OUT <b id="tlOutVal">—</b>
          <span class="dot dot-out"></span></span>
      </div>
    </div>

  </div>

  <!-- ============ RIGHT: sidebar ============ -->
  <div class="sidebar" id="sidebar">

    <div class="sec">
      <div class="sec-title">Target Size
        <span class="hint" id="sizeHint">~10 MB</span></div>
      <div class="preset-grid">
        <button class="preset active" data-size="10">
          <div class="p-name">🎮 Discord</div>
          <div class="p-sub">10 MB</div></button>
        <button class="preset" data-size="50">
          <div class="p-name">🚀 Nitro</div>
          <div class="p-sub">50 MB</div></button>
        <button class="preset" data-size="512">
          <div class="p-name">𝕏 X / Twitter</div>
          <div class="p-sub">512 MB</div></button>
        <button class="preset" data-size="0" id="presetCustom">
          <div class="p-name">⚙ Custom</div>
          <div class="p-sub">—</div></button>
      </div>
      <div class="size-input-wrap">
        <input type="number" class="field" id="sizeInput"
               value="10" min="1" step="1">
        <span class="unit">MB</span>
      </div>
    </div>

    <div class="sec">
      <div class="sec-title">Trim Points</div>
      <div class="trim-row in">
        <span class="tag">IN</span>
        <span class="tval" id="inVal">—</span>
        <button class="nudge" data-nudge="in" data-d="-0.0334">−</button>
        <button class="nudge" data-nudge="in" data-d="0.0334">+</button>
        <button class="nudge set" id="setInSide">⊢ Set</button>
      </div>
      <div class="trim-row out">
        <span class="tag">OUT</span>
        <span class="tval" id="outVal">—</span>
        <button class="nudge" data-nudge="out" data-d="-0.0334">−</button>
        <button class="nudge" data-nudge="out" data-d="0.0334">+</button>
        <button class="nudge set" id="setOutSide">Set ⊣</button>
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
        <div style="display:flex;align-items:center;gap:9px;">
          <span id="audioLabel" style="font-size:12.5px;">Keep audio</span>
          <div class="toggle on" id="audioToggle">
            <div class="knob"></div></div>
        </div>
      </div>
      <div class="out-row">
        <label>Save to</label>
        <span class="pathbox" id="outFolder" title="">—</span>
        <button class="folder-btn" id="chooseFolder" title="Choose folder">📁</button>
      </div>
    </div>

    <div class="bitrate">
      <div class="br-top">
        <span class="br-label">Estimated bitrate</span>
        <span class="br-val" id="brVal">— kbps</span>
      </div>
      <div class="br-meter"><div class="br-mask" id="brMask"
        style="width:50%;"></div></div>
      <div class="br-scale"><span>Lower quality</span>
        <span>Higher</span></div>
    </div>

    <button class="go-btn" id="exportBtn" disabled>
      ⚡ Trim &amp; Compress
    </button>

    <button class="reset-btn" id="resetBtn" style="display:none;">
      ↺ Reset &amp; load a new clip
    </button>

    <div class="toast" id="toast"></div>

  </div>
</div>

<script>
const FFMPEG_READY = __FFMPEG_READY__;
const TOKEN = "__TOKEN__";

// Append the per-session token to a request path so the server accepts it.
function api(path) {
  return path + (path.includes('?') ? '&' : '?') + 'token=' + TOKEN;
}

// ---------- element refs ----------
const $ = id => document.getElementById(id);
const video = $('video');
let serverPath = null;
let inPoint = null, outPoint = null;
let pollTimer = null;
let exporting = false;
const FRAME = 0.0334;   // ~1 frame at 30fps, used for nudges

if (!FFMPEG_READY) $('ffwarn').classList.add('show');

// ---------- helpers ----------
function fmt(t) {
  if (t == null || isNaN(t)) return '—';
  const m = Math.floor(t / 60);
  const s = (t % 60).toFixed(2).padStart(5, '0');
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

function loadFile(file) {
  showToast('Loading video…', '');
  // Stream the file straight as the request body (the browser streams it, so
  // nothing huge is held in memory on either side); the name rides in the URL.
  fetch(api('/upload?name=' + encodeURIComponent(file.name)),
        { method: 'POST', body: file })
    .then(r => r.json())
    .then(info => {
      serverPath = info.path;
      video.src = api('/video?path=' + encodeURIComponent(info.path));
      $('fileName').textContent = file.name;
      const mb = (file.size / 1048576).toFixed(1);
      $('fileChip').style.display = 'flex';
      $('dropzone').style.display = 'none';
      $('stage').style.display = 'block';
      $('controlsRow').style.display = 'flex';
      $('timeline').style.display = 'block';
      $('resetBtn').style.display = 'block';
      $('exportBtn').disabled = !FFMPEG_READY;
      hideToast();
      video.addEventListener('loadedmetadata', () => {
        $('fileMeta').textContent = fmt(video.duration) + '  ·  ' + mb + ' MB';
        $('totTime').textContent = fmt(video.duration);
        buildTicks();
        updateBitrate();
      }, { once: true });
    })
    .catch(err => showToast('Could not load file: ' + err, 'err'));
}

// ---------- playback ----------
$('playBtn').addEventListener('click', togglePlay);
video.addEventListener('click', togglePlay);
function togglePlay() {
  if (video.paused) video.play(); else video.pause();
}
video.addEventListener('play',  () => $('playBtn').textContent = '⏸');
video.addEventListener('pause', () => $('playBtn').textContent = '▶');
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
  if (scrubbing && dragEl) scrubTo(fracFrom(dragEl, e), false);
});
window.addEventListener('mouseup', e => {
  if (scrubbing && dragEl) scrubTo(fracFrom(dragEl, e), true);
  scrubbing = false; dragEl = null;
});

$('muteBtn').addEventListener('click', () => {
  video.muted = !video.muted;
  $('muteBtn').textContent = video.muted ? '🔇' : '🔊';
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
  if (outPoint != null && inPoint >= outPoint) outPoint = null;
  refreshMarks();
}
function setOutPoint() {
  outPoint = video.currentTime;
  if (inPoint != null && outPoint <= inPoint) inPoint = null;
  refreshMarks();
}
document.querySelectorAll('[data-nudge]').forEach(b => {
  b.addEventListener('click', () => {
    const d = parseFloat(b.dataset.d);
    if (b.dataset.nudge === 'in' && inPoint != null)
      inPoint = clamp(inPoint + d, 0, (outPoint ?? video.duration));
    if (b.dataset.nudge === 'out' && outPoint != null)
      outPoint = clamp(outPoint + d, (inPoint ?? 0), video.duration);
    refreshMarks();
  });
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
  updateBitrate();
}

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
  });
});
$('sizeInput').addEventListener('input', () => {
  activeSize = parseFloat($('sizeInput').value) || 0;
  // typing a custom number selects the Custom preset unless it matches one
  const presets = { 10: 0, 50: 1, 512: 2 };
  document.querySelectorAll('.preset').forEach(x =>
    x.classList.remove('active'));
  const known = document.querySelector(
    `.preset[data-size="${activeSize}"]`);
  (known || $('presetCustom')).classList.add('active');
  updateBitrate();
});

// ---------- audio toggle ----------
let keepAudio = true;
$('audioToggle').addEventListener('click', () => {
  keepAudio = !keepAudio;
  $('audioToggle').classList.toggle('on', keepAudio);
  $('audioLabel').textContent = keepAudio ? 'Keep audio' : 'No audio';
  updateBitrate();
});

// ---------- bitrate estimate ----------
function updateBitrate() {
  $('sizeHint').textContent = '~' + (activeSize || 0) + ' MB';
  const len = (inPoint != null && outPoint != null)
    ? (outPoint - inPoint)
    : (video.duration || 0);
  if (!len || !activeSize) {
    $('brVal').textContent = '— kbps';
    $('brMask').style.width = '50%';
    return;
  }
  // mirrors the backend math
  const totalKbits = activeSize * 8 * 1024;
  let audioKbps = 0;
  if (keepAudio) audioKbps = activeSize <= 4 ? 96
    : activeSize <= 8 ? 128 : 160;
  let videoKbps = Math.floor((totalKbits / len - audioKbps) * 0.97);
  $('brVal').textContent = videoKbps > 0
    ? videoKbps.toLocaleString() + ' kbps' : 'too small';
  // map bitrate onto the meter: 0..8000 kbps -> 0..100%
  const q = clamp(videoKbps / 8000, 0, 1);
  $('brMask').style.width = ((1 - q) * 100) + '%';
}

// ---------- folder picker ----------
function setFolder(p) {
  $('outFolder').textContent = p;
  $('outFolder').title = p;
}
fetch(api('/outdir')).then(r => r.json()).then(j => setFolder(j.folder))
  .catch(() => {});
$('chooseFolder').addEventListener('click', () => {
  const b = $('chooseFolder');
  b.disabled = true; b.textContent = '…';
  fetch(api('/pickfolder')).then(r => r.json())
    .then(j => setFolder(j.folder))
    .catch(() => {})
    .finally(() => { b.disabled = false; b.textContent = '📁'; });
});

// ---------- export ----------
$('exportBtn').addEventListener('click', startExport);
function startExport() {
  if (exporting || !FFMPEG_READY || !serverPath) return;
  const start = inPoint != null ? inPoint : 0;
  const end   = outPoint != null ? outPoint : video.duration;
  if (!end || end - start < 0.1) {
    showToast('Set valid In and Out points first.', 'err'); return;
  }
  if (!activeSize || activeSize < 1) {
    showToast('Enter a target size of at least 1 MB.', 'err'); return;
  }
  exporting = true;
  $('exportBtn').disabled = true;
  hideToast();
  showOverlay(0, 'Starting…');

  fetch(api('/export'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ path: serverPath, start, end,
      target_mb: activeSize, resolution:
        parseInt($('resSelect').value, 10) || 0,
      keep_audio: keepAudio })
  })
  .then(r => r.json())
  .then(j => { if (j.job) poll(j.job);
    else throw new Error(j.error || 'failed'); })
  .catch(err => { endExport(); showToast('Export failed: ' + err, 'err'); });
}

function poll(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(() => {
    fetch(api('/status?job=' + jobId)).then(r => r.json()).then(j => {
      if (typeof j.progress === 'number')
        showOverlay(j.progress, j.message || 'Encoding…');
      if (j.status === 'done') {
        clearInterval(pollTimer);
        showOverlay(100, 'Done');
        setTimeout(() => { endExport();
          showToast(j.message, 'ok'); }, 600);
      } else if (j.status === 'error') {
        clearInterval(pollTimer);
        endExport();
        showToast(j.message, 'err');
      }
    });
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
  $('exportOverlay').classList.remove('show');
  $('exportBtn').disabled = !FFMPEG_READY;
}

// ---------- toast ----------
function showToast(msg, kind) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (kind ? ' ' + kind : '');
}
function hideToast() { $('toast').className = 'toast'; }

// ---------- reset / load a new clip ----------
$('resetBtn').addEventListener('click', resetApp);
function resetApp() {
  try { video.pause(); } catch (e) {}
  video.removeAttribute('src');
  video.load();
  serverPath = null;
  inPoint = null;
  outPoint = null;
  $('fileInput').value = '';
  $('fileChip').style.display = 'none';
  $('stage').style.display = 'none';
  $('controlsRow').style.display = 'none';
  $('timeline').style.display = 'none';
  $('resetBtn').style.display = 'none';
  $('dropzone').style.display = 'flex';
  $('exportBtn').disabled = true;
  hideToast();
  refreshMarks();
}

// ---------- the one hotkey: Space = play / pause ----------
document.addEventListener('keydown', e => {
  if (e.code !== 'Space' && e.key !== ' ') return;
  const t = e.target.tagName;
  if (t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA') return;
  e.preventDefault();          // stop page scroll + avoid double-firing a focused button
  if (video.src) togglePlay();
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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, parsed):
        """Every endpoint except the entry page requires the session token."""
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

        if parsed.path == "/status":
            qs = parse_qs(parsed.query)
            job_id = qs.get("job", [""])[0]
            job = JOBS.get(job_id, {"status": "error", "message": "Unknown job"})
            self._send(200, json.dumps(job))
            return

        if parsed.path == "/outdir":
            # Report the current output folder so the UI can show it on load.
            self._send(200, json.dumps({"folder": OUTPUT_DIR[0]}))
            return

        if parsed.path == "/pickfolder":
            # Open a native folder dialog (blocks until the user picks/cancels).
            chosen = pick_output_folder()
            if chosen:
                OUTPUT_DIR[0] = chosen
            self._send(200, json.dumps({"folder": OUTPUT_DIR[0]}))
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
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length))
            self._handle_export(data)
            return

        self._send(404, json.dumps({"error": "not found"}))

    # -- serve video with HTTP range support so scrubbing works --
    def _serve_video(self, path):
        # Only ever serve files from inside the app's own temp dir (the uploaded
        # clip). This prevents the endpoint from being used to read arbitrary
        # files off the machine.
        try:
            real = os.path.realpath(path) if path else ""
            base = os.path.realpath(str(APP_DIR))
        except Exception:
            real, base = "", "\0"
        inside = real == base or real.startswith(base + os.sep)
        if not inside or not os.path.isfile(real):
            self._send(403, json.dumps({"error": "forbidden"}))
            return
        path = real
        size = os.path.getsize(path)
        range_hdr = self.headers.get("Range")
        start, end = 0, size - 1
        if range_hdr and range_hdr.startswith("bytes="):
            rng = range_hdr.split("=", 1)[1].split("-")
            if rng[0]:
                start = int(rng[0])
            if len(rng) > 1 and rng[1]:
                end = int(rng[1])
        end = min(end, size - 1)
        length = end - start + 1

        self.send_response(206 if range_hdr else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if range_hdr:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

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

        # Drop previous source files so the temp dir doesn't accumulate clips.
        for old in APP_DIR.glob("source_*"):
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
        self._send(200, json.dumps({"path": str(temp_path)}))

    # -- kick off a background export job --
    def _handle_export(self, data):
        if not (FFMPEG and FFPROBE):
            self._send(200, json.dumps({"error": "FFmpeg not available"}))
            return
        # Only transcode files from our own temp dir (the uploaded clip), never
        # an arbitrary path supplied in the request.
        src = data.get("path", "")
        real_src = os.path.realpath(src) if src else ""
        base = os.path.realpath(str(APP_DIR))
        inside = real_src == base or real_src.startswith(base + os.sep)
        if not inside or not os.path.isfile(real_src):
            self._send(200, json.dumps({"error": "source file missing"}))
            return
        src = real_src

        start = float(data["start"])
        end = float(data["end"])
        target_mb = float(data["target_mb"])
        resolution = int(data.get("resolution", 0))
        keep_audio = bool(data.get("keep_audio", True))

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
        JOBS[job_id] = {"status": "queued", "message": "Queued...",
                        "progress": 0.0}

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
    """Start the local HTTP server in a background thread and return it."""
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
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
    try:
        server.shutdown()
    except Exception:
        pass
    os._exit(0)


def main():
    global FFMPEG, FFPROBE

    if not (FFMPEG and FFPROBE):
        if os.name == "nt":
            ok = download_ffmpeg()
            if ok:
                FFMPEG, FFPROBE = find_ffmpeg()
        if not (FFMPEG and FFPROBE):
            print("WARNING: FFmpeg not found. Trimming/compression will not work.")
            print("Install FFmpeg from https://ffmpeg.org and restart.")

    server = _start_server()
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
