"""Generate installer/app.ico.

    python installer\\make_icon.py

Hand-rolled PNG + ICO through ``zlib`` and ``struct``.  Adding Pillow just to
draw one icon that is generated once would put an image library into a project
whose whole dependency list is deliberately short.

The mark is a ¥ on a dark teal square — money, locally.
"""

from __future__ import annotations

from pathlib import Path
import struct
import zlib

BACKGROUND = (15, 79, 86)
FOREGROUND = (233, 196, 106)
SIZES = (16, 32, 48, 64, 128, 256)


def _png(size: int) -> bytes:
    margin = max(2, size // 16)
    stroke = max(1, size // 10)
    centre = size // 2
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray([0])  # PNG per-scanline filter: none
        for x in range(size):
            colour = BACKGROUND
            if margin <= x < size - margin and margin <= y < size - margin:
                stem = abs(x - centre) <= stroke // 2 and size * 0.30 <= y <= size * 0.78
                bar_upper = (
                    abs(y - size * 0.52) <= stroke / 2
                    and size * 0.28 <= x <= size * 0.72
                )
                bar_lower = (
                    abs(y - size * 0.66) <= stroke / 2
                    and size * 0.28 <= x <= size * 0.72
                )
                diagonal = any(
                    abs((x - centre) - sign * (y - size * 0.30) * 0.75) <= stroke / 2
                    and size * 0.28 <= y <= size * 0.52
                    for sign in (-1, 1)
                )
                if stem or bar_upper or bar_lower or diagonal:
                    colour = FOREGROUND
            row += bytes(colour)
        rows.append(bytes(row))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    images = [_png(size) for size in SIZES]
    # PNG-compressed ICO entries are valid from Windows Vista onwards.
    header = struct.pack("<HHH", 0, 1, len(SIZES))
    offset = len(header) + 16 * len(SIZES)
    directory = bytearray()
    for size, data in zip(SIZES, images):
        dimension = size if size < 256 else 0  # 0 means 256 in the ICO format
        directory += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 24, len(data), offset
        )
        offset += len(data)
    target = Path(__file__).resolve().parent / "app.ico"
    target.write_bytes(header + bytes(directory) + b"".join(images))
    print(f"已生成 {target}  {target.stat().st_size} 字节  含 {len(SIZES)} 种尺寸")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
