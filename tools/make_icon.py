"""Write assets/ClipTrimmer.ico from the icon drawn in clipt/theme.py.

ICO files may hold PNG images, so each size is just a PNG from Qt.
"""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from clipt.theme import app_icon_pixmap  # noqa: E402

app = QApplication([])
sizes = [16, 24, 32, 48, 64, 128, 256]
blobs = []
for s in sizes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    app_icon_pixmap(s).save(buf, "PNG")
    blobs.append(bytes(buf.data()))

out = ROOT / "assets" / "ClipTrimmer.ico"
out.parent.mkdir(exist_ok=True)
header = struct.pack("<HHH", 0, 1, len(sizes))
offset = 6 + 16 * len(sizes)
entries = b""
for s, blob in zip(sizes, blobs):
    dim = 0 if s == 256 else s
    entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
    offset += len(blob)
out.write_bytes(header + entries + b"".join(blobs))
print(f"wrote {out} ({out.stat().st_size} bytes)")
