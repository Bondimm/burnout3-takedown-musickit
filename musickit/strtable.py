"""Burnout 3: Takedown text files (DATA/GLOBAL{EN,SP,DU,SW}.BIN on the European disc, DATA/GLOBALUS.BIN on the US
disc).

Header: u32 magic 0x69F60308, u32 type id 0xB93622FA, u32 count, u32 index offset (0x10).
Index: count u32 offsets (from the file start), in string order. Strings: UTF-16LE + NUL, back to back (no
padding), in index order. Strings are addressed by their number; the EA Trax song table stores the numbers of each
song's title, album and artist. The game loads the whole file into a fixed buffer (see `BUFFER_SIZE` in elfpatch),
so a file may grow only up to that size.
"""
import struct
import unicodedata

MAGIC = 0x69F60308


class IndexedStringTable:
    def __init__(self, data):
        self.magic, self.type_id, n, io = struct.unpack_from("<4I", data, 0)
        if self.magic != MAGIC or io != 0x10:
            raise ValueError("not a Burnout 3 text file")
        offs = struct.unpack_from("<%dI" % n, data, io)
        self.strings = []
        for o in offs:
            e = o
            while data[e:e + 2] != b"\0\0":
                e += 2
            self.strings.append(data[o:e].decode("utf-16-le"))

    def __len__(self):
        return len(self.strings)

    def get(self, i):
        return self.strings[i] if 0 <= i < len(self.strings) else None

    def set(self, i, text):
        self.strings[i] = text

    def append(self, text):
        self.strings.append(text)
        return len(self.strings) - 1

    def truncate(self, n):
        del self.strings[n:]

    def build(self):
        n = len(self.strings)
        pos = 0x10 + 4 * n
        index = []
        body = bytearray()
        for s in self.strings:
            index.append(pos + len(body))
            body += s.encode("utf-16-le") + b"\0\0"
        return struct.pack("<4I", self.magic, self.type_id, n, 0x10) + struct.pack("<%dI" % n, *index) + bytes(body)

    def size(self):
        return 0x10 + sum(4 + 2 * len(s) + 2 for s in self.strings)


def string_bytes(text):
    """Bytes one string takes in the file (index entry + UTF-16 text + NUL)."""
    return 4 + 2 * len(text) + 2


def charset(tables):
    """Characters used anywhere in the original tables = characters the game fonts can draw."""
    cs = set()
    for t in tables:
        for s in t.strings:
            cs.update(s)
    return cs


def sanitize(text, allowed, max_len=None):
    """Map text into the allowed charset (accents are stripped only when the accented glyph is missing)."""
    out = []
    for ch in text:
        if ch in allowed:
            out.append(ch)
            continue
        repl = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                "…": "..."}.get(ch)
        if repl is None:
            repl = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append("".join(c for c in repl if c in allowed) or ("?" if "?" in allowed else ""))
    s = "".join(out).strip()
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip()
    return s
