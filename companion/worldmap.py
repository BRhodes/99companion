"""World map: a terrain image of Dereth rendered from cell.dat, and map coordinates.

The image is MapAC's renderer (DatInspector/render_map.py in the server repo): every landblock's 9x9
terrain vertices coloured by terrain type (roads drawn over), hill-shaded by height. 2041 x 2041 px,
north up, one pixel per 24 m vertex:

    landblock 0xXXYY -> pixels x = XX*8 .. XX*8+8, y = 2040 - YY*8 - 8 .. 2040 - YY*8

Map coordinates (Util.cpp GetCoordString): EW = (blockX*192 + x) / 240 - 101.95, likewise NS from the
block Y and local y; positive is north/east.
"""
import math
from pathlib import Path

LANDSIZE = 2041  # 255 * 8 + 1
COLOR_CORRECTION, LIGHT_CORRECTION, AMBIENT = 70.0, 2.25, 64.0
# terrain type -> RGB, index 32 = road (RegionHelper.cs ACDM palette, the pre-ToD look)
PALETTE = [
    (101, 81, 46), (68, 80, 26), (133, 133, 145), (62, 84, 15), (83, 46, 11),
    (65, 43, 29), (48, 44, 50), (104, 77, 41), (68, 50, 14), (53, 91, 3),
    (166, 116, 59), (152, 152, 108), (157, 107, 60), (128, 81, 49), (86, 97, 65),
    (234, 247, 254), (63, 110, 133), (23, 81, 58), (34, 82, 101), (46, 78, 96),
    (46, 61, 84), (37, 49, 9), (63, 110, 133), (33, 41, 30), (101, 81, 46),
    (47, 36, 36), (76, 77, 76), (195, 224, 239), (116, 135, 87), (64, 99, 49),
    (68, 41, 47), (101, 81, 46), (138, 131, 113),
]


def coords(cell, x, y):
    """(NS, EW) map coordinates of an outdoor position, or None indoors (dungeons have none)."""
    if (cell & 0xFFFF) >= 0x100:
        return None
    bx, by = cell >> 24, (cell >> 16) & 0xFF
    return (by * 192 + y) / 240 - 101.95, (bx * 192 + x) / 240 - 101.95


def coord_text(ns, ew):
    return f"{abs(ns):.1f}{'N' if ns >= 0 else 'S'}, {abs(ew):.1f}{'E' if ew >= 0 else 'W'}"


def distance(a, b):
    """Map units between two (NS, EW) points (1 unit = 240 m)."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _landheight(np, h):
    h = h.astype(np.float64)
    return np.where(h <= 200, h * 2.0, np.where(h <= 240, 400 + (h - 200) * 4.0,
                    np.where(h <= 250, 560 + (h - 240) * 8.0, 640 + (h - 250) * 10.0)))


def _shift(np, a, dy, dx):
    out = np.zeros_like(a)
    ys = slice(max(0, -dy), a.shape[0] - max(0, dy))
    xs = slice(max(0, -dx), a.shape[1] - max(0, dx))
    yd = slice(max(0, dy), a.shape[0] - max(0, -dy))
    xd = slice(max(0, dx), a.shape[1] - max(0, -dx))
    out[ys, xs] = a[yd, xd]
    return out


def render(cell_dat):
    """RGB image (PIL) of the whole world from a DatFile over cell.dat."""
    import numpy as np
    from PIL import Image
    types = np.zeros((LANDSIZE, LANDSIZE), dtype=np.uint16)
    z = np.zeros((LANDSIZE, LANDSIZE), dtype=np.float64)
    used = np.zeros((LANDSIZE, LANDSIZE), dtype=bool)
    for fid in cell_dat.index:
        if fid & 0xFFFF != 0xFFFF:
            continue
        raw = cell_dat.get(fid)
        if not raw or len(raw) < 8 + 162 + 81:
            continue
        # CLandBlock: id, has_objects, u16 terrain[81], u8 height[81]; vertex index x*9 + y (x east, y north)
        terrain = np.frombuffer(raw, dtype="<u2", count=81, offset=8).reshape(9, 9)
        height = np.frombuffer(raw, dtype="<u1", count=81, offset=170).reshape(9, 9)
        bx, by = fid >> 24, (fid >> 16) & 0xFF
        top = LANDSIZE - by * 8 - 1
        rows, cols = slice(top - 8, top + 1), slice(bx * 8, bx * 8 + 9)
        types[rows, cols] = terrain.T[::-1, :]
        z[rows, cols] = _landheight(np, height.T[::-1, :])
        used[rows, cols] = True
    pal = np.array(PALETTE, dtype=np.float64)
    dxp, dxm = _shift(np, z, 0, 1) - z, _shift(np, z, 0, -1) - z
    dyp, dym = _shift(np, z, 1, 0) - z, _shift(np, z, -1, 0) - z
    uxp, uxm, uyp, uym = (_shift(np, used, 0, 1), _shift(np, used, 0, -1), _shift(np, used, 1, 0), _shift(np, used, -1, 0))
    m1, m2, m3, m4 = ((a & b).astype(np.float64) for a, b in ((uxp, uyp), (uxm, uyp), (uxm, uym), (uxp, uym)))
    v0 = -dxp * m1 + dxm * m2 + dxm * m3 - dxp * m4
    v1 = -dyp * m1 - dyp * m2 + dym * m3 + dym * m4
    v2 = 12.0 * (m1 + m2 + m3 + m4)
    denom = np.sqrt(2.0 * (v0 * v0 + v1 * v1 + v2 * v2))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(denom > 0, (-v0 - v1) / denom, 0.0)
    light = (ratio * 128.0 + 128.0) * LIGHT_CORRECTION + AMBIENT
    idx = np.clip(np.where(types & 0x3 != 0, 32, (types & 0xFF) >> 2), 0, 32)
    rgb = np.clip(pal[idx] * (COLOR_CORRECTION / 100.0) * (light / 256.0)[..., None], 0, 255).astype(np.uint8)
    rgb[~used] = 0
    return Image.fromarray(rgb, "RGB")


def map_png(cell_dat_path, cache_path):
    """PNG bytes of the world map, rendered once and cached on disk."""
    cache = Path(cache_path)
    if cache.exists() and cache.stat().st_mtime >= Path(cell_dat_path).stat().st_mtime:
        return cache.read_bytes()
    from .dat import DatFile
    image = render(DatFile(cell_dat_path))
    cache.parent.mkdir(parents=True, exist_ok=True)
    image.save(cache, "PNG", optimize=True)
    return cache.read_bytes()
