"""Colours, fonts, icons and the Qt style sheet."""
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QPainter,
                           QPainterPath, QPixmap)
from PySide6.QtSvg import QSvgRenderer

BG = "#0B0B0B"
PANEL = "#111111"
SURFACE = "#161616"
LINE = "#1C1C1C"
LINE2 = "#262626"
LINE3 = "#2E2E2E"
DASH = "#3A3A3A"
TEXT = "#EDEDED"
TEXT2 = "#B5B5B5"
MUTED = "#8C8C8C"
GREEN = "#5CD08F"
GREEN_BG = "#1F3A2B"
GREEN_LINE = "#2F6B4B"
RED = "#F2615A"
RED_BG = "#3F1F1D"
RED_LINE = "#7A3330"
AMBER = "#FFB547"
AMBER_BG = "#16140F"
AMBER_LINE = "#4A3B1E"

SANS = "Segoe UI Variable Text"
MONO = "Consolas"


def load_fonts(font_dir):
    """Register the bundled fonts, falling back to Windows ones."""
    global SANS, MONO
    families = set()
    for f in Path(font_dir).glob("*.ttf"):
        fid = QFontDatabase.addApplicationFont(str(f))
        families.update(QFontDatabase.applicationFontFamilies(fid))
    if "Instrument Sans" in families:
        SANS = "Instrument Sans"
    if "JetBrains Mono" in families:
        MONO = "JetBrains Mono"


def sans(px, weight=QFont.Weight.Normal):
    f = QFont(SANS)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def mono(px, weight=QFont.Weight.Normal):
    f = QFont(MONO)
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


_ICONS = {
    "play": '<path d="M4.5 2.5v11l9-5.5z" fill="{c}"/>',
    "pause": '<path d="M4 2.5h3v11H4zM9 2.5h3v11H9z" fill="{c}"/>',
    "prev": '<path d="M10 3 5 8l5 5" fill="none" stroke="{c}" stroke-width="1.6"/>',
    "next": '<path d="m6 3 5 5-5 5" fill="none" stroke="{c}" stroke-width="1.6"/>',
    "x": '<path d="m4.5 4.5 7 7M11.5 4.5l-7 7" fill="none" stroke="{c}" stroke-width="1.5"/>',
    "check": '<path d="m3 8.5 3.2 3L13 4.5" fill="none" stroke="{c}" stroke-width="1.8"/>',
    "folder": '<path d="M1.5 4.5v8h13v-6.5H8L6.5 4.5z" fill="none" stroke="{c}" stroke-width="1.4"/>',
    "square": '<rect x="4" y="4" width="8" height="8" rx="1.5" fill="{c}"/>',
    "volume": '<path d="M1.5 6h2.5l3.5-3v10L4 10H1.5z" fill="{c}"/>'
              '<path d="M10 5.5a3.5 3.5 0 0 1 0 5M11.8 3.3a6.5 6.5 0 0 1 0 9.4" fill="none" stroke="{c}" stroke-width="1.4"/>',
    "volumeLow": '<path d="M1.5 6h2.5l3.5-3v10L4 10H1.5z" fill="{c}"/>'
                 '<path d="M10 5.5a3.5 3.5 0 0 1 0 5" fill="none" stroke="{c}" stroke-width="1.4"/>',
    "mute": '<path d="M1.5 6h2.5l3.5-3v10L4 10H1.5z" fill="{c}"/>'
            '<path d="m10 6 4 4M14 6l-4 4" fill="none" stroke="{c}" stroke-width="1.4"/>',
}


def icon(name, color, px=16):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
           + _ICONS[name].format(c=color) + "</svg>")
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    pm = QPixmap(px * 2, px * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


def app_icon_pixmap(size=256):
    """The app icon: a frame strip with a green IN and red OUT edge."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(8 * s, 8 * s, 240 * s, 240 * s), 52 * s, 52 * s)
    p.fillPath(bg, QColor("#161616"))
    strip = QRectF(40 * s, 88 * s, 176 * s, 80 * s)
    p.fillRect(strip, QColor("#2B2722"))
    p.fillRect(QRectF(40 * s, 128 * s, 176 * s, 40 * s), QColor("#3C3933"))
    p.fillRect(QRectF(40 * s, 72 * s, 20 * s, 112 * s), QColor(GREEN))
    p.fillRect(QRectF(196 * s, 72 * s, 20 * s, 112 * s), QColor(RED))
    p.fillRect(QRectF(60 * s, 88 * s, 136 * s, 6 * s), QColor(TEXT))
    p.fillRect(QRectF(60 * s, 162 * s, 136 * s, 6 * s), QColor(TEXT))
    p.end()
    return pm


def _check_image():
    """Style sheets need a file for the checkbox tick; write a tiny SVG."""
    import tempfile
    path = Path(tempfile.gettempdir()) / "clipt_check.svg"
    if not path.exists():
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                        f'<path d="m3.5 8.5 3 3 6-7" fill="none" stroke="{BG}" stroke-width="2"/></svg>')
    return path.as_posix()


def _chevron_image():
    """The dropdown arrow for combo boxes (style sheets need a file)."""
    import tempfile
    path = Path(tempfile.gettempdir()) / "clipt_chevron.svg"
    if not path.exists():
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                        f'<path d="m4 6 4 4 4-4" fill="none" stroke="{TEXT2}" stroke-width="1.6"/></svg>')
    return path.as_posix()


def stylesheet():
    check = _check_image()
    chevron = _chevron_image()
    return f"""
QWidget {{ color: {TEXT}; font-family: "{SANS}"; font-size: 13px; }}
QMainWindow, #central {{ background: {BG}; }}
QMenuBar {{ background: {BG}; color: {TEXT2}; padding: 2px 10px; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: 6px; background: transparent; }}
QMenuBar::item:selected {{ background: #1C1C1C; color: {TEXT}; }}
QMenu {{ background: {SURFACE}; border: 1px solid {LINE3}; padding: 6px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: #262626; }}
QMenu::item:disabled {{ color: #5A5A5A; }}
QMenu::separator {{ height: 1px; background: {LINE2}; margin: 5px 6px; }}
#header {{ background: {BG}; border-top: 1px solid {LINE}; border-bottom: 1px solid {LINE}; }}
#rail {{ background: {PANEL}; border-left: 1px solid {LINE}; }}
#empty {{ background: {BG}; }}
QLabel#fileName {{ font-size: 14px; font-weight: 600; }}
QLabel#meta, QLabel#keys {{ color: {MUTED}; font-size: 12px; }}
QLabel#meta {{ font-family: "{MONO}"; }}
QLabel#time {{ font-family: "{MONO}"; font-size: 14px; }}
QLabel#duration {{ font-family: "{MONO}"; font-size: 14px; color: {MUTED}; }}
QLabel#h2 {{ font-size: 13px; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; font-size: 12px; }}
QLabel#monoSmall {{ font-family: "{MONO}"; font-size: 11px; }}
QLabel#monoMuted {{ font-family: "{MONO}"; font-size: 11px; color: {MUTED}; }}
QLabel#emptyTitle {{ font-size: 20px; font-weight: 600; }}
QLabel#hint {{ color: {TEXT2}; font-size: 12px; }}

QPushButton {{ background: transparent; border: 1px solid #2A2A2A; border-radius: 16px;
  padding: 0 12px; min-height: 32px; color: {TEXT2}; font-size: 12px; }}
QPushButton:hover {{ border-color: #4A4A4A; color: {TEXT}; }}
QPushButton:disabled {{ color: #4E4E4E; border-color: #1E1E1E; }}
QPushButton#setIn {{ background: {GREEN_BG}; color: {GREEN}; border: 1px solid {GREEN_LINE};
  border-radius: 19px; min-height: 38px; padding: 0 16px; font-size: 13px; font-weight: 600; }}
QPushButton#setIn:hover {{ border-color: {GREEN}; }}
QPushButton#setOut {{ background: {RED_BG}; color: {RED}; border: 1px solid {RED_LINE};
  border-radius: 19px; min-height: 38px; padding: 0 16px; font-size: 13px; font-weight: 600; }}
QPushButton#setOut:hover {{ border-color: {RED}; }}
QPushButton#setIn:disabled, QPushButton#setOut:disabled {{ background: #141414; color: #4E4E4E; border-color: #1E1E1E; }}
QPushButton#play {{ background: {TEXT}; border: none; border-radius: 23px; padding: 0;
  min-width: 46px; max-width: 46px; min-height: 46px; max-height: 46px; }}
QPushButton#play:hover {{ background: #FFFFFF; }}
QPushButton#play:disabled {{ background: #2A2A2A; }}
QPushButton#iconBtn {{ border: none; border-radius: 18px; padding: 0;
  min-width: 36px; max-width: 36px; min-height: 36px; max-height: 36px; }}
QPushButton#iconBtn:hover {{ background: #1C1C1C; }}
QPushButton#primary {{ background: {TEXT}; color: {BG}; border: none; border-radius: 22px;
  min-height: 44px; font-size: 14px; font-weight: 600; }}
QPushButton#primary:hover {{ background: #FFFFFF; }}
QPushButton#primary:disabled {{ background: #2A2A2A; color: #6A6A6A; }}
QPushButton#navBack {{ border: 1px solid {LINE3}; border-radius: 16px; padding: 0 14px 0 10px;
  min-height: 32px; color: {TEXT}; font-size: 13px; font-weight: 600; }}
QPushButton#navBack:hover {{ background: #1C1C1C; border-color: #4A4A4A; }}
QPushButton#link {{ border: none; color: {MUTED}; padding: 0 4px; min-height: 28px; }}
QPushButton#link:hover {{ color: {TEXT}; }}
QPushButton#dashed {{ border: 1px dashed {DASH}; border-radius: 8px; min-height: 40px; color: {TEXT}; }}
QPushButton#dashed:hover {{ border-color: #5A5A5A; }}
QPushButton#amber {{ background: {AMBER}; color: {BG}; border: none; border-radius: 8px;
  min-height: 36px; font-weight: 600; }}
QPushButton#amber:hover {{ background: #FFC66E; }}
QPushButton#outline {{ border: 1px solid {DASH}; border-radius: 8px; min-height: 36px; color: {TEXT}; }}
QPushButton#pill {{ border: 1px solid {DASH}; border-radius: 22px; min-height: 44px; padding: 0 20px;
  color: {TEXT}; font-size: 13px; }}
QPushButton#pill:hover {{ border-color: #5A5A5A; }}
QPushButton#copyBig {{ background: {AMBER}; color: {BG}; border: none; border-radius: 10px;
  min-height: 44px; font-size: 14px; font-weight: 700; }}
QPushButton#copyBig:hover {{ background: #FFC66E; }}
QPushButton#rowPlay {{ border: 1px solid {LINE3}; border-radius: 15px; padding: 0;
  min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; }}
QPushButton#rowX {{ border: none; border-radius: 15px; padding: 0;
  min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; }}
QPushButton#rowX:hover {{ background: #222222; }}

#sectionRow {{ border: 1px solid #222222; border-radius: 8px; background: transparent; }}
#sectionRow[active="true"] {{ border-color: {TEXT}; background: #1A1A1A; }}
#card {{ background: {SURFACE}; border: 1px solid {LINE2}; border-radius: 10px; }}
#savedCard {{ background: {AMBER_BG}; border: 1px solid {AMBER_LINE}; border-radius: 10px; }}
#segGroup {{ background: {BG}; border-radius: 8px; }}
#emptyNote {{ border: 1px dashed {LINE3}; border-radius: 8px; }}
#divider {{ background: #1F1F1F; }}
#clipCard {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px; }}
#clipCard:hover {{ border-color: #4A4A4A; background: {SURFACE}; }}
#clipCard[open="true"] {{ border: 1px solid {TEXT}; }}
QLabel#openBadge {{ background: {TEXT}; color: {BG}; border-radius: 9px; padding: 1px 8px;
  font-size: 11px; font-weight: 700; }}
QLabel#cardThumb {{ background: {SURFACE}; border-top-left-radius: 9px; border-top-right-radius: 9px; }}
QLabel#cardTitle {{ font-size: 14px; font-weight: 600; }}
QLabel#cardMeta {{ font-family: "{MONO}"; font-size: 11px; color: {MUTED}; }}
QLabel#badge {{ background: {GREEN_BG}; color: {GREEN}; border: 1px solid {GREEN_LINE}; border-radius: 9px;
  padding: 1px 8px; font-size: 11px; font-weight: 600; }}
QLabel#bitrateValue {{ font-size: 13px; font-weight: 700; }}
QLabel#scaleLabel {{ font-size: 10px; color: #6A6A6A; }}
QLabel#dayHeading {{ font-size: 15px; font-weight: 600; }}

QComboBox {{ background: {BG}; border: 1px solid {LINE2}; border-radius: 8px; padding: 0 10px;
  min-height: 30px; font-size: 12px; }}
QComboBox:hover {{ border-color: #3A3A3A; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: url({chevron}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {LINE3};
  selection-background-color: #2A2A2A; outline: none; padding: 4px; }}
QLineEdit#mbBox {{ background: {BG}; border: 1px solid {LINE2}; border-radius: 8px; padding: 0 6px;
  min-height: 30px; font-family: "{MONO}"; font-size: 13px; }}
QLineEdit#mbBox:focus {{ border-color: #4A4A4A; }}
QSpinBox {{ background: {BG}; border: 1px solid {LINE2}; border-radius: 8px; padding: 0 8px;
  min-height: 30px; font-family: "{MONO}"; font-size: 12px; }}
QCheckBox {{ font-size: 12px; spacing: 8px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 1px solid #5A5A5A; background: {BG}; }}
QCheckBox::indicator:checked {{ background: {AMBER}; border-color: {AMBER}; image: url({check}); }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2A2A2A; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QSlider#volume {{ min-height: 20px; }}
QSlider#volume::groove:horizontal {{ background: {LINE2}; height: 4px; border-radius: 2px; }}
QSlider#volume::sub-page:horizontal {{ background: {TEXT2}; height: 4px; border-radius: 2px; }}
QSlider#volume::sub-page:horizontal:disabled {{ background: #3A3A3A; }}
QSlider#volume::handle:horizontal {{ background: {TEXT}; width: 12px; height: 12px; margin: -4px 0;
  border-radius: 6px; }}
QSlider#volume::handle:horizontal:hover {{ background: #FFFFFF; }}
QSlider#volume::handle:horizontal:disabled {{ background: #4E4E4E; }}
QProgressBar {{ background: {BG}; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }}
QProgressBar::chunk {{ background: {AMBER}; border-radius: 3px; }}
QToolTip {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {LINE3}; padding: 4px 8px; }}
QMessageBox {{ background: {SURFACE}; }}
"""
