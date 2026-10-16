"""ClipTrimmer main window."""
import ctypes
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject, QSettings, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QKeySequence, QPalette
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QMainWindow, QMessageBox, QPushButton,
                               QVBoxLayout, QWidget)

from . import library, theme
from .browser import ClipBrowser
from .engine import ExportCancelled, ExportError, Exporter, plan_bitrate, probe
from .model import Sections, fmt
from .player import Player, VideoView
from .rail import Rail
from .strip import THUMBS, Strip
from .thumbs import ThumbLoader

VERSION = "2.0"
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts", ".m4v"}
FRAME = 1 / 120


def find_tool(bin_dir, name):
    p = Path(bin_dir) / f"{name}.exe"
    return str(p) if p.is_file() else (shutil.which(name) or name)


def _button(text="", name=None, icon=None, tip=None):
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    if icon:
        b.setIcon(icon)
    if tip:
        b.setToolTip(tip)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def _label(text="", name=None):
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    return l


class ExportWorker(QThread):
    progress = Signal(float, str)
    done = Signal(bool, str, float)      # ok, output path or error, size MB

    def __init__(self, exporter, src, ranges, target_mb, out, resolution, audio):
        super().__init__()
        self.exporter = exporter
        self.args = (src, ranges, target_mb, out, resolution, audio)

    def run(self):
        src, ranges, mb, out, res, audio = self.args
        try:
            size = self.exporter.run(src, ranges, mb, out, res, audio,
                                     progress=lambda p, l: self.progress.emit(p, l))
            self.done.emit(True, out, size)
        except ExportCancelled:
            self.done.emit(False, "", 0.0)
        except ExportError as e:
            self.done.emit(False, str(e), 0.0)
        except Exception as e:      # keep the UI alive whatever happens
            self.done.emit(False, f"Unexpected error: {e}", 0.0)


class Stage(QWidget):
    """Holds the video view, sized to fill it."""

    def __init__(self, video, parent=None):
        super().__init__(parent)
        self.video = video
        video.setParent(self)

    def resizeEvent(self, e):
        self.video.setGeometry(self.rect())
        super().resizeEvent(e)


class Cover(QObject):
    """Keeps `top` laid over the whole of `host` (the clip browser over the editor)."""

    def __init__(self, host, top):
        super().__init__(host)
        self.top = top
        top.setParent(host)
        top.raise_()
        host.installEventFilter(self)

    def eventFilter(self, obj, e):
        if e.type() == QEvent.Type.Resize:
            self.top.setGeometry(obj.rect())
        return False


class MainWindow(QMainWindow):
    def __init__(self, bin_dir):
        super().__init__()
        self.settings = QSettings()
        self.ffmpeg = find_tool(bin_dir, "ffmpeg")
        self.ffprobe = find_tool(bin_dir, "ffprobe")
        self.exporter = Exporter(self.ffmpeg, self.ffprobe)
        self.model = Sections()
        self.player = Player()
        self.thumbs = ThumbLoader(self.ffmpeg)
        self.thumb_gen = 0
        self.path = None
        self.info = None
        self.duration = 0.0
        self.pos = 0.0
        self.mpv_pos = 0.0
        self.paused = True
        self.play_until = None
        self.scrubbing = False
        self.worker = None
        self.last_export = None
        self.out_dir_used = None
        self.clip_title, self.clip_meta = "No clip open", ""
        self.browser_stale = False       # an export happened: redraw the cards (Trimmed badges) next time
        self.exact_target = None
        self.exact_timer = QTimer(self, singleShot=True, interval=120)
        self.exact_timer.timeout.connect(self._exact_seek)

        self._build_ui()
        self._build_menus()
        self._restore_settings()
        self._connect()
        self.setAcceptDrops(True)
        self.setWindowTitle("ClipTrimmer")
        self.setMinimumSize(1000, 680)
        self.refresh()
        self._update_latest()
        self._browser_header()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(54)
        h = QHBoxLayout(header)
        h.setContentsMargins(24, 0, 24, 0)
        h.setSpacing(12)
        # One header for both screens. The nav button (top-left, like a back button) is the same
        # widget at the same size in both, so switching never moves it: "All clips" / "Back to clip".
        self.open_btn = _button("All clips", "navBack", theme.icon("prev", theme.TEXT, 12))
        fm = QFontMetrics(theme.sans(13, QFont.Weight.DemiBold))
        self.open_btn.setFixedWidth(max(fm.horizontalAdvance(t) for t in ("All clips", "Back to clip")) + 56)
        self.open_btn.clicked.connect(self.toggle_browser)
        h.addWidget(self.open_btn)
        self.nav_div = QFrame()
        self.nav_div.setObjectName("divider")
        self.nav_div.setFixedSize(1, 22)
        h.addWidget(self.nav_div)
        self.file_label = _label("No clip open", "fileName")
        self.meta_label = _label("", "meta")
        h.addWidget(self.file_label)
        h.addWidget(self.meta_label)
        h.addStretch()
        v.addWidget(header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)

        self.video = VideoView(self.player)
        self.video.setFocusPolicy(Qt.FocusPolicy.ClickFocus)    # clicking the video takes the keyboard back from the size box
        self.empty = ClipBrowser(self.ffmpeg, self.ffprobe, lambda: self.path)
        stage = Stage(self.video)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(24, 20, 24, 0)
        wl.addWidget(stage)
        lv.addWidget(wrap, 1)

        tr = QHBoxLayout()
        tr.setContentsMargins(24, 12, 24, 12)
        tr.setSpacing(12)
        self.in_btn = _button("Set In", "setIn", theme.icon("square", theme.GREEN, 12),
                              "Mark where the section starts (I)")
        self.out_btn = _button("Set Out", "setOut", theme.icon("square", theme.RED, 12),
                               "Mark where the section ends (O)")
        self.out_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.time_label = _label("0:00.00", "time")
        self.time_label.setFixedWidth(80)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.dur_label = _label("0:00.00", "duration")
        self.dur_label.setFixedWidth(80)
        self.prev_btn = _button("", "iconBtn", theme.icon("prev", theme.TEXT), "Previous frame (←)")
        self.next_btn = _button("", "iconBtn", theme.icon("next", theme.TEXT), "Next frame (→)")
        self.play_btn = _button("", "play", theme.icon("play", theme.BG, 14), "Play / pause (Space)")
        self.play_btn.setIconSize(QSize(14, 14))
        tr.addStretch()
        for w in (self.in_btn, self.time_label, self.prev_btn, self.play_btn,
                  self.next_btn, self.dur_label, self.out_btn):
            tr.addWidget(w)
        tr.addStretch()
        lv.addLayout(tr)

        self.strip = Strip(self.model)
        self.strip.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        sw = QWidget()
        sl = QVBoxLayout(sw)
        sl.setContentsMargins(24, 0, 24, 18)
        sl.addWidget(self.strip)
        lv.addWidget(sw)

        body.addWidget(left, 1)
        self.rail = Rail()
        body.addWidget(self.rail)
        editor = QWidget()
        editor.setLayout(body)
        v.addWidget(editor, 1)
        # The clip browser covers the editor (everything under the header) until a clip is picked.
        self.cover = Cover(editor, self.empty)

    def _build_menus(self):
        mb = self.menuBar()

        def act(menu, text, slot, *keys):
            a = QAction(text, self)
            if keys:
                a.setShortcuts([QKeySequence(k) for k in keys])
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        f = mb.addMenu("&File")
        act(f, "Open…", self.open_dialog, "Ctrl+O")
        self.a_latest = act(f, "Open latest replay", self.open_latest, "Ctrl+L")
        act(f, "All clips", self.show_browser, "Ctrl+B")
        self.recent_menu = f.addMenu("Open recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        f.addSeparator()
        self.a_export = act(f, "Export", self.export, "Ctrl+E")
        self.a_copy = act(f, "Copy exported file", self.copy_file, "Ctrl+Shift+C")
        f.addSeparator()
        act(f, "Output folder…", self.choose_out_dir)
        act(f, "Replays folder…", self.choose_replays_dir)
        f.addSeparator()
        act(f, "Exit", self.close)

        e = mb.addMenu("&Edit")
        self.a_undo = act(e, "Undo", self.undo, "Ctrl+Z")
        self.a_redo = act(e, "Redo", self.redo, "Ctrl+Y", "Ctrl+Shift+Z")
        e.addSeparator()
        self.a_in = act(e, "Set In", self.set_in, "I")
        self.a_out = act(e, "Set Out", self.set_out, "O")
        self.a_new = act(e, "New section at playhead", self.new_section, "N")
        self.a_del = act(e, "Delete section", self.delete_section, "Delete")
        self.a_clear = act(e, "Clear all sections", self.clear_sections)

        p = mb.addMenu("&Playback")
        self.a_play = act(p, "Play / pause", self.toggle_play, "Space", "K")
        self.a_play_sec = act(p, "Play section", lambda: self.play_section(self.model.active), "P")
        p.addSeparator()
        self.a_prev = act(p, "Previous frame", lambda: self.step(False), "Left", ",")
        self.a_next = act(p, "Next frame", lambda: self.step(True), "Right", ".")
        self.a_back = act(p, "Back 1 second", lambda: self.jump(-1), "Shift+Left", "J")
        self.a_fwd = act(p, "Forward 1 second", lambda: self.jump(1), "Shift+Right", "L")
        act(p, "Back to clip", self.hide_browser, "Esc")
        self.a_home = act(p, "Go to start", lambda: self.seek_to(0), "Home")
        self.a_end = act(p, "Go to end", lambda: self.seek_to(self.duration), "End")

        hm = mb.addMenu("&Help")
        act(hm, "Keyboard shortcuts", self.show_shortcuts, "F1")
        act(hm, "About ClipTrimmer", self.show_about)

        self.clip_actions = [self.a_export, self.a_in, self.a_out, self.a_new, self.a_del,
                             self.a_clear, self.a_play, self.a_play_sec, self.a_prev,
                             self.a_next, self.a_back, self.a_fwd, self.a_home, self.a_end]

    def _connect(self):
        self.player.position.connect(self.on_position)
        self.player.duration_changed.connect(self.on_duration)
        self.player.pause_changed.connect(self.on_pause)
        self.video.clicked.connect(self.toggle_play)
        self.empty.open_latest.connect(self.open_latest)
        self.empty.browse.connect(self.open_dialog)
        self.empty.change_folder.connect(self.choose_replays_dir)
        self.empty.open_clip.connect(self.load)
        self.empty.count_changed.connect(self._browser_header)
        self.empty.count_changed.connect(self._latest_label)
        self.in_btn.clicked.connect(self.set_in)
        self.out_btn.clicked.connect(self.set_out)
        self.play_btn.clicked.connect(self.toggle_play)
        self.prev_btn.clicked.connect(lambda: self.step(False))
        self.next_btn.clicked.connect(lambda: self.step(True))
        self.strip.scrubbed.connect(self.on_scrub)
        self.strip.edge_drag_started.connect(self.on_edge_start)
        self.strip.edge_dragged.connect(self.on_edge_drag)
        self.strip.edge_drag_finished.connect(self.on_edge_end)
        self.thumbs.ready.connect(self.on_thumb)
        r = self.rail
        r.select.connect(self.select_section)
        r.play_section.connect(self.play_section)
        r.remove.connect(self.remove_section)
        r.clear_all.connect(self.clear_sections)
        r.new_section.connect(self.new_section)
        r.export.connect(self.export)
        r.cancel_export.connect(self.exporter.cancel)
        r.copy_file.connect(self.copy_file)
        r.show_in_folder.connect(self.show_in_folder)
        r.settings_changed.connect(self.on_settings_changed)

    # ------------------------------------------------------------ settings
    def _default_out(self):
        d = Path.home() / "Desktop"
        return str(d if d.is_dir() else Path.home())

    def out_dir(self):
        d = Path(self.settings.value("out_dir", self._default_out()))
        return d if d.is_dir() else Path(self._default_out())

    def replays_dir(self):
        return Path(self.settings.value("replays_dir", str(Path.home() / "Videos" / "OBS")))

    def _restore_settings(self):
        self.rail.set_target(int(self.settings.value("target_mb", 10)))
        self.rail.set_resolution(int(self.settings.value("resolution", 0)))
        self.rail.audio.setChecked(self.settings.value("keep_audio", "true") in (True, "true"))
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 820)

    def on_settings_changed(self):
        self.settings.setValue("target_mb", self.rail.target_mb())
        self.settings.setValue("resolution", self.rail.resolution())
        self.settings.setValue("keep_audio", self.rail.audio.isChecked())
        if not self.worker and self.rail.showing_result:      # new settings: back to the Export button
            self.rail.hide_cards()
        self.refresh()

    def recent(self):
        r = self.settings.value("recent", [])
        if isinstance(r, str):
            r = [r]
        return [p for p in (r or []) if os.path.isfile(p)]

    def _fill_recent(self):
        self.recent_menu.clear()
        files = self.recent()
        if not files:
            a = self.recent_menu.addAction("No recent clips")
            a.setEnabled(False)
        for p in files:
            self.recent_menu.addAction(Path(p).name, lambda p=p: self.load(p))

    def choose_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Save exported clips to", str(self.out_dir()))
        if d:
            self.settings.setValue("out_dir", d)

    def choose_replays_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Replays folder", str(self.replays_dir()))
        if d:
            self.settings.setValue("replays_dir", d)
            self._update_latest()

    def latest_clip(self):
        clips = library.list_clips(self.replays_dir())      # skips cover temp files
        return clips[0] if clips else None

    def latest_replay(self):
        c = self.latest_clip()
        return c.path if c else None

    def _update_latest(self, force=False):
        self.empty.show_folder(self.replays_dir(), self.out_dir(), force)
        self._latest_label()

    def _latest_label(self, *_):
        """The "Open latest replay · Today 2:49 PM" button; also kept current as replays arrive."""
        c = self.latest_clip()
        self.empty.set_latest(library.when_label(c.when) if c else None)
        self.a_latest.setEnabled(bool(c))

    # ------------------------------------------------------------- browser
    def show_browser(self):
        if self.path and not self.paused:
            self.player.pause()
        self._update_latest(force=self.browser_stale)
        self.browser_stale = False
        self.empty.mark_open()
        self.empty.show()
        self.empty.raise_()
        self._browser_header()
        self.refresh()          # editing shortcuts (Delete, Space, I/O...) are off while the clip is hidden

    def hide_browser(self):
        if self.path:
            self.empty.hide()
            self._editor_header()
            self.refresh()

    def toggle_browser(self):
        if not self.empty.isHidden():
            self.hide_browser()
        else:
            self.show_browser()

    def _browser_header(self, *_):
        if self.empty.isHidden():      # (isHidden, not isVisible: this also runs before the window is shown)
            return
        has_clip = bool(self.path)
        self.open_btn.setText("Back to clip")
        self.open_btn.setToolTip("Back to the clip you were trimming (Esc)")
        self.open_btn.setVisible(has_clip)
        self.nav_div.setVisible(has_clip)
        n = self.empty.count
        self.file_label.setText("Your clips")
        self.meta_label.setText(f"{n} replay{'' if n == 1 else 's'}")

    def _editor_header(self):
        self.open_btn.setText("All clips")
        self.open_btn.setToolTip("Back to your clips (Ctrl+B)")
        self.open_btn.setVisible(True)
        self.nav_div.setVisible(True)
        self.file_label.setText(self.clip_title)
        self.meta_label.setText(self.clip_meta)

    def bring_up(self, path=""):
        """Another launch (e.g. holding the PadDeck button) hands over to this window."""
        if self.isMinimized():      # keep maximized windows maximized
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        if path and os.path.isfile(path):
            self.load(path)
        elif not self.worker:
            self.show_browser()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------- loading
    def open_dialog(self):
        start = str(self.replays_dir() if self.replays_dir().is_dir() else Path.home())
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXT))
        path, _ = QFileDialog.getOpenFileName(self, "Open clip", start,
                                              f"Videos ({exts});;All files (*)")
        if path:
            self.load(path)

    def open_latest(self):
        p = self.latest_replay()
        if p:
            self.load(str(p))

    def load(self, path):
        if self.worker:
            QMessageBox.information(self, "Export running",
                                    "Wait for the export to finish before opening another clip.")
            return
        try:
            info = probe(self.ffprobe, path)
        except ExportError as e:
            QMessageBox.warning(self, "Can't open this file", str(e))
            return
        self.path, self.info = path, info
        self.duration = info["duration"]
        self.model.reset()
        self.play_until = None
        self.last_export = None
        self.rail.hide_cards()
        self.strip.set_duration(self.duration)
        self.strip.clear_thumbs()
        self.thumb_gen = self.thumbs.start(path, self.duration, THUMBS)
        self.player.load(path)
        self.set_ui_pos(0.0)
        name = Path(path).stem
        fps = info["fps"]
        fps_s = f"{fps:.0f}" if abs(fps - round(fps)) < 0.05 else f"{fps:.2f}"
        self.clip_title = name
        self.clip_meta = (f"{fmt(self.duration)[:-3]} · {info['height']}p · {fps_s} fps · "
                          f"{info['size'] / 1048576:.0f} MB")
        self.dur_label.setText(fmt(self.duration))
        self.empty.hide()
        self._editor_header()
        rec = [path] + [p for p in self.recent() if os.path.normcase(p) != os.path.normcase(path)]
        self.settings.setValue("recent", rec[:10])
        self.setWindowTitle(f"{name} — ClipTrimmer")
        self.refresh()

    def on_thumb(self, gen, index, image):
        if gen == self.thumb_gen:
            self.strip.set_thumb(index, image)
            self.rail.set_sections(self.model.items, self.model.active, self.strip.thumb_at)

    # ------------------------------------------------------------ playback
    def on_position(self, t):
        self.mpv_pos = t
        if self.scrubbing or not self.path:
            return
        if self.play_until is not None and not self.paused and t >= self.play_until - FRAME:
            stop = self.play_until
            self.play_until = None
            self.player.pause()
            self.player.seek(stop)
            t = stop
        self.set_ui_pos(t)

    def on_duration(self, d):
        if self.path and d > 0:
            self.duration = d
            self.strip.set_duration(d)
            self.dur_label.setText(fmt(d))

    def on_pause(self, paused):
        self.paused = paused
        self.play_btn.setIcon(theme.icon("play" if paused else "pause", theme.BG, 14))
        if paused:
            self.play_until = None

    def set_ui_pos(self, t):
        self.pos = t
        self.time_label.setText(fmt(t))
        self.strip.set_position(t)

    def toggle_play(self):
        if not self.path:
            return
        if self.paused:
            if self.pos >= self.duration - 0.05:
                self.player.seek(0)
            self.player.play()
        else:
            self.play_until = None
            self.player.pause()

    def seek_to(self, t, exact=True):
        if not self.path:
            return
        t = min(max(t, 0.0), self.duration)
        self.set_ui_pos(t)
        self.player.seek(t, exact)

    def step(self, forward):
        """One frame. mpv's own frame-step is instant but gets lost if a seek
        is still in flight, so then (and for going back) seek to the
        neighbouring frame from the time shown on screen instead."""
        if not self.path:
            return
        self.play_until = None
        fps = self.info["fps"] or 60
        if forward and self.paused and not self.player.seeking \
                and abs(self.mpv_pos - self.pos) < 0.5 / fps:
            self.player.frame_step(True)
            return
        self.player.pause()
        t = min(max(self.pos + (1 if forward else -1) / fps, 0.0), self.duration)
        self.set_ui_pos(t)
        # exact seeks show the first frame at or after the target
        self.player.seek(max(0.0, t - 0.25 / fps), True)

    def jump(self, delta):
        self.play_until = None
        self.player.pause()
        self.seek_to(self.pos + delta)

    def _exact_seek(self):
        if self.exact_target is not None:
            self.player.seek(self.exact_target, True)

    def _scrub_seek(self, t, final):
        """Fast keyframe seeks while the mouse moves, an exact one when it
        rests or lets go, so dragging stays smooth on long-GOP footage."""
        self.set_ui_pos(t)
        self.exact_target = t
        if final:
            self.exact_timer.stop()
            self.player.seek(t, True)
        else:
            self.player.seek(t, False)
            self.exact_timer.start()

    def on_scrub(self, t, final):
        self.play_until = None
        if not self.paused:
            self.player.pause()
        self.scrubbing = not final
        self._scrub_seek(t, final)

    def on_edge_start(self, idx, _which):
        self.play_until = None
        self.player.pause()
        self.scrubbing = True
        self.model.checkpoint()
        self.model.select(idx)
        self.edited()

    def on_edge_drag(self, idx, which, t):
        t = self.model.move_edge(idx, which, t, self.duration)
        self._scrub_seek(t, False)
        self.edited()

    def on_edge_end(self, idx, which):
        self.scrubbing = False
        a, b = self.model.items[idx]
        self._scrub_seek(a if which == "in" else b, True)
        self.edited()

    # ------------------------------------------------------------ sections
    def edited(self):
        if not self.worker and self.rail.showing_result:
            self.rail.hide_cards()
        self.refresh()

    def set_in(self):
        if self.path:
            self.model.set_in(self.pos)
            self.edited()

    def set_out(self):
        if self.path:
            self.model.set_out(self.pos)
            self.edited()

    def new_section(self):
        if self.path:
            self.model.new_at(self.pos)
            self.edited()

    def delete_section(self):
        if self.model.items:
            self.model.remove(self.model.active)
            self.edited()

    def remove_section(self, idx):
        self.model.remove(idx)
        self.edited()

    def clear_sections(self):
        self.model.clear()
        self.edited()

    def select_section(self, idx):
        self.model.select(idx)
        a, b = self.model.items[idx]
        t = a if a is not None else b
        self.play_until = None
        self.player.pause()
        if t is not None:
            self.seek_to(t)
        self.refresh()

    def play_section(self, idx):
        if not 0 <= idx < len(self.model.items):
            return
        a, b = self.model.items[idx]
        if a is None:
            return
        self.model.select(idx)
        self.seek_to(a)
        self.play_until = b if b is not None else self.duration
        self.player.play()
        self.refresh()

    def undo(self):
        if self.model.undo():
            self.edited()

    def redo(self):
        if self.model.redo():
            self.edited()

    # ------------------------------------------------------------- refresh
    def refresh(self):
        loaded = bool(self.path)
        editing = loaded and self.empty.isHidden()      # not while the clip list covers the editor
        for w in (self.in_btn, self.out_btn, self.play_btn, self.prev_btn, self.next_btn):
            w.setEnabled(loaded)
        for a in self.clip_actions:
            a.setEnabled(editing)
        self.a_undo.setEnabled(self.model.can_undo())
        self.a_redo.setEnabled(self.model.can_redo())
        self.a_copy.setEnabled(bool(self.last_export))
        self.rail.setEnabled(loaded)
        self.strip.update()
        self.rail.set_sections(self.model.items, self.model.active, self.strip.thumb_at)
        if not loaded:
            self.rail.set_summary(0, 0)
            self.rail.bitrate.set_kbps(None)
            return
        ranges = self.model.ranges(self.duration)
        total = sum(e - s for s, e in ranges)
        length = total if ranges else self.duration
        self.rail.set_summary(len(self.model.items) if ranges else 0, total)
        audio = self.rail.audio.isChecked() and self.info["has_audio"]
        kbps, _ = plan_bitrate(self.rail.target_mb(), length, audio)
        self.rail.bitrate.set_kbps(kbps)

    # -------------------------------------------------------------- export
    def export(self):
        if not self.path or self.worker:
            return
        ranges = self.model.ranges(self.duration) or [[0.0, self.duration]]
        out_dir = self.out_dir()
        stem = Path(self.path).stem
        out = out_dir / f"{stem}_trimmed.mp4"
        n = 1
        while out.exists():
            out = out_dir / f"{stem}_trimmed_{n}.mp4"
            n += 1
        self.play_until = None
        self.player.pause()
        self.out_dir_used = out_dir
        self.worker = ExportWorker(self.exporter, self.path, ranges, self.rail.target_mb(),
                                   str(out), self.rail.resolution(), self.rail.audio.isChecked())
        self.worker.progress.connect(self.rail.show_progress)
        self.worker.done.connect(self.on_export_done)
        self.rail.show_progress(0, "Starting")
        self.worker.start()

    def on_export_done(self, ok, msg, size):
        self.worker.wait()
        self.worker = None
        if ok:
            self.last_export = msg
            self.browser_stale = True
            d = self.out_dir_used
            where = "Desktop" if d and d == Path.home() / "Desktop" else (d.name if d else "folder")
            self.rail.show_saved(where, size)
        else:
            self.rail.hide_cards()
            if msg:
                QMessageBox.warning(self, "Export failed", msg)
        self.refresh()

    def copy_file(self):
        if not self.last_export or not os.path.isfile(self.last_export):
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(self.last_export)])
        QApplication.clipboard().setMimeData(md)
        self.rail.mark_copied()

    def show_in_folder(self):
        if self.last_export and os.path.isfile(self.last_export):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(self.last_export)}"')

    # --------------------------------------------------------------- misc
    def show_shortcuts(self):
        rows = [("I", "Set In"), ("O", "Set Out"), ("N", "New section at playhead"),
                ("Delete", "Delete the selected section"), ("Space / K", "Play / pause"),
                ("P", "Play the selected section"), ("← / ,", "Previous frame"),
                ("→ / .", "Next frame"), ("Shift+← / J", "Back 1 second"),
                ("Shift+→ / L", "Forward 1 second"), ("Home / End", "Start / end of clip"),
                ("Ctrl+Z / Ctrl+Y", "Undo / redo"), ("Ctrl+E", "Export"),
                ("Ctrl+Shift+C", "Copy the exported file"), ("Ctrl+O / Ctrl+L", "Open / open latest replay")]
        html = "<table cellspacing='6'>" + "".join(
            f"<tr><td style='font-family:\"{theme.MONO}\"; color:{theme.TEXT2}; padding-right:16px'>{k}</td>"
            f"<td>{d}</td></tr>" for k, d in rows) + "</table>"
        QMessageBox.information(self, "Keyboard shortcuts", html)

    def show_about(self):
        QMessageBox.about(self, "About ClipTrimmer",
                          f"<b>ClipTrimmer {VERSION}</b><br>Cut moments out of game clips "
                          "and fit them to a size.<br><br>Playback by mpv, encoding by FFmpeg.")

    def dragEnterEvent(self, e):
        if any(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in VIDEO_EXT
               for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in VIDEO_EXT:
                self.load(u.toLocalFile())
                return

    def closeEvent(self, e):
        if self.worker:
            if QMessageBox.question(self, "Export running",
                                    "An export is still running. Stop it and quit?") \
                    != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
            self.exporter.cancel()
            self.worker.wait(5000)
        self.settings.setValue("geometry", self.saveGeometry())
        self.thumbs.shutdown()
        self.empty.shutdown()
        self.video.free()
        self.player.terminate()
        e.accept()


def _dark_title_bar(win):
    """Windows 11: dark caption that matches the app background."""
    try:
        hwnd = int(win.winId())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)          # immersive dark mode
        color = ctypes.c_int(0x000B0B0B)                                   # COLORREF (BGR)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)       # caption colour
    except Exception:
        pass


def setup_app(argv, font_dir, org="ClipTrimmer"):
    """QApplication with the dark theme, fonts and icon. `org` picks the
    settings store (tests use their own so they don't touch yours)."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ClipTrimmer.App")
    except Exception:
        pass
    app = QApplication(argv)
    app.setApplicationName("ClipTrimmer")
    app.setOrganizationName(org)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, c in ((QPalette.ColorRole.Window, theme.BG), (QPalette.ColorRole.WindowText, theme.TEXT),
                    (QPalette.ColorRole.Base, theme.BG), (QPalette.ColorRole.AlternateBase, theme.SURFACE),
                    (QPalette.ColorRole.Text, theme.TEXT), (QPalette.ColorRole.Button, theme.SURFACE),
                    (QPalette.ColorRole.ButtonText, theme.TEXT), (QPalette.ColorRole.Highlight, "#2E2E2E"),
                    (QPalette.ColorRole.HighlightedText, theme.TEXT), (QPalette.ColorRole.ToolTipBase, theme.SURFACE),
                    (QPalette.ColorRole.ToolTipText, theme.TEXT)):
        pal.setColor(role, QColor(c))
    app.setPalette(pal)
    theme.load_fonts(font_dir)
    app.setFont(theme.sans(13))
    app.setStyleSheet(theme.stylesheet())
    app.setWindowIcon(QIcon(theme.app_icon_pixmap()))
    return app


INSTANCE = "ClipTrimmer-" + os.environ.get("USERNAME", "")


def _focus(hwnd):
    """Bring another process's window to the front. Windows only lets the app that owns the
    foreground do that, so this briefly shares input with it (no fake key presses, which
    could land in a game)."""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        fg = u.GetForegroundWindow()
        me = ctypes.windll.kernel32.GetCurrentThreadId()
        other = u.GetWindowThreadProcessId(ctypes.c_void_p(fg), None) if fg else 0
        if other and other != me:
            u.AttachThreadInput(me, other, True)
        u.ShowWindow(ctypes.c_void_p(hwnd), 5)            # SW_SHOW
        u.SetForegroundWindow(ctypes.c_void_p(hwnd))
        u.BringWindowToTop(ctypes.c_void_p(hwnd))
        if other and other != me:
            u.AttachThreadInput(me, other, False)
    except Exception:
        pass


def _first_instance():
    """True for the first ClipTrimmer; the mutex lives as long as this process."""
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    _first_instance.handle = k.CreateMutexW(None, False, INSTANCE.replace("ClipTrimmer-", "Local\\ClipTrimmer.Single."))
    return k.GetLastError() != 183                     # ERROR_ALREADY_EXISTS


def _hand_over(argv):
    """If ClipTrimmer is already open, pass it the clip (or just bring it up) and return True."""
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE)
    if not sock.waitForConnected(500):
        return False
    path = os.path.abspath(argv[1]) if len(argv) > 1 and os.path.isfile(argv[1]) else ""
    sock.write((path + "\n").encode("utf-8"))
    sock.flush()
    if sock.waitForReadyRead(3000):
        try:
            _focus(int(bytes(sock.readAll().data()).decode() or 0))
        except ValueError:
            pass
    sock.disconnectFromServer()
    return True


def _serve(holder):
    """Listen for later launches. Starts before the window exists (so two quick launches
    can't both open one); requests are handled once holder["win"] is set."""
    server = QLocalServer()
    QLocalServer.removeServer(INSTANCE)
    server.listen(INSTANCE)

    def on_connection():
        c = server.nextPendingConnection()
        path = ""
        if c.bytesAvailable() or c.waitForReadyRead(1000):
            path = bytes(c.readAll().data()).decode("utf-8", "replace").strip()
        win = holder.get("win")
        c.write(str(int(win.winId()) if win else 0).encode())   # reply first, so the other launch can focus us
        c.flush()
        c.disconnectFromServer()
        if win:
            QTimer.singleShot(0, lambda: win.bring_up(path))     # never block inside the handler (dialogs)

    server.newConnection.connect(on_connection)
    return server


def run_covers(bin_dir, files):
    """ClipTrimmer.exe --covers [clip ...]: give clips their cover picture without opening
    a window. Named clips (e.g. just saved by PadDeck) are waited for; with none, the
    whole replays folder is done."""
    ff, fp = find_tool(bin_dir, "ffmpeg"), find_tool(bin_dir, "ffprobe")
    if files:
        return library.add_covers(ff, fp, files, settle=1, retries=4)
    folder = QSettings("ClipTrimmer", "ClipTrimmer").value("replays_dir", str(Path.home() / "Videos" / "OBS"))
    library.sweep_temps(folder)
    return library.add_covers(ff, fp, [c.path for c in library.list_clips(folder)])


def main(bin_dir, font_dir, argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) > 1 and argv[1] == "--covers":
        run_covers(bin_dir, argv[2:])
        return 0
    app = setup_app(argv, font_dir)
    if not _first_instance():
        # Another ClipTrimmer is open (or just starting): hand over to it.
        for _ in range(40):
            if _hand_over(argv):
                return 0
            QThread.msleep(250)
        return 0
    holder = {}
    server = _serve(holder)
    win = MainWindow(bin_dir)
    holder["win"] = win
    win.show()
    _dark_title_bar(win)
    if len(argv) > 1 and os.path.isfile(argv[1]):
        QTimer.singleShot(0, lambda: win.load(argv[1]))
    code = app.exec()
    server.close()
    return code
