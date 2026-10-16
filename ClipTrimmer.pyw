"""ClipTrimmer - cut moments out of game clips and fit them to a size.

Run with the project's venv:  .venv\\Scripts\\pythonw.exe ClipTrimmer.pyw [clip]
"""
import os
import sys
from pathlib import Path

# When frozen by PyInstaller everything lives under sys._MEIPASS.
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
BIN = BASE / "bin"
os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory") and BIN.is_dir():
    os.add_dll_directory(str(BIN))

from clipt.app import main  # noqa: E402  (needs PATH set for libmpv first)

if __name__ == "__main__":
    sys.exit(main(BIN, BASE / "clipt" / "fonts"))
