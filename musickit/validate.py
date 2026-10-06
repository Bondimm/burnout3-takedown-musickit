"""Validate a MusicKit output ISO against its source: file systems parse, untouched files are byte-identical
and keep their LSNs, the song list/names/streams of the new image are consistent."""
import hashlib
import struct

from . import core, iso

from . import elfpatch

# PCSX2 patches (resources/patches.zip) for the supported versions, by game CRC: written EE addresses.
PNACH = {
    0x75BECC18: {
        "Widescreen 16:9 (PeterDelta)": [0x4E0FB8, 0x4E11FC, 0x66644D],
        "50/60 FPS (PeterDelta)": [0x4E1D5C, 0x4E2CC0, 0x4F561C, 0x51AE34, 0x51C024],
        "480p Mode (felixthecat1970)": [0x21B764, 0x228154, 0x228170, 0x437118, 0x437120],
    },
    0xBEBF8793: {
        "Widescreen 16:9 (Aero_)": [
            0x134F2C, 0x134F74, 0x1A176C, 0x1A1774, 0x1A17D0, 0x1A17D8, 0x30D7E4, 0x30D834, 0x31B180, 0x31B1F4,
            0x31D6E4, 0x31D6F8, 0x31D73C, 0x31D750, 0x31D790, 0x31D7A4, 0x31D7E8, 0x31D7FC, 0x31D840, 0x31D854,
            0x31DA20, 0x31DA40, 0x38AE00, 0x38AE04, 0x38AE38, 0x38AE4C, 0x3A6984, 0x3A6998, 0x3A69C8, 0x3A69DC,
            0x3D70BC, 0x3D7238, 0x3D72F4] + list(range(0x4858C0, 0x485960, 4)) + list(range(0x4859A0, 0x4859E8, 4)) + [
            0x4B7658, 0x4B7668, 0x4B7678, 0x4B7688, 0x4CA638, 0x4CA640, 0x4CA650, 0x4CA658, 0x4CA660, 0x4D1568,
            0x4D1570, 0x4E0A38, 0x4E0A94, 0x4E0C3C, 0x4E0C70, 0x4E0C7C, 0x4E0C80, 0x4E105C, 0x665ECD, 0x6682B0,
            0x669B30],
        "Disable Motion Blur / Bloom (escape209)": [0x665F2A, 0x665F28],
        "60 FPS for Menus / Crashes (Nehalem)": [0x130DD8, 0x130DDC, 0x130DE0, 0x130DE4, 0x130DE8, 0x1D3F2C, 0x1320D8],
        "Progressive Scan / MPH to KPH / extras (Nehalem)": [0x437758, 0x1A3BA0, 0x1A3D78, 0x261E6C],
    },
}


def _hash_file(img, e):
    h = hashlib.sha1()
    img.f.seek(e.lsn * iso.SECTOR)
    left = e.size
    while left:
        b = img.f.read(min(left, 8 << 20))
        h.update(b)
        left -= len(b)
    return h.hexdigest()


def validate(src_path, out_path, log=print, hash_all=True):
    ok = True
    src = iso.IsoImage(src_path)
    out = iso.IsoImage(out_path)
    s0 = core.Disc(src_path)
    CHANGED = s0.changed_paths
    if not out.udf:
        log("FAIL: UDF bridge not readable: %s" % getattr(out, "udf_error", "?"))
        ok = False
    # pycdlib cross-check (independent ISO9660/UDF parser)
    try:
        import pycdlib
        p = pycdlib.PyCdlib()
        p.open(out_path)
        n = 0
        for root, dirs, files in p.walk(iso_path="/"):
            n += len(files)
        nu = 0
        if p.has_udf():
            for root, dirs, files in p.walk(udf_path="/"):
                nu += len(files)
        p.close()
        log("pycdlib: %d ISO9660 files, %d UDF files" % (n, nu))
        if n != len(src.entries) or (nu and nu != len(src.entries)):
            log("FAIL: file count differs from source (%d)" % len(src.entries))
            ok = False
    except ImportError:
        log("pycdlib not installed, skipped")
    except Exception as exc:
        log("FAIL: pycdlib could not parse the image: %s" % exc)
        ok = False
    # UDF and ISO9660 agree
    for k, e in out.entries.items():
        if e.udf_fe is None:
            continue
        d = out.read(e.udf_fe)
        ext = out._fe_extents(d)
        size = struct.unpack_from("<Q", d, 56)[0]
        if size != e.size or ext[0][0] + out.part_start != e.lsn:
            log("FAIL: UDF/ISO9660 mismatch for %s" % k)
            ok = False
        if bytes(iso.udf_fix_tag(d)) != d:
            log("FAIL: bad UDF descriptor CRC for %s" % k)
            ok = False
    same = moved = 0
    for k, e in src.entries.items():
        o = out.entries.get(k)
        if o is None:
            log("FAIL: %s missing" % k)
            ok = False
            continue
        if k in CHANGED:
            continue
        if o.lsn != e.lsn or o.size != e.size:
            log("FAIL: %s moved (%d -> %d)" % (k, e.lsn, o.lsn))
            moved += 1
            ok = False
        elif hash_all and _hash_file(src, e) != _hash_file(out, o):
            log("FAIL: %s content differs" % k)
            ok = False
        else:
            same += 1
    log("untouched files identical at original LSNs: %d/%d%s" % (same, len(src.entries) - len(CHANGED),
                                                                 "" if hash_all else " (LSN/size only)"))
    for k in sorted(CHANGED):
        log("changed %-34s lsn %8d -> %8d  size %10d -> %10d" % (k, src.entries[k].lsn, out.entries[k].lsn,
                                                                 src.entries[k].size, out.entries[k].size))
    d = core.Disc(out_path)
    log("version: %s" % d.region)
    log("songs: %d -> %d" % (s0.count, d.count))
    log("switch any song OFF: %s" % ("yes" if d.unlocked else "no (original behaviour)"))
    c_src, c_out = elfpatch.crc(s0.elf), elfpatch.crc(d.elf)
    log("PCSX2 game CRC: %08X -> %08X %s" % (c_src, c_out, "(unchanged)" if c_src == c_out else "CHANGED"))
    if c_src != c_out:
        ok = False
    ranges = elfpatch.touched_ranges(d.elf)
    for name, addrs in PNACH.get(c_src, {}).items():
        clash = [a for a in addrs if any(lo <= a < hi for lo, hi in ranges)]
        log("pnach '%s': %s" % (name, "no overlap" if not clash else "OVERLAP at " + ", ".join(map(hex, clash))))
        if clash:
            ok = False
    log("note: the modified disc can never match the redump MD5 (expected for any modified image)")
    size = out.volume_sectors * iso.SECTOR
    log(("" if size <= core.DVD5_SECTORS * iso.SECTOR else "warning: ") + core.capacity_text(size))
    ok = check_songs(s0, d, log) and ok
    log("RESULT: %s" % ("OK" if ok else "FAILED"))
    return ok


def check_songs(s0, d, log=print):
    """Song list consistency of output disc `d` (built from `s0`): table, names, streams. Songs whose audio was
    kept (same segment UUID) must be byte-identical to the source; new / replaced songs must decode."""
    ok = True
    table = elfpatch.read_table(d.elf)
    if [t[:2] for t in table] != [(i, 0) for i in range(d.count)]:
        log("FAIL: song table entries do not match their positions")
        ok = False
    if d.count > core.ORIGINAL_SONGS and not d.patched:
        log("FAIL: more than %d songs without the moved song table" % core.ORIGINAL_SONGS)
        ok = False
    if d.patched:
        lay = elfpatch.Layout(d.elf)
        bad = [hex(va) for va, _, t, _ in lay.sites() if t is not None
               and lay.elf.r32(va) != (t(lay.ctx()) if callable(t) else t)]
        if bad or lay.elf.r32(lay.sbrk) != lay.heap:
            log("FAIL: executable patch incomplete at %s" % ", ".join(bad or ["heap start"]))
            ok = False
    if not 1 <= d.count <= core.MAX_SONGS:
        log("FAIL: song count %d" % d.count)
        ok = False
    nseg = [len(h.segments) for h in d.headers]
    if nseg[0] < min(d.count, core.SPLIT) or (d.count > core.SPLIT and nseg[1] < d.count - core.SPLIT):
        log("FAIL: stream files have %s segments for %d songs" % (nseg, d.count))
        ok = False
    src_by_uuid = {}
    for s in s0.songs:
        src_by_uuid[s0.headers[s.rws_index].segments[s.segment].uuid] = s
    for l in d.langs:
        size = d.img.entries[core.LANG_FILE % l].size
        log("text file %s: %d of %d bytes" % (l, size, d.buffer_size))
        if size > d.buffer_size:
            log("FAIL: text file %s is larger than the game's buffer" % l)
            ok = False
    for s in d.songs:
        for l in d.langs:
            if not all(0 <= sid < len(d.tables[l]) for sid in s.sids) or not d.tables[l].get(s.sids[0]):
                log("FAIL: song %d has no title in %s" % (s.index + 1, l))
                ok = False
        seg = d.headers[s.rws_index].segments[s.segment] if s.segment < nseg[s.rws_index] else None
        if seg is None:
            continue
        o = src_by_uuid.get(seg.uuid)
        if o is not None and o.usable == s.usable and _seg_hash(s0, o) == _seg_hash(d, s):
            continue
        if o is not None and not seg.uuid.startswith(core.MARK):
            log("FAIL: song %d audio differs from the source song %d" % (s.index + 1, o.index + 1))
            ok = False
            continue
        pcm = d.decode_song(s.index)
        log("  #%d %s / %s / %s  %.1f s  peak %d  (new audio)" % (s.index + 1, s.artist, s.title, s.album,
                                                                 len(pcm) / 32000.0, int(abs(pcm.astype(int)).max())))
        if len(pcm) < 32000:
            log("FAIL: song %d is shorter than 1 s" % (s.index + 1))
            ok = False
    kept = sum(1 for s in d.songs if s.original)
    log("songs: %d total, %d original, %d added or replaced" % (d.count, kept, d.count - kept))
    return ok


def _seg_hash(disc, s):
    hdr = disc.headers[s.rws_index]
    seg = hdr.segments[s.segment]
    f, e = disc.img.open_file(core.RWS_FILES[s.rws_index])
    with f:
        f.seek(e.lsn * iso.SECTOR + hdr.segment_file_offset(seg))
        h = hashlib.sha1()
        left = seg.size
        while left:
            b = f.read(min(left, 8 << 20))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return h.hexdigest()
