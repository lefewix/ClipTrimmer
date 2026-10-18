"""The replays folder as a library: listing, grouping by day, and cover pictures.

A cover is the frame from just before the replay was saved (the moment you
pressed the button), stored inside the mp4 as an attached picture. File
Explorer shows it as the thumbnail, and the clip browser shows it on the card.
Adding one is a lossless remux, checked before it replaces the original.
"""
import ctypes
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v"}
COVER_BEFORE_END = 4.0      # seconds before the end: just before the save was pressed
SETTLE = 10.0               # leave files alone this long after they last changed (OBS may be writing)
STALE = 600.0               # a cover temp or lock file this old was left behind by a crash


@dataclass
class Clip:
    path: Path
    mtime: float
    size: int

    @property
    def when(self):
        return datetime.fromtimestamp(self.mtime)


def list_clips(folder):
    """Video files in the folder, newest first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    clips = []
    for p in folder.iterdir():
        if p.suffix.lower() in VIDEO_EXT and p.is_file() and not p.name.startswith("~"):
            try:
                st = p.stat()
            except OSError:
                continue
            clips.append(Clip(p, st.st_mtime, st.st_size))
    clips.sort(key=lambda c: c.mtime, reverse=True)
    return clips


def day_label(d, today):
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if today - d < timedelta(days=7):
        return d.strftime("%A")
    return f"{d:%b} {d.day}" if d.year == today.year else f"{d:%b} {d.day}, {d.year}"


def group_by_day(clips, today=None):
    """[(label, [clips])] in the order given (newest first)."""
    today = today or date.today()
    groups = []
    for c in clips:
        label = day_label(c.when.date(), today)
        if not groups or groups[-1][0] != label:
            groups.append((label, []))
        groups[-1][1].append(c)
    return groups


def when_label(dt, today=None):
    """Today 2:49 PM / Yesterday 9:05 AM / Oct 3 2:49 PM"""
    return f"{day_label(dt.date(), today or date.today())} {clock(dt)}"


def age(mtime):
    """Seconds since the file changed; never negative (a clock change can date files in the future)."""
    return max(0.0, time.time() - mtime)


def sweep_temps(folder):
    """Remove cover temp/lock files a crash (or antivirus holding them) left behind."""
    try:
        for p in Path(folder).glob("~cover *"):
            try:
                if age(p.stat().st_mtime) > STALE:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def clock(dt):
    """2:49 PM"""
    return dt.strftime("%I:%M %p").lstrip("0")


def was_trimmed(out_dir, stem):
    """True when ClipTrimmer has exported this clip to out_dir before."""
    try:
        return any(Path(out_dir).glob(f"{stem}_trimmed*.mp4"))
    except OSError:
        return False


# ------------------------------------------------------------------ covers

def _probe(ffprobe, path):
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                        "format=duration:stream=index,codec_type:stream_tags=name,title:stream_disposition=attached_pic",
                        "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise OSError(r.stderr.strip() or "ffprobe failed")
    j = json.loads(r.stdout or "{}")
    return float(j.get("format", {}).get("duration") or 0), j.get("streams", [])


def _is_cover(s):
    return s.get("disposition", {}).get("attached_pic") == 1


def cover_image(ffmpeg, path):
    """The clip's attached cover picture as JPEG bytes, or None."""
    r = subprocess.run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v", "-map", "-0:V",
                        "-frames:v", "1", "-c", "copy", "-f", "image2pipe", "-"],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None


def frame_at_end(ffmpeg, path, duration, width=640):
    """JPEG bytes of the frame from just before the end."""
    t = max(0.0, duration - COVER_BEFORE_END)
    r = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                       capture_output=True, timeout=60, creationflags=NO_WINDOW)
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None


class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                             ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.GetFileTime.argtypes = _k32.SetFileTime.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(_FILETIME)] * 3
_INVALID = ctypes.c_void_p(-1).value


def _open_attrs(path, access):
    """A handle for reading (0x80) or writing (0x100) a file's attributes, or None."""
    h = _k32.CreateFileW(str(path), access, 7, None, 3, 0x02000000, None)  # share all, OPEN_EXISTING, BACKUP_SEMANTICS
    return None if h in (None, _INVALID) else h


def _file_times(path):
    """(created, accessed, modified) as Windows FILETIMEs, or None."""
    h = _open_attrs(path, 0x80)
    if h is None:
        return None
    try:
        t = (_FILETIME(), _FILETIME(), _FILETIME())
        return t if _k32.GetFileTime(h, *[ctypes.byref(x) for x in t]) else None
    finally:
        _k32.CloseHandle(h)


def _set_file_times(path, times):
    h = _open_attrs(path, 0x100)
    if h is None:
        return
    try:
        _k32.SetFileTime(h, *[ctypes.byref(x) for x in times])
    finally:
        _k32.CloseHandle(h)


ADDED, HAD, SKIPPED, FAILED = "added", "had", "skipped", "failed"


def add_cover(ffmpeg, ffprobe, path, image=None):
    """
    Give an mp4 a cover picture (lossless remux into a temp file next to it,
    checked, then swapped in with the original dates kept). Returns
    (status, cover JPEG bytes or None): ADDED; HAD (already has one); SKIPPED
    (not an mp4); FAILED (e.g. open in a player, or still being written; worth
    another try later). Never raises.
    """
    path = Path(path)
    # Temp names are per process, and a lock file next to the clip stops two
    # programs (the browser and PadDeck's --covers run) working on it at once.
    tmp = path.with_name(f"~cover {os.getpid()} {path.name}")
    jpg = path.with_name(f"~cover {os.getpid()} {path.stem}.jpg")
    lock = path.with_name(f"~cover {path.name}.lock")
    locked = False
    try:
        if path.suffix.lower() not in (".mp4", ".m4v"):
            return SKIPPED, None
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            locked = True
        except FileExistsError:
            if age(lock.stat().st_mtime) < STALE:
                return FAILED, None      # someone else is on it; it'll have a cover soon
            lock.unlink()
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            locked = True
        duration, streams = _probe(ffprobe, path)
        if any(_is_cover(s) for s in streams):
            return HAD, None
        if duration <= 0:
            return FAILED, None
        image = image or frame_at_end(ffmpeg, path, duration)
        if not image:
            return FAILED, None
        jpg.write_bytes(image)
        n = len(streams)
        cmd = [ffmpeg, "-v", "error", "-y", "-i", str(path), "-i", str(jpg),
               "-map", "0", "-map", "1", "-c", "copy", f"-disposition:v:{_video_count(streams)}", "attached_pic",
               "-map_metadata", "0"]
        # Track names (OBS's "Game audio", "Mic", ...) only survive a remux when written back as titles.
        for s in streams:
            name = (s.get("tags") or {}).get("name") or (s.get("tags") or {}).get("title")
            if name:
                cmd += [f"-metadata:s:{s['index']}", f"title={name}"]
        cmd.append(str(tmp))
        r = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=NO_WINDOW)
        if r.returncode != 0:
            return FAILED, None
        d2, s2 = _probe(ffprobe, tmp)
        same = (abs(d2 - duration) < 0.05 and len(s2) == n + 1 and _is_cover(s2[-1])
                and [s.get("codec_type") for s in s2[:n]] == [s.get("codec_type") for s in streams]
                and tmp.stat().st_size >= path.stat().st_size)      # everything copied, plus the picture
        if not same:
            return FAILED, None
        times = _file_times(path)
        os.replace(tmp, path)       # fails (and changes nothing) while a player has the clip open
        if times:
            _set_file_times(path, times)
        return ADDED, image
    except Exception:
        return FAILED, None
    finally:
        for f in (tmp, jpg) + ((lock,) if locked else ()):
            try:
                f.unlink()
            except OSError:
                pass


def _video_count(streams):
    """Index of the new cover among the output's video streams (it comes after the existing ones)."""
    return sum(1 for s in streams if s.get("codec_type") == "video")


def add_covers(ffmpeg, ffprobe, paths, settle=SETTLE, retries=1):
    """add_cover for each path; returns how many got one. Files changed less than
    `settle` seconds ago may still be being written: a folder scan (retries=1) skips
    them, while one clip named right after it was saved (retries > 1) is waited for,
    and a failed try is repeated a few seconds later."""
    done = 0
    for p in paths:
        p = Path(p)
        for attempt in range(retries):
            try:
                a = age(p.stat().st_mtime)
            except OSError:
                break
            if a < settle:
                if retries == 1:
                    break
                time.sleep(settle - a)
            status, _ = add_cover(ffmpeg, ffprobe, p)
            if status == ADDED:
                done += 1
            if status != FAILED:
                break
            if attempt + 1 < retries:
                time.sleep(3)
    return done
