"""Patches the Burnout 3: Takedown executable (Europe SLES_525.84 / SLES_525.85 or USA SLUS_210.50) so the EA Trax playlist can be
changed and can hold more than the 44 original songs.

Facts (European addresses; the USA build has the same code at other addresses and is located by signature):
- song table: 44 entries x 24 bytes {u32 song id (= position), u32 0, u32 title string, u32 album string,
  u32 artist string, u32 flags (1/2/4 = race/menu/..., 8 = "not heard yet")} at 0x4A5A90, followed by the playlist
  struct (0x4A5EB0): +0x04 song count (44), +0x4C table pointer. The strings are numbers into the DATA/GLOBAL*.BIN
  text files.
- song i streams from tracks\\_EATRAX0.rws segment i (i < 22) or tracks\\_EATRAX1.rws segment i-22.
- the profile (0x4F55C0, saved on the memory card) holds one flag byte per original song (44 bytes, followed by
  the play mode and volume settings). Code copies it into the table (table_copy, profile_load_a/_b, menu_cancel)
  and writes it back (menu_confirm, song_finished). Those must stay below index 44.

- shuffle mode keeps its play order as one byte per song in the music manager object, at +0x0D. There is room for
  51 songs there: the shuffle position (+0x40), the shuffle song count (+0x44), the stream file name (+0x48..+0x67)
  and a copy of the playlist struct (+0x68, song count at +0x6C) follow. Two functions use that list (shuffle_set:
  refill it when the song count changed / move to a chosen song, which loops over the shuffle song count;
  shuffle_next: pick the next song).

Up to 44 songs the table is rewritten where it is (data only). For more songs it moves into a new 4 KB program
segment at the old end of the executable's memory (the third program header is an empty one at exactly that
address); the heap start moves up by 4 KB (crt0 SetupHeap argument + the libc sbrk start word) and the loops above
are limited to 44. The shuffle order list moves to the last SHUFFLE_SIZE bytes of the same segment (memory the
kit already owns, below the moved heap start): each of the two functions loads the list address into a register it
never uses (t2, or t3 in the USA shuffle_set) in a branch delay slot that held a nop, and its list accesses use that
register instead of the manager pointer. The USA shuffle_set is compiled differently (manager pointer kept in a0),
so it has its own signature window and patch words (shuffle_set_usa, USA addresses).

The game loads one text file into a fixed 0x41800-byte buffer (memory_map): the text files may grow only up to
that size (Layout.buffer_size, read from the executable).

PCSX2 identifies games by the XOR of all 32-bit words of the boot ELF (ElfObject::GetCRC) and picks patches /
widescreen pnach files by it. A compensation word is appended after the last byte of the file (never loaded) so
the patched ELF keeps the original CRC.
"""
import struct

ORIGINAL_SONGS = 44
ENTRY = 24
SEG_SIZE = 0x1000                  # the new program segment (table + shuffle order list) and the heap shift
SHUFFLE_SIZE = 0x80                # shuffle order list (one byte per song) at the end of the new segment
TABLE_SLOTS = (SEG_SIZE - SHUFFLE_SIZE) // ENTRY    # 165
MAX_SONGS = 95                     # as in the Burnout Revenge MusicKit
# Music manager fields after the shuffle order list (offsets into the manager object). The list stays in the manager
# up to 44 songs (room for 51) and is moved into the new segment for more songs.
MANAGER_SHUFFLE = 0x0D
MANAGER_FIELDS = [(0x40, 0x44, "shuffle position"), (0x44, 0x48, "shuffle song count"),
                  (0x48, 0x68, "stream file name"), (0x68, 0xB8, "playlist copy (song count at +0x6C)")]
_DETECT_MAX = 100
KNOWN = {0x75BECC18: "Europe SLES-52584", 0xCE49B0DE: "Europe SLES-52585", 0xBEBF8793: "USA SLUS-21050"}

# Signature groups: European function windows containing the patch sites (located in other builds by masked search).
EU_GROUPS = {
    "trax_setmode": (0x431460, 0x431530),     # EA Trax menu: set a song's mode (ALL / RACE / MENU / OFF)
    "table_copy": (0x3FD020, 0x3FD088),       # profile flags -> table (fixed table address)
    "song_finished": (0x3FCE30, 0x3FCE80),    # song played to the end: clear "not heard yet"
    "profile_load_a": (0x28B380, 0x28B3C8),   # memory card load: apply flags to every song
    "profile_load_b": (0x28B560, 0x28B5AC),   # profile apply: same
    "menu_confirm": (0x4318B0, 0x431904),     # EA Trax menu confirm: table -> profile
    "menu_cancel": (0x431960, 0x4319B4),      # EA Trax menu cancel: profile -> table
    "crt0_heap": (0x100198, 0x1001E8),        # start-up: SetupHeap(end of program, -1)
    "memory_map": (0x222640, 0x2226D0),       # fixed memory regions (text file buffer size)
    "shuffle_set": (0x3FBC10, 0x3FBD04),      # shuffle: refill the order list / move to a chosen song
    "shuffle_next": (0x3FC1DC, 0x3FC400),     # next song (shuffle mode takes it from the order list)
}
# Groups whose code differs between builds: one of each set is found (exactly once), the others are absent.
# Their windows (and patch addresses) are USA addresses, signatures generated from SLUS_210.50.
USA_GROUPS = {
    "shuffle_set_usa": (0x3FB8B0, 0x3FB9C4),
}
ALTERNATIVES = [("shuffle_set", "shuffle_set_usa")]

# Optional "unlock": the game refuses OFF for a song whose "not heard yet" bit (flags & 8) is set - every original
# song starts with it and loses it only after playing to the end once. This word drops that check.
UNLOCK_PATCHES = [
    (0x4314F8, "trax_setmode", 0x0000102D, "daddu v0,zero,zero  (was andi v0,v0,8: OFF refused for unheard songs)"),
]

# Patches for the moved table (more than 44 songs): (EU vaddr, group, template, meaning).
# Template: int word, None = keep the original word, or a callable(ctx).
RELOC_PATCHES = [
    (0x3FD040, "table_copy", lambda c: 0x3C030000 | hi16(c["table"]), "lui v1,%hi(table)"),
    (0x3FD050, "table_copy", lambda c: 0x24630000 | (c["table"] & 0xFFFF), "addiu v1,v1,%lo(table)"),
    (0x28B3B8, "profile_load_a", 0x2403002C, "addiu v1,zero,44  (was lw v1,0x54(sp) = song count)"),
    (0x28B5A0, "profile_load_b", 0x2402002C, "addiu v0,zero,44  (was lw v0,0x44(sp) = song count)"),
    (0x4318F4, "menu_confirm", 0x2402002C, "addiu v0,zero,44  (was lw v0,0xb4(sp) = song count)"),
    (0x4319A8, "menu_cancel", 0x2402002C, "addiu v0,zero,44  (was lw v0,0x34(sp) = song count)"),
    (0x3FCE40, "song_finished", None, "lw a1,0xc4(s0)"),
    (0x3FCE44, "song_finished", 0x8CA40014, "lw a0,0x14(a1)"),
    (0x3FCE48, "song_finished", 0x3084FFF7, "andi a0,a0,0xfff7"),
    (0x3FCE4C, "song_finished", 0xACA40014, "sw a0,0x14(a1)"),
    (0x3FCE50, "song_finished", 0x8E0300D8, "lw v1,0xd8(s0)"),
    (0x3FCE54, "song_finished", 0x2C62002C, "sltiu v0,v1,44"),
    (0x3FCE58, "song_finished", 0x0002180A, "movz v1,zero,v0"),
    (0x3FCE5C, "song_finished", lambda c: 0x3C040000 | hi16(c["profile"]), "lui a0,%hi(profile)"),
    (0x3FCE60, "song_finished", 0x00641821, "addu v1,v1,a0"),
    (0x3FCE64, "song_finished", lambda c: 0x90620000 | (c["profile"] & 0xFFFF), "lbu v0,%lo(profile)(v1)"),
    (0x3FCE68, "song_finished", 0x304200F7, "andi v0,v0,0xf7"),
    (0x3FCE6C, "song_finished", lambda c: 0xA0620000 | (c["profile"] & 0xFFFF), "sb v0,%lo(profile)(v1)"),
    (0x1001D0, "crt0_heap", lambda c: 0x3C040000 | hi16(c["heap"]), "lui a0,%hi(heap start)"),
    (0x1001D8, "crt0_heap", lambda c: 0x24840000 | (c["heap"] & 0xFFFF), "addiu a0,a0,%lo(heap start)"),
]


def _shuffle_lo(word):
    return lambda c: word | (c["shuffle"] & 0xFFFF)


def _shuffle_lui(c):
    return 0x3C0A0000 | hi16(c["shuffle"])


# Shuffle order list moved to the new segment: t2 = %hi(list) in a branch delay slot (was nop, runs on every path
# before the list is used; no call lies in between), then addu x,t2,i (was addu x,s1,i = manager + i) and
# lbu/sb y,%lo(list)(x) (was 0xd(x)). t2 is not used anywhere else in these two functions.
SHUFFLE_PATCHES = [
    (0x3FBC30, "shuffle_set", _shuffle_lui, "lui t2,%hi(shuffle)  (was nop)"),
    (0x3FBC4C, "shuffle_set", 0x01441821, "addu v1,t2,a0  (was addu v1,s1,a0)"),
    (0x3FBC50, "shuffle_set", _shuffle_lo(0x90630000), "lbu v1,%lo(shuffle)(v1)  (was lbu v1,0xd(v1))"),
    (0x3FBC84, "shuffle_set", 0x01462021, "addu a0,t2,a2  (was addu a0,s1,a2)"),
    (0x3FBC88, "shuffle_set", _shuffle_lo(0xA0860000), "sb a2,%lo(shuffle)(a0)"),
    (0x3FBCD8, "shuffle_set", 0x01493021, "addu a2,t2,t1  (was addu a2,s1,t1)"),
    (0x3FBCDC, "shuffle_set", _shuffle_lo(0x90C40000), "lbu a0,%lo(shuffle)(a2)"),
    (0x3FBCE4, "shuffle_set", 0x01474021, "addu t0,t2,a3  (was addu t0,s1,a3)"),
    (0x3FBCEC, "shuffle_set", _shuffle_lo(0x91070000), "lbu a3,%lo(shuffle)(t0)"),
    (0x3FBCF0, "shuffle_set", _shuffle_lo(0xA1040000), "sb a0,%lo(shuffle)(t0)"),
    (0x3FBCF4, "shuffle_set", _shuffle_lo(0xA0C70000), "sb a3,%lo(shuffle)(a2)"),
    (0x3FC1F8, "shuffle_next", _shuffle_lui, "lui t2,%hi(shuffle)  (was nop)"),
    (0x3FC2D8, "shuffle_next", 0x01453021, "addu a2,t2,a1  (was addu a2,s1,a1)"),
    (0x3FC2E0, "shuffle_next", _shuffle_lo(0x90D00000), "lbu s0,%lo(shuffle)(a2)"),
    (0x3FC388, "shuffle_next", 0x01462821, "addu a1,t2,a2  (was addu a1,s1,a2)"),
    (0x3FC38C, "shuffle_next", _shuffle_lo(0xA0A60000), "sb a2,%lo(shuffle)(a1)"),
    (0x3FC3DC, "shuffle_next", 0x01493021, "addu a2,t2,t1  (was addu a2,s1,t1)"),
    (0x3FC3E0, "shuffle_next", _shuffle_lo(0x90C50000), "lbu a1,%lo(shuffle)(a2)"),
    (0x3FC3E8, "shuffle_next", 0x01474021, "addu t0,t2,a3  (was addu t0,s1,a3)"),
    (0x3FC3F0, "shuffle_next", _shuffle_lo(0x91070000), "lbu a3,%lo(shuffle)(t0)"),
    (0x3FC3F4, "shuffle_next", _shuffle_lo(0xA1050000), "sb a1,%lo(shuffle)(t0)"),
    (0x3FC3F8, "shuffle_next", _shuffle_lo(0xA0C70000), "sb a3,%lo(shuffle)(a2)"),
    # USA shuffle_set (manager pointer in a0, t2 taken, no calls: t3 holds the list address)
    (0x3FB8BC, "shuffle_set_usa", lambda c: 0x3C0B0000 | hi16(c["shuffle"]), "lui t3,%hi(shuffle)  (was nop)"),
    (0x3FB8D8, "shuffle_set_usa", 0x01661821, "addu v1,t3,a2  (was addu v1,a0,a2)"),
    (0x3FB8DC, "shuffle_set_usa", _shuffle_lo(0x90630000), "lbu v1,%lo(shuffle)(v1)"),
    (0x3FB910, "shuffle_set_usa", 0x01673021, "addu a2,t3,a3  (was addu a2,a0,a3)"),
    (0x3FB914, "shuffle_set_usa", _shuffle_lo(0xA0C70000), "sb a3,%lo(shuffle)(a2)"),
    (0x3FB964, "shuffle_set_usa", 0x016A3821, "addu a3,t3,t2  (was addu a3,a0,t2)"),
    (0x3FB968, "shuffle_set_usa", _shuffle_lo(0x90E60000), "lbu a2,%lo(shuffle)(a3)"),
    (0x3FB970, "shuffle_set_usa", 0x01684821, "addu t1,t3,t0  (was addu t1,a0,t0)"),
    (0x3FB978, "shuffle_set_usa", _shuffle_lo(0x91280000), "lbu t0,%lo(shuffle)(t1)"),
    (0x3FB97C, "shuffle_set_usa", _shuffle_lo(0xA1260000), "sb a2,%lo(shuffle)(t1)"),
    (0x3FB980, "shuffle_set_usa", _shuffle_lo(0xA0E80000), "sb t0,%lo(shuffle)(a3)"),
]
RELOC_PATCHES += SHUFFLE_PATCHES
_SITES = {(g, eu) for eu, g, _, _ in RELOC_PATCHES + UNLOCK_PATCHES}


def crc(data):
    """PCSX2 game CRC: XOR of every little-endian 32-bit word of the ELF file."""
    import numpy as np
    n = len(data) // 4 * 4
    return int(np.bitwise_xor.reduce(np.frombuffer(bytes(data[:n]), dtype="<u4"))) if n else 0


def hi16(a):
    return ((a + 0x8000) >> 16) & 0xFFFF


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


class Elf:
    def __init__(self, data):
        self.d = bytearray(data)
        if self.d[:4] != b"\x7fELF":
            raise ValueError("not an ELF file")
        self.phoff = struct.unpack_from("<I", self.d, 0x1C)[0]
        self.phnum = struct.unpack_from("<H", self.d, 0x2C)[0]
        self.shoff = struct.unpack_from("<I", self.d, 0x20)[0]
        self.shnum = struct.unpack_from("<H", self.d, 0x30)[0]

    def phdrs(self):
        return [list(struct.unpack_from("<8I", self.d, self.phoff + 32 * i)) for i in range(self.phnum)]

    def set_phdr(self, i, ph):
        struct.pack_into("<8I", self.d, self.phoff + 32 * i, *ph)

    def sections(self):
        return [list(struct.unpack_from("<10I", self.d, self.shoff + 40 * i)) for i in range(self.shnum)]

    def set_section(self, i, sh):
        struct.pack_into("<10I", self.d, self.shoff + 40 * i, *sh)

    def file_offset(self, va):
        for ph in self.phdrs():
            if ph[0] == 1 and ph[4] and ph[2] <= va < ph[2] + ph[4]:
                return ph[1] + va - ph[2]
        raise KeyError(hex(va))

    def r32(self, va):
        return struct.unpack_from("<I", self.d, self.file_offset(va))[0]

    def w32(self, va, v):
        struct.pack_into("<I", self.d, self.file_offset(va), v)

    def content_end(self):
        end = self.shoff + self.shnum * 40
        for sh in self.sections():
            if sh[1] != 8:
                end = max(end, sh[4] + sh[5])
        for ph in self.phdrs():
            end = max(end, ph[1] + ph[4])
        return end


def _find_group(e, sig, seg):
    """sig = [[value, mask], ...]; returns the vaddrs where (word & mask) == value for the whole window."""
    import numpy as np
    base_va, off, size = seg[2], seg[1], seg[4]
    text = np.frombuffer(bytes(e.d[off:off + size - size % 4]), dtype="<u4")
    vals = np.array([v for v, m in sig], dtype=np.uint32)
    masks = np.array([m for v, m in sig], dtype=np.uint32)
    first = next(i for i, (v, m) in enumerate(sig) if m)
    cand = np.nonzero((text & masks[first]) == vals[first])[0] - first
    n = len(sig)
    hits = [int(i) for i in cand if 0 <= i and i + n <= len(text) and np.array_equal(text[i:i + n] & masks, vals)]
    return [base_va + 4 * i for i in hits]


class Layout:
    """Addresses of everything MusicKit touches, for a given executable."""

    def __init__(self, data):
        e = Elf(data)
        self.elf = e
        ph = e.phdrs()
        if len(ph) < 3 or ph[0][0] != 1 or ph[2][0] != 1 or ph[2][2] != ph[0][2] + ph[0][5]:
            raise ValueError("unexpected ELF layout")
        self.seg1 = ph[0]
        self.end = ph[2][2]                    # end of the original program memory (_end)
        self.relocated = ph[2][5] > 0          # the table lives in the extra segment
        if self.relocated and ph[2][5] != SEG_SIZE:
            raise ValueError("unexpected extra program segment")
        self.table_new = self.end
        self.shuffle = self.end + SEG_SIZE - SHUFFLE_SIZE    # moved shuffle order list
        self.delta = {}
        found = {}
        for g, (a, b) in list(EU_GROUPS.items()) + list(USA_GROUPS.items()):
            hits = _find_group(e, SIGNATURES[g], self.seg1)
            found[g] = len(hits)
            self.delta[g] = hits[0] - a if len(hits) == 1 else None
        alt = {g for group in ALTERNATIVES for g in group}
        for g, n in found.items():
            if g not in alt and n != 1:
                raise ValueError("code signature %s found %d times (unsupported executable)" % (g, n))
        for group in ALTERNATIVES:
            if sorted(found[g] for g in group) != [0] * (len(group) - 1) + [1]:
                raise ValueError("code signature %s found %s times (unsupported executable)"
                                 % ("/".join(group), "/".join(str(found[g]) for g in group)))

        def site(eu, g):
            return eu + self.delta[g]
        # profile address (song_finished) and table address (table_copy)
        if self.relocated:
            hi = e.r32(site(0x3FCE5C, "song_finished")) & 0xFFFF
            lo = e.r32(site(0x3FCE64, "song_finished")) & 0xFFFF
        else:
            hi = e.r32(site(0x3FCE44, "song_finished")) & 0xFFFF
            lo = e.r32(site(0x3FCE4C, "song_finished")) & 0xFFFF
        self.profile = (hi << 16) + _s16(lo)
        hi = e.r32(site(0x3FD040, "table_copy")) & 0xFFFF
        lo = e.r32(site(0x3FD050, "table_copy")) & 0xFFFF
        self.table = (hi << 16) + _s16(lo)
        if self.relocated != (self.table == self.table_new):
            raise ValueError("song table address does not match the program layout")
        self.playlist = self._find_playlist(e, self.table)
        self.table_old = self.table if not self.relocated else self.playlist - ORIGINAL_SONGS * ENTRY
        if not self.relocated and self.table + ORIGINAL_SONGS * ENTRY != self.playlist:
            raise ValueError("unexpected song table layout")
        # heap start (crt0) and the sbrk start word
        hi = e.r32(site(0x1001D0, "crt0_heap")) & 0xFFFF
        lo = e.r32(site(0x1001D8, "crt0_heap")) & 0xFFFF
        self.heap = (hi << 16) + _s16(lo)
        if self.heap != self.end + (SEG_SIZE if self.relocated else 0):
            raise ValueError("unexpected heap start %#x" % self.heap)
        self.sbrk = self._find_word(e, self.heap)
        # text file buffer: lui v0,hi ... ori a3,v0,lo  (the region after the first one)
        m = site(0x222640, "memory_map")
        hi = e.r32(m + 0x58) & 0xFFFF
        lo = e.r32(m + 0x70) & 0xFFFF
        self.buffer_size = (hi << 16) | lo

    def _find_word(self, e, value):
        s = self.seg1
        d = bytes(e.d[s[1]:s[1] + s[4]])
        key = struct.pack("<I", value)
        hits = []
        i = d.find(key)
        while i >= 0:
            if i % 4 == 0:
                hits.append(s[2] + i)
            i = d.find(key, i + 1)
        if len(hits) != 1:
            raise ValueError("heap start word found %d times" % len(hits))
        return hits[0]

    def _find_playlist(self, e, table):
        s = self.seg1
        d = bytes(e.d[s[1]:s[1] + s[4]])
        key = struct.pack("<I", table)
        hits = []
        i = d.find(key)
        while i >= 0:
            if i % 4 == 0 and i >= 0x4C:
                base = i - 0x4C
                z, n = struct.unpack_from("<II", d, base)
                if z == 0 and 1 <= n <= _DETECT_MAX:
                    hits.append(s[2] + base)
            i = d.find(key, i + 1)
        if len(hits) != 1:
            raise ValueError("playlist structure not found (%d candidates)" % len(hits))
        return hits[0]

    def ctx(self):
        return {"table": self.table_new, "profile": self.profile, "heap": self.end + SEG_SIZE,
                "shuffle": self.shuffle}

    def sites(self):
        """[(vaddr, EU vaddr, template, meaning)] of the relocation patches for this build."""
        return [(eu + self.delta[g], eu, t, m) for eu, g, t, m in RELOC_PATCHES if self.delta[g] is not None]

    def unlock_sites(self):
        return [(eu + self.delta[g], eu, t, m) for eu, g, t, m in UNLOCK_PATCHES]


def is_unlocked(data):
    """True if the "switch off unheard songs" unlock is applied."""
    lay = Layout(data)
    return all(lay.elf.r32(va) == t for va, _, t, _ in lay.unlock_sites())


def unlock(data):
    """Let the EA Trax menu switch any song OFF, including original songs not heard yet (same PCSX2 CRC).
    Works on original and MusicKit executables."""
    if is_unlocked(data):
        return bytes(data)
    lay = Layout(data)
    e = lay.elf
    target_crc = crc(data)
    appended = len(e.d) > e.content_end()     # a compensation word is already there
    for va, _, t, _ in lay.unlock_sites():
        e.w32(va, t)
    return _fix_crc(e.d, target_crc, appended)


def is_patched(data):
    """True if the song table was moved (more than 44 songs possible)."""
    return Elf(data).phdrs()[2][5] > 0


def read_song_count(data):
    lay = Layout(data)
    return lay.elf.r32(lay.playlist + 4)


def read_table(data):
    """Song table entries [(song id, 0, title string, album string, artist string, flags)] for the current count."""
    lay = Layout(data)
    e = lay.elf
    n = e.r32(lay.playlist + 4)
    ptr = e.r32(lay.playlist + 0x4C)
    return [struct.unpack_from("<6I", e.d, e.file_offset(ptr + ENTRY * i)) for i in range(n)]


def text_buffer_size(data):
    """Bytes the game reserves for one text file (DATA/GLOBAL*.BIN)."""
    return Layout(data).buffer_size


def relocate(data):
    """Return a copy of an executable whose song table is moved into the new 4 KB program segment (room for
    MAX_SONGS songs), with the heap start and the profile loops adjusted (same PCSX2 CRC)."""
    lay = Layout(data)
    if lay.relocated:
        raise ValueError("executable already patched")
    e = lay.elf
    target_crc = crc(data)
    if e.r32(lay.sbrk) != lay.end:
        raise ValueError("heap data does not match an original Burnout 3 executable")
    for i in range(ORIGINAL_SONGS):
        if struct.unpack_from("<2I", e.d, e.file_offset(lay.table_old + ENTRY * i)) != (i, 0):
            raise ValueError("song table does not match")
    # new program segment after the ELF content (a compensation word there is dropped and written again)
    body = bytes(e.d[:e.content_end()])
    off = (len(body) + 15) & ~15
    seg = bytearray(SEG_SIZE)
    o0 = e.file_offset(lay.table_old)
    seg[:ORIGINAL_SONGS * ENTRY] = e.d[o0:o0 + ORIGINAL_SONGS * ENTRY]
    out = Elf(body + bytes(off - len(body)) + bytes(seg))
    ph = out.phdrs()
    ph[2][1], ph[2][4], ph[2][5] = off, SEG_SIZE, SEG_SIZE
    out.set_phdr(2, ph[2])
    for i, sh in enumerate(out.sections()):       # keep the matching section header consistent
        if sh[1] == 1 and sh[3] == lay.end:
            sh[4], sh[5] = off, SEG_SIZE
            out.set_section(i, sh)
    out.w32(lay.playlist + 0x4C, lay.table_new)
    out.w32(lay.sbrk, lay.ctx()["heap"])
    _write_sites(out, lay)
    return _fix_crc(out.d, target_crc, appended=False)


def _write_sites(e, lay):
    """Write every relocation patch word (the same words again on an executable that already has them)."""
    c = lay.ctx()
    for va, eu, tmpl, _ in lay.sites():
        if tmpl is not None:
            e.w32(va, tmpl(c) if callable(tmpl) else tmpl)


def set_table(data, entries):
    """Return a copy whose song table is entry i = (i, 0, title, album, artist, flags) for entries[i] =
    (title, album, artist, flags), 1..MAX_SONGS songs, same PCSX2 CRC. Up to 44 songs the table of an original
    executable is rewritten in place; for more songs (or an executable already patched) the moved table is used.
    Songs are identified by their position only: the profile flag bytes 0..43 (memory card) apply to entries
    0..43; later entries keep the flags written here."""
    total = len(entries)
    if not 1 <= total <= MAX_SONGS:
        raise ValueError("between 1 and %d songs are supported (got %d)" % (MAX_SONGS, total))
    target_crc = crc(data)
    lay = Layout(data)
    if not lay.relocated and total > ORIGINAL_SONGS:
        data = relocate(data)
        lay = Layout(data)
    e = lay.elf
    if lay.relocated:
        for i in range(TABLE_SLOTS):
            if i < total:
                ent = (i, 0) + tuple(entries[i])
            elif i < ORIGINAL_SONGS:      # the profile loops still read / write entries 0..43
                ent = (i, 0) + tuple(entries[0][:3]) + (7,)
            else:
                ent = (0,) * 6
            struct.pack_into("<6I", e.d, e.file_offset(lay.table_new + ENTRY * i), *ent)
        o = e.file_offset(lay.shuffle)
        e.d[o:o + SHUFFLE_SIZE] = bytes(SHUFFLE_SIZE)     # the game fills the shuffle order list itself
        # executables patched by older MusicKit versions kept the shuffle order list in the music manager (room for
        # 51 songs only): writing all patch words again moves it
        _write_sites(e, lay)
    else:
        for i in range(total):
            struct.pack_into("<6I", e.d, e.file_offset(lay.table_old + ENTRY * i), i, 0, *entries[i])
    e.w32(lay.playlist + 4, total)
    appended = len(e.d) > e.content_end()
    return _fix_crc(e.d, target_crc, appended)


def shuffle_list(data):
    """Where the game keeps the shuffle order list: ("segment", address) once moved into the new program segment,
    else ("manager", MANAGER_SHUFFLE) (offset into the music manager object, room up to the shuffle position)."""
    lay = Layout(data)
    if lay.relocated:
        c = lay.ctx()
        if all(lay.elf.r32(va) == (t(c) if callable(t) else t) for va, eu, t, _ in lay.sites()
               if eu in {s[0] for s in SHUFFLE_PATCHES}):
            return "segment", lay.shuffle
    return "manager", MANAGER_SHUFFLE


def manager_overlap(start, count):
    """Music manager fields a per-song byte list at manager offset `start` with `count` songs runs into."""
    return [name for lo, hi, name in MANAGER_FIELDS if start < hi and lo < start + count]


def _fix_crc(data, target, appended):
    """Make crc(data) == target via a compensation word after the ELF content (appended if needed)."""
    out = bytearray(data)
    if len(out) % 4:
        out += b"\0" * (4 - len(out) % 4)
    if not appended:
        out += b"\0\0\0\0"
    struct.pack_into("<I", out, len(out) - 4, 0)
    struct.pack_into("<I", out, len(out) - 4, crc(out) ^ target)
    assert crc(out) == target
    return bytes(out)


def touched_ranges(data):
    """Virtual address ranges MusicKit writes (for conflict checks against PCSX2 pnach patches)."""
    lay = Layout(data)
    r = [(lay.playlist + 4, lay.playlist + 8), (lay.table_old, lay.table_old + ORIGINAL_SONGS * ENTRY)]
    if lay.relocated:
        r += [(va, va + 4) for va, _, t, _ in lay.sites() if t is not None]
        r += [(lay.playlist + 0x4C, lay.playlist + 0x50), (lay.sbrk, lay.sbrk + 4),
              (lay.table_new, lay.table_new + SEG_SIZE)]
    if is_unlocked(data):
        r += [(va, va + 4) for va, _, _, _ in lay.unlock_sites()]
    return r


# Signatures of the groups ([value, mask] per word), generated from SLES_525.84 (and SLUS_210.50 for USA_GROUPS) by
# gen_signatures() into signatures.json. Patch sites match any word, so the same signatures find original and patched code.
def _mask_of(w):
    op = w >> 26
    if op in (0x02, 0x03):                 # j / jal: targets differ between builds
        return 0xFC000000
    if op in (0x0F, 0x09, 0x0D):           # lui / addiu / ori: address halves differ
        return 0xFFFF0000
    return 0xFFFFFFFF


def gen_signatures(eu_data, path=None, usa_data=None):
    """Signatures of EU_GROUPS from SLES_525.84 and of USA_GROUPS from SLUS_210.50 (kept as they are without it)."""
    import json
    import os
    sig = {}
    for groups, data in ((EU_GROUPS, eu_data), (USA_GROUPS, usa_data)):
        if data is None:
            sig.update({g: SIGNATURES[g] for g in groups})
            continue
        e = Elf(data)
        for g, (a, b) in groups.items():
            words = []
            for va in range(a, b, 4):
                if (g, va) in _SITES:
                    words.append([0, 0])
                else:
                    m = _mask_of(e.r32(va))
                    words.append([e.r32(va) & m, m])
            sig[g] = words
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "signatures.json")
    json.dump(sig, open(path, "w"))
    global SIGNATURES
    SIGNATURES = sig
    return path


def _load_signatures():
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "signatures.json")
    if os.path.exists(p):
        return json.load(open(p))
    return {}


SIGNATURES = _load_signatures()
