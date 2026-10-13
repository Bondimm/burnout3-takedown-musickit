"""MusicKit core: read the EA Trax soundtrack of a Burnout 3: Takedown ISO and build a new ISO with a changed song
list (songs added, removed, reordered, renamed or with new audio).

Disc facts:
- songs are identified by their position only: song i streams from /TRACKS/_EATRAX0.RWS segment i (i < 22) or
  /TRACKS/_EATRAX1.RWS segment i-22.
- the song table in the boot executable (patched by elfpatch) holds the count and, per song, the numbers of its
  title, album and artist strings in /DATA/GLOBAL{EN,SP,DU,SW}.BIN (Europe) or /DATA/GLOBALUS.BIN (USA). Added
  songs get new strings after the last original one (a GUID that ends every original file); strings of removed
  songs are reused first. The game loads a text file into a fixed buffer, so the names of added songs must fit.
- the memory card profile keeps one settings byte per position 0..43, so removing / reordering songs among the
  first 44 positions shifts those saved settings to other songs.
- segments MusicKit writes carry a UUID starting with MARK, so a MusicKit image can be read back (which songs
  are original).
"""
import collections
import io
import json
import os
import re
import statistics
import struct
import tempfile

import numpy as np

from . import audio, elfpatch, hostfs, iso, rws, strtable

LANGS = ("EN", "FR", "GE", "IT", "SP", "DU", "SW", "US")    # all text files MusicKit knows (a disc has a subset)
LANG_NAMES = {"EN": "English", "FR": "French", "GE": "German", "IT": "Italian", "SP": "Spanish", "DU": "Dutch",
              "SW": "Swedish", "US": "English (USA)"}
LANG_FILE = "/DATA/GLOBAL%s.BIN"
ELF_PATH = "/SLES_525.84"                 # European default; the boot ELF is read from SYSTEM.CNF
REGIONS = {"SLES_525.84": "Europe (SLES-52584)", "SLES_525.85": "Europe (SLES-52585)", "SLUS_210.50": "USA (SLUS-21050)"}
RWS_FILES = ("/TRACKS/_EATRAX0.RWS", "/TRACKS/_EATRAX1.RWS")
SPLIT = 22
MARK = b"MusicKit"     # first 8 bytes of the RWS segment UUID of songs MusicKit encoded
MAX_TEXT = 60          # longest original title is 41 characters
MAX_SONGS = elfpatch.MAX_SONGS   # 95, as in the Burnout Revenge MusicKit
ORIGINAL_SONGS = elfpatch.ORIGINAL_SONGS   # 44: settings saved on the memory card
DVD5_SECTORS = 2295104   # single-layer DVD (4,700,372,992 bytes)
ADPCM_BYTES_PER_SEC = 32000 * 2 * 16 / 28    # stereo PS-ADPCM at 32 kHz (~36.6 KB/s)
TYPICAL_SONG_SECONDS = 210
DEFAULT_ISO = ""  # no default: the user picks their own disc image
GAME = "Burnout 3 Takedown"       # name of the HostFS boot image: "Burnout 3 Takedown (HostFS).iso"
NAHELAM_DIR = "Burnout 3 - Takedown"  # folder of the HostFS cheats for this game in Nahelam's repository
# a game folder must have these (directory: at least that many files, about 60% of the disc's; "/FILE": that file),
# else it is an incompletely extracted folder
REQUIRED = {"CNF": 1, "DATA": 10, "IOP": 20, "OVID": 40, "PVEH": 140, "SOUND": 12, "TRACKS": 220,
            "/IOP/GTFSHOST.IRX": 1}
KEEP_HINT = ("To keep those changes, open this folder as the source instead (step 1 \"Folder...\", or --iso "
             "<folder>) and change its songs there; or choose an empty folder to start again from your ISO.")
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache")
_GUID = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$")
FIELDS = ("title", "artist", "album")
_SID = {"title": 0, "album": 1, "artist": 2}      # order of the string numbers in a song table entry


def default_output(src):
    base, ext = os.path.splitext(src)
    if base.endswith(" (MusicKit)"):
        return base + ext
    return base + " (MusicKit)" + (ext or ".iso")


def default_folder(src):
    """Suggested HostFS folder for the ISO `src`: next to it, "<iso name> (HostFS)"."""
    base = os.path.splitext(os.path.abspath(src))[0]
    if base.endswith(" (MusicKit)"):
        base = base[:-len(" (MusicKit)")]
    return base + " (HostFS)"


def open_image(path):
    """The disc image (iso.IsoImage) or HostFS game folder (hostfs.FolderImage) at `path`."""
    if os.path.isdir(path):
        return hostfs.FolderImage(path)
    return iso.IsoImage(path)


def span(img, path):
    """(file on this computer, byte offset) of the disc file `path` in a disc image or game folder."""
    return img.span(path) if getattr(img, "is_folder", False) else hostfs.iso_span(img, path)


class Song:
    def __init__(self, index, title, artist, album, rws_index, segment, usable, original=True, names=None, flags=7,
                 sids=(0, 0, 0)):
        self.index = index
        self.title = title
        self.artist = artist
        self.album = album
        self.rws_index = rws_index
        self.segment = segment
        self.usable = usable
        self.original = original
        self.names = names or {}
        self.flags = flags
        self.sids = tuple(sids)      # (title, album, artist) string numbers

    @property
    def duration(self):
        return self.usable / 2 / 16 * 28 / 32000.0


class SongRef:
    """A song of the opened disc in the new song list, optionally with new audio (`audio` = file path) and new
    names (`title`/`artist`/`album` for every language, `names` = {lang: {field: text}} per language)."""

    def __init__(self, src, audio=None, title=None, artist=None, album=None, names=None):
        self.src = src
        self.audio = audio
        self.title = title
        self.artist = artist
        self.album = album
        self.names = names or {}

    def text(self, disc, lang, field):
        v = (self.names.get(lang) or {}).get(field)
        if v:
            return v
        v = getattr(self, field)
        if v:
            return v
        return disc.songs[self.src].names.get(lang, {}).get(field, "")

    def edited(self, disc):
        return any(self.text(disc, l, f) != disc.songs[self.src].names.get(l, {}).get(f, "")
                   for l in disc.langs for f in FIELDS)

    def to_json(self):
        return {"src": self.src, "audio": self.audio, "title": self.title, "artist": self.artist,
                "album": self.album, "names": self.names}

    @classmethod
    def from_json(cls, j):
        return cls(j["src"], j.get("audio"), j.get("title"), j.get("artist"), j.get("album"), j.get("names") or {})


def item_from_json(j):
    return SongRef.from_json(j) if "src" in j else NewSong.from_json(j)


class NewSong:
    """A song to add: source file + names (per language; missing languages use the main text)."""

    def __init__(self, path, title="", artist="", album="", names=None):
        self.path = path
        self.title = title
        self.artist = artist
        self.album = album
        self.names = names or {}     # lang -> {"title":..,"artist":..,"album":..}
        self.info = None

    def probe(self):
        self.info = audio.AudioInfo(self.path)
        return self.info

    def text(self, lang, field):
        v = (self.names.get(lang) or {}).get(field)
        return v if v else getattr(self, field)

    def to_json(self):
        return {"path": self.path, "title": self.title, "artist": self.artist, "album": self.album,
                "names": self.names}

    @classmethod
    def from_json(cls, j):
        return cls(j["path"], j.get("title", ""), j.get("artist", ""), j.get("album", ""), j.get("names") or {})


def _base_count(table):
    """Number of strings of the original file: everything up to the GUID that ends it (MusicKit appends after it)."""
    for i in range(len(table) - 1, -1, -1):
        if _GUID.match(table.strings[i]):
            return i + 1
    return len(table)


class Disc:
    def __init__(self, path):
        self.path = path
        self.img = open_image(path)
        self.is_folder = getattr(self.img, "is_folder", False)
        if self.is_folder:
            missing = hostfs.missing_parts(self.img, REQUIRED)
            if missing:
                raise ValueError(hostfs.incomplete_message(missing))
        self.elf_path = self._boot_elf()
        for p in (self.elf_path,) + RWS_FILES:
            if p not in self.img.entries:
                raise ValueError("not a Burnout 3: Takedown disc (missing %s)" % p)
        name = self.elf_path.lstrip("/")
        if name not in REGIONS:
            raise ValueError("unsupported Burnout 3 version (%s); supported: Europe SLES-52584 or SLES-52585, USA SLUS-21050" % name)
        self.region = REGIONS[name]
        self.elf = self.img.read_file(self.elf_path)
        self.crc = elfpatch.crc(self.elf)
        try:
            self.count = elfpatch.read_song_count(self.elf)
            table = elfpatch.read_table(self.elf)
            self.buffer_size = elfpatch.text_buffer_size(self.elf)
        except Exception as exc:
            raise ValueError("unsupported game executable %s: %s" % (name, exc))
        self.patched = elfpatch.is_patched(self.elf)
        self.unlocked = elfpatch.is_unlocked(self.elf)
        self.langs = tuple(l for l in LANGS if LANG_FILE % l in self.img.entries)
        if not self.langs:
            raise ValueError("no text files found")
        self.changed_paths = {self.elf_path} | set(RWS_FILES) | {LANG_FILE % l for l in self.langs}
        self.tables = {l: strtable.IndexedStringTable(self.img.read_file(LANG_FILE % l)) for l in self.langs}
        bases = {_base_count(t) for t in self.tables.values()}
        if len(bases) != 1:
            raise ValueError("the text files of this disc do not match each other")
        self.base = bases.pop()
        self.allowed = strtable.charset(self.tables.values())
        self.headers = []
        for p in RWS_FILES:
            self.headers.append(rws.RwsHeader(self._rws_header_blob(RWS_FILES.index(p))))
        self.songs = []
        for i in range(self.count):
            ri = 0 if i < SPLIT else 1
            seg_i = i if i < SPLIT else i - SPLIT
            hdr = self.headers[ri]
            seg = hdr.segments[seg_i] if seg_i < len(hdr.segments) else None
            sids = table[i][2:5]
            names = {}
            for l in self.langs:
                t = self.tables[l]
                names[l] = {f: t.get(sids[_SID[f]]) or "" for f in FIELDS}
            first = names[self.langs[0]]
            original = seg is not None and not seg.uuid.startswith(MARK)
            self.songs.append(Song(i, first["title"].strip(), first["artist"].strip(), first["album"].strip(), ri,
                                   seg_i, seg.usable if seg else 0, original=original, names=names,
                                   flags=table[i][5], sids=sids))

    def sanitize(self, text):
        return strtable.sanitize(text, self.allowed, MAX_TEXT)

    def decode_song(self, index):
        s = self.songs[index]
        hdr = self.headers[s.rws_index]
        seg = hdr.segments[s.segment]
        f, e = self.img.open_file(RWS_FILES[s.rws_index])
        with f:
            f.seek(e.lsn * iso.SECTOR + hdr.segment_file_offset(seg))
            payload = f.read(seg.size)
        return rws.decode_segment(payload, seg.usable, hdr.block_size, hdr.channels)

    def preview_wav(self, index, seconds=None):
        import soundfile as sf
        pcm = self.decode_song(index)
        if seconds:
            pcm = pcm[: int(seconds * 32000)]
        path = os.path.join(tempfile.gettempdir(), "musickit_preview_%d.wav" % index)
        sf.write(path, pcm, 32000)
        return path

    def reference_loudness(self, progress=None):
        """Median integrated loudness of the original songs (cached per image)."""
        st = os.stat(span(self.img, RWS_FILES[0])[0] if self.is_folder else self.path)
        key = "%s|%d|%d" % (os.path.basename(self.path), st.st_size, int(st.st_mtime))
        cache = os.path.join(CACHE_DIR, "loudness.json")
        try:
            data = json.load(open(cache))
        except Exception:
            data = {}
        if key in data:
            return data[key]
        vals = []
        orig = [s.index for s in self.songs if s.original] or list(range(self.count))
        n = len(orig)
        for k, i in enumerate(orig):
            if progress:
                progress(k / n, "measuring loudness of original songs (%d/%d)" % (k + 1, n))
            pcm = self.decode_song(i)
            vals.append(audio.measure_loudness(pcm.astype(np.float64) / 32768.0, 32000))
        res = {"median_lufs": statistics.median(vals), "min_lufs": min(vals), "max_lufs": max(vals),
               "songs": vals}
        os.makedirs(CACHE_DIR, exist_ok=True)
        data[key] = res
        json.dump(data, open(cache, "w"), indent=1)
        return res

    # ------------------------------------------------------------------ build
    def current_list(self):
        """The song list of this disc as build items (nothing changed)."""
        return [SongRef(i) for i in range(self.count)]

    def is_unchanged(self, items):
        return len(items) == self.count and all(
            isinstance(it, SongRef) and it.src == i and not it.audio and not it.edited(self)
            for i, it in enumerate(items))

    def save_shift(self, items):
        """Positions (1-based) among the first 44 whose memory card settings will apply to a different song after
        the build (removing / moving songs; replacing audio or editing names keeps them)."""
        n = min(self.count, ORIGINAL_SONGS)
        return [i + 1 for i in range(n)
                if i >= len(items) or not (isinstance(items[i], SongRef) and items[i].src == i)]

    def save_warning(self, items):
        pos = self.save_shift(items)
        if not pos:
            return None
        return ("Removing or moving songs shifts the EA Trax settings saved on your memory card (all / race only / "
                "menu only / off per song): position(s) %s will use the setting saved for the song that was there "
                "before. Check the EA Trax list after loading your save." % _ranges(pos))

    def texts(self, items):
        """The song names of `items` as written to the disc: [{lang: {field: text}}] per position."""
        out = []
        for it in items:
            d = {}
            for l in self.langs:
                src = {} if isinstance(it, NewSong) else self.songs[it.src].names.get(l, {})
                vals = {}
                for f in FIELDS:
                    v = it.text(l, f) if isinstance(it, NewSong) else it.text(self, l, f)
                    if isinstance(it, SongRef) and v == src.get(f, ""):
                        vals[f] = v                     # unchanged text stays exactly as it is
                    else:
                        vals[f] = self.sanitize(v) or ("Untitled" if f == "title" else " ")
                d[l] = vals
            out.append(d)
        return out

    def string_plan(self, items):
        """Text files and string numbers for `items`: ({lang: IndexedStringTable}, [(title, album, artist)]).
        Kept songs keep their strings (renamed in place); added songs reuse the strings of removed songs first,
        then get new strings after the original ones. Strings MusicKit added earlier are rebuilt compactly."""
        texts = self.texts(items)
        kept = [it for it in items if isinstance(it, SongRef)]
        refs = collections.Counter(sid for it in kept for sid in self.songs[it.src].sids)
        used_src = {it.src for it in kept}
        free = sorted({sid for s in self.songs if s.index not in used_src for sid in s.sids
                       if sid < self.base and sid not in refs})
        appended = []          # texts per new string (number self.base + k), {lang: text}
        sids = []
        for k, it in enumerate(items):
            ids = [None, None, None]
            if isinstance(it, SongRef):
                s = self.songs[it.src]
                for f, j in _SID.items():
                    sid = s.sids[j]
                    if sid >= self.base:
                        continue                  # rebuilt below
                    shared = refs[sid] > 1 and any(texts[k][l][f] != self.tables[l].get(sid) for l in self.langs)
                    if not shared:
                        ids[j] = sid
            for f, j in _SID.items():
                if ids[j] is None:
                    if isinstance(it, NewSong) and free:
                        ids[j] = free.pop(0)
                    else:
                        ids[j] = self.base + len(appended)
                        appended.append(None)
            sids.append(tuple(ids))
        tables = {}
        for l in self.langs:
            t = strtable.IndexedStringTable(self.img.read_file(LANG_FILE % l))
            t.truncate(self.base)
            for _ in appended:
                t.append("")
            for k, ids in enumerate(sids):
                for f, j in _SID.items():
                    t.set(ids[j], texts[k][l][f])
            tables[l] = t
        return tables, sids

    def text_room(self, items, tables=None):
        """{lang: bytes left} in the game's text buffer after the build (negative = names do not fit)."""
        if tables is None:
            tables, _ = self.string_plan(items)
        return {l: self.buffer_size - t.size() for l, t in tables.items()}

    def text_warning(self, items, tables=None):
        room = self.text_room(items, tables)
        over = {l: -r for l, r in room.items() if r < 0}
        if not over:
            return None
        l, n = max(over.items(), key=lambda x: x[1])
        return ("The song names do not fit into the game's %s text (%d bytes too many, about %d characters): use "
                "shorter titles, artists or albums, or add fewer songs." % (LANG_NAMES[l], n, (n + 1) // 2))

    def build(self, new_songs, out_path, normalize=True, progress=None, unlock_off=False):
        """Add `new_songs` after the current songs and write a new ISO. Returns a report dict.
        unlock_off: see build_list(); can be the only change."""
        if not new_songs and not (unlock_off and not self.unlocked):
            raise ValueError("nothing to do: add songs or choose the switch-off unlock")
        return self.build_list(self.current_list() + list(new_songs), out_path, normalize, progress, unlock_off)

    def build_list(self, items, out_path, normalize=True, progress=None, unlock_off=False):
        """Write a new ISO whose songs are `items` in this order: SongRef (a song of this disc, optionally with
        new audio / names) or NewSong (added). Returns a report dict.
        unlock_off: let the EA Trax menu switch every song OFF (the game normally refuses OFF for songs not
        heard to the end yet). With an unchanged song list only the executable changes."""
        prog = progress or (lambda f, m: None)
        if self.is_folder:
            raise ValueError("a HostFS folder is saved into itself (build_folder), not as a new ISO")
        if os.path.abspath(out_path) == os.path.abspath(self.path):
            raise ValueError("the output must be a new file (the source ISO is never modified)")
        reps, report = self.replacements(items, normalize, prog, unlock_off)
        placed = self.img.build(out_path, reps, lambda f, m: prog(0.55 + 0.45 * f, m))
        report["placed"] = {k: v for k, v in placed.items()}
        return report

    def replacements(self, items, normalize=True, progress=None, unlock_off=False, estimate=None):
        """The changed files {iso path: bytes or iso.Parts} for the song list `items`, and a report dict.
        estimate: {position: seconds} (missing positions are probed) - nothing is encoded, new audio becomes
        placeholder parts of the size the encoder will produce (for size_plan())."""
        prog = progress or (lambda f, m: None)
        unlock_off = unlock_off and not self.unlocked
        if unlock_off and self.is_unchanged(items):      # unlock only: the executable is the one changed file
            elf = elfpatch.unlock(self.elf)
            assert elfpatch.crc(elf) == self.crc
            return {self.elf_path: elf}, {"songs": [], "save_shift": [], "unlocked": True,
                                          "total_songs": self.count, "removed": 0}
        total = len(items)
        if total < 1:
            raise ValueError("at least one song must stay on the disc")
        if total > MAX_SONGS:
            raise ValueError("too many songs (max %d in total)" % MAX_SONGS)
        for it in items:
            if isinstance(it, SongRef) and not 0 <= it.src < self.count:
                raise ValueError("song %d is not on this disc" % (it.src + 1))
        tables, sids = self.string_plan(items)
        if estimate is None:
            w = self.text_warning(items, tables)
            if w:
                raise ValueError(w)
        report = {"songs": [], "save_shift": self.save_shift(items), "unlocked": unlock_off or self.unlocked}
        target = None
        enc = [k for k, it in enumerate(items) if isinstance(it, NewSong) or it.audio]
        if normalize and enc and estimate is None:
            ref = self.reference_loudness(lambda f, m: prog(0.15 * f, m))
            target = ref["median_lufs"]
            report["target_lufs"] = target
        # 1. segments: (payload: bytes or (path, offset, size), padded size, usable, uuid, info template)
        block = self.headers[0].block_size
        segs = []
        for k, it in enumerate(items):
            if k in enc and estimate is not None:
                sec = estimate.get(k)
                if sec is None:
                    sec = audio.AudioInfo(it.path if isinstance(it, NewSong) else it.audio).duration
                size, usable = self.encoded_size(sec)
                segs.append((("", 0, size), size, usable, MARK + bytes(8), self.headers[1].segments[-1].info))
            elif k in enc:
                path = it.path if isinstance(it, NewSong) else it.audio
                title = it.title if isinstance(it, NewSong) else it.text(self, self.langs[0], "title")
                prog(0.15 + 0.35 * enc.index(k) / len(enc), "encoding %s" % (title or os.path.basename(path)))
                pcm, in_lufs = audio.prepare(path, target, normalize)
                payload, usable = rws.encode_segment(pcm, block)
                out_lufs = audio.measure_loudness(pcm.astype(np.float64) / 32768.0, 32000) if len(pcm) > 32000 else None
                report["songs"].append({"index": k, "title": title, "seconds": len(pcm) / 32000.0,
                                        "input_lufs": in_lufs, "output_lufs": out_lufs, "bytes": len(payload),
                                        "replaced": isinstance(it, SongRef)})
                segs.append((payload, len(payload), usable, MARK + os.urandom(8), self.headers[1].segments[-1].info))
            else:
                s = self.songs[it.src]
                hdr = self.headers[s.rws_index]
                seg = hdr.segments[s.segment]
                host, base = span(self.img, RWS_FILES[s.rws_index])
                src = (host, base + hdr.segment_file_offset(seg), seg.size)
                segs.append((src, seg.size, seg.usable, seg.uuid, seg.info))
        # 2. stream files (a file whose songs did not change is kept as it is)
        prog(0.5, "building stream files")
        reps = {}
        for ri in range(len(RWS_FILES)):
            lo, hi = (0, SPLIT) if ri == 0 else (SPLIT, total)
            part = segs[lo:hi]
            if not part:
                continue     # never opened by the game when no song position maps to it
            old = self.headers[ri].segments
            if len(part) == len(old) and all(
                    isinstance(items[lo + j], SongRef) and items[lo + j].src == lo + j and lo + j not in enc
                    and part[j][3] == old[j].uuid for j in range(len(part))):
                continue
            hdr = rws.RwsHeader(self._rws_header_blob(ri))
            hdr.segments = []
            parts = []
            off = 0
            for j, (src, size, usable, uid, info) in enumerate(part):
                info = bytearray(info)
                struct.pack_into("<II", info, 0x18, size, off)
                name = "%02d" % (lo + j)
                hdr.segments.append(rws.Segment(info, usable, uid, name, rws._pad_string(name)))
                parts.append(src)
                off += size
            reps[RWS_FILES[ri]] = iso.Parts([hdr.build()] + parts)
        # 3. text files (unchanged files are kept as they are)
        for l in self.langs:
            data = tables[l].build()
            if data != self.img.read_file(LANG_FILE % l):
                reps[LANG_FILE % l] = data
        # 4. executable: song count + table (flags travel with their song), optional unlock
        entries = [sids[k] + (7 if isinstance(it, NewSong) else self.songs[it.src].flags,)
                   for k, it in enumerate(items)]
        elf = elfpatch.set_table(self.elf, entries)
        if unlock_off:
            elf = elfpatch.unlock(elf)
        reps[self.elf_path] = elf
        assert elfpatch.crc(reps[self.elf_path]) == self.crc   # PCSX2 game CRC unchanged
        report["total_songs"] = total
        report["removed"] = self.count - len({it.src for it in items if isinstance(it, SongRef)})
        return reps, report

    def encoded_size(self, seconds):
        """(padded payload bytes, usable bytes) of a song of `seconds` encoded for the game."""
        block = self.headers[0].block_size
        per_ch = (int(round(seconds * 32000)) + 27) // 28 * 16
        half = block // 2
        return (per_ch + half - 1) // half * block, per_ch * 2

    def size_plan(self, items, unlock_off=False, estimate=None):
        """Projected output image for the song list `items` without encoding anything:
        {"bytes", "sectors", "fits" (single-layer DVD), "room_bytes", "text"}. None for a HostFS folder."""
        if self.is_folder:
            return None
        if self.is_unchanged(items) and not (unlock_off and not self.unlocked):
            sectors = self.img.volume_sectors
        else:
            reps, _ = self.replacements(items, normalize=False, unlock_off=unlock_off, estimate=estimate or {})
            sectors = self.img.plan({k: len(v) for k, v in reps.items()})[1]
        size = sectors * iso.SECTOR
        room = DVD5_SECTORS * iso.SECTOR - size
        return {"bytes": size, "sectors": sectors, "fits": room >= 0, "room_bytes": max(room, 0),
                "text": capacity_text(size, max(room, 0))}

    # ------------------------------------------------------------------ HostFS folder
    def check_folder(self, folder):
        """Why `folder` cannot receive this disc (None if it can): a game folder of another game / version, a
        folder with other files, or (for a folder opened as the source) any other folder than itself."""
        folder = os.path.abspath(folder)
        if self.is_folder:
            if os.path.normcase(folder) != os.path.normcase(os.path.abspath(self.path)):
                return "an opened folder is saved into itself (to fill another folder, open your ISO)"
            return None
        if os.path.isfile(folder):
            return "this is a file - choose a folder"
        if os.path.isdir(folder) and os.listdir(folder):
            if not hostfs.is_game_folder(folder):
                return hostfs.not_a_game_folder(folder)
            try:
                img = hostfs.FolderImage(folder)
                elf = hostfs.boot_elf(img.read_file("/SYSTEM.CNF"))
                if elf != self.elf_path:
                    return "this folder has another game or version (%s, not %s)" % (
                        (elf or "?").strip("/"), self.elf_path.strip("/"))
                if elf in img.entries and elfpatch.crc(img.read_file(elf)) != self.crc:
                    return "the game executable in this folder was changed by another tool (other PCSX2 CRC)"
                if elf in img.entries:
                    other = self.foreign_elf_changes(img.read_file(elf))
                    if other:
                        return ("the game executable in this folder has changes MusicKit did not make (%s): saving "
                                "your ISO into it would remove them. %s" % (other, KEEP_HINT))
                problem = self.folder_text_problem(img)
                if problem:
                    return problem
            except Exception as exc:
                return "cannot read this folder: %s" % exc
        return None

    def _folder_tables(self, img):
        """{lang: IndexedStringTable} of the game folder `img` (ValueError when they cannot be read)."""
        out = {}
        for l in self.langs:
            p = LANG_FILE % l
            if p in img.entries:
                out[l] = strtable.IndexedStringTable(img.read_file(p))
        return out

    def folder_text_problem(self, img):
        """Why the text files of the game folder `img` cannot take the song names of this disc (None if they can):
        another tool changed how many texts they have before the song names MusicKit adds."""
        try:
            tables = self._folder_tables(img)
        except Exception as exc:
            return "the text files in this folder cannot be read (%s). %s" % (exc, KEEP_HINT)
        for l, t in tables.items():
            if _base_count(t) != self.base:
                return ("%s in this folder has %d texts before the song names, your ISO %d (changed by another "
                        "tool): saving your ISO into it would remove those changes. %s"
                        % ((LANG_FILE % l).lstrip("/"), _base_count(t), self.base, KEEP_HINT))
        return None

    def other_texts(self, img):
        """Text files of the game folder `img` with texts (not song names) that differ from this disc:
        ["GLOBALEN.BIN (3 texts)", ...]."""
        try:
            ftables = self._folder_tables(img)
        except Exception:
            return ["text files unreadable"]
        managed = {sid for s in self.songs for sid in s.sids}
        if self.elf_path in img.entries:
            try:
                managed |= {sid for row in elfpatch.read_table(img.read_file(self.elf_path)) for sid in row[2:5]}
            except Exception:
                pass
        out = []
        for l, ft in ftables.items():
            it = self.tables[l]
            n = sum(1 for i in range(min(self.base, len(ft))) if i not in managed and ft.get(i) != it.get(i))
            if n:
                out.append("%s (%d text%s)" % ((LANG_FILE % l).split("/")[-1], n, "" if n == 1 else "s"))
        return out

    def keep_folder_texts(self, folder, reps):
        """Texts another mod changed in the folder's text files (not song names): put back on top of the song names
        in `reps` (so saving an ISO into the folder keeps them). Returns ["GLOBALEN.BIN (3 texts)", ...]."""
        fimg = hostfs.FolderImage(folder)
        ftables = self._folder_tables(fimg)
        if not ftables:
            return []
        managed = {sid for s in self.songs for sid in s.sids}
        for p in (self.elf_path,):
            for data in (reps.get(p), fimg.read_file(p) if p in fimg.entries else None):
                if isinstance(data, bytes):
                    try:
                        managed |= {sid for row in elfpatch.read_table(data) for sid in row[2:5]}
                    except Exception:
                        pass
        kept = []
        for l, ft in ftables.items():
            p = LANG_FILE % l
            it = self.tables[l]
            diff = [i for i in range(min(self.base, len(ft))) if i not in managed and ft.get(i) != it.get(i)]
            if not diff:
                continue
            data = reps.get(p)
            nt = strtable.IndexedStringTable(data if isinstance(data, bytes) else self.img.read_file(p))
            for i in diff:
                nt.set(i, ft.get(i))
            if nt.size() > self.buffer_size:
                raise ValueError("%s: the texts another mod changed in this folder and the song names do not fit "
                                 "into the game's text buffer together. %s" % (p.lstrip("/"), KEEP_HINT))
            reps[p] = nt.build()
            kept.append("%s (%d text%s)" % (p.split("/")[-1], len(diff), "" if len(diff) == 1 else "s"))
        return kept

    def foreign_elf_changes(self, elf):
        """Description of the changes in the executable `elf` (of a game folder) that are neither on this disc nor
        MusicKit's own (song list); None when there are none."""
        try:
            skip = elfpatch.own_ranges(self.elf) + elfpatch.own_ranges(elf)
        except Exception as exc:
            return "MusicKit cannot read it: %s" % exc
        r = hostfs.elf_changes(self.elf, elf, skip)
        return hostfs.describe_ranges(r) if r else None

    def folder_plan(self, folder):
        """{"extract_bytes": bytes still to copy from the ISO into `folder`, "has_game": it has the game already}."""
        folder = os.path.abspath(folder)
        if self.is_folder:
            return {"extract_bytes": 0, "has_game": True}
        if not os.path.isdir(folder):
            return {"extract_bytes": sum(e.size for e in self.img.entries.values()), "has_game": False}
        return {"extract_bytes": hostfs.needed_bytes(self.img, folder), "has_game": hostfs.is_game_folder(folder)}

    def build_folder(self, items, folder, normalize=True, progress=None, unlock_off=False):
        """Save the song list `items` into the HostFS game folder `folder` (see hostfs.py). From an ISO the game's
        files are extracted first (missing files only), then the changed files are written and the boot image
        "<game> (HostFS).iso" is rebuilt; every file MusicKit manages ends up exactly as in a new ISO built from
        the same source. A folder opened as the source is saved into itself. Returns a report dict."""
        prog = progress or (lambda f, m: None)
        folder = os.path.abspath(folder)
        problem = self.check_folder(folder)
        if problem:
            raise ValueError(problem)
        boot = hostfs.boot_iso_path(folder, GAME)
        if os.path.isdir(folder):          # refuse before encoding when the game is running
            fimg = hostfs.FolderImage(folder) if hostfs.is_game_folder(folder) else None
            hostfs.check_unlocked([boot] + [hostfs.host_path(folder, p, fimg) for p in sorted(self.changed_paths)])
        reps, report = self.replacements(items, normalize, lambda f, m: prog(0.6 * f, m), unlock_off)
        if not self.is_folder:
            hostfs.extract(self.img, folder, lambda f, m: prog(0.6 + 0.25 * f, m), skip=reps)
            # files MusicKit manages that this song list leaves unchanged: as on the source ISO
            fimg = hostfs.FolderImage(folder)
            for p in sorted(self.changed_paths):
                if p in reps:
                    continue
                e, fe = self.img.entries[p], fimg.entries[p]
                if fe.size != e.size or not hostfs.same_file(self.path, e.lsn * iso.SECTOR, fe.host, 0, e.size):
                    reps[p] = (self.path, e.lsn * iso.SECTOR, e.size)
            # another mod's texts in the text files stay (song names are MusicKit's)
            report["kept_texts"] = self.keep_folder_texts(folder, reps)
        written =hostfs.write_files(folder, reps, lambda f, m: prog(0.85 + 0.13 * f, m), extra_locked=[boot])
        prog(0.99, "writing the boot image")
        report["boot_iso"] = hostfs.write_boot_iso(folder, GAME)
        report["folder"] = folder
        report["written"] = sorted(written)
        report["boot_hint"] = hostfs.boot_hint(folder, GAME)
        prog(1.0, "done")
        return report

    def hostfs_steps(self, folder=None):
        """How to play the folder in PCSX2 (numbered steps)."""
        return hostfs.instructions(GAME, NAHELAM_DIR, self.elf_path, self.crc, folder)

    def close(self):
        f = getattr(self.img, "f", None)
        if f:
            f.close()

    def _boot_elf(self):
        try:
            cnf = self.img.read_file("/SYSTEM.CNF").decode("latin-1")
            for line in cnf.splitlines():
                if line.replace(" ", "").upper().startswith("BOOT2="):
                    return "/" + line.replace("/", "\\").split("\\")[-1].split(";")[0].strip().upper()
        except KeyError:
            pass
        return ELF_PATH

    def _rws_header_blob(self, i):
        f, e = self.img.open_file(RWS_FILES[i])
        with f:
            head = f.read(0x20)
            hs = int.from_bytes(head[0x10:0x14], "little")
            f.seek(e.lsn * iso.SECTOR)
            return f.read(0x18 + hs + 12)


def capacity_text(size_bytes, room_bytes=None):
    """One line about the single-layer DVD limit for an image of `size_bytes` (room_bytes: free room left)."""
    if size_bytes > DVD5_SECTORS * iso.SECTOR:
        return ("The new image will be %.2f GB - larger than a single-layer DVD (4.7 GB). It works in emulators "
                "(PCSX2), but it can't be burned to a normal DVD for a real PS2." % (size_bytes / 1e9))
    room = DVD5_SECTORS * iso.SECTOR - size_bytes if room_bytes is None else room_bytes
    minutes = room / ADPCM_BYTES_PER_SEC / 60
    return ("%.2f GB - fits on a single-layer DVD, with room for about %d more minutes of music (~%d songs of "
            "%d:%02d)." % (size_bytes / 1e9, minutes, minutes * 60 // TYPICAL_SONG_SECONDS,
                          TYPICAL_SONG_SECONDS // 60, TYPICAL_SONG_SECONDS % 60))


def _ranges(nums):
    out = []
    for n in nums:
        if out and out[-1][1] == n - 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join("%d" % a if a == b else "%d-%d" % (a, b) for a, b in out)
