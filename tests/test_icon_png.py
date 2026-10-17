"""portal.dat icons: red, green, blue per pixel; black transparent; the white outline round the shape drawn black."""

import struct
import unittest
import zlib

from companion.dat import icon_png


class FakeDat:
    def __init__(self, w, h, pixels):
        self.data = struct.pack("<III", 0x06000001, w, h) + bytes(c for p in pixels for c in p)

    def get(self, _id):
        return self.data


def decode(png):
    """[(r, g, b, a)] of a PNG icon_png made (no filters, one IDAT)."""
    w, h = struct.unpack(">II", png[16:24])
    i = png.index(b"IDAT")
    n = struct.unpack(">I", png[i - 4:i])[0]
    raw = zlib.decompress(png[i + 4:i + 4 + n])
    out = []
    for y in range(h):
        row = raw[y * (1 + 4 * w) + 1:(y + 1) * (1 + 4 * w)]
        out += [tuple(row[4 * x:4 * x + 4]) for x in range(w)]
    return out


class IconTest(unittest.TestCase):
    def test_colours_in_order_black_clear_white_outline_black(self):
        W, B, R = (255, 255, 255), (0, 0, 0), (200, 30, 20)
        # 3x3: black corners, a white edge, a red middle with white inside it (not touching the background)
        pixels = [B, W, B,
                  W, W, R,
                  B, R, R]
        px = decode(icon_png(FakeDat(3, 3, pixels), 1))
        self.assertEqual(px[0], (0, 0, 0, 0))                 # black: transparent
        self.assertEqual(px[5], (200, 30, 20, 255))           # red stays red (not swapped to blue)
        self.assertEqual(px[1], (0, 0, 0, 255))               # white next to the background: black outline
        self.assertEqual(px[4], (255, 255, 255, 255))         # white inside the shape: kept


if __name__ == "__main__":
    unittest.main()
