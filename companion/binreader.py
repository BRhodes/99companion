"""Little-endian cursor over a bytes buffer, with the string encodings the 1999 protocol uses."""

import struct


class ReadError(Exception):
    pass


class Reader:
    def __init__(self, data, pos=0, end=None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def remaining(self):
        return self.end - self.pos

    def _take(self, n):
        if n < 0 or self.pos + n > self.end:
            raise ReadError(f"need {n} bytes at {self.pos}, have {self.end - self.pos}")
        start = self.pos
        self.pos += n
        return start

    def _unpack(self, fmt, size):
        return struct.unpack_from(fmt, self.data, self._take(size))[0]

    def u8(self):
        return self._unpack("<B", 1)

    def u16(self):
        return self._unpack("<H", 2)

    def i16(self):
        return self._unpack("<h", 2)

    def u32(self):
        return self._unpack("<I", 4)

    def i32(self):
        return self._unpack("<i", 4)

    def u64(self):
        return self._unpack("<Q", 8)

    def f32(self):
        return self._unpack("<f", 4)

    def f64(self):
        return self._unpack("<d", 8)

    def bytes(self, n):
        start = self._take(n)
        return self.data[start:start + n]

    def skip(self, n):
        self._take(n)

    def align(self, base=0, boundary=4):
        """Advance so (pos - base) is a multiple of boundary."""
        pad = (-(self.pos - base)) % boundary
        if pad:
            self.skip(min(pad, self.remaining()))

    def string16(self, base=0, align=True):
        """u16 length, then that many bytes, then pad to 4 relative to `base`.

        The length may or may not include a trailing NUL depending on the writer; any NULs are stripped.
        """
        n = self.u16()
        raw = self.bytes(n)
        if align:
            self.align(base)
        return raw.split(b"\x00", 1)[0].decode("latin-1")

    def sub(self, n):
        """A reader over the next n bytes; advances this reader past them."""
        start = self._take(n)
        return Reader(self.data, start, start + n)
