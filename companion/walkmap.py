"""Where you can walk outdoors, and the walking path between two map points.

Built once from cell.dat (each landblock's 9x9 terrain vertices) and portal.dat (the region's height
table), cached on disk. One grid cell per landscape cell (24 m, 0.1 map units), 2040 x 2040, as the
server's physics sees the ground (PhatSDK LandCell / LandBlockStruct / Transition):

  * a landblock that is entirely water (every vertex a water terrain) can't be entered at all;
    partly watered cells are waded through
  * deep water is left out too: a cell under water at all four corners that's more than DEEP cells from
    any dry ground. The server would let you wade across a sea wherever a chain of islands keeps its
    landblocks from being entirely water; routes shouldn't (rivers and shallows, narrower than about
    2 x DEEP + 1 cells, still can be crossed)
  * each cell is two triangles, split SW-NE or SE-NW by FSplitNESW; a triangle whose normal is
    steeper than PhysicsGlobals::floor_z (cos ~48 deg) can't be walked up. A cell is passable if
    either of its triangles is (half a cell is plenty of room for a character)
  * landblocks missing from cell.dat are left out

Not modelled: buildings, walls, trees and rocks; and that a steep slope can still be run DOWN.

Paths are A* over the grid (8 neighbours), then pulled tight (any straight stretch that stays on
passable cells replaces the zigzag), so the distance is close to what you'd actually run. Cells are
also labelled by connected area, so a destination you can't walk to at all is known at once.
"""
import heapq
import math
import struct
from pathlib import Path

CELLS = 2040                    # 255 landblocks x 8 cells
UNIT = 0.1                      # map units per cell (24 m)
FLOOR_Z = math.cos(3437.746770784939)
WATER = range(16, 21)           # terrain types the server counts as water (LandBlockStruct terrain_is_water)
SNAP = 6                        # cells to look around for passable ground at a point that isn't
WEIGHT = 1.3                    # A* heuristic weight: a little longer paths for far fewer cells searched
MAX_SEARCH = 400_000            # cells an A* may expand before giving up on a leg
DEEP = 3                        # cells from dry ground past which flooded water counts as too deep to cross
BLOCKS, BLOCK = 255, 0.8        # landblocks a side, and one's size in map units (192 m)
BLOCKED_CELLS = 60              # impassable cells (of 64) that make a whole landblock one a straight run can't cross
NEAREST = 600                   # cells out from an unreachable destination to look for the nearest you can reach
VERSION = 2                     # of the cached grid: bumped when what's passable changes
SQRT2 = math.sqrt(2.0)
_NEIGHBOURS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
               (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2))


def height_table(portal_dat):
    """The region's 256 land heights (metres) from portal.dat's RegionDesc 0x13000000."""
    raw = portal_dat.get(0x13000000)
    p = 12
    p += 2 + struct.unpack_from("<H", raw, p)[0]
    p = (p + 3) & ~3
    return list(struct.unpack_from("<256f", raw, p + 32))


def split_ne(x, y):
    """FSplitNESW for global cell (x east, y north): True if the cell's diagonal runs SW-NE."""
    v = (x * y * 0x0CCAC033 - x * 0x421BE3BD + y * 0x6C1AC587 - 0x519B8F25) & 0xFFFFFFFF
    return bool(v & 0x80000000)


def build(cell_dat, heights):
    """bytes of CELLS*CELLS, 1 = passable, row-major from the south-west (index y*CELLS + x)."""
    import numpy as np
    n = CELLS + 1
    z = np.zeros((n, n), dtype=np.float64)          # [x, y] vertex heights
    water = np.ones((n, n), dtype=bool)
    present = np.zeros((CELLS, CELLS), dtype=bool)  # [x, y] cells
    sea = np.zeros((CELLS, CELLS), dtype=bool)
    flooded = np.zeros((CELLS, CELLS), dtype=bool)  # [x, y] cells under water at all four corners
    table = np.array(heights, dtype=np.float64)
    for fid in cell_dat.index:
        if fid & 0xFFFF != 0xFFFF:
            continue
        raw = cell_dat.get(fid)
        if not raw or len(raw) < 8 + 162 + 81:
            continue
        terrain = np.frombuffer(raw, dtype="<u2", count=81, offset=8).reshape(9, 9)   # [vx, vy]
        height = np.frombuffer(raw, dtype="<u1", count=81, offset=170).reshape(9, 9)
        bx, by = fid >> 24, (fid >> 16) & 0xFF
        xs, ys = slice(bx * 8, bx * 8 + 9), slice(by * 8, by * 8 + 9)
        z[xs, ys] = table[height]
        wet = np.isin((terrain & 0x7F) >> 2, list(WATER))
        water[xs, ys] = wet
        flooded[bx * 8:bx * 8 + 8, by * 8:by * 8 + 8] = wet[:-1, :-1] & wet[1:, :-1] & wet[:-1, 1:] & wet[1:, 1:]
        present[bx * 8:bx * 8 + 8, by * 8:by * 8 + 8] = True
        if wet.all():
            sea[bx * 8:bx * 8 + 8, by * 8:by * 8 + 8] = True
    z00, z10, z01, z11 = z[:-1, :-1], z[1:, :-1], z[:-1, 1:], z[1:, 1:]
    xs, ys = np.meshgrid(np.arange(CELLS, dtype=np.int64), np.arange(CELLS, dtype=np.int64), indexing="ij")
    v = (xs * ys * 0x0CCAC033 - xs * 0x421BE3BD + ys * 0x6C1AC587 - 0x519B8F25) & 0xFFFFFFFF
    ne = (v & 0x80000000) != 0

    def flat(gx, gy):
        return 1.0 / np.sqrt(1.0 + (gx / 24.0) ** 2 + (gy / 24.0) ** 2) >= FLOOR_Z

    # SW-NE diagonal: (00,10,11) and (00,11,01); SE-NW: (00,10,01) and (11,01,10)
    a = np.where(ne, flat(z10 - z00, z11 - z10), flat(z10 - z00, z01 - z00))
    b = np.where(ne, flat(z11 - z01, z01 - z00), flat(z11 - z01, z11 - z10))
    ok = present & ~sea & (a | b) & ~deep_water(flooded, present, DEEP)
    return ok.T.astype(np.uint8).tobytes()           # [y, x] row-major


def deep_water(flooded, present, depth):
    """[x, y] flooded cells more than `depth` cells (8 ways) from dry ground: the flooded area eroded
    `depth` times. Off the map and missing landblocks aren't dry ground."""
    import numpy as np
    deep = flooded | ~present
    for _ in range(depth):
        p = np.pad(deep, 1, constant_values=True)
        n = p[1:-1, 1:-1].copy()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n &= p[1 + dx:p.shape[0] - 1 + dx, 1 + dy:p.shape[1] - 1 + dy]
        deep = n
    return deep & flooded


def label(grid):
    """Connected-area label per cell (0 = impassable), as a list."""
    from array import array
    n = CELLS
    labels = array("i", bytes(4 * n * n))
    current = 0
    for start in range(n * n):
        if not grid[start] or labels[start]:
            continue
        current += 1
        labels[start] = current
        stack = [start]
        while stack:
            i = stack.pop()
            x = i % n
            for j in (i - 1 if x else -1, i + 1 if x < n - 1 else -1, i - n, i + n):
                if 0 <= j < n * n and grid[j] and not labels[j]:
                    labels[j] = current
                    stack.append(j)
    return labels


def blocked_blocks(grid):
    """bytes of BLOCKS*BLOCKS (row-major from the south-west): 1 where a landblock is all but impassable
    (BLOCKED_CELLS of its 64 cells or more): open sea, or ground too steep to cross."""
    import numpy as np
    g = np.frombuffer(bytes(grid), dtype=np.uint8).reshape(CELLS, CELLS)        # [y, x]
    shut = (g == 0).reshape(BLOCKS, 8, BLOCKS, 8).sum(axis=(1, 3))              # [by, bx]
    return (shut >= BLOCKED_CELLS).astype(np.uint8).tobytes()


def load(cell_dat_path, portal_dat, cache_dir):
    """A WalkMap, built and cached the first time (takes a while: run it off the event loop)."""
    from array import array
    cache = Path(cache_dir)
    gpath, lpath = cache / f"walkgrid{VERSION}.bin", cache / f"walklabels{VERSION}.bin"
    stamp = Path(cell_dat_path).stat().st_mtime
    if gpath.exists() and lpath.exists() and gpath.stat().st_mtime >= stamp and lpath.stat().st_mtime >= stamp:
        grid = gpath.read_bytes()
        labels = array("i")
        labels.frombytes(lpath.read_bytes())
        return WalkMap(grid, labels)
    from .dat import DatFile
    grid = build(DatFile(cell_dat_path), height_table(portal_dat))
    labels = label(grid)
    cache.mkdir(parents=True, exist_ok=True)
    gpath.write_bytes(grid)
    lpath.write_bytes(labels.tobytes())
    return WalkMap(grid, labels)


class WalkMap:
    def __init__(self, grid, labels):
        self.grid = grid
        self.labels = labels
        self.cache = {}
        self.blocked = blocked_blocks(grid)

    def area(self, ns, ew):
        """The connected walkable area a point is in (its label), or None off walkable ground."""
        c = self.snap(ns, ew)
        return None if c is None else self.labels[c[1] * CELLS + c[0]]

    def nearest_in(self, ns, ew, areas, radius=NEAREST):
        """The map point (ns, ew) nearest to a spot that's walkable and in one of `areas` (labels), searching
        out to `radius` cells, or None."""
        x0, y0 = self.cell(ns, ew)
        best = None
        for r in range(radius + 1):
            if best is not None and r > best[0] ** 0.5 + 1:
                break                          # a nearer ring can't come after this
            ring = [(x0 + dx, y0 + dy) for dx in range(-r, r + 1) for dy in (-r, r)] + \
                   [(x0 + dx, y0 + dy) for dx in (-r, r) for dy in range(-r + 1, r)] if r else [(x0, y0)]
            for x, y in ring:
                if self.passable(x, y) and self.labels[y * CELLS + x] in areas:
                    d = (x - x0) ** 2 + (y - y0) ** 2
                    if best is None or d < best[0]:
                        best = (d, x, y)
        return None if best is None else self.centre(best[1], best[2])

    def crosses_blocked(self, a, b):
        """True if the straight line between two map points (ns, ew) runs through a landblock you can't
        get through (open sea, a whole block too steep to climb): a cheap check, a sample every half
        landblock, for straight-line runs (the route's estimate). The ends' own landblocks don't count."""
        def block(ns, ew):
            return int((ew + 101.95) / BLOCK), int((ns + 101.95) / BLOCK)
        ends = {block(*a), block(*b)}
        steps = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / (BLOCK / 2)))
        for k in range(1, steps):
            bx, by = block(a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)
            if (bx, by) not in ends and 0 <= bx < BLOCKS and 0 <= by < BLOCKS and self.blocked[by * BLOCKS + bx]:
                return True
        return False

    @staticmethod
    def cell(ns, ew):
        return int((ew + 101.95) / UNIT), int((ns + 101.95) / UNIT)

    @staticmethod
    def centre(x, y):
        return (y + 0.5) * UNIT - 101.95, (x + 0.5) * UNIT - 101.95

    def passable(self, x, y):
        return 0 <= x < CELLS and 0 <= y < CELLS and self.grid[y * CELLS + x] == 1

    def snap(self, ns, ew):
        """The passable cell at (or nearest, within SNAP cells of) a point, or None."""
        x, y = self.cell(ns, ew)
        if self.passable(x, y):
            return x, y
        for r in range(1, SNAP + 1):
            ring = [(x + dx, y + dy) for dx in range(-r, r + 1) for dy in (-r, r)] + \
                   [(x + dx, y + dy) for dx in (-r, r) for dy in range(-r + 1, r)]
            found = [c for c in ring if self.passable(*c)]
            if found:
                return min(found, key=lambda c: (c[0] - x) ** 2 + (c[1] - y) ** 2)
        return None

    def clear(self, a, b):
        """True if every cell the straight line between two cells' centres crosses is passable."""
        (x0, y0), (x1, y1) = a, b
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx, sy = (1 if x1 > x0 else -1), (1 if y1 > y0 else -1)
        x, y, err = x0, y0, dx - dy
        dx2, dy2 = 2 * dx, 2 * dy
        grid = self.grid
        for _ in range(dx + dy + 1):          # supercover: a diagonal step checks both side cells
            if grid[y * CELLS + x] != 1:
                return False
            e2 = 2 * err
            if e2 > -dy and e2 < dx:
                if grid[y * CELLS + x + sx] != 1 or grid[(y + sy) * CELLS + x] != 1:
                    return False
                err += -dy + dx
                x += sx
                y += sy
            elif e2 > -dy:
                err -= dy
                x += sx
            else:
                err += dx
                y += sy
            if (x, y) == (x1, y1):
                return grid[y * CELLS + x] == 1
        return True

    def walk(self, a, b):
        """Walking path between two map points (ns, ew): {distance (map units), points [(ns, ew)...]},
        or None if one isn't on walkable ground or you can't walk from one to the other."""
        ca, cb = self.snap(*a), self.snap(*b)
        if ca is None or cb is None or self.labels[ca[1] * CELLS + ca[0]] != self.labels[cb[1] * CELLS + cb[0]]:
            return None
        key = (ca, cb)
        if key not in self.cache:
            if len(self.cache) > 50_000:
                self.cache.clear()
            cells = [ca, cb] if self.clear(ca, cb) else self._astar(ca, cb)
            self.cache[key] = self._tighten(cells) if cells else None
        cells = self.cache[key]
        if cells is None:
            return None
        points = [tuple(a)] + [self.centre(*c) for c in cells[1:-1]] + [tuple(b)]
        dist = sum(math.hypot(p[0] - q[0], p[1] - q[1]) for p, q in zip(points, points[1:]))
        return {"distance": dist, "points": points}

    def _astar(self, a, b):
        grid, n = self.grid, CELLS
        bx, by = b
        start, goal = a[1] * n + a[0], b[1] * n + b[0]
        g = {start: 0.0}
        back = {}
        heap = [(0.0, start)]
        done = set()
        while heap and len(done) < MAX_SEARCH:
            _, i = heapq.heappop(heap)
            if i == goal:
                out = [i]
                while i in back:
                    i = back[i]
                    out.append(i)
                return [(c % n, c // n) for c in reversed(out)]
            if i in done:
                continue
            done.add(i)
            x, y, gi = i % n, i // n, g[i]
            for dx, dy, cost in _NEIGHBOURS:
                nx, ny = x + dx, y + dy
                if not (0 <= nx < n and 0 <= ny < n):
                    continue
                j = ny * n + nx
                if grid[j] != 1 or j in done:
                    continue
                if dx and dy and (grid[y * n + nx] != 1 or grid[ny * n + x] != 1):
                    continue                    # no cutting a blocked corner
                c = gi + cost
                if c < g.get(j, 1e18):
                    g[j] = c
                    back[j] = i
                    ddx, ddy = abs(bx - nx), abs(by - ny)
                    h = max(ddx, ddy) + (SQRT2 - 1) * min(ddx, ddy)
                    heapq.heappush(heap, (c + WEIGHT * h, j))
        return None

    def _tighten(self, cells):
        """String-pull a cell path: keep only the corners a straight line can't skip."""
        if len(cells) <= 2:
            return list(cells)
        out, i = [cells[0]], 0
        while i < len(cells) - 1:
            j = len(cells) - 1
            lo = i + 1
            # the furthest cell visible from cells[i] (binary search, then walk back if needed)
            hi = j
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if self.clear(cells[i], cells[mid]):
                    lo = mid
                else:
                    hi = mid - 1
            out.append(cells[lo])
            i = lo
        return out
