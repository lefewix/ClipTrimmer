"""Video playback: libmpv drawing straight into a native child window with Direct3D 11.

D3D11 lets the GPU decoder hand frames to the renderer without copying them
back to system memory, which uses far less RAM and CPU than OpenGL (whose AMD
driver alone costs ~250 MB). mpv's window lets clicks pass through to the
Qt widget underneath, so clicks and keys still stay in Qt.
"""
import mpv
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from . import theme


class Player(QObject):
    position = Signal(float)
    duration_changed = Signal(float)
    pause_changed = Signal(bool)
    loaded = Signal()
    _restarted = Signal()

    def __init__(self):
        super().__init__()
        self.seeking = False      # True from seek() until mpv shows the new frame
        self._restarted.connect(self._seek_done)
        self.mpv = mpv.MPV(
            vo="gpu-next",
            gpu_api="d3d11",
            hwdec="auto-safe",            # GPU decode (HEVC/AV1), frames stay on the GPU
            input_cursor_passthrough=True,  # mpv's window ignores the mouse; Qt gets the clicks
            cursor_autohide="no",
            keep_open="always",           # stay on the last frame at the end
            hr_seek="yes",                # exact seeks land on the exact frame
            hr_seek_framedrop="yes",
            idle="yes",
            pause=True,
            input_default_bindings=False,
            input_vo_keyboard=False,
            osc=False,
            audio_display="no",
            terminal=False,
        )
        try:
            self.mpv["background-color"] = "#0B0B0B"
        except Exception:
            pass
        self.mpv.observe_property("time-pos", self._on_time)
        self.mpv.observe_property("duration", self._on_duration)
        self.mpv.observe_property("pause", self._on_pause)
        self.mpv.event_callback("file-loaded")(lambda _e: self.loaded.emit())
        self.mpv.event_callback("playback-restart")(lambda _e: self._restarted.emit())

    def _seek_done(self):
        self.seeking = False

    # mpv calls these from its own thread; Qt queues the signals for us.
    def _on_time(self, _name, value):
        if value is not None:
            self.position.emit(float(value))

    def _on_duration(self, _name, value):
        if value:
            self.duration_changed.emit(float(value))

    def _on_pause(self, _name, value):
        self.pause_changed.emit(bool(value))

    def load(self, path):
        self.mpv.pause = True
        self.mpv.loadfile(path)

    def play(self):
        self.mpv.pause = False

    def pause(self):
        self.mpv.pause = True

    def is_paused(self):
        return bool(self.mpv.pause)

    def set_volume(self, percent, muted=False):
        try:
            self.mpv.volume = max(0, min(100, percent))
            self.mpv.mute = muted
        except (mpv.ShutdownError, SystemError, OSError):
            pass

    def seek(self, t, exact=True):
        self.seeking = True
        try:
            self.mpv.seek(max(0.0, t), reference="absolute",
                          precision="exact" if exact else "keyframes")
        except (mpv.ShutdownError, SystemError, OSError):
            pass

    def frame_step(self, forward=True):
        try:
            if forward:
                self.mpv.frame_step()
            else:
                self.mpv.frame_back_step()
        except (mpv.ShutdownError, SystemError, OSError):
            pass

    def stop(self):
        try:
            self.mpv.command("stop")
        except (mpv.ShutdownError, SystemError, OSError):
            pass

    def terminate(self):
        try:
            self.mpv.terminate()
        except Exception:
            pass


class VideoView(QWidget):
    """A native window for mpv to draw into. Being native, it always sits on top of
    ordinary widgets, so the app hides it while the clip browser covers the editor."""
    clicked = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self._attached = False
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 180)

    def attach(self):
        """Give mpv this window. Call once the widget is in the window, before the first load."""
        if not self._attached:
            self.player.mpv.wid = str(int(self.winId()))
            self._attached = True

    def paintEvent(self, _e):
        # Only seen before the first clip, or between clips; mpv's window covers it otherwise.
        QPainter(self).fillRect(self.rect(), QColor(theme.BG))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
