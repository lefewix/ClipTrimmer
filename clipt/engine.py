"""Export engine: cut sections out of a clip, join them, fit to a size.

Pure Python + ffmpeg, no Qt, so it can be tested on its own.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ExportError(Exception):
    """A user-facing failure (bad input, ffmpeg error)."""


class ExportCancelled(Exception):
    pass


def probe(ffprobe, src):
    """Return basic facts about a media file, or raise ExportError."""
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,width,height,avg_frame_rate",
         "-of", "json", src],
        capture_output=True, text=True, creationflags=NO_WINDOW)
    try:
        j = json.loads(r.stdout or "{}")
    except ValueError:
        j = {}
    streams = j.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ExportError("This file has no video stream.")
    fps = 0.0
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        pass
    fmt = j.get("format", {})
    return {
        "duration": float(fmt.get("duration") or 0),
        "size": int(fmt.get("size") or os.path.getsize(src)),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def normalize_segments(segments):
    """Sort [start, end] pairs and merge any that overlap."""
    segs = sorted((max(0.0, float(s)), float(e)) for s, e in segments)
    merged = []
    for s, e in segs:
        if e - s < 0.05:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def plan_bitrate(target_mb, duration, keep_audio):
    """
    (video_kbps, audio_kbps) that fit `duration` seconds into target_mb.
    Audio scales down for tiny targets; 3% headroom for container overhead.
    """
    if not keep_audio:
        audio_kbps = 0
    elif target_mb <= 4:
        audio_kbps = 96
    elif target_mb <= 8:
        audio_kbps = 128
    else:
        audio_kbps = 160
    total_kbits = target_mb * 8 * 1024
    video_kbps = int(((total_kbits / max(0.1, duration)) - audio_kbps) * 0.97)
    return video_kbps, audio_kbps


def video_filters(height, resolution):
    """Optional downscale to the target height; never upscales."""
    if resolution and resolution > 0 and height > resolution:
        return [f"scale=-2:{resolution}"]
    return []


def build_filtergraph(segments, base, post_filters, with_audio):
    """
    Filtergraph that cuts each segment out of the input and joins them back
    to back. Input is already seeked to `base`, so trim times are relative.
    Outputs [vout] and, if with_audio, [aout].
    """
    n = len(segments)
    post = ",".join(post_filters) or "null"
    graph = []
    if n == 1:
        graph.append(f"[0:v:0]{post}[vout]")
        if with_audio:
            graph.append("[0:a:0]anull[aout]")
        return ";".join(graph)

    def cut(kind, trim, setpts):
        labels = "".join(f"[{kind}s{i}]" for i in range(n))
        split = "split" if kind == "v" else "asplit"
        src = "[0:v:0]" if kind == "v" else "[0:a:0]"
        graph.append(f"{src}{split}={n}{labels}")
        for i, (s, e) in enumerate(segments):
            graph.append(f"[{kind}s{i}]{trim}=start={s - base:.6f}:"
                         f"end={e - base:.6f},{setpts}=PTS-STARTPTS[{kind}{i}]")

    cut("v", "trim", "setpts")
    joined = "".join(f"[v{i}]" for i in range(n))
    graph.append(f"{joined}concat=n={n}:v=1:a=0[vc]")
    graph.append(f"[vc]{post}[vout]")
    if with_audio:
        cut("a", "atrim", "asetpts")
        joined = "".join(f"[a{i}]" for i in range(n))
        graph.append(f"{joined}concat=n={n}:v=0:a=1[aout]")
    return ";".join(graph)


class Exporter:
    """Runs one export at a time; cancel() may be called from another thread."""

    def __init__(self, ffmpeg, ffprobe):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._proc = None
        self._cancelled = threading.Event()

    def cancel(self):
        self._cancelled.set()
        proc = self._proc
        if proc and proc.poll() is None:
            proc.kill()

    def _pass(self, cmd, duration, base, span, label, progress):
        """Run one ffmpeg pass, mapping its progress onto base..base+span."""
        if self._cancelled.is_set():
            raise ExportCancelled()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1,
                                creationflags=NO_WINDOW)
        self._proc = proc
        err_tail = []

        def drain():
            for line in proc.stderr:
                err_tail.append(line)
                del err_tail[:-30]

        t = threading.Thread(target=drain, daemon=True)
        t.start()
        for line in proc.stdout:
            if line.startswith("out_time_ms="):
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                frac = min(1.0, max(0.0, done / duration))
                if progress:
                    progress(base + frac * span, label)
        proc.wait()
        t.join(timeout=1)
        self._proc = None
        if self._cancelled.is_set():
            raise ExportCancelled()
        if proc.returncode != 0:
            raise ExportError(f"ffmpeg failed during {label.lower()}.\n"
                              + "".join(err_tail[-12:]))

    def run(self, src, segments, target_mb, out_path, resolution=0,
            keep_audio=True, progress=None):
        """
        Cut `segments` ([start, end] seconds) from src, join them in timeline
        order and compress to about target_mb with two-pass x264.
        Returns the final size in MB. Raises ExportError / ExportCancelled.
        """
        self._cancelled.clear()
        segments = normalize_segments(segments)
        if not segments:
            raise ExportError("Nothing to export - set at least one section.")
        duration = max(0.1, sum(e - s for s, e in segments))
        info = probe(self.ffprobe, src)
        keep_audio = keep_audio and info["has_audio"]
        video_kbps, audio_kbps = plan_bitrate(target_mb, duration, keep_audio)
        if video_kbps < 50:
            raise ExportError("The target size is too small for this length. "
                              "Pick a bigger size or shorter sections.")

        base, last = segments[0][0], segments[-1][1]
        input_args = ["-ss", f"{base:.6f}", "-to", f"{last:.6f}", "-i", src]
        post = video_filters(info["height"], resolution)
        graph_v = build_filtergraph(segments, base, post, False)
        graph_va = build_filtergraph(segments, base, post, keep_audio)
        audio_args = (["-map", "[aout]", "-c:a", "aac", "-b:a", f"{audio_kbps}k"]
                      if keep_audio else ["-an"])

        workdir = tempfile.mkdtemp(prefix="clipt_")
        passlog = os.path.join(workdir, "pass")
        null_out = "NUL" if os.name == "nt" else "/dev/null"
        try:
            self._pass([self.ffmpeg, "-y", *input_args,
                        "-filter_complex", graph_v, "-map", "[vout]",
                        "-c:v", "libx264", "-b:v", f"{video_kbps}k",
                        "-pass", "1", "-passlogfile", passlog,
                        "-an", "-f", "mp4",
                        "-progress", "pipe:1", "-nostats", null_out],
                       duration, 0, 40, "Pass 1 of 2", progress)

            def pass2(kbps, label):
                self._pass([self.ffmpeg, "-y", *input_args,
                            "-filter_complex", graph_va, "-map", "[vout]",
                            "-c:v", "libx264", "-b:v", f"{kbps}k",
                            "-pass", "2", "-passlogfile", passlog,
                            *audio_args, "-movflags", "+faststart",
                            "-progress", "pipe:1", "-nostats", out_path],
                           duration, 40, 60, label, progress)

            pass2(video_kbps, "Pass 2 of 2")
            # x264 can overshoot a few percent, mostly on small frames where
            # the bitrate is generous; refit if the file came out too big.
            target_bytes = target_mb * 1024 * 1024
            for _ in range(2):
                size = os.path.getsize(out_path)
                if size <= target_bytes:
                    break
                video_kbps = int(video_kbps * target_bytes / size * 0.97)
                if video_kbps < 50:
                    break
                pass2(video_kbps, "Fitting to size")
        except BaseException:
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        if progress:
            progress(100.0, "Done")
        return os.path.getsize(out_path) / (1024 * 1024)
