"""Minimal reader for the 1999 portal.dat: B-tree directory, spell table, spell component table.

Spell table (0x0E00000E) entries are located by pattern (id, then a nibble-swapped name) rather than a
full parse of the variable-length MetaSpell. Each entry ends with a fixed tail -- formula (8 x u32),
then caster/target/fizzle effects (3 x i32), recovery interval (f64), recovery amount (f32) -- so the
formula sits 56 bytes before the next entry. Formulas are stored obfuscated; see decrypt_formula.
"""

import re
import struct

SPELL_TABLE_ID = 0x0E00000E
COMPONENT_TABLE_ID = 0x0E00000F
SCHOOLS = {1: "War", 2: "Life", 3: "Item", 4: "Creature", 5: "Void"}

# Spell school -> skill id (see skills.SKILL_NAMES)
SCHOOL_SKILL = {1: 34, 2: 33, 3: 32, 4: 31, 5: 43}

_ENTRY_TAIL = 3 * 4 + 8 + 4   # caster/target/fizzle effect, recovery interval, recovery amount
_FORMULA = struct.Struct("<8I")


class DatFile:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.buf = f.read()
        self.block_size = struct.unpack_from("<I", self.buf, 0x130)[0]
        root = struct.unpack_from("<I", self.buf, 0x148)[0]
        self.index = {}
        self._walk(root)

    def _read_chain(self, off, size):
        out = bytearray()
        bs = self.block_size
        while len(out) < size and off:
            nxt = struct.unpack_from("<I", self.buf, off)[0]
            out += self.buf[off + 4:off + bs]
            off = nxt & 0x7FFFFFFF
        return bytes(out[:size])

    def _walk(self, off):
        node = self._read_chain(off, 62 * 4 + 4 + 61 * 12)
        branches = struct.unpack_from("<62I", node, 0)
        count = struct.unpack_from("<I", node, 248)[0]
        for i in range(count):
            fid, foff, fsize = struct.unpack_from("<3I", node, 252 + 12 * i)
            self.index[fid] = (foff, fsize)
        if branches[0]:
            for i in range(count + 1):
                self._walk(branches[i])

    def get(self, fid):
        entry = self.index.get(fid)
        if entry is None:
            return None
        return self._read_chain(*entry)


def _nibble_swap(b):
    return bytes(((x << 4) | (x >> 4)) & 0xFF for x in b).rstrip(b"\x00")


def _read_string(d, p):
    """Dat string: u16 length, bytes, padded to 4 (dat buffers are 4-aligned from their start)."""
    n = struct.unpack_from("<H", d, p)[0]
    return d[p + 2:p + 2 + n], (p + 2 + n + 3) & ~3


def compute_hash(text):
    """PString::compute_hash, with C `char` (signed) semantics."""
    result = 0
    for ch in text.encode("latin-1", "replace"):
        c = ch - 256 if ch > 127 else ch
        result = (result * 16 + c) & 0xFFFFFFFF
        if result & 0xF0000000:
            result = (result ^ ((result & 0xF0000000) >> 24)) & 0x0FFFFFFF
    return 0xFFFFFFFE if result == 0xFFFFFFFF else result


def decrypt_formula(raw, name, desc):
    """CSpellBase::InqSpellFormula: subtract the name/description hash from each component id."""
    key = (compute_hash(name) % 0x12107680) + (compute_hash(desc) % 0xBEADCF45)
    return [((c - key) & 0xFFFFFFFF) if c else 0 for c in raw]


def load_spells(dat):
    """Return {spell_id: {name, school, school_name, category, power, mana, component_loss, formula}}.

    `formula` is the base (non-personalised) list of 8 component ids, 0 = empty slot.
    """
    d = dat.get(SPELL_TABLE_ID)
    if d is None:
        return {}

    found = []
    p = 8
    name_re = re.compile(rb"[A-Za-z0-9' :.\-]+")
    while p < len(d) - 40:
        key = struct.unpack_from("<I", d, p)[0]
        n = struct.unpack_from("<H", d, p + 4)[0]
        if 0 < key < 0x2000 and 3 <= n <= 60 and name_re.fullmatch(_nibble_swap(d[p + 6:p + 6 + n])):
            name, q = _read_string(d, p + 4)
            desc, q = _read_string(d, q)
            school, icon, category, bits, mana = struct.unpack_from("<IIIIi", d, q)
            range_base, range_per_skill = struct.unpack_from("<ff", d, q + 20)   # metres: base + per skill point
            power = struct.unpack_from("<i", d, q + 28)[0]
            if 0 <= school <= 5 and -1 < power < 5000:
                _economy, _fver, loss = struct.unpack_from("<fIf", d, q + 32)
                # MetaSpell: [type][spell id], then for enchantments [duration f64][degrade mod][degrade limit]
                meta_type = struct.unpack_from("<I", d, q + 44)[0]
                duration = struct.unpack_from("<d", d, q + 52)[0] if meta_type in (1, 0xC) else None
                found.append((p, key, {
                    "name": _nibble_swap(name).decode("latin-1"),
                    "desc": _nibble_swap(desc).decode("latin-1"),
                    "school": school,
                    "school_name": SCHOOLS.get(school, "?"),
                    "category": category,  # spells in one category don't stack: the strongest wins
                    "range": (round(range_base, 3), round(range_per_skill, 4)),
                    "power": power,
                    "mana": mana,
                    "icon": icon,
                    "bitfield": bits,
                    "component_loss": round(loss, 4),
                    "meta_type": meta_type,
                    "duration": duration,
                }))
                p = q + 44
                continue
        p += 4

    spells = {}
    for i, (start, key, spell) in enumerate(found):
        end = found[i + 1][0] if i + 1 < len(found) else len(d)
        raw = _FORMULA.unpack_from(d, end - _ENTRY_TAIL - _FORMULA.size)
        formula = decrypt_formula(raw, spell["name"], spell.pop("desc"))
        spell["formula"] = formula if all(c <= 0xFF for c in formula) else None
        spells[key] = spell
    return spells


def load_components(dat):
    """Return {component_id: {name, category, type, cdm}} from the spell component table."""
    d = dat.get(COMPONENT_TABLE_ID)
    if d is None:
        return {}
    count = struct.unpack_from("<H", d, 4)[0]
    p = 8
    out = {}
    for _ in range(count):
        key = struct.unpack_from("<I", d, p)[0]
        name, p = _read_string(d, p + 4)
        category, _icon, ctype, _gesture = struct.unpack_from("<iIiI", d, p)
        p += 20  # four fields above + casting time (f32)
        word, p = _read_string(d, p)    # what a caster says for it ("Malar", "Zhapaj", ...)
        cdm = struct.unpack_from("<f", d, p)[0]
        p += 4
        out[key] = {"name": _nibble_swap(name).decode("latin-1"), "category": category, "type": ctype,
                    "cdm": round(cdm, 4), "word": _nibble_swap(word).decode("latin-1")}
    return out


def icon_png(dat, icon_id):
    """Render a 24-bit portal.dat texture (red, green, blue per pixel) as a PNG; pure black becomes transparent, as in
    the client. The white outline round an icon's shape (a white pixel next to the transparent background, or at the
    edge) is drawn black instead: it was jarring against the dashboard."""
    import zlib
    d = dat.get(icon_id)
    if not d or len(d) < 12:
        return None
    w, h = struct.unpack_from("<II", d, 4)
    if not (0 < w <= 256 and 0 < h <= 256) or len(d) < 12 + w * h * 3:
        return None
    px = [tuple(d[12 + 3 * i:15 + 3 * i]) for i in range(w * h)]
    clear = lambda x, y: not (0 <= x < w and 0 <= y < h) or px[y * w + x] == (0, 0, 0)
    rows = bytearray()
    for y in range(h):
        rows.append(0)  # filter: none
        for x in range(w):
            r, g, b = px[y * w + x]
            if (r, g, b) == (0, 0, 0):
                rows += bytes(4)
            elif (r, g, b) == (255, 255, 255) and any(clear(x + dx, y + dy) for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                rows += bytes((0, 0, 0, 255))            # the outline: black
            else:
                rows += bytes((r, g, b, 255))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


VITAL_TABLE_ID = 0x0E000003
# (w, x, y, z, attr1, attr2) per max vital, as in the 1999 portal.dat: Endurance/2, Endurance, Self
DEFAULT_VITAL_FORMULAS = {1: (0, 1, 0, 2, 2, 0), 3: (0, 1, 0, 1, 2, 0), 5: (0, 1, 0, 1, 6, 0)}


SKILL_TABLE_ID = 0x0E000004


XP_TABLE_ID = 0x0E000018


def load_xp_tables(dat):
    """The ExperienceTable's cumulative costs: {"attribute": [XP for n raises], "vital", "trained", "specialized":
    [XP for rank n], "level": [total XP for level n]} (see load_level_xp), or {}."""
    d = dat.get(XP_TABLE_ID) if dat else None
    if not d or len(d) < 24:
        return {}
    try:
        _id, *counts = struct.unpack_from("<6I", d, 0)
        out, p = {}, 24
        for name, n in zip(("attribute", "vital", "trained", "specialized", "level"), counts):
            out[name] = list(struct.unpack_from(f"<{n + 1}I", d, p))
            p += 4 * (n + 1)
        return out
    except struct.error:
        return {}


def load_level_xp(dat):
    """[total XP to reach level n] for n = 0 .. the last level, from the ExperienceTable: its id, then how many
    attribute / vital / trained / specialized ranks and levels there are, the four rank tables (u32, one more than
    the count each), then the levels' (u32: 0, 0, 1000, 2777 ...), then each level's skill credits."""
    d = dat.get(XP_TABLE_ID) if dat else None
    if not d or len(d) < 24:
        return []
    try:
        _id, a, v, t, s, levels = struct.unpack_from("<6I", d, 0)
        return list(struct.unpack_from(f"<{levels + 1}I", d, 24 + 4 * (a + v + t + s + 4)))
    except struct.error:
        return []


def load_level_credits(dat):
    """[skill credits gained at level n] for n = 0 .. the last level (the ExperienceTable's last array)."""
    d = dat.get(XP_TABLE_ID) if dat else None
    if not d or len(d) < 24:
        return []
    try:
        _id, a, v, t, s, levels = struct.unpack_from("<6I", d, 0)
        return list(struct.unpack_from(f"<{levels + 1}I", d, 24 + 4 * (a + v + t + s + 4) + 4 * (levels + 1)))
    except struct.error:
        return []


def load_skill_formulas(dat):
    """{skill id: (w, x, y, z, attr1, attr2)} from the SkillTable: skill base = round((w + x*a1 + y*a2) / z)."""
    d = dat.get(SKILL_TABLE_ID) if dat else None
    if not d:
        return {}
    out = {}
    try:
        p = 4
        count = struct.unpack_from("<H", d, p)[0]
        p += 4
        for _ in range(count):
            key = struct.unpack_from("<I", d, p)[0]
            _desc, p = _read_string(d, p + 4)
            _name, p = _read_string(d, p)
            p += 24                     # icon, trained/specialized cost, category, chargen use, min level
            out[key] = struct.unpack_from("<6I", d, p)
            p += 24 + 24                # formula, then upper/lower bound and learn mod (3 doubles)
    except struct.error:
        pass
    return out


def load_skill_bases(dat):
    """{skill id: {trained_cost, specialized_cost (skill credits: 0 trained, it comes trained), formula (w, x, y, z,
    attr1, attr2)}} from the SkillTable."""
    d = dat.get(SKILL_TABLE_ID) if dat else None
    if not d:
        return {}
    out = {}
    try:
        p = 4
        count = struct.unpack_from("<H", d, p)[0]
        p += 4
        for _ in range(count):
            key = struct.unpack_from("<I", d, p)[0]
            _desc, p = _read_string(d, p + 4)
            _name, p = _read_string(d, p)
            _icon, trained, specialized = struct.unpack_from("<3I", d, p)
            p += 24
            out[key] = {"trained_cost": trained, "specialized_cost": specialized, "formula": struct.unpack_from("<6I", d, p)}
            p += 24 + 24
    except struct.error:
        pass
    return out


def load_vital_formulas(dat):
    """{max vital key (1 health, 3 stamina, 5 mana): (w, x, y, z, attr1, attr2)} from Attribute2ndTable."""
    d = dat.get(VITAL_TABLE_ID) if dat else None
    if not d or len(d) < 4 + 3 * 24:
        return dict(DEFAULT_VITAL_FORMULAS)
    return {key: struct.unpack_from("<6I", d, 4 + 24 * i) for i, key in enumerate((1, 3, 5))}
