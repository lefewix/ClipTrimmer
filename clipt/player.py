"""Video playback: libmpv rendered into a Qt OpenGL widget.

Rendering through mpv's render API (instead of handing mpv a window handle)
keeps the video a normal Qt widget, so clicks, keys and overlays just work.
"""
import ctypes

import mpv
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget

_opengl32 = ctypes.WinDLL("opengl32")
_wglGetProcAddress = _opengl32.wglGetProcAddress
_wglGetProcAddress.restype = ctypes.c_void_p
_wglGetProcAddress.argtypes = [ctypes.c_char_p]
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.GetProcAddress.restype = ctypes.c_void_p
_kernel32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]


def _gl_proc(_ctx, name):
    # wglGetProcAddress only knows extension/GL>1.1 functions; the 1.1 core
    # ones come straight from opengl32.dll.
    addr = _wglGetProcAddress(name)
    if not addr or addr in (1, 2, 3) or addr == ctypes.c_void_p(-1).value:
        addr = _kernel32.GetProcAddress(_opengl32._handle, name)
    return addr or 0


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
            vo="libmpv",
            hwdec="auto-copy",            # GPU decode (HEVC/AV1), copied back for GL
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


class VideoView(QOpenGLWidget):
    clicked = Signal()
    _frame = Signal()

    def __init__(self, player, parent=None):
        super().__init__(parent)
        self.player = player
        self.ctx = None
        self._proc = mpv.MpvGlGetProcAddressFn(_gl_proc)
        self._frame.connect(self.update, Qt.ConnectionType.QueuedConnection)
        self.setMinimumSize(320, 180)

    def initializeGL(self):
        self.ctx = mpv.MpvRenderContext(
            self.player.mpv, "opengl",
            opengl_init_params={"get_proc_address": self._proc})
        self.ctx.update_cb = self._frame.emit

    def paintGL(self):
        if not self.ctx:
            return
        r = self.devicePixelRatioF()
        self.ctx.render(flip_y=True, opengl_fbo={
            "w": int(self.width() * r), "h": int(self.height() * r),
            "fbo": self.defaultFramebufferObject()})

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def free(self):
        if self.ctx:
            self.makeCurrent()
            self.ctx.update_cb = None
            self.ctx.free()
            self.ctx = None
            self.doneCurrent()
