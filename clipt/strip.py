"""The frame strip: thumbnails, sections, IN/OUT flags and the playhead."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QWidget

from . import theme
from .model import fmt

FLAG_H = 20          # height of the IN / OUT time flags
TOP = 28             # y where the frames start
STRIP_H = 64         # height of the frames
OUT_Y = TOP + STRIP_H + 8   # y of the OUT flags, below the frames
HEIGHT = OUT_Y + FLAG_H
THUMBS = 24


class Strip(QWidget):
    scrubbed = Signal(float, bool)            # time, released
    edge_drag_started = Signal(int, str)      # section, "in"/"out"
    edge_dragged = Signal(int, str, float)
    edge_drag_finished = Signal(int, str)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self.duration = 0.0
        self.pos = 0.0
        self.thumbs = {}
        self._hits = []
        self._drag = None
        self.setFixedHeight(HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.f_flag = theme.mono(11)
        self.f_flag_b = theme.mono(11, QFont.Weight.DemiBold)

    # ---- data ----
    def set_duration(self, d):
        self.duration = d
        self.update()

    def set_position(self, t):
        # At 120 fps the playhead moves well under a pixel per frame, and repainting
        # the whole strip every frame cost ~8% CPU; only repaint when it reaches a new pixel.
        r = self.devicePixelRatioF()
        moved = round(self.x_of(t) * r) != round(self.x_of(self.pos) * r)
        self.pos = t
        if moved:
            self.update()

    def clear_thumbs(self):
        self.thumbs = {}
        self.update()

    def set_thumb(self, index, image):
        self.thumbs[index] = QPixmap.fromImage(image)
        self.update()

    def thumb_at(self, t):
        """Nearest thumbnail to time t (for the section list)."""
        if not self.duration or not self.thumbs:
            return None
        i = min(THUMBS - 1, max(0, int(t / self.duration * THUMBS)))
        return self.thumbs.get(i)

    # ---- geometry ----
    def x_of(self, t):
        return 0.0 if not self.duration else t / self.duration * self.width()

    def t_of(self, x):
        if not self.duration:
            return 0.0
        return min(max(x / max(1, self.width()), 0.0), 1.0) * self.duration

    # ---- painting ----
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        frames = QRectF(0, TOP, w, STRIP_H)
        clip = QPainterPath()
        clip.addRoundedRect(frames, 6, 6)

        p.save()
        p.setClipPath(clip)
        p.fillRect(frames, QColor("#1A1A1A"))
        slot = w / THUMBS
        for i in range(THUMBS):
            r = QRectF(i * slot, TOP, slot, STRIP_H)
            pm = self.thumbs.get(i)
            if pm:
                # centre-crop the thumbnail into its slot
                scale = max(r.width() / pm.width(), r.height() / pm.height())
                sw, sh = r.width() / scale, r.height() / scale
                src = QRectF((pm.width() - sw) / 2, (pm.height() - sh) / 2, sw, sh)
                p.drawPixmap(r, pm, src)
            p.fillRect(QRectF(r.right() - 1, TOP, 1, STRIP_H), QColor(theme.BG))
        if self.duration:
            dim = QColor(11, 11, 11, 158)
            prev = 0.0
            ranges = self.model.ranges(self.duration)
            for s, e in ranges:
                if s > prev:
                    p.fillRect(QRectF(self.x_of(prev), TOP, self.x_of(s) - self.x_of(prev), STRIP_H), dim)
                prev = e
            if ranges and prev < self.duration:
                p.fillRect(QRectF(self.x_of(prev), TOP, w - self.x_of(prev), STRIP_H), dim)
        p.restore()

        if not self.duration:
            p.end()
            return

        hits = []
        order = [i for i in range(len(self.model.items)) if i != self.model.active]
        if self.model.items:
            order.append(self.model.active)          # active one drawn on top
        for idx in order:
            a, b = self.model.items[idx]
            act = idx == self.model.active
            edge = QColor(theme.TEXT if act else "#5A5A5A")
            hw = 12 if act else 5
            if a is not None and b is not None:
                x1, x2 = self.x_of(a), self.x_of(b)
                p.fillRect(QRectF(x1, TOP, x2 - x1, 2), edge)
                p.fillRect(QRectF(x1, TOP + STRIP_H - 2, x2 - x1, 2), edge)
            elif a is not None and self.pos > a:
                # an open section: dashed box from IN to the playhead
                x1, x2 = self.x_of(a), self.x_of(self.pos)
                p.fillRect(QRectF(x1, TOP, x2 - x1, STRIP_H), QColor(92, 208, 143, 22))
                pen = QPen(QColor(theme.GREEN), 2, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(QPointF(x1, TOP + 1), QPointF(x2, TOP + 1))
                p.drawLine(QPointF(x1, TOP + STRIP_H - 1), QPointF(x2, TOP + STRIP_H - 1))
                p.drawLine(QPointF(x2 - 1, TOP), QPointF(x2 - 1, TOP + STRIP_H))
                p.setPen(Qt.PenStyle.NoPen)
            if a is not None:
                x = self.x_of(a)
                handle = QRectF(x, TOP - 8, hw, STRIP_H + 8)
                self._round_rect(p, handle, QColor(theme.GREEN), left=True)
                flag = self._flag(p, x, 0, "IN", fmt(a), theme.GREEN, act, anchor_right=False)
                hits.append((handle.adjusted(-4, 0, 4, 0).united(flag), idx, "in", act))
            if b is not None:
                x = self.x_of(b)
                handle = QRectF(x - hw, TOP, hw, STRIP_H + 8)
                self._round_rect(p, handle, QColor(theme.RED), left=False)
                flag = self._flag(p, x, OUT_Y, "OUT", fmt(b), theme.RED, act, anchor_right=True)
                hits.append((handle.adjusted(-4, 0, 4, 0).united(flag), idx, "out", act))

        x = self.x_of(self.pos)
        p.fillRect(QRectF(x - 1, TOP - 4, 2, STRIP_H + 8), QColor("#FFFFFF"))
        p.end()
        # active section's edges win when they overlap others
        hits.sort(key=lambda h: not h[3])
        self._hits = hits

    def _round_rect(self, p, r, color, left):
        path = QPainterPath()
        path.addRoundedRect(r, 3, 3)
        p.fillPath(path, color)
        # square off the inner side so it reads as part of the section
        inner = QRectF(r.right() - 3, r.top(), 3, r.height()) if left \
            else QRectF(r.left(), r.top(), 3, r.height())
        p.fillRect(inner, color)

    def _flag(self, p, x, y, word, time_text, color, solid, anchor_right):
        fm_b = QFontMetricsF(self.f_flag_b)
        fm = QFontMetricsF(self.f_flag)
        gap = 6
        tw = fm_b.horizontalAdvance(word) + gap + fm.horizontalAdvance(time_text) + 14
        left = x - tw if anchor_right else x
        left = min(max(left, 0), self.width() - tw)
        r = QRectF(left, y, tw, FLAG_H)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        c = QColor(color)
        if solid:
            p.fillPath(path, c)
            ink = QColor(theme.BG)
        else:
            p.fillPath(path, QColor(theme.BG))
            p.setPen(QPen(c, 1))
            p.drawPath(path)
            ink = c
        p.setPen(ink)
        first, second = (time_text, word) if anchor_right else (word, time_text)
        f1, f2 = (self.f_flag, self.f_flag_b) if anchor_right else (self.f_flag_b, self.f_flag)
        m1 = fm if anchor_right else fm_b
        tx = r.left() + 7
        p.setFont(f1)
        p.drawText(QRectF(tx, y, r.width(), FLAG_H), Qt.AlignmentFlag.AlignVCenter, first)
        p.setFont(f2)
        p.drawText(QRectF(tx + m1.horizontalAdvance(first) + gap, y, r.width(), FLAG_H),
                   Qt.AlignmentFlag.AlignVCenter, second)
        p.setPen(Qt.PenStyle.NoPen)
        return r

    # ---- mouse ----
    def _hit(self, pt):
        for rect, idx, which, _act in self._hits:
            if rect.contains(pt):
                return idx, which
        return None

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or not self.duration:
            return
        hit = self._hit(e.position())
        if hit:
            self._drag = ("edge",) + hit
            self.edge_drag_started.emit(*hit)
        else:
            self._drag = ("scrub",)
            self.scrubbed.emit(self.t_of(e.position().x()), False)

    def mouseMoveEvent(self, e):
        if self._drag:
            t = self.t_of(e.position().x())
            if self._drag[0] == "edge":
                self.edge_dragged.emit(self._drag[1], self._drag[2], t)
            else:
                self.scrubbed.emit(t, False)
            return
        over = self._hit(e.position())
        self.setCursor(Qt.CursorShape.SizeHorCursor if over else Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if not self._drag:
            return
        drag, self._drag = self._drag, None
        if drag[0] == "edge":
            self.edge_drag_finished.emit(drag[1], drag[2])
        else:
            self.scrubbed.emit(self.t_of(e.position().x()), True)
