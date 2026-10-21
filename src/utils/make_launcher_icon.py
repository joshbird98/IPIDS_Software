"""Write src/launcher/sindie.ico, the icon of the Windows shortcuts the launcher makes, from the
drawing in src/launcher/icon.py: each size drawn on its own (sharper than one picture scaled), stored
as PNG images in the .ico container (Windows Vista and later read them).

    python src/utils/make_launcher_icon.py
"""
import os
import struct
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT = os.path.join(ROOT, "src", "launcher", "sindie.ico")
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def ico_bytes(pngs: dict) -> bytes:
    """An .ico file holding PNG images, {size: png bytes}."""
    sizes = sorted(pngs)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(header) + 16 * len(sizes)
    entries, data = b"", b""
    for size in sizes:
        png = pngs[size]
        dim = 0 if size >= 256 else size            # (0 means 256)
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    return header + entries + data


def main():
    sys.path.insert(0, ROOT)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])      # (pixmaps need one)
    from src.launcher import icon
    pngs = {}
    for size in ICO_SIZES:
        app.processEvents()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        icon.pixmap(size).save(buf, "PNG")
        pngs[size] = bytes(buf.data())
    with open(OUT, "wb") as f:
        f.write(ico_bytes(pngs))
    print(f"Wrote {OUT} ({os.path.getsize(OUT)} bytes, sizes {', '.join(map(str, ICO_SIZES))})")


if __name__ == "__main__":
    main()
