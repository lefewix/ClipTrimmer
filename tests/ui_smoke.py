"""Drive the real ClipTrimmer window end to end and take screenshots.

usage: .venv\\Scripts\\python.exe tests\\ui_smoke.py <workdir with test clips> <shots dir>
Uses its own settings store ("ClipTrimmerTest"), so your settings are untouched.
A window appears on screen for about a minute while it runs.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
os.environ["PATH"] = str(BIN) + os.pathsep + os.environ["PATH"]
os.add_dll_directory(str(BIN))
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipt import strip as strip_mod  # noqa: E402
from clipt.app import MainWindow, _dark_title_bar, setup_app  # noqa: E402

WORK = Path(sys.argv[1])
SHOTS = Path(sys.argv[2])
SHOTS.mkdir(parents=True, exist_ok=True)
OUT = WORK / "ui_out"
OUT.mkdir(exist_ok=True)
CLIP = WORK / "src_hevc_120_3tracks.mp4"
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def wait_until(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def screen_grab(w):
    """What's really on screen over a widget (mpv draws into its own native window,
    which QWidget.grab() can't see)."""
    top_left = w.mapToGlobal(QPoint(0, 0))
    return w.screen().grabWindow(0, top_left.x(), top_left.y(), w.width(), w.height())


def shot(name):
    pump(0.3)
    screen_grab(win).save(str(SHOTS / f"{name}.png"))


def brightness(img):
    total = n = 0
    for y in range(0, img.height(), 23):
        for x in range(0, img.width(), 23):
            c = img.pixelColor(x, y)
            total += c.red() + c.green() + c.blue()
            n += 1
    return total / max(1, n) / 3


app = setup_app([sys.argv[0]], ROOT / "clipt" / "fonts", org="ClipTrimmerTest")
s = QSettings()
s.clear()
s.setValue("out_dir", str(OUT))
s.setValue("replays_dir", str(WORK))
win = MainWindow(BIN)
win.resize(1280, 820)
win.show()
_dark_title_bar(win)
pump(1.0)

check("empty state shown before a clip is open", win.empty.isVisible())
check("latest replay button found the newest clip", win.empty.latest.isEnabled(), win.empty.latest.text())
check("Set In disabled with no clip", not win.in_btn.isEnabled())
key = next((k for k in win.empty.cards if os.path.normcase(k) == os.path.normcase(str(CLIP))), None)
check("browser shows a card per clip", key is not None and len(win.empty.cards) >= 1, len(win.empty.cards))
check("card pictures load", wait_until(lambda: all(not c.thumb.pixmap().isNull() for c in win.empty.cards.values()), 60))
check("card shows the clip length", wait_until(lambda: "0:30" in win.empty.cards[key].meta.text(), 10), win.empty.cards[key].meta.text())
check("no back button before a clip is open", not win.open_btn.isVisible())
check("header says Your clips with the count", win.file_label.text() == "Your clips" and "replays" in win.meta_label.text(),
      win.meta_label.text())
check("latest-replay button uses the 12-hour clock", win.empty.latest.text().endswith(("AM", "PM")), win.empty.latest.text())
tmp_cover = WORK / "~cover 1 newest.mp4"           # what a cover remux looks like mid-way
tmp_cover.write_bytes(b"not a video")
check("latest replay never picks a cover temp file", win.latest_replay() is not None and not win.latest_replay().name.startswith("~"),
      win.latest_replay())
tmp_cover.unlink()
shot("1_empty")

card = win.empty.cards[key]
QTest.mouseClick(card, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, card.rect().center())
check("clicking a card opens the clip", wait_until(lambda: win.path and not win.empty.isVisible(), 10), win.path)
win.in_btn.click()
pump(0.1)
win.open_btn.click()
pump(0.4)
check("the open clip is marked in the list", win.empty.cards[key].open_badge.isVisible())
win.activateWindow()
QTest.keyClick(win, Qt.Key.Key_Delete)
QTest.keyClick(win, Qt.Key.Key_Space)
pump(0.3)
check("Delete/Space don't touch the hidden clip while the list is showing", len(win.model.items) == 1 and win.paused,
      f"{len(win.model.items)} sections, paused {win.paused}")
check("All clips brings the browser back, with a way back", win.empty.isVisible() and win.open_btn.isVisible()
      and win.open_btn.text() == "Back to clip", win.open_btn.text())
geo_browser = win.open_btn.geometry()
check("nav button sits top-left in the header", win.open_btn.mapTo(win, win.open_btn.rect().topLeft()).x() < 60,
      win.open_btn.mapTo(win, win.open_btn.rect().topLeft()).x())
shot("1b_browser_over_clip")
win.activateWindow()
QTest.keyClick(win, Qt.Key.Key_Escape)
pump(0.3)
check("Esc goes back to the clip", not win.empty.isVisible())
check("nav button doesn't move or resize between screens", win.open_btn.geometry() == geo_browser and win.open_btn.text() == "All clips",
      f"{geo_browser} vs {win.open_btn.geometry()}")
check("nav button fits its text", win.open_btn.width() >= win.open_btn.sizeHint().width() - 1,
      f"{win.open_btn.width()} vs hint {win.open_btn.sizeHint().width()}")
win.clear_sections()
check("header shows the clip again", win.file_label.text() == CLIP.stem, win.file_label.text())
win.open_btn.click()
pump(0.3)
win.open_btn.click()
pump(0.3)
check("the nav button toggles back and forth", not win.empty.isVisible())

win.load(str(CLIP))
check("clip loads", wait_until(lambda: win.duration > 29, 10), win.duration)
check("thumbnails fill in", wait_until(lambda: len(win.strip.thumbs) == strip_mod.THUMBS, 30), len(win.strip.thumbs))
check("header shows clip facts", "120 fps" in win.meta_label.text() and "1080p" in win.meta_label.text(), win.meta_label.text())

win.seek_to(5.0)
pump(0.8)
img = screen_grab(win.video).toImage()
check("video actually renders (not black)", brightness(img) > 40, f"avg {brightness(img):.0f}")
shot("2_loaded")

# ---- make two sections: buttons for the first, keyboard for the second ----
win.in_btn.click()
win.seek_to(8.0)
pump(0.3)
win.out_btn.click()
win.seek_to(20.0)
pump(0.3)
win.activateWindow()
QTest.keyClick(win, Qt.Key.Key_I)
win.seek_to(25.0)
pump(0.3)
QTest.keyClick(win, Qt.Key.Key_O)
items = [[round(a, 2), round(b, 2)] for a, b in win.model.items]
check("two sections from buttons + I/O keys", items == [[5.0, 8.0], [20.0, 25.0]], items)
check("side panel lists both", len(win.rail.rows) == 2)
from clipt.engine import plan_bitrate  # noqa: E402
want = f"{plan_bitrate(10, 8.0, True)[0]:,} kbps"
check("bitrate card uses the 8 s of sections", win.rail.bitrate.value.text() == want, win.rail.bitrate.value.text())

# ---- playback ----
win.seek_to(10.0)
pump(0.5)
win.toggle_play()
pump(1.2)
win.toggle_play()
pump(0.3)
check("Space/play advances time", 10.6 < win.pos < 11.8, f"{win.pos:.2f}")

# ---- volume ----
check("volume defaults to 100", win.volume.value() == 100 and win.player.mpv.volume == 100)
win.volume.setValue(40)
check("slider sets mpv volume", win.player.mpv.volume == 40, win.player.mpv.volume)
QTest.keyClick(win, Qt.Key.Key_M)
check("M mutes", win.player.mpv.mute is True)
QTest.keyClick(win, Qt.Key.Key_Up)
check("Up raises volume and unmutes", win.volume.value() == 50 and win.player.mpv.mute is False)
QTest.keyClick(win, Qt.Key.Key_Down)
check("Down lowers volume", win.volume.value() == 40)
check("volume is remembered", int(QSettings().value("volume")) == 40)
win.volume.setValue(100)
win.play_section(0)
check("play section stops at its OUT",
      wait_until(lambda: win.paused and abs(win.pos - 8.0) < 0.05 and win.play_until is None, 6),
      f"pos {win.pos:.3f} paused {win.paused}")
before = win.pos
win.step(True)
pump(0.5)
check("next frame steps 1/120 s", abs(win.pos - before - 1 / 120) < 0.004, f"{win.pos - before:.4f}")
QTest.keyClick(win, Qt.Key.Key_Left)
pump(0.5)
check("Left arrow steps back", abs(win.pos - before) < 0.004, f"{win.pos:.4f}")
for _ in range(5):                       # held key: rapid steps must not get lost
    QTest.keyClick(win, Qt.Key.Key_Right)
    pump(0.03)
pump(0.6)
check("5 quick → presses = 5 frames", abs(win.pos - before - 5 / 120) < 0.004, f"{(win.pos - before) * 120:.2f} frames")

# ---- drag section 1's IN flag from 5.0 to ~3.0 ----
win.rail.select.emit(0)
pump(0.4)
st = win.strip
x_in = st.x_of(5.0) + 8
QTest.mousePress(st, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x_in), 10))
for i in range(1, 9):
    QTest.mouseMove(st, QPoint(int(x_in + (st.x_of(3.0) - st.x_of(5.0)) * i / 8), 10))
    pump(0.02)
QTest.mouseRelease(st, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                   QPoint(int(st.x_of(3.0) + 8), 10))
pump(0.8)
a = win.model.items[0][0]
check("dragging the IN flag moves IN", 2.7 < a < 3.4, f"{a:.2f}")
check("video followed the drag", abs(win.pos - a) < 0.05, f"pos {win.pos:.2f}")
QTest.keyClick(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
pump(0.2)
check("Ctrl+Z undoes the drag", abs(win.model.items[0][0] - 5.0) < 1e-6, win.model.items[0][0])

# click empty strip = scrub
QTest.mouseClick(st, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                 QPoint(int(st.x_of(15.0)), strip_mod.TOP + 30))
pump(0.5)
check("clicking the strip scrubs", abs(win.pos - 15.0) < 0.15, f"{win.pos:.2f}")
shot("3_sections")

# ---- open section while recording ----
QTest.keyClick(win, Qt.Key.Key_N)
win.seek_to(17.0)
pump(0.3)
check("N starts an open section at the playhead", win.model.items[-1] == [15.0, None] or abs(win.model.items[-1][0] - 15.0) < 0.15,
      win.model.items[-1])
shot("4_open_section")
QTest.keyClick(win, Qt.Key.Key_Delete)
pump(0.2)
check("Delete removes the selected section", len(win.model.items) == 2)

# ---- custom size box ----
custom = [b for b in win.rail.size_group.buttons() if b.property("mb") == 0][0]
custom.click()
pump(0.2)
check("Custom shows the size box", win.rail.custom_row.isVisible() and win.rail.custom.width() > 60, win.rail.custom.width())
win.rail.custom.setText("37")
pump(0.1)
check("typing a size sets the target", win.rail.target_mb() == 37, win.rail.target_mb())
win.rail.custom.setText("4x")
check("letters aren't accepted", win.rail.custom.text() != "4x" or win.rail.target_mb() == 37, win.rail.custom.text())
seg_y = win.rail.size_group.buttons()[0].mapTo(win.rail, win.rail.size_group.buttons()[0].rect().topLeft()).y()
win.rail.custom.setFocus()
win.rail.custom.selectAll()
QTest.keyClicks(win.rail.custom, "0")
QTest.keyClick(win.rail.custom, Qt.Key.Key_Return)
pump(0.2)
check("typing 0 shows the 1 MB actually used", win.rail.custom.text() == "1" and win.rail.target_mb() == 1, win.rail.custom.text())
check("Enter gives the keyboard back (shortcuts work again)", not win.rail.custom.hasFocus())
win.rail.custom.setValue(37)
ten = [b for b in win.rail.size_group.buttons() if b.property("mb") == 10][0]
ten.click()
pump(0.2)
check("back to 10 MB hides the box", not win.rail.custom_row.isVisible() and win.rail.target_mb() == 10)
check("Export controls don't move when Custom shows/hides",
      win.rail.size_group.buttons()[0].mapTo(win.rail, win.rail.size_group.buttons()[0].rect().topLeft()).y() == seg_y)

# ---- export ----
win.export()
check("export shows progress", wait_until(lambda: win.rail.progress_card.isVisible(), 3))
check("…in the same spot, controls staying put",
      win.rail.size_group.buttons()[0].mapTo(win.rail, win.rail.size_group.buttons()[0].rect().topLeft()).y() == seg_y)
check("size can't be changed mid-export", not win.rail.res.isEnabled())
pump(1.5)
shot("5_exporting")
check("export finishes", wait_until(lambda: win.worker is None, 180))
check("saved card shown", win.rail.saved_card.isVisible(), win.rail.saved_label.text())
check("Copy file is the big next step", win.rail.copy_btn.objectName() == "copyBig" and win.rail.copy_btn.width() > 250,
      win.rail.copy_btn.width())
out = win.last_export
ok = bool(out) and os.path.isfile(out)
check("output file written", ok, out)
if ok:
    j = json.loads(subprocess.run([str(BIN / "ffprobe.exe"), "-v", "error", "-show_entries", "format=duration",
                                   "-of", "json", out], capture_output=True, text=True).stdout)
    d = float(j["format"]["duration"])
    check("output is the two sections joined (8 s)", abs(d - 8.0) < 0.15, f"{d:.3f}s")
    check("output fits 10 MB", os.path.getsize(out) <= 10 * 1048576, f"{os.path.getsize(out) / 1048576:.2f} MB")
shot("6_saved")
win.copy_file()
md = QApplication.clipboard().mimeData()
check("Copy file puts the file on the clipboard",
      md.hasUrls() and md.urls()[0].toLocalFile().lower() == (out or "").replace("\\", "/").lower(),
      md.urls()[0].toLocalFile() if md.hasUrls() else "no urls")
pump(0.2)
fits = win.rail.copy_btn.fontMetrics().horizontalAdvance(win.rail.copy_btn.text()) < win.rail.copy_btn.width() - 20
check("'Copied' fits on its button", win.rail.copy_btn.text().startswith("Copied") and fits, win.rail.copy_btn.text())
shot("6b_copied")

win.clear_sections()
pump(0.2)
check("Clear all empties the list", win.model.items == [] and win.rail.empty_note.isVisible())
shot("7_cleared")

win.close()
pump(0.5)
check("window closes cleanly", not win.isVisible())
QSettings().clear()
print(f"\n{sum(results)}/{len(results)} passed", flush=True)
os._exit(0 if all(results) else 1)
