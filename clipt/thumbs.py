"""Frame-strip thumbnails, pulled with ffmpeg in the background."""
import subprocess
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ThumbLoader(QObject):
    ready = Signal(int, int, QImage)   # generation, index, image

    def __init__(self, ffmpeg):
        super().__init__()
        self.ffmpeg = ffmpeg
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.generation = 0

    def start(self, path, duration, count):
        """Grab `count` evenly spaced frames. Starting again (new clip)
        makes any still-running jobs from the old one drop their results."""
        self.generation += 1
        gen = self.generation
        for i in range(count):
            t = (i + 0.5) * duration / count
            self.pool.submit(self._grab, gen, path, t, i)
        return gen

    def _grab(self, gen, path, t, index):
        if gen != self.generation:
            return
        # Keyframe-only decoding makes each grab near-instant; near the very
        # end there may be no keyframe left, so retry with a normal decode.
        for fast in (True, False):
            cmd = [self.ffmpeg, "-v", "error"]
            if fast:
                cmd += ["-skip_frame", "nokey"]
            cmd += ["-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                    "-vf", "scale=240:-2", "-f", "image2pipe",
                    "-vcodec", "mjpeg", "-q:v", "5", "-"]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=30,
                                   creationflags=NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                return
            img = QImage.fromData(r.stdout)
            if not img.isNull():
                if gen == self.generation:
                    self.ready.emit(gen, index, img)
                return

    def shutdown(self):
        self.generation += 1
        self.pool.shutdown(wait=False, cancel_futures=True)
