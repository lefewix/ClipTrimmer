"""The right-hand panel: section list and export settings."""
from PySide6.QtCore import QLocale, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIntValidator, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QCheckBox,
                               QComboBox, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QStackedWidget, QVBoxLayout, QWidget)

from . import theme
from .model import fmt, secs

PRESETS = [(10, "Discord"), (50, "Nitro"), (512, "X"), (0, "Custom")]
RESOLUTIONS = [("Original", 0), ("1080p", 1080), ("720p", 720), ("480p", 480)]


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


class SegButton(QAbstractButton):
    """One cell of the size picker: bold size over a small caption."""

    def __init__(self, top, bottom, parent=None):
        super().__init__(parent)
        self.top, self.bottom = top, bottom
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(60, 44)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        if self.isChecked() or self.underMouse():
            path = QPainterPath()
            path.addRoundedRect(r, 6, 6)
            p.fillPath(path, QColor("#2E2E2E" if self.isChecked() else "#1A1A1A"))
        p.setPen(QColor(theme.TEXT if self.isChecked() else theme.TEXT2))
        p.setFont(theme.sans(12, QFont.Weight.DemiBold))
        p.drawText(QRectF(r.left(), r.top() + 6, r.width(), 18), Qt.AlignmentFlag.AlignCenter, self.top)
        p.setPen(QColor(theme.TEXT2 if self.isChecked() else theme.MUTED))
        p.setFont(theme.sans(10))
        p.drawText(QRectF(r.left(), r.top() + 22, r.width(), 16), Qt.AlignmentFlag.AlignCenter, self.bottom)

    def enterEvent(self, e):
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.update()
        super().leaveEvent(e)


class MbBox(QLineEdit):
    """Whole megabytes, typed (or scrolled). A QSpinBox can't be used here: with the app's
    style sheet and no arrow buttons, Qt shrinks its text field to 1 px."""
    valueChanged = Signal(int)

    def __init__(self, lo, hi, value, parent=None):
        super().__init__(parent)
        self.lo, self.hi = lo, hi
        v = QIntValidator(lo, hi, self)
        v.setLocale(QLocale.c())          # digits only: no "1,000" that silently reverts
        self.setValidator(v)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setObjectName("mbBox")
        self._last = None
        self.setValue(value)
        self.textChanged.connect(self._changed)


    def value(self):
        try:
            return min(self.hi, max(self.lo, int(self.text())))
        except ValueError:
            return self._last or self.lo

    def setValue(self, v):
        v = min(self.hi, max(self.lo, int(v)))
        if self.text() != str(v):
            self.setText(str(v))
        self._changed()

    def _changed(self, *_):
        if self.text().isdigit() and self.value() != self._last:
            self._last = self.value()
            self.valueChanged.emit(self._last)

    def keyPressEvent(self, e):
        # Enter finishes the edit and hands the keyboard back, so I / O / Space work again.
        # (Qt's returnPressed doesn't fire for not-quite-valid text like "0", so it's done here.)
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            self.setValue(self.value())
            self.clearFocus()
            e.accept()
            return
        super().keyPressEvent(e)

    def focusOutEvent(self, e):
        # Always show the size that's actually used ("0" → "1", empty → the last size).
        self.setValue(self.value())
        super().focusOutEvent(e)

    def wheelEvent(self, e):
        step = 1 if e.angleDelta().y() > 0 else -1
        self.setValue(self.value() + step * (10 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1))
        e.accept()


class BitrateMeter(QFrame):
    """'Estimated bitrate' with a red→amber→green bar from lower to higher quality
    (0–8000 kbps), as in ClipTrimmer 1."""
    TOP = 8000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 9)
        v.setSpacing(7)
        top = QHBoxLayout()
        top.addWidget(_label("Estimated bitrate", "muted"))
        top.addStretch()
        self.value = _label("— kbps", "bitrateValue")
        top.addWidget(self.value)
        v.addLayout(top)
        self.bar = _MeterBar()
        v.addWidget(self.bar)
        scale = QHBoxLayout()
        scale.addWidget(_label("Lower quality", "scaleLabel"))
        scale.addStretch()
        scale.addWidget(_label("Higher", "scaleLabel"))
        v.addLayout(scale)

    def set_kbps(self, kbps):
        """None = no clip (bar half way, like before one is open)."""
        if kbps is None:
            self.value.setText("— kbps")
            self.bar.set_fill(0.5)
        elif kbps < 50:
            self.value.setText("too small")
            self.bar.set_fill(0.0)
        else:
            self.value.setText(f"{kbps:,} kbps")
            self.bar.set_fill(min(1.0, kbps / self.TOP))


class _MeterBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fill = 0.5
        self.setFixedHeight(7)

    def set_fill(self, f):
        self.fill = f
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, 3.5, 3.5)
        p.setClipPath(path)
        g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0.0, QColor(theme.RED))
        g.setColorAt(0.48, QColor(theme.AMBER))
        g.setColorAt(1.0, QColor(theme.GREEN))
        p.fillRect(r, g)
        x = r.left() + r.width() * self.fill
        p.fillRect(QRectF(x, r.top(), r.right() - x + 1, r.height()), QColor(theme.LINE2))   # the empty part of the track
        if 0 < self.fill < 1:
            p.fillRect(QRectF(x - 1, r.top(), 2, r.height()), QColor(theme.TEXT))


class SectionRow(QFrame):
    selected = Signal(int)
    play = Signal(int)
    remove = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.index = 0
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 8, 6, 8)
        lay.setSpacing(10)
        self.thumb = QLabel()
        self.thumb.setFixedSize(56, 40)
        self.thumb.setStyleSheet("background: #1E1E1E; border-radius: 4px;")
        lay.addWidget(self.thumb)

        col = QVBoxLayout()
        col.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.title = _label("", "h2")
        self.length = _label("", "monoMuted")
        top.addWidget(self.title)
        top.addWidget(self.length)
        top.addStretch()
        col.addLayout(top)
        times = QHBoxLayout()
        times.setSpacing(5)
        self.in_sq = _label()
        self.in_sq.setFixedSize(8, 8)
        self.in_sq.setStyleSheet(f"background: {theme.GREEN}; border-radius: 2px;")
        self.in_t = _label("", "monoSmall")
        self.out_sq = _label()
        self.out_sq.setFixedSize(8, 8)
        self.out_sq.setStyleSheet(f"background: {theme.RED}; border-radius: 2px;")
        self.out_t = _label("", "monoSmall")
        times.addWidget(self.in_sq)
        times.addWidget(self.in_t)
        times.addSpacing(8)
        times.addWidget(self.out_sq)
        times.addWidget(self.out_t)
        times.addStretch()
        col.addLayout(times)
        lay.addLayout(col, 1)

        self.play_btn = _button("", "rowPlay", theme.icon("play", theme.TEXT, 10), "Play this section")
        self.play_btn.setIconSize(QSize(10, 10))
        self.x_btn = _button("", "rowX", theme.icon("x", theme.MUTED, 12), "Remove section")
        self.x_btn.setIconSize(QSize(12, 12))
        lay.addWidget(self.play_btn)
        lay.addWidget(self.x_btn)
        self.play_btn.clicked.connect(lambda: self.play.emit(self.index))
        self.x_btn.clicked.connect(lambda: self.remove.emit(self.index))

    def set_data(self, index, a, b, active, pixmap):
        self.index = index
        self.title.setText(f"Section {index + 1}")
        # A missing In exports from the start of the clip, a missing Out to the end.
        if a is not None and b is not None:
            self.length.setText(secs(b - a))
        else:
            self.length.setText("to the end" if a is not None else "from the start")
        self.in_t.setText(fmt(a) if a is not None else "start")
        self.out_t.setText(fmt(b) if b is not None else "end")
        self.play_btn.setEnabled(a is not None)
        if pixmap:
            self.thumb.setPixmap(pixmap.scaled(56, 40, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                               Qt.TransformationMode.SmoothTransformation)
                                 .copy(0, 0, 56, 40))
        else:
            self.thumb.clear()
        if self.property("active") != active:
            self.setProperty("active", active)
            self.style().unpolish(self)
            self.style().polish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.selected.emit(self.index)


class Rail(QFrame):
    select = Signal(int)
    play_section = Signal(int)
    remove = Signal(int)
    clear_all = Signal()
    new_section = Signal()
    export = Signal()
    cancel_export = Signal()
    copy_file = Signal()
    show_in_folder = Signal()
    settings_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rail")
        self.setFixedWidth(340)
        self.rows = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(_label("Sections", "h2"))
        self.summary = _label("", "muted")
        head.addWidget(self.summary)
        head.addStretch()
        self.clear_btn = _button("Clear all", "link")
        self.clear_btn.clicked.connect(self.clear_all)
        head.addWidget(self.clear_btn)
        lay.addLayout(head)

        self.list_host = QWidget()
        self.list_lay = QVBoxLayout(self.list_host)
        self.list_lay.setContentsMargins(0, 0, 0, 0)
        self.list_lay.setSpacing(6)
        self.empty_note = _label("No sections yet. Scrub to where the moment starts and press "
                                 f"<span style='color:{theme.GREEN}; font-family:\"{theme.MONO}\"'>I</span>, "
                                 "then to where it ends and press "
                                 f"<span style='color:{theme.RED}; font-family:\"{theme.MONO}\"'>O</span>.",
                                 "hint")
        self.empty_note.setObjectName("emptyNote")
        self.empty_note.setWordWrap(True)
        self.empty_note.setTextFormat(Qt.TextFormat.RichText)
        self.empty_note.setContentsMargins(12, 12, 12, 12)
        self.empty_note.setStyleSheet(f"color: {theme.TEXT2}; font-size: 12px;")
        self.list_lay.addWidget(self.empty_note)
        self.new_btn = _button("+ New section", "dashed")
        self.new_btn.clicked.connect(self.new_section)
        self.list_lay.addWidget(self.new_btn)
        self.list_lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.list_host)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(scroll, 1)

        div = QFrame()
        div.setObjectName("divider")
        div.setFixedHeight(1)
        lay.addWidget(div)

        lay.addWidget(_label("Export", "h2"))
        seg = QFrame()
        seg.setObjectName("segGroup")
        seg_lay = QHBoxLayout(seg)
        seg_lay.setContentsMargins(3, 3, 3, 3)
        seg_lay.setSpacing(3)
        self.size_group = QButtonGroup(self)
        for mb, name in PRESETS:
            b = SegButton(f"{mb} MB" if mb else "Custom", name if mb else "MB")
            b.setProperty("mb", mb)
            self.size_group.addButton(b)
            seg_lay.addWidget(b)
        self.size_group.buttonClicked.connect(self._size_clicked)
        lay.addWidget(seg)

        # Custom size: a label and a centred number box (type it, or scroll over it).
        self.custom = MbBox(1, 4000, 25)
        self.custom.setFixedWidth(90)
        self.custom.setToolTip("Type a size, or scroll over it (Shift = 10 MB steps)")
        self.custom.valueChanged.connect(self.settings_changed)
        self.custom_row = QWidget()
        cr = QHBoxLayout(self.custom_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(8)
        cr.addWidget(_label("Custom size", "muted"))
        cr.addStretch()
        cr.addWidget(self.custom)
        cr.addWidget(_label("MB", "muted"))
        sp = self.custom_row.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.custom_row.setSizePolicy(sp)
        self.custom_row.setVisible(False)
        lay.addWidget(self.custom_row)

        opts = QHBoxLayout()
        opts.setSpacing(10)
        opts.addWidget(_label("Resolution", "muted"))
        self.res = QComboBox()
        for text, h in RESOLUTIONS:
            self.res.addItem(text, h)
        self.res.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.res.currentIndexChanged.connect(self.settings_changed)
        opts.addWidget(self.res, 1)
        self.audio = QCheckBox("Audio")
        self.audio.setChecked(True)
        self.audio.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.audio.toggled.connect(self.settings_changed)
        opts.addWidget(self.audio)
        lay.addLayout(opts)

        # Ready (bitrate + Export), Exporting and Saved take turns in one slot that's as tall as
        # the tallest of them, so the size picker and everything above it never move.
        self.stage = QStackedWidget()

        ready = QWidget()
        rl = QVBoxLayout(ready)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(12)
        self.bitrate = BitrateMeter()
        rl.addWidget(self.bitrate)
        rl.addStretch()
        self.export_btn = _button("Export", "primary")
        self.export_btn.clicked.connect(self.export)
        rl.addWidget(self.export_btn)
        self.stage.addWidget(ready)

        self.progress_card = QFrame()
        self.progress_card.setObjectName("card")
        pc = QVBoxLayout(self.progress_card)
        pc.setContentsMargins(14, 14, 14, 14)
        pc.setSpacing(12)
        row = QHBoxLayout()
        row.addWidget(_label("Exporting…", "h2"))
        row.addStretch()
        self.pct_label = _label("0%", "bitrateValue")
        row.addWidget(self.pct_label)
        pc.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        pc.addWidget(self.bar)
        pc.addStretch()
        cancel = _button("Cancel", "outline")
        cancel.clicked.connect(self.cancel_export)
        pc.addWidget(cancel)
        self.stage.addWidget(self.progress_card)

        self.saved_card = QFrame()
        self.saved_card.setObjectName("savedCard")
        sc = QVBoxLayout(self.saved_card)
        sc.setContentsMargins(14, 12, 14, 12)
        sc.setSpacing(10)
        row = QHBoxLayout()
        row.setSpacing(8)
        tick = QLabel()
        tick.setPixmap(theme.icon("check", theme.AMBER, 16).pixmap(16, 16))
        row.addWidget(tick)
        self.saved_label = _label("Saved", "h2")
        row.addWidget(self.saved_label)
        row.addStretch()
        self.saved_size = _label("", "monoSmall")
        row.addWidget(self.saved_size)
        sc.addLayout(row)
        self.saved_hint = _label("Next: copy it, then paste it in Discord", "muted")
        sc.addWidget(self.saved_hint)
        sc.addStretch()
        # The next step is the big button; exporting again is the small one.
        self.copy_btn = _button("Copy file", "copyBig")
        self.copy_btn.clicked.connect(self.copy_file)
        sc.addWidget(self.copy_btn)
        btns = QHBoxLayout()
        btns.setSpacing(6)
        folder = _button("Show in folder", "outline")
        folder.clicked.connect(self.show_in_folder)
        again = _button("Export again", "outline")
        again.clicked.connect(self.export)
        btns.addWidget(folder)
        btns.addWidget(again)
        sc.addLayout(btns)
        self.stage.addWidget(self.saved_card)

        lay.addWidget(self.stage)
        self.settings_widgets = [seg, self.custom_row, self.res, self.audio]
        self._pct = 0.0

    # ---- settings ----
    def _size_clicked(self, b):
        self.custom_row.setVisible(b.property("mb") == 0)
        self.settings_changed.emit()

    def target_mb(self):
        b = self.size_group.checkedButton()
        mb = b.property("mb") if b else 10
        return self.custom.value() if mb == 0 else mb

    def set_target(self, mb):
        match = next((b for b in self.size_group.buttons() if b.property("mb") == mb), None)
        if match is None:
            match = next(b for b in self.size_group.buttons() if b.property("mb") == 0)
            self.custom.setValue(int(mb))
        match.setChecked(True)
        self.custom_row.setVisible(match.property("mb") == 0)

    def resolution(self):
        return self.res.currentData()

    def set_resolution(self, h):
        i = self.res.findData(h)
        self.res.setCurrentIndex(max(0, i))

    # ---- sections ----
    def set_sections(self, items, active, thumb_for):
        while len(self.rows) < len(items):
            r = SectionRow()
            r.selected.connect(self.select)
            r.play.connect(self.play_section)
            r.remove.connect(self.remove)
            self.list_lay.insertWidget(len(self.rows), r)
            self.rows.append(r)
        while len(self.rows) > len(items):
            r = self.rows.pop()
            r.setParent(None)
            r.deleteLater()
        for i, (a, b) in enumerate(items):
            t = a if a is not None else b
            self.rows[i].set_data(i, a, b, i == active, thumb_for(t) if t is not None else None)
        self.empty_note.setVisible(not items)
        self.new_btn.setVisible(bool(items))     # with none yet, the note above says what to do
        self.clear_btn.setVisible(bool(items))

    def set_summary(self, n, total):
        self.summary.setText(f"{n} section{'' if n == 1 else 's'} · {secs(total)}" if n else "")

    # ---- export state ----
    def _settings_enabled(self, on):
        """Size, resolution and audio are locked while an export runs (changing them wouldn't change it)."""
        for w in self.settings_widgets:
            w.setEnabled(on)

    def show_progress(self, pct, _label=""):
        if self.stage.currentWidget() is not self.progress_card:
            self._pct = 0.0
            self.stage.setCurrentWidget(self.progress_card)
            self._settings_enabled(False)
        # A refit to size re-runs the last pass; the bar never goes backwards.
        self._pct = max(self._pct, pct)
        self.pct_label.setText(f"{self._pct:.0f}%")
        self.bar.setValue(int(self._pct * 10))

    def show_saved(self, where, size_mb):
        self.saved_label.setText(f"Saved to {where}")
        self.saved_size.setText(f"{size_mb:.2f} MB")
        self.copy_btn.setText("Copy file")
        self.saved_hint.setText("Next: copy it, then paste it in Discord")
        self.stage.setCurrentWidget(self.saved_card)
        self._settings_enabled(True)

    def hide_cards(self):
        self.stage.setCurrentIndex(0)
        self._settings_enabled(True)

    @property
    def showing_result(self):
        return self.stage.currentWidget() is self.saved_card

    def mark_copied(self):
        self.copy_btn.setText("Copied ✓")
        self.saved_hint.setText("Now paste it in Discord (Ctrl+V)")
