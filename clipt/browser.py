"""The clip browser: what ClipTrimmer shows when no clip is open (and on "All clips").

Every replay in the folder as a card, grouped by day, newest first. The picture
on each card is the moment the replay was saved; it's also written into the
clip as its cover, so File Explorer shows the same thumbnail.
"""
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QFile, QFileSystemWatcher, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import library
from .model import fmt

CARD_W, THUMB_H, GAP = 256, 144, 16


def _button(text, name=None):
    b = QPushButton(text)
    if name:
        b.setObjectName(name)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def _label(text="", name=None):
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    return l


def _same(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def card_picture(img):
    """Crop/scale a frame to the card's picture size (what gets cached, so memory stays small)."""
    if img.isNull():
        return img
    s = img.scaled(CARD_W, THUMB_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                   Qt.TransformationMode.SmoothTransformation)
    return s.copy((s.width() - CARD_W) // 2, (s.height() - THUMB_H) // 2, CARD_W, THUMB_H)


class CoverLoader(QObject):
    """Card pictures and lengths, worked out in the background. Clips without a
    cover get one (see library.add_cover), except the one open in the player."""
    ready = Signal(str, float, QImage, float)     # path, mtime, picture, duration

    def __init__(self, ffmpeg, ffprobe, in_use):
        super().__init__()
        self.ffmpeg, self.ffprobe, self.in_use = ffmpeg, ffprobe, in_use
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.cache = {}      # (path, mtime) -> (card-sized QImage, duration)
        self.pending = set()

    def request(self, clip):
        key = (str(clip.path), clip.mtime)
        if key in self.cache:
            img, dur = self.cache[key]
            self.ready.emit(key[0], clip.mtime, img, dur)
        elif key not in self.pending:
            self.pending.add(key)
            self.pool.submit(self._work, clip, key)

    def prune(self, keys):
        """Forget clips that are no longer in the folder."""
        for k in [k for k in self.cache if k not in keys]:
            self.cache.pop(k, None)

    def _work(self, clip, key):
        try:
            p = clip.path
            duration, _ = library._probe(self.ffprobe, p)
            image = None
            settled = library.age(clip.mtime) > library.SETTLE
            busy = _same(self.in_use(), str(p))
            if settled and not busy:
                status, image = library.add_cover(self.ffmpeg, self.ffprobe, p)
                if status == library.HAD:
                    image = library.cover_image(self.ffmpeg, p)
            if not image:     # not an mp4, still being written, or open: picture only, file untouched
                image = library.frame_at_end(self.ffmpeg, p, duration)
            img = card_picture(QImage.fromData(image)) if image else QImage()
            if settled:          # a clip still being written is looked at again later, for its cover
                self.cache[key] = (img, duration)
            self.ready.emit(key[0], clip.mtime, img, duration)
        except Exception:
            pass
        finally:
            self.pending.discard(key)

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


class ClipCard(QFrame):
    clicked = Signal(str)
    menu = Signal(str, QPoint)

    def __init__(self, clip, trimmed, parent=None):
        super().__init__(parent)
        self.setObjectName("clipCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(CARD_W + 2)
        self.path = str(clip.path)
        self.size_mb = clip.size / 1048576
        v = QVBoxLayout(self)
        v.setContentsMargins(1, 1, 1, 10)
        v.setSpacing(8)
        self.thumb = _label("", "cardThumb")
        self.thumb.setFixedSize(CARD_W, THUMB_H)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.thumb)
        row = QHBoxLayout()
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(6)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_label(library.clock(clip.when), "cardTitle"))
        self.meta = _label(f"{self.size_mb:.0f} MB", "cardMeta")
        text.addWidget(self.meta)
        row.addLayout(text, 1)
        self.open_badge = _label("Open", "openBadge")
        self.open_badge.setToolTip("This is the clip you’re trimming")
        self.open_badge.hide()
        row.addWidget(self.open_badge, 0, Qt.AlignmentFlag.AlignTop)
        if trimmed:
            badge = _label("Trimmed", "badge")
            badge.setToolTip("You’ve exported a clip from this one before")
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(row)
        self.setToolTip(clip.path.name)

    def set_picture(self, img, duration):
        if not img.isNull():
            self.thumb.setPixmap(QPixmap.fromImage(card_picture(img)))
        if duration > 0:
            self.meta.setText(f"{fmt(duration)[:-3]}  ·  {self.size_mb:.0f} MB")

    def set_open(self, on):
        """The clip that's open in the editor gets a white outline and an "Open" badge."""
        self.open_badge.setVisible(on)
        if self.property("open") != on:
            self.setProperty("open", on)
            self.style().unpolish(self)
            self.style().polish(self)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.path)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self.menu.emit(self.path, e.globalPos())


class ElidedLabel(QLabel):
    """One line that shortens with "…" in the middle instead of pushing other things off."""

    def __init__(self, text="", name=None):
        super().__init__()
        if name:
            self.setObjectName(name)
        self.full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full(text)

    def set_full(self, text):
        self.full = text
        self.setToolTip(text)
        self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(self.full, Qt.TextElideMode.ElideMiddle, max(40, self.width())))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()


class ClipBrowser(QWidget):
    open_clip = Signal(str)
    open_latest = Signal()
    browse = Signal()
    change_folder = Signal()
    count_changed = Signal(int)      # also fires when the listing changes, so the app can refresh "latest"

    def __init__(self, ffmpeg, ffprobe, in_use, parent=None):
        super().__init__(parent)
        self.setObjectName("empty")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.in_use = in_use
        self.loader = CoverLoader(ffmpeg, ffprobe, in_use)
        self.loader.ready.connect(self._on_picture)
        self.cards = {}
        self.grids = []
        self.folder = None
        self.out_dir = None
        self.cols = 0
        self.shown_list = None
        self.count = 0

        v = QVBoxLayout(self)
        v.setContentsMargins(32, 18, 20, 0)
        v.setSpacing(0)
        # The title ("Your clips", count) and the back button live in the window's header.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 12, 0)
        top.setSpacing(4)
        self.folder_label = ElidedLabel("", "muted")
        top.addWidget(self.folder_label, 1)
        change = _button("Change…", "link")
        change.clicked.connect(self.change_folder)
        top.addWidget(change)
        top.addSpacing(16)
        self.browse_btn = _button("Open a video…", "pill")
        self.browse_btn.setToolTip("Open any video file (Ctrl+O)")
        self.browse_btn.clicked.connect(self.browse)
        self.latest = _button("Open latest replay", "primary")
        self.latest.setToolTip("Ctrl+L")
        self.latest.clicked.connect(self.open_latest)
        for w in (self.browse_btn, self.latest):
            top.addSpacing(8)
            top.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
        v.addLayout(top)
        v.addSpacing(18)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 12, 24)
        self.body_layout.setSpacing(0)
        self.scroll.setWidget(self.body)
        v.addWidget(self.scroll, 1)

        # New replays show up on their own, a moment after OBS has written them.
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _: self.reload_timer.start())
        self.reload_timer = QTimer(self, singleShot=True, interval=1500)
        self.reload_timer.timeout.connect(self.reload)
        self.settle_timer = QTimer(self, singleShot=True)
        self.settle_timer.timeout.connect(lambda: self.reload(force=True))

    # ------------------------------------------------------------- content
    def set_latest(self, label):
        self.latest.setVisible(bool(label))
        self.latest.setText(f"Open latest replay  ·  {label}" if label else "")

    def set_folder(self, path):
        self.folder_label.set_full(str(path))

    def show_folder(self, folder, out_dir, force=False):
        """Point the browser at a replays folder (and where exports go, for the Trimmed badge).
        The same folder again only redraws when something changed, or with force (e.g. after an export)."""
        folder = str(folder)
        if folder == self.folder and out_dir == self.out_dir:
            self.reload(force=force)
            return
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        if os.path.isdir(folder):
            self.watcher.addPath(folder)
        self.folder, self.out_dir = folder, out_dir
        self.set_folder(folder)
        self.reload(force=True)

    def mark_open(self):
        open_path = self.in_use()
        for path, card in self.cards.items():
            card.set_open(_same(path, open_path))

    def reload(self, force=False):
        clips = library.list_clips(self.folder) if self.folder else []
        # Adding covers rewrites files (dates kept), which also wakes the folder watcher; nothing to redraw then.
        listing = [(str(c.path), c.mtime) for c in clips]
        if listing == self.shown_list and not force:
            self.mark_open()
            return
        self.shown_list = listing
        if self.folder:
            library.sweep_temps(self.folder)
        self.loader.prune(set(listing))
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards, self.grids = {}, []
        self.count = len(clips)
        self.browse_btn.setVisible(bool(clips))
        self.count_changed.emit(self.count)
        if not clips:
            self._empty_state()
            return
        for label, group in library.group_by_day(clips):
            head = _label(f"{label}   <span style='color:#8C8C8C; font-weight:400'>{len(group)}</span>", "dayHeading")
            head.setTextFormat(Qt.TextFormat.RichText)
            head.setContentsMargins(0, 6 if not self.grids else 22, 0, 12)
            self.body_layout.addWidget(head)
            box = QWidget()
            grid = QGridLayout(box)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(GAP)
            grid.setVerticalSpacing(GAP)
            grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            cards = []
            for c in group:
                card = ClipCard(c, self.out_dir and library.was_trimmed(self.out_dir, c.path.stem))
                card.clicked.connect(self.open_clip)
                card.menu.connect(self._menu)
                self.cards[card.path] = card
                cards.append(card)
            self.grids.append((grid, cards))
            self.body_layout.addWidget(box)
        self.body_layout.addStretch()
        self.cols = 0
        self._layout_cards()
        self.mark_open()
        for c in clips:
            self.loader.request(c)
        # A clip that's still settling gets its cover on a later look.
        young = [library.age(c.mtime) for c in clips if library.age(c.mtime) < library.SETTLE]
        if young:
            self.settle_timer.start(int((library.SETTLE - min(young)) * 1000) + 1500)

    def _empty_state(self):
        missing = not os.path.isdir(self.folder or "")
        box = QWidget()
        b = QVBoxLayout(box)
        b.setContentsMargins(0, 80, 0, 0)
        b.setSpacing(8)
        title = _label("This folder doesn’t exist" if missing else "No replays here yet", "emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b.addWidget(title)
        where = _label(str(self.folder or ""), "muted")
        where.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b.addWidget(where)
        b.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch()
        change = _button("Change folder…", "pill")
        change.clicked.connect(self.change_folder)
        other = _button("Open a video…", "pill")
        other.clicked.connect(self.browse)
        row.addWidget(change)
        row.addSpacing(8)
        row.addWidget(other)
        row.addStretch()
        b.addLayout(row)
        self.body_layout.addWidget(box)
        self.body_layout.addStretch()

    def _on_picture(self, path, mtime, img, duration):
        card = self.cards.get(path)
        if card:
            card.set_picture(img, duration)

    def _layout_cards(self):
        width = self.scroll.viewport().width() - 12
        cols = max(1, (width + GAP) // (CARD_W + 2 + GAP))
        if cols == self.cols:
            return
        self.cols = cols
        for grid, cards in self.grids:
            for card in cards:
                grid.removeWidget(card)
            for i, card in enumerate(cards):
                grid.addWidget(card, i // cols, i % cols)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_cards()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._layout_cards)

    # ---------------------------------------------------------------- menu
    def _menu(self, path, pos):
        is_open = _same(path, self.in_use())
        m = QMenu(self)
        m.addAction("Trim this clip", lambda: self.open_clip.emit(path))
        m.addAction("Play", lambda: os.startfile(path))
        m.addAction("Show in folder", lambda: subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"'))
        m.addSeparator()
        trash = m.addAction("Move to Recycle Bin" + ("  (it’s open)" if is_open else ""), lambda: self._trash(path))
        trash.setEnabled(not is_open)
        m.exec(pos)

    def _trash(self, path):
        if _same(path, self.in_use()):
            return
        r = QFile.moveToTrash(path)
        if r[0] if isinstance(r, tuple) else r:
            self.reload()
        else:
            QMessageBox.warning(self, "Couldn’t move it to the Recycle Bin",
                                "The clip is in use right now (a player, or its picture being made). "
                                "Try again in a moment.")

    def shutdown(self):
        self.loader.shutdown()
