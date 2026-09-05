"""Generate the add-in's PNG icons with the stdlib only (no Pillow needed).

Draws a rounded indigo tile with a white waveform mark, supersampled 4x for
smooth edges. Run: python scripts/make_icons.py
"""
from __future__ import annotations

import pathlib
import struct
import zlib

BG = (79, 70, 229)      # indigo-600
FG = (255, 255, 255)
SS = 4                  # supersampling factor


def _rounded_rect(x: float, y: float, w: float, h: float, r: float, px: float, py: float) -> bool:
    if not (x <= px <= x + w and y <= py <= y + h):
        return False
    for cx, cy in ((x + r, y + r), (x + w - r, y + r), (x + r, y + h - r), (x + w - r, y + h - r)):
        inside_x = (px < cx) == (cx == x + r)
        inside_y = (py < cy) == (cy == y + r)
        if inside_x and inside_y:
            return (px - cx) ** 2 + (py - cy) ** 2 <= r * r
    return True


def _shape(px: float, py: float, n: float) -> tuple[int, int, int] | None:
    """Return the colour at (px, py) in an n x n tile, or None for transparent."""
    if not _rounded_rect(0.06 * n, 0.06 * n, 0.88 * n, 0.88 * n, 0.22 * n, px, py):
        return None
    # Five waveform bars of varying height, centred.
    heights = (0.24, 0.46, 0.66, 0.40, 0.20)
    bar_w = 0.075 * n
    gap = 0.055 * n
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x0 = (n - total) / 2
    for i, hf in enumerate(heights):
        bx = x0 + i * (bar_w + gap)
        bh = hf * n
        by = (n - bh) / 2
        if _rounded_rect(bx, by, bar_w, bh, bar_w / 2, px, py):
            return FG
    return BG


def render(n: int) -> bytes:
    """Render an n x n RGBA image, supersampled, as raw scanline bytes."""
    rows = []
    inv = 1.0 / (SS * SS)
    for y in range(n):
        row = bytearray(b"\x00")  # PNG filter type 0
        for x in range(n):
            r = g = b = a = 0.0
            for sy in range(SS):
                for sx in range(SS):
                    px = x + (sx + 0.5) / SS
                    py = y + (sy + 0.5) / SS
                    c = _shape(px, py, float(n))
                    if c is not None:
                        r += c[0]
                        g += c[1]
                        b += c[2]
                        a += 255
            if a == 0:
                row += b"\x00\x00\x00\x00"
            else:
                # Un-premultiply so edge pixels keep the fill colour.
                k = 255.0 / a
                row += bytes((
                    min(255, round(r * k)),
                    min(255, round(g * k)),
                    min(255, round(b * k)),
                    round(a * inv),
                ))
        rows.append(bytes(row))
    return b"".join(rows)


def write_png(path: pathlib.Path, n: int) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(render(n), 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


if __name__ == "__main__":
    out = pathlib.Path(__file__).resolve().parent.parent / "taskpane" / "assets"
    out.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 64, 80, 128):
        write_png(out / f"icon-{size}.png", size)
        print(f"wrote {out / f'icon-{size}.png'}")
