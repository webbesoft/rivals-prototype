"""Generate RivalSpy's radar icons with Python's standard library.

Run from any directory: python bin/generate-pwa-icons.py
The artwork stays inside the central maskable safe area.
"""

import math
import struct
import zlib
from pathlib import Path

OUTPUT = Path(__file__).resolve().parent.parent / "assets/images/pwa"
BACKGROUND = (15, 79, 47)
ACCENT = (185, 236, 85)
FOREGROUND = (245, 243, 232)


def color(x, y):
    radius = math.hypot(x - 0.5, y - 0.5)
    if math.hypot(x - 0.64, y - 0.36) < 0.043:
        return ACCENT
    if abs(radius - 0.29) < 0.012 or abs(radius - 0.18) < 0.009:
        return FOREGROUND
    if abs(x - 0.5) < 0.009 and 0.17 < y < 0.83:
        return FOREGROUND
    if abs(y - 0.5) < 0.009 and 0.17 < x < 0.83:
        return FOREGROUND
    if 0.49 < x < 0.72 and abs(x + y - 1) < 0.018:
        return ACCENT
    return BACKGROUND


def chunk(kind, data):
    return (
        struct.pack("!I", len(data))
        + kind
        + data
        + struct.pack("!I", zlib.crc32(kind + data))
    )


def generate(size):
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            samples = [
                color((x + dx) / size, (y + dy) / size)
                for dx in (0.25, 0.75)
                for dy in (0.25, 0.75)
            ]
            rows.extend(
                round(sum(c[channel] for c in samples) / 4) for channel in range(3)
            )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, size in (
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("icon-maskable-512.png", 512),
        ("apple-touch-icon.png", 180),
    ):
        (OUTPUT / name).write_bytes(generate(size))
