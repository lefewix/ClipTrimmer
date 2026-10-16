"""The sections being kept from the clip, with undo/redo."""

MIN_LEN = 0.1   # shortest section an edge drag can make, in seconds


def fmt(t):
    """Seconds -> m:ss.ss"""
    if t is None:
        return "—"
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:05.2f}"


def secs(t):
    """A length for people: 3.5 s, 12 s, 1:05."""
    if t < 10:
        return f"{t:.1f} s"
    if t < 60:
        return f"{t:.0f} s"
    return f"{int(t // 60)}:{int(t % 60):02d}"


class Sections:
    """
    Each item is [in, out] in seconds; either may be None while a section is
    being made. Set In / Set Out act on the active item, like a tape deck:
    pressing In after a finished section starts the next one.
    """

    def __init__(self):
        self.items = []
        self.active = 0
        self._undo = []
        self._redo = []

    # ---- history ----
    def _snapshot(self):
        return [list(s) for s in self.items], self.active

    def checkpoint(self):
        """Call before a change so it can be undone."""
        self._undo.append(self._snapshot())
        del self._undo[:-200]
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return False
        self._redo.append(self._snapshot())
        self.items, self.active = self._undo.pop()
        return True

    def redo(self):
        if not self._redo:
            return False
        self._undo.append(self._snapshot())
        self.items, self.active = self._redo.pop()
        return True

    def can_undo(self):
        return bool(self._undo)

    def can_redo(self):
        return bool(self._redo)

    def reset(self):
        self.items, self.active = [], 0
        self._undo.clear()
        self._redo.clear()

    # ---- editing ----
    def current(self):
        return self.items[self.active] if self.items else None

    def set_in(self, t):
        self.checkpoint()
        a = self.current()
        if a is None or (a[0] is not None and a[1] is not None):
            self.items.append([t, None])
            self.active = len(self.items) - 1
        else:
            a[0] = t
            if a[1] is not None and a[1] <= t:
                a[1] = None

    def set_out(self, t):
        self.checkpoint()
        a = self.current()
        if a is None:
            self.items.append([None, t])
            self.active = len(self.items) - 1
        else:
            a[1] = t
            if a[0] is not None and a[0] >= t:
                a[0] = None

    def new_at(self, t):
        self.checkpoint()
        self.items.append([t, None])
        self.active = len(self.items) - 1

    def remove(self, idx):
        if not 0 <= idx < len(self.items):
            return
        self.checkpoint()
        del self.items[idx]
        if self.active > idx:
            self.active -= 1
        self.active = max(0, min(self.active, len(self.items) - 1))

    def clear(self):
        if self.items:
            self.checkpoint()
        self.items, self.active = [], 0

    def select(self, idx):
        if 0 <= idx < len(self.items):
            self.active = idx

    def move_edge(self, idx, which, t, duration):
        """Drag an edge; it can't cross its partner. Returns the clamped time.
        No checkpoint here - the caller takes one when the drag starts."""
        a = self.items[idx]
        if which == "in":
            hi = (a[1] if a[1] is not None else duration) - MIN_LEN
            a[0] = min(max(t, 0.0), hi)
            return a[0]
        lo = (a[0] if a[0] is not None else 0.0) + MIN_LEN
        a[1] = max(min(t, duration), lo)
        return a[1]

    # ---- what gets exported ----
    def ranges(self, duration):
        """Sorted, merged [start, end] pairs. A missing IN means the start of
        the clip, a missing OUT the end. Empty list = nothing marked."""
        spans = []
        for a, b in self.items:
            if a is None and b is None:
                continue
            s = a if a is not None else 0.0
            e = b if b is not None else duration
            if e - s >= MIN_LEN:
                spans.append([s, e])
        spans.sort()
        merged = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        return merged
