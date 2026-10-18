# ClipTrimmer

Cut the good moment out of a game replay and shrink it to fit Discord (or any size), in a native Windows app.

ClipTrimmer 2.0 is a Qt + mpv rewrite of the original browser-based ClipTrimmer (still in this repo's history).
It's built around OBS replay-buffer clips: 2-minute HEVC 1080p120 mp4s with three named audio tracks, saved to
`Videos\OBS`.

## What it does

- **Your clips**: opens to every replay in the folder as a card, grouped by day (Today, Yesterday,
  Wednesday, Oct 3…), newest first. Each card shows the moment the replay was saved, its time and length, and a
  green **Trimmed** tag once you've exported from it. The clip that's open in the editor is outlined and
  tagged **Open**. Right-click a card to play it, show it in Explorer or move it to the Recycle Bin.
- **Thumbnails that show the moment**: each replay gets a *cover picture*, the frame from 4 seconds before the
  end (just before you pressed save). File Explorer uses it as the thumbnail, too.
- **Trim**: mark sections with **Set In / Set Out** (or I / O) on a frame strip, drag the flags to adjust,
  play a section on its own. Several sections are joined in timeline order.
- **Export to a size**: 10 MB (Discord), 50 MB (Nitro), 512 MB (X) or a custom size, with an optional
  resolution cap and audio on/off. Two-pass x264 hits the size; the **Estimated bitrate** card shows the
  quality you'll get. After exporting, **Copy file** puts it on the clipboard to paste into Discord.

## Using it

| Where | What |
|---|---|
| Top-left button | **‹ All clips** in the editor, **‹ Back to clip** in the clip list. Same spot on both screens. |
| Ctrl+B / Esc | All clips / back to the clip |
| Ctrl+L | Open the newest replay |
| I / O / N | Set In / Set Out / new section at the playhead |
| Space or K, P | Play/pause, play the selected section |
| ← → (or , .) | One frame back/forward; Shift+← → or J/L = 1 second |
| Delete, Ctrl+Z / Ctrl+Y | Delete the selected section, undo/redo |
| Ctrl+E, Ctrl+Shift+C | Export, copy the exported file |
| F1 | All shortcuts |

Exports go to the Desktop unless you pick another folder (File → Output folder…). The replays folder is
`Videos\OBS` by default (File → Replays folder…, or **Change…** in the clip list).

Only one ClipTrimmer runs at a time: starting it again (e.g. from PadDeck) brings the open window to the
front, and opens the clip if one was given.

### Command line

```
ClipTrimmer.exe [clip.mp4]          open a clip
ClipTrimmer.exe --covers [clip …]   add cover pictures without opening a window
```

`--covers` with no file does the whole replays folder. With a file it waits for the file to finish being
written first. [PadDeck](https://github.com/lefewix/PadDeck) runs it right after it saves an OBS replay.

## How covers stay safe

Adding a cover rewrites the mp4, so it's careful:

- It's a **lossless remux** (`ffmpeg -c copy`): video and audio packets are untouched. Audio track names
  (OBS's "Game audio", "Mic"…) are carried over.
- It goes to a temp file next to the clip (`~cover <pid> <name>`), which is **checked** (same length, same
  streams plus the picture, not smaller) before it replaces the original. Created/modified dates are kept.
- A lock file (`~cover <name>.lock`) stops two programs working on the same clip. Files changed in the last
  10 seconds (OBS may still be writing) and the clip open in the player are skipped. If anything holds the
  file open, the swap simply fails and nothing changes. Temp files left by a crash are cleaned up after 10 minutes.
- Only `.mp4` / `.m4v` get covers; other formats just get a card picture.

## Building

Needs Python 3.12 and the binaries below.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File build.ps1     # → dist\ClipTrimmer\ClipTrimmer.exe
```

Run `build.ps1` with `-File` as shown: started with `&` from a PowerShell prompt, PowerShell treats
PyInstaller's progress output as an error and stops.

Run from source: `.venv\Scripts\pythonw.exe ClipTrimmer.pyw [clip]`.

### `bin\` (not in git; too big for GitHub)

| File | Source |
|---|---|
| `ffmpeg.exe`, `ffprobe.exe` | [BtbN FFmpeg builds](https://github.com/BtbN/FFmpeg-Builds/releases), win64 **gpl** (needs libx264) |
| `libmpv-2.dll` | [shinchiro mpv builds](https://github.com/shinchiro/mpv-winbuild-cmake/releases), `mpv-dev-x86_64` |

## Tests

```powershell
.venv\Scripts\python.exe tests\test_engine.py  <workdir>        # export engine + sections (36)
.venv\Scripts\python.exe tests\test_library.py <workdir>        # clip list, covers, locks, cleanup (29)
.venv\Scripts\python.exe tests\ui_smoke.py <workdir> <shots>    # drives the real window (58); run test_engine first
```

They make their own synthetic clips in `<workdir>` and never touch your replays folder. `ui_smoke` opens a window
for about a minute and uses its own settings store.

## Layout

```
ClipTrimmer.pyw      entry point (sets up PATH for bin\)
clipt\app.py         main window, header/navigation, single instance, --covers
clipt\browser.py     the clip list (cards, cover loader, folder watcher)
clipt\library.py     listing, day grouping, cover remux
clipt\rail.py        right panel: sections, size, bitrate card, export states
clipt\strip.py       frame strip with In/Out flags
clipt\engine.py      ffmpeg export (cut, join, two-pass to size)
clipt\model.py       sections + undo
clipt\player.py      mpv playback in a QOpenGLWidget
clipt\theme.py       colours, fonts, icons, style sheet
```

Fonts: Instrument Sans and JetBrains Mono (SIL Open Font License), bundled in `clipt\fonts`.
