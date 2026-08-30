"""Procedural ORIGINAL theater-overlay characters (RGBA PNG, pure stdlib).

Solo v1: one bot silhouette at the bottom of the frame — original design
(a dome-headed bot with an antenna), drawn procedurally so it scales to any
output width. Zero copyrighted assets.
"""
import random
import struct
import zlib
from pathlib import Path


def _chunk(tag: bytes, data: bytes) -> bytes:
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_theater(out: Path, width: int = 640, height: int | None = None) -> Path:
    """Draw the theater strip. Height scales with width (~16%)."""
    if out.exists():
        return out
    W = width
    H = height or max(60, int(W * 0.16))
    random.seed(7)

    heads = []
    n = max(3, W // 150)
    for i in range(n):
        cx = int(W * (i + 0.5) / n + random.uniform(-12, 12))
        r = int(H * random.uniform(0.16, 0.22))
        cy = int(H * 0.42)
        heads.append((cx, cy, r, i))

    seat_top = int(H * 0.62)
    rows = bytearray()
    for y in range(H):
        rows += b"\x00"  # filter none
        for x in range(W):
            a = 0
            if y >= seat_top:
                a = 255
            else:
                # scalloped seat backs
                span = int(W / 14)
                cx = ((x + span // 2) % span) - span // 2
                r = span // 2
                if cx * cx + (y - seat_top) ** 2 <= r * r and y >= seat_top - r:
                    a = 255
                for hx, hy, hr, i in heads:
                    dx, dy = x - hx, y - hy
                    # dome head: circle with flat-ish top
                    if dx * dx + dy * dy <= hr * hr:
                        a = 255
                    # antenna on the middle bot
                    if i == n // 2 and abs(dx) <= 2 and 0 <= hy - hr - dy <= int(hr * 0.8):
                        a = 255
                    if i == n // 2 and dx * dx + (dy + hr + int(hr * 0.8)) ** 2 <= 9:
                        a = 255
                    # shoulders
                    if (dx / (hr * 1.7)) ** 2 + ((y - (hy + hr * 1.3)) / (hr * 1.2)) ** 2 <= 1.0:
                        a = 255
            rows += bytes((0, 0, 0, a))

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += _chunk(b"IEND", b"")
    out.write_bytes(png)
    return out
