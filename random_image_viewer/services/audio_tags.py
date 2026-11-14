"""Titles, artists, albums and cover art of music files - read straight from
the file, no extra library.

ID3v2 (mp3, also found in some wav / aac files), the ID3v1 trailer, FLAC
Vorbis comments and pictures, and MP4 (m4a) atoms. Anything that cannot
be read falls back to the file name, so a song always has a title.
Read once per file and kept (the grid asks on every repaint).
"""

import os
import struct
from functools import lru_cache

# Cover art bigger than this is not loaded for the grid (the card loads it).
_TAG_READ_MAX = 16 << 20


def _title_from_name(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return stem.replace('_', ' ').strip() or stem


def _text(data, encoding_byte):
    enc = {0: 'latin-1', 1: 'utf-16', 2: 'utf-16-be', 3: 'utf-8'}.get(encoding_byte, 'latin-1')
    try:
        return data.decode(enc, errors='replace').replace('\x00', '').strip()
    except LookupError:
        return ''


def _syncsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def _id3v2(f, want_cover):
    head = f.read(10)
    if len(head) < 10 or head[:3] != b'ID3':
        return None
    major = head[3]
    size = _syncsafe(head[6:10])
    if size <= 0 or size > _TAG_READ_MAX:
        return None
    body = f.read(size)
    tags, pos = {}, 0
    if head[5] & 0x40 and major >= 3 and len(body) >= 4:         # extended header
        ext = (_syncsafe(body[:4]) if major == 4 else struct.unpack('>I', body[:4])[0] + 4)
        pos = ext
    frame_id_len = 3 if major == 2 else 4
    header_len = 6 if major == 2 else 10
    while pos + header_len <= len(body):
        fid = body[pos:pos + frame_id_len]
        if not fid.strip(b'\x00'):
            break
        if major == 2:
            fsize = int.from_bytes(body[pos + 3:pos + 6], 'big')
        elif major == 4:
            fsize = _syncsafe(body[pos + 4:pos + 8])
        else:
            fsize = struct.unpack('>I', body[pos + 4:pos + 8])[0]
        data = body[pos + header_len:pos + header_len + fsize]
        pos += header_len + fsize
        if fsize <= 0 or not data:
            continue
        fid = fid.decode('latin-1', errors='replace')
        key = {'TIT2': 'title', 'TT2': 'title', 'TPE1': 'artist', 'TP1': 'artist',
               'TALB': 'album', 'TAL': 'album', 'TRCK': 'track', 'TRK': 'track'}.get(fid)
        if key and key not in tags:
            tags[key] = _text(data[1:], data[0])
        elif want_cover and fid in ('APIC', 'PIC') and 'cover' not in tags:
            enc = data[0]
            if fid == 'PIC':
                rest = data[5:]                                   # 3-letter format + type
            else:
                mime_end = data.find(b'\x00', 1)
                rest = data[mime_end + 2:]                        # mime, \0, picture type
            # the description, ended by \0 (\0\0 in UTF-16)
            term = b'\x00\x00' if enc in (1, 2) else b'\x00'
            i = rest.find(term)
            while enc in (1, 2) and i >= 0 and i % 2:
                i = rest.find(term, i + 1)
            if i >= 0:
                tags['cover'] = rest[i + len(term):]
    return tags


def _id3v1(f):
    try:
        f.seek(-128, os.SEEK_END)
    except OSError:
        return {}
    block = f.read(128)
    if len(block) != 128 or block[:3] != b'TAG':
        return {}
    out = {}
    for key, a, b in (('title', 3, 33), ('artist', 33, 63), ('album', 63, 93)):
        value = block[a:b].split(b'\x00')[0].decode('latin-1', errors='replace').strip()
        if value:
            out[key] = value
    return out


def _flac(f, want_cover):
    if f.read(4) != b'fLaC':
        return None
    tags = {}
    while True:
        head = f.read(4)
        if len(head) < 4:
            break
        last, kind = head[0] & 0x80, head[0] & 0x7F
        size = int.from_bytes(head[1:4], 'big')
        if kind == 4:                                             # Vorbis comments
            block = f.read(size)
            n = struct.unpack('<I', block[:4])[0]
            pos = 4 + n
            count = struct.unpack('<I', block[pos:pos + 4])[0]
            pos += 4
            for _ in range(count):
                length = struct.unpack('<I', block[pos:pos + 4])[0]
                entry = block[pos + 4:pos + 4 + length].decode('utf-8', errors='replace')
                pos += 4 + length
                name, _, value = entry.partition('=')
                key = {'TITLE': 'title', 'ARTIST': 'artist', 'ALBUM': 'album',
                       'TRACKNUMBER': 'track'}.get(name.upper())
                if key and key not in tags:
                    tags[key] = value.strip()
        elif kind == 6 and want_cover and 'cover' not in tags and size <= _TAG_READ_MAX:
            block = f.read(size)
            pos = 4
            mlen = struct.unpack('>I', block[pos:pos + 4])[0]; pos += 4 + mlen
            dlen = struct.unpack('>I', block[pos:pos + 4])[0]; pos += 4 + dlen
            pos += 16
            plen = struct.unpack('>I', block[pos:pos + 4])[0]; pos += 4
            tags['cover'] = block[pos:pos + plen]
        else:
            f.seek(size, os.SEEK_CUR)
        if last:
            break
    return tags


def _mp4(f, want_cover):
    """m4a: the ilst atom under moov/udta/meta."""
    def atoms(start, end):
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            head = f.read(8)
            if len(head) < 8:
                return
            size, name = struct.unpack('>I4s', head)
            hdr = 8
            if size == 1:
                size = struct.unpack('>Q', f.read(8))[0]
                hdr = 16
            elif size == 0:
                size = end - pos
            if size < hdr:
                return
            yield name, pos + hdr, pos + size
            pos += size

    f.seek(0, os.SEEK_END)
    total = f.tell()
    f.seek(4)
    if f.read(4) != b'ftyp':
        return None
    tags = {}

    def walk(start, end, path):
        for name, a, b in atoms(start, end):
            if path == () and name == b'moov' or path == (b'moov',) and name == b'udta' \
                    or path == (b'moov', b'udta') and name == b'meta':
                walk(a + (4 if name == b'meta' else 0), b, path + (name,))
            elif path == (b'moov', b'udta', b'meta') and name == b'ilst':
                walk(a, b, path + (name,))
            elif len(path) == 4:
                key = {b'\xa9nam': 'title', b'\xa9ART': 'artist', b'\xa9alb': 'album',
                       b'covr': 'cover'}.get(name)
                if key is None or key in tags or (key == 'cover' and not want_cover):
                    continue
                for dname, da, db in atoms(a, b):
                    if dname == b'data' and db - da > 8 and db - da <= _TAG_READ_MAX:
                        f.seek(da + 8)
                        raw = f.read(db - da - 8)
                        tags[key] = raw if key == 'cover' else raw.decode('utf-8', errors='replace')
                        break

    walk(0, total, ())
    return tags


@lru_cache(maxsize=4096)
def _read(path, mtime, want_cover):
    tags = {}
    try:
        with open(path, 'rb') as f:
            ext = os.path.splitext(path)[1].lower()
            got = None
            if ext == '.flac':
                got = _flac(f, want_cover)
            elif ext in ('.m4a', '.aac', '.mp4'):
                got = _mp4(f, want_cover)
            if got is None:
                f.seek(0)
                got = _id3v2(f, want_cover)
            tags = dict(got or {})
            if not tags.get('title') and ext in ('.mp3', '.aac'):
                for key, value in _id3v1(f).items():
                    tags.setdefault(key, value)
    except (OSError, struct.error, ValueError, IndexError):
        pass
    tags = {k: v for k, v in tags.items() if v}
    tags.setdefault('title', _title_from_name(path))
    return tags


def read_tags(path, cover=False):
    """{'title', and 'artist' / 'album' / 'track' when the file has them,
    'cover': image bytes when *cover* and it has one}."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = 0
    return _read(path, mtime, bool(cover))
