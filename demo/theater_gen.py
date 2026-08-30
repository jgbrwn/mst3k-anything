"""Generate an MST3K-style theater-silhouette overlay PNG (RGBA, pure stdlib)."""
import zlib, struct, random

W, H = 640, 90

def chunk(tag, data):
    c = struct.pack(">I", len(data)) + tag + data
    return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

random.seed(7)
# heads: (cx, cy, r) — silhouetted heads poking above the seat line
heads = [(70, 46, 17), (168, 41, 15), (318, 36, 20), (452, 43, 15), (566, 46, 16)]

SEAT_TOP = 62          # y where the solid band begins
SCALLOP_R = 20
SCALLOP_SPAN = 40

rows = bytearray()
for y in range(H):
    row = bytearray(b"\x00")  # PNG filter: none
    for x in range(W):
        a = 0
        if y >= SEAT_TOP:
            a = 255
        else:
            # scalloped seat tops: arcs centered on y=SEAT_TOP
            cx = ((x + SCALLOP_SPAN // 2) % SCALLOP_SPAN) - SCALLOP_SPAN // 2
            if cx * cx + (y - SEAT_TOP) ** 2 <= SCALLOP_R * SCALLOP_R and y >= SEAT_TOP - SCALLOP_R:
                a = 255
            for hx, hy, hr in heads:
                if (x - hx) ** 2 + (y - hy) ** 2 <= hr * hr:
                    a = 255
                # shoulders: ellipse below each head
                if (x - hx) ** 2 / ((hr * 1.7) ** 2) + (y - (hy + hr * 1.4)) ** 2 / ((hr * 1.3) ** 2) <= 1.0:
                    a = 255
        row += bytes((0, 0, 0, a))
    rows += row

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0))  # 8-bit RGBA
png += chunk(b"IDAT", zlib.compress(bytes(rows), 9))
png += chunk(b"IEND", b"")

with open("/tmp/p9/theater.png", "wb") as f:
    f.write(png)
print("wrote /tmp/p9/theater.png", len(png), "bytes")
