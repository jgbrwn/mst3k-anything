"""Animated procedural theater overlay (original characters, RGBA).

Draws N frame variants with subtle head offsets (reaction bobbing), then
concatenates them into an overlay video that ffmpeg loops over the source
for the full runtime. Stdlib-only PNG writer; no Pillow needed.
"""
import math
import random
import struct
import subprocess
import zlib
from pathlib import Path

from . import config


def _chunk(tag: bytes, data: bytes) -> bytes:
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _draw(out: Path, width: int, height: int, phase: float) -> None:
    """Draw one RGBA frame of the theater strip, phase=0..1 modulates head bob."""
    if out.exists():
        return
    W, H = width, height
    random.seed(7 + int(phase * 8))
    heads = []
    n = max(3, W // 150)
    for i in range(n):
        cx = int(W * (i + 0.5) / n + random.uniform(-12, 12))
        r = int(H * random.uniform(0.16, 0.22))
        # per-bot bob phase — some lead, some lag, varying amount
        bob = int(math.sin((phase * 2 * math.pi) + i * 1.3) * r * 0.15)
        cy = int(H * 0.42) + bob
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
                    if dx * dx + dy * dy <= hr * hr:
                        a = 255
                    if i == n // 2 and abs(dx) <= 2 and 0 <= hy - hr - dy <= int(hr * 0.8):
                        a = 255
                    if i == n // 2 and dx * dx + (dy + hr + int(hr * 0.8)) ** 2 <= 9:
                        a = 255
                    if (dx / (hr * 1.7)) ** 2 + ((y - (hy + hr * 1.3)) / (hr * 1.2)) ** 2 <= 1.0:
                        a = 255
            rows += bytes((0, 0, 0, a))

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
    png += _chunk(b"IEND", b"")
    out.write_bytes(png)


def make_theater(out: Path, width: int = 640, height: int | None = None) -> Path:
    """Static PNG fallback. Height scales with width (~16%)."""
    H = height or max(60, int(width * 0.16))
    _draw(out, width, H, 0.0)
    return out


def make_animated_theater(job: dict, video_out: Path, frames: int = 16,
                          width: int = 640, height: int | None = None) -> Path:
    """Render N phase-offset PNG frames and concatenate into a looped overlay
    video (webm/VP9 with alpha, universally readable by ffmpeg+(Adobe swirl)
    and browsers). Cached under the job dir."""
    if video_out.exists():
        return video_out
    H = height or max(60, int(width * 0.16))
    d = job["dir"] / "theater_frames"
    d.mkdir(exist_ok=True)
    for i in range(frames):
        _draw(d / f"f{i:03d}.png", width, H, i / frames)
    subprocess.run([
        config.tool("ffmpeg"), "-y", "-v", "error",
        "-framerate", str(max(1, frames // 2)),  # slow bob (≈0.5 Hz per cycle)
        "-i", str(d / "f%03d.png"),
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-auto-alt-ref", "0",
        "-crf", "28", "-b:v", "0", str(video_out),
    ], check=True)
    # clean up frame PNGs (keep the webm as the artifact)
    for f in d.glob("*.png"):
        f.unlink()
    d.rmdir()
    return video_out
