"""MusicKit tests. Run from the repository folder: .venv\\Scripts\\python -m pytest tests

Format tests use your own disc images when MUSICKIT_ISO (European ISO) and/or MUSICKIT_USA_ISO (USA ISO) are set;
they only read them. Without them those tests are skipped. MUSICKIT_FULL_TEST=1 also builds complete images
(several GB each, written to the temp folder and deleted afterwards).
"""
import os
import struct
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from musickit import adpcm, core, elfpatch, iso, rws, strtable  # noqa: E402

ISO = os.environ.get("MUSICKIT_ISO", "")  # path to your own Burnout 3: Takedown ISO (Europe)
USA_ISO = os.environ.get("MUSICKIT_USA_ISO", "")
# per version: CRC, playlist, table, profile, heap start word, signature deltas (trax_setmode, profile_load_a,
# menu_confirm, memory_map), title string of song 41
VERSIONS = {
    "SLES_525.84": (0x75BECC18, 0x4A5EB0, 0x4A5A90, 0x4F55C0, 0x4845D4, (0, 0, 0, 0), 3277),
    "SLUS_210.50": (0xBEBF8793, 0x4A5A20, 0x4A5600, 0x4F5040, 0x484154, (-0x410, -0x70, -0x460, 0x10), 3276),
}


def _skip(path):
    if not path or not os.path.exists(path):
        import pytest
        pytest.skip("missing " + (path or "disc image (set MUSICKIT_ISO / MUSICKIT_USA_ISO)"))


def _isos():
    out = [p for p in (ISO, USA_ISO) if p and os.path.exists(p)]
    if not out:
        import pytest
        pytest.skip("set MUSICKIT_ISO and/or MUSICKIT_USA_ISO")
    return out


def _elf_versions():
    out = []
    for p in (ISO, USA_ISO):
        if p and os.path.exists(p):
            d = core.Disc(p)
            out.append((d.elf_path.lstrip("/"), d.elf) + VERSIONS[d.elf_path.lstrip("/")])
            d.img.f.close()
    if not out:
        import pytest
        pytest.skip("no executable")
    return out


def _ffmpeg():
    from musickit import audio
    try:
        audio.AudioInfo(os.path.join(HERE, "data", "test_chords_22k_mono.wav"))
    except Exception:
        import pytest
        pytest.skip("ffmpeg not found")


def test_adpcm_roundtrip_exact():
    rng = np.random.default_rng(1)
    t = np.arange(32000) / 32000.0
    x = (np.sin(2 * np.pi * 440 * t) * 12000 + rng.normal(0, 300, len(t))).astype(np.int16)
    enc = adpcm.encode(x)
    assert len(enc) == (len(x) + 27) // 28 * 16
    y = adpcm.decode(enc)[: len(x)]
    snr = 10 * np.log10((x.astype(float) ** 2).sum() / ((x - y.astype(float)) ** 2).sum())
    assert snr > 30
    # re-encoding decoded ADPCM is lossless (encoder finds the same frames)
    assert np.array_equal(adpcm.decode(adpcm.encode(y)), adpcm.decode(enc)[: len(adpcm.decode(adpcm.encode(y)))])


def test_rws_header_rebuild_identical():
    for p in _isos():
        d = core.Disc(p)
        for i, path in enumerate(core.RWS_FILES):
            blob = d._rws_header_blob(i)
            h = rws.RwsHeader(blob)
            f, e = d.img.open_file(path)
            with f:
                assert h.build() == f.read(h.data_offset + 12), path
            assert len(h.segments) == 22 and h.sample_rate == 32000 and h.channels == 2
            assert h.codec == rws.CODEC_PSADPCM and h.block_size == 0x2000
        d.img.f.close()


def test_rws_segment_reencode_lossless():
    p = _isos()[0]
    d = core.Disc(p)
    seg = d.headers[1].segments[-1]
    pcm = d.decode_song(d.count - 1)
    pay2, us2 = rws.encode_segment(pcm)
    assert us2 == seg.usable and len(pay2) == seg.size
    assert np.array_equal(rws.decode_segment(pay2, us2), pcm)
    d.img.f.close()


def test_rws_add_segment_layout():
    p = _isos()[0]
    d = core.Disc(p)
    h = rws.RwsHeader(d._rws_header_blob(1))
    end = h.data_size()
    h.add_segment("44", 0x4000, 0x3F00)
    head = h.build()
    h2 = rws.RwsHeader(head)
    assert len(h2.segments) == 23 and h2.segments[-1].name == "44"
    assert h2.segments[-1].offset == end and h2.segments[-1].size == 0x4000
    assert struct.unpack_from("<I", head, 0x50)[0] == h2.data_offset + 12
    d.img.f.close()


def test_indexed_string_table():
    """Text files: byte-identical round trip, song strings, set / append / truncate."""
    for p in _isos():
        d = core.Disc(p)
        for l in d.langs:
            raw = d.img.read_file(core.LANG_FILE % l)
            t = strtable.IndexedStringTable(raw)
            assert t.build() == raw and t.size() == len(raw), l
            assert len(t) == d.base and core._GUID.match(t.get(len(t) - 1)), l
            assert (t.get(477), t.get(478), t.get(479)) == ("Independence Day", "Daylight Breaking", "No Motiv")
            n = len(t)
            assert t.append("Test Song") == n
            t.set(477, "Neu")
            t2 = strtable.IndexedStringTable(t.build())
            assert t2.get(n) == "Test Song" and t2.get(477) == "Neu" and t2.get(478) == "Daylight Breaking"
            assert t2.size() == len(t.build()) == len(raw) + strtable.string_bytes("Test Song") - 2 * 13
            t2.truncate(n)
            t2.set(477, "Independence Day")
            assert t2.build() == raw
            assert len(raw) < d.buffer_size == 0x41800
        assert [s.title for s in d.songs[40:]] == ["Heart Full Of Black", "Right Side Of The Bed", "Radio Up",
                                                    "Just Tonight..."]
        assert d.songs[0].artist == "No Motiv" and d.songs[43].artist == "Jimmy Eat World"
        d.img.f.close()


def test_sanitize():
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 '-éü")
    assert strtable.sanitize("Café Motörhead", allowed) == "Café Motorhead"
    assert strtable.sanitize("Don’t", allowed) == "Don't"


def test_elf_layout_and_relocate():
    for name, d, crc, playlist, table, profile, sbrk, deltas, s40 in _elf_versions():
        assert elfpatch.crc(d) == crc
        lay = elfpatch.Layout(d)
        assert (lay.playlist, lay.table, lay.table_old, lay.profile, lay.sbrk) == (playlist, table, table, profile, sbrk)
        assert (lay.delta["trax_setmode"], lay.delta["profile_load_a"], lay.delta["menu_confirm"],
                lay.delta["memory_map"]) == deltas, name
        assert lay.buffer_size == 0x41800 and not lay.relocated and lay.heap == lay.end
        t = elfpatch.read_table(d)
        assert len(t) == 44 and t[0] == (0, 0, 477, 478, 479, 15) and t[40][2] == s40
        out = elfpatch.relocate(d)
        assert elfpatch.crc(out) == crc and elfpatch.is_patched(out), name       # PCSX2 CRC kept
        assert len(out) == len(d) + 8 + elfpatch.SEG_SIZE + 4                    # aligned segment + CRC word
        r = elfpatch.Layout(out)
        assert r.relocated and r.table == r.table_new == lay.end and r.heap == lay.end + 0x1000
        assert (r.playlist, r.profile, r.sbrk, r.delta) == (playlist, profile, sbrk, lay.delta)
        assert elfpatch.read_table(out) == t and elfpatch.read_song_count(out) == 44
        c = lay.ctx()
        for va, eu, tmpl, _ in r.sites():
            if tmpl is not None:
                assert r.elf.r32(va) == (tmpl(c) if callable(tmpl) else tmpl), (name, hex(eu))
        e, o = r.elf, elfpatch.Elf(d)
        ph = e.phdrs()
        assert ph[2][2] == lay.end and ph[2][4] == ph[2][5] == 0x1000 and ph[2][1] % 16 == 0
        assert ph[0] == o.phdrs()[0] and ph[1] == o.phdrs()[1]
        # nothing else of the loaded program changed
        s = ph[0]
        a = np.frombuffer(bytes(o.d[s[1]:s[1] + s[4]]), "<u4")
        b = np.frombuffer(bytes(e.d[s[1]:s[1] + s[4]]), "<u4")
        diff = {s[2] + 4 * int(i) for i in np.nonzero(a != b)[0]}
        want = {va for va, _, tmpl, _ in r.sites() if tmpl is not None} | {sbrk, playlist + 0x4C}
        assert diff <= want and len(diff) >= len(want) - 2, name
        # the moved table is valid for the profile loops (44 entries) at least
        out45 = elfpatch.set_table(out, [(477, 478, 479, 15)] * 45)
        assert elfpatch.read_song_count(out45) == 45 and elfpatch.crc(out45) == crc and len(out45) == len(out)


def test_set_table_in_place_and_moved():
    for name, d, crc, playlist, table, *_ in _elf_versions():
        orig = elfpatch.read_table(d)
        ents = [o[2:] for o in orig]
        for n in (1, 20, 43, 44):                    # up to 44 songs: data only, the table stays where it is
            out = elfpatch.set_table(d, ents[::-1][:n])
            assert elfpatch.crc(out) == crc and not elfpatch.is_patched(out) and len(out) == len(d) + 4, (name, n)
            assert [t[2:] for t in elfpatch.read_table(out)] == ents[::-1][:n]
            assert elfpatch.read_table(out)[0][:2] == (0, 0)
            e, o = elfpatch.Elf(out), elfpatch.Elf(d)
            s = o.phdrs()[0]
            a = np.frombuffer(bytes(o.d[s[1]:s[1] + s[4]]), "<u4")
            b = np.frombuffer(bytes(e.d[s[1]:s[1] + s[4]]), "<u4")
            diff = [s[2] + 4 * int(i) for i in np.nonzero(a != b)[0]]
            assert all(table <= x < playlist + 8 for x in diff), (name, n)
        for n in (45, 60, elfpatch.MAX_SONGS):
            flags = [(i % 7) + 1 for i in range(n)]
            out = elfpatch.set_table(d, [(4007 + i, 5000 + i, 6000 + i, flags[i]) for i in range(n)])
            assert elfpatch.crc(out) == crc and elfpatch.is_patched(out) and elfpatch.read_song_count(out) == n
            assert [t[5] for t in elfpatch.read_table(out)] == flags
            again = elfpatch.set_table(out, [(477, 478, 479, 7)] * 3)     # shrink a moved table
            assert elfpatch.crc(again) == crc and len(again) == len(out) and elfpatch.read_song_count(again) == 3
            lay = elfpatch.Layout(again)
            for i in range(44):    # entries 0..43 stay valid for the memory card profile loops
                ent = struct.unpack_from("<6I", again, lay.elf.file_offset(lay.table_new + 24 * i))
                assert ent[:2] == (i, 0) and ent[2:5] == (477, 478, 479)
        import pytest
        with pytest.raises(ValueError):
            elfpatch.set_table(d, [(477, 478, 479, 7)] * 96)
        with pytest.raises(ValueError):
            elfpatch.set_table(d, [])


def test_unlock_off():
    """The switch-off unlock: one word, any order with the table changes, PCSX2 CRC kept, idempotent."""
    for name, d, crc, *_ in _elf_versions():
        lay = elfpatch.Layout(d)
        assert [lay.elf.r32(va) for va, _, _, _ in lay.unlock_sites()] == [0x30420008], name
        assert not elfpatch.is_unlocked(d)
        u = elfpatch.unlock(d)
        assert elfpatch.crc(u) == crc and elfpatch.is_unlocked(u), name
        assert elfpatch.unlock(u) == u                                   # idempotent
        e, eu = elfpatch.Elf(d), elfpatch.Elf(u)
        sites = {va for va, _, _, _ in lay.unlock_sites()}
        s = e.phdrs()[0]
        diff = [s[2] + i for i in range(0, s[4], 4) if e.r32(s[2] + i) != eu.r32(s[2] + i)]
        assert set(diff) == sites, name                                  # nothing else in the code changed
        ru = elfpatch.unlock(elfpatch.relocate(d))
        ur = elfpatch.relocate(u)
        for x in (ru, ur, elfpatch.set_table(ru, [(477, 478, 479, 7)] * 50)):
            assert elfpatch.crc(x) == crc and elfpatch.is_unlocked(x) and elfpatch.is_patched(x), name
        assert any(lo <= max(sites) < hi for lo, hi in elfpatch.touched_ranges(ru))


def test_udf_tag_crc():
    for p in _isos():
        img = iso.IsoImage(p)
        elf = [k for k in img.entries if k.startswith("/SL")][0]
        for lsn in (256, img.pd_lsns[0], img.entries[elf].udf_fe):
            d = img.read(lsn)
            assert bytes(iso.udf_fix_tag(d)) == d
        img.f.close()


# ---------------------------------------------------------------- song management (replace/edit/remove/move)
def _check_reps(d, items, reps):
    """Song list consistency of the files build_list writes for `items`: executable table + count + CRC, the
    strings of every language and the stream segment of every position."""
    total = len(items)
    elf = reps[d.elf_path]
    assert elfpatch.crc(elf) == d.crc and elfpatch.read_song_count(elf) == total
    assert elfpatch.is_patched(elf) == (total > 44 or d.patched)
    table = elfpatch.read_table(elf)
    texts = d.texts(items)
    for l in d.langs:
        raw = reps.get(core.LANG_FILE % l) or d.img.read_file(core.LANG_FILE % l)
        assert len(raw) <= d.buffer_size
        t = strtable.IndexedStringTable(raw)
        assert t.get(d.base - 1) == d.tables[l].get(d.base - 1)        # the GUID stays where it is
        for k, it in enumerate(items):
            for f in core.FIELDS:
                assert t.get(table[k][2 + core._SID[f]]) == texts[k][l][f], (l, k, f)
    for k, it in enumerate(items):
        assert table[k][:2] == (k, 0)
        assert table[k][5] == (7 if isinstance(it, core.NewSong) else d.songs[it.src].flags)
    for ri, path in enumerate(core.RWS_FILES):
        lo, hi = (0, min(core.SPLIT, total)) if ri == 0 else (core.SPLIT, total)
        if path not in reps:          # unchanged file: same songs at the same place, or not used at all
            assert hi <= lo or all(isinstance(items[k], core.SongRef) and items[k].src == k and not items[k].audio
                                   for k in range(lo, hi))
            continue
        parts = reps[path].parts
        h = rws.RwsHeader(parts[0])
        assert len(h.segments) == hi - lo and len(parts) == 1 + hi - lo
        for j, seg in enumerate(h.segments):
            it = items[lo + j]
            assert seg.name == "%02d" % (lo + j)
            assert seg.offset == sum(s.size for s in h.segments[:j])
            if isinstance(it, core.SongRef) and not it.audio:
                s = d.songs[it.src]
                src_seg = d.headers[s.rws_index].segments[s.segment]
                e = d.img.entries[core.RWS_FILES[s.rws_index]]
                assert parts[1 + j] == (d.path, e.lsn * iso.SECTOR + d.headers[s.rws_index].segment_file_offset(src_seg),
                                        src_seg.size)
                assert seg.usable == src_seg.usable and seg.uuid == src_seg.uuid
            else:
                assert seg.uuid.startswith(core.MARK) and len(parts[1 + j]) == seg.size


def test_remove_move_edit_consistency():
    for p in _isos():
        d = core.Disc(p)
        items = d.current_list()
        assert d.is_unchanged(items) and d.save_warning(items) is None
        items.pop(2)                                   # remove an original song in _EATRAX0
        items.insert(0, items.pop(-1))                 # move the last song (in _EATRAX1) to the top
        items[24], items[25] = items[25], items[24]    # swap two songs in _EATRAX1
        items[5].title = "Edited Title"                # rename for every language
        items[5].names = {d.langs[-1]: {"artist": "Edited Artist"}}
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert rep["total_songs"] == 43 and rep["removed"] == 1
        assert d.save_shift(items)[:3] == [1, 2, 3] and d.save_warning(items)
        elf = reps[d.elf_path]
        assert not elfpatch.is_patched(elf)            # 43 songs: data only
        t = strtable.IndexedStringTable(reps[core.LANG_FILE % d.langs[-1]])
        sids = elfpatch.read_table(elf)[5][2:5]
        assert t.get(sids[0]) == "Edited Title" and t.get(sids[2]) == "Edited Artist"
        assert sids == d.songs[items[5].src].sids        # renamed in place
        assert len(t) == d.base                          # no string added
        d.img.f.close()


def test_edit_only_keeps_streams_and_saves():
    for p in _isos():
        d = core.Disc(p)
        items = d.current_list()
        items[0].names = {l: {"title": "Neu %s" % l} for l in d.langs}
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert not any(k in reps for k in core.RWS_FILES)   # streams untouched
        assert d.save_shift(items) == [] and d.save_warning(items) is None
        for l in d.langs:
            assert len(reps[core.LANG_FILE % l]) == len(d.img.read_file(core.LANG_FILE % l)) + 2 * (
                len("Neu %s" % l) - len("Independence Day"))
        d.img.f.close()


def test_added_strings_reuse_and_compaction():
    """Added songs take the strings of removed songs first, then new strings after the GUID."""
    for p in _isos():
        d = core.Disc(p)
        items = d.current_list() + [core.NewSong("a.flac", "Add A", "Band A", "Album A"),
                                    core.NewSong("b.flac", "Add B", "Band B", "")]
        tables, sids = d.string_plan(items)
        assert sids[44] == (d.base, d.base + 1, d.base + 2) and sids[45] == (d.base + 3, d.base + 4, d.base + 5)
        for l in d.langs:
            assert len(tables[l]) == d.base + 6 and tables[l].get(d.base + 5) == "Band B"
            assert tables[l].get(d.base + 4) == " "        # empty album
        less = d.current_list()
        del less[7]
        less.append(core.NewSong("a.flac", "Add A", "Band A", "Album A"))
        tables, sids = d.string_plan(less)
        assert sids[-1] == d.songs[7].sids and all(len(t) == d.base for t in tables.values())
        assert d.text_room(items)[d.langs[0]] < d.text_room(d.current_list())[d.langs[0]]
        d.img.f.close()


def test_text_buffer_limit():
    for p in _isos():
        d = core.Disc(p)
        room = min(d.text_room(d.current_list()).values())
        assert room > 10000
        per_song = 3 * strtable.string_bytes("x" * core.MAX_TEXT)
        n = room // per_song + 1                       # just too many maximum-length names
        items = d.current_list() + [core.NewSong("x%d.flac" % k, "T" * 60, "A" * 60, "B" * 60) for k in range(n)]
        if len(items) <= core.MAX_SONGS:
            w = d.text_warning(items)
            assert w and "bytes too many" in w
            import pytest
            with pytest.raises(ValueError):
                d.replacements(items, normalize=False)
            assert d.size_plan(items, estimate={k: 1.0 for k in range(44, len(items))})["bytes"] > 0
        ok = d.current_list() + [core.NewSong("x.flac", "Song %d" % k, "My Band", "My Album") for k in range(51)]
        assert d.text_warning(ok) is None
        d.img.f.close()


def test_minimum_one_song_and_limits():
    import pytest
    for p in _isos():
        d = core.Disc(p)
        items = [core.SongRef(30)]
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert core.RWS_FILES[1] not in reps and len(d.save_shift(items)) == 44
        with pytest.raises(ValueError):
            d.replacements([], normalize=False)
        with pytest.raises(ValueError):
            d.replacements([core.SongRef(0)] * (core.MAX_SONGS + 1), normalize=False)
        assert core.MAX_SONGS == elfpatch.MAX_SONGS == 95
        d.img.f.close()


def test_replace_audio_keeps_slot():
    _ffmpeg()
    for p in _isos():
        d = core.Disc(p)
        items = d.current_list()
        items[4].audio = os.path.join(HERE, "data", "test_chords_22k_mono.wav")      # in _EATRAX0
        items[30].audio = os.path.join(HERE, "data", "test_chords_44k_24bit.flac")   # in _EATRAX1
        items.append(core.NewSong(os.path.join(HERE, "data", "test_chords_22k_mono.wav"), "New", "MusicKit", ""))
        reps, rep = d.replacements(items, normalize=False)
        _check_reps(d, items, reps)
        assert d.save_shift(items) == []
        assert [s["index"] for s in rep["songs"]] == [4, 30, 44] and rep["songs"][0]["replaced"]
        h = rws.RwsHeader(reps[core.RWS_FILES[0]].parts[0])
        pcm = rws.decode_segment(reps[core.RWS_FILES[0]].parts[5], h.segments[4].usable)
        assert abs(len(pcm) / 32000.0 - 20.0) < 0.1
        d.img.f.close()


def test_unlock_with_song_list():
    """unlock_off with an unchanged list changes only the executable; with a changed list it rides along."""
    for p in _isos():
        d = core.Disc(p)
        reps, rep = d.replacements(d.current_list(), normalize=False, unlock_off=True)
        assert list(reps) == [d.elf_path] and rep["unlocked"] and rep["total_songs"] == d.count
        elf = reps[d.elf_path]
        assert elfpatch.is_unlocked(elf) and elfpatch.crc(elf) == d.crc and not elfpatch.is_patched(elf)
        assert elfpatch.read_table(elf) == elfpatch.read_table(d.elf)
        items = d.current_list()
        items.pop(3)
        items.insert(0, items.pop(30))
        items[7].title = "Edited"
        items += [core.NewSong("x.flac", "Added %d" % k, "Band", "") for k in range(3)]
        reps, rep = d.replacements(items, normalize=False, unlock_off=True, estimate={k: 60.0 for k in range(43, 46)})
        elf = reps[d.elf_path]
        assert rep["unlocked"] and elfpatch.is_unlocked(elf) and elfpatch.is_patched(elf)
        again = elfpatch.set_table(elf, [(477, 478, 479, 7)] * 50)          # a later song change keeps the unlock
        assert elfpatch.is_unlocked(again) and elfpatch.crc(again) == d.crc
        reps, rep = d.replacements(items[:40], normalize=False)
        assert not rep["unlocked"] and not elfpatch.is_unlocked(reps[d.elf_path])
        d.img.f.close()


def test_size_plan_and_dvd_capacity():
    """Projected image size from the planned layout (no encoding) and the single-layer DVD check."""
    for p in _isos():
        d = core.Disc(p)
        src = d.img.volume_sectors * iso.SECTOR
        same = d.size_plan(d.current_list())
        assert same["bytes"] == src and same["fits"] and "fits on a single-layer DVD" in same["text"] and "more minutes" in same["text"]
        pcm = (np.sin(np.arange(32000 * 3) / 7.0) * 8000).astype(np.int16)
        payload, usable = rws.encode_segment(np.stack([pcm, pcm], 1), d.headers[0].block_size)
        assert d.encoded_size(3.0) == (len(payload), usable)
        items = d.current_list() + [core.NewSong("fake%d.flac" % k, "Song %d" % k, "Band", "") for k in range(10)]
        small = d.size_plan(items, estimate={k: 200.0 for k in range(d.count, len(items))})
        audio_bytes = 10 * d.encoded_size(200.0)[0]
        rws1 = d.img.entries[core.RWS_FILES[1]].size     # the grown _EATRAX1.RWS is written after the volume end
        assert small["fits"] and audio_bytes + rws1 <= small["bytes"] - src <= audio_bytes + rws1 + (8 << 20)
        big = d.size_plan(items, estimate={k: 9000.0 for k in range(d.count, len(items))})   # 10 x 150 min
        assert not big["fits"] and big["bytes"] > core.DVD5_SECTORS * iso.SECTOR
        assert "larger than a single-layer DVD (4.7 GB)" in big["text"] and "%.2f GB" % (big["bytes"] / 1e9) in big["text"]
        fewer = d.current_list()
        del fewer[35:]                     # shorter stream file: rewritten in place (only the executable may move)
        assert d.size_plan(fewer)["bytes"] - src <= d.img.entries[d.elf_path].size + (1 << 20)
        moved = d.current_list()
        del moved[5:15]                    # songs move from _EATRAX1 into _EATRAX0: that file grows and is appended
        assert d.size_plan(moved)["bytes"] > src
        d.img.f.close()
    assert "about 0 more minutes" in core.capacity_text(core.DVD5_SECTORS * iso.SECTOR)


def test_build_iso_end_to_end():
    """Full build into a temp folder (writes ~3.6 GB) - only with MUSICKIT_FULL_TEST=1."""
    if not os.environ.get("MUSICKIT_FULL_TEST"):
        import pytest
        pytest.skip("set MUSICKIT_FULL_TEST=1")
    _skip(ISO)
    from musickit import validate
    sys.path.insert(0, HERE)
    import make_test_songs
    tmp = tempfile.mkdtemp()
    make_test_songs.main(tmp)
    out = os.path.join(tmp, "test.iso")
    d = core.Disc(ISO)
    song = core.NewSong(os.path.join(tmp, "test_chords_44k_24bit.flac"), "Chord Test", "MusicKit", "Demo")
    d.build([song], out)
    assert validate.validate(ISO, out, log=lambda *a: None)
    o = core.Disc(out)
    assert not o.unlocked and o.patched and o.count == 45 and o.songs[44].title == "Chord Test"
    o.img.f.close()
    os.remove(out)
    # unlock only (no new songs): only the executable changes
    rep = d.build([], out, unlock_off=True)
    assert rep["unlocked"] and rep["total_songs"] == d.count
    assert validate.validate(ISO, out, log=lambda *a: None)
    o = core.Disc(out)
    assert o.unlocked and o.count == d.count and not o.patched
    o.img.f.close()
    os.remove(out)
    d.img.f.close()


def test_manage_end_to_end():
    """Remove an original, replace one, rename one, add two, reopen the output, rename + move + remove again
    (writes 2 images of ~3 GB per ISO, one at a time) - only with MUSICKIT_FULL_TEST=1."""
    if not os.environ.get("MUSICKIT_FULL_TEST"):
        import pytest
        pytest.skip("set MUSICKIT_FULL_TEST=1")
    _ffmpeg()
    from musickit import validate
    flac = os.path.join(HERE, "data", "test_chords_44k_24bit.flac")
    wav = os.path.join(HERE, "data", "test_chords_22k_mono.wav")
    quiet = lambda *a: None   # noqa: E731
    for p in _isos():
        tmp = tempfile.mkdtemp()
        out1, out2 = os.path.join(tmp, "step1.iso"), os.path.join(tmp, "step2.iso")
        d = core.Disc(p)
        names = [s.title for s in d.songs]
        items = d.current_list()
        items.pop(1)                                   # remove original #2
        items[8].audio = flac                          # replace (now #9, was #10)
        items[3].title = "Renamed Once"                # rename (now #4, was #5)
        items.append(core.NewSong(wav, "Added Song", "MusicKit", "Demo"))
        items.append(core.NewSong(flac, "Added Two", "MusicKit", "Demo"))
        plan = d.size_plan(items, unlock_off=True)
        rep = d.build_list(items, out1, unlock_off=True)   # song changes + "allow switching any song OFF"
        assert rep["unlocked"]
        assert abs(os.path.getsize(out1) - plan["bytes"]) <= 64 << 10, (os.path.getsize(out1), plan["bytes"])
        assert validate.validate(p, out1, log=quiet)
        r = core.Disc(out1)                            # read back the modified image
        assert r.count == 45 and r.patched and r.crc == d.crc and r.unlocked
        assert [s.title for s in r.songs[:3]] == [names[0], names[2], names[3]]
        assert r.songs[3].title == "Renamed Once" and r.songs[43].title == "Added Song"
        assert r.songs[44].title == "Added Two"
        assert [s.original for s in r.songs] == [True] * 8 + [False] + [True] * 34 + [False, False]
        assert abs(r.songs[8].duration - 30.0) < 0.1
        assert r.songs[43].sids == d.songs[1].sids     # the removed song's strings were reused
        items2 = r.current_list()                      # edit again + move + remove + add on the modified image
        items2[3].title = "Renamed Twice"
        items2.insert(0, items2.pop(44))
        items2.pop(10)
        items2.append(core.NewSong(flac, "Added Later", "MusicKit", ""))
        r.build_list(items2, out2)
        assert validate.validate(out1, out2, log=quiet)
        r2 = core.Disc(out2)
        assert r2.count == 45 and r2.crc == d.crc and r2.unlocked     # the unlock stays on later edits
        assert [s.title for s in r2.songs[:2]] == ["Added Two", names[0]]
        assert r2.songs[4].title == "Renamed Twice" and r2.songs[44].title == "Added Later"
        assert sum(not s.original for s in r2.songs) == 4
        for l in r2.langs:
            assert len(r2.tables[l]) <= r2.base + 6                   # strings are rebuilt compactly
        r.img.f.close()
        r2.img.f.close()
        d.img.f.close()
        os.remove(out1)
        os.remove(out2)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
