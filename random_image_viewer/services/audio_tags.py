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
            elif ext in ('.m4a', '.m4b', '.aac', '.mp4'):
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


# -- how long a song is (seconds), from the file's own headers --------------

_MPEG_BITRATES = {   # (version is 1, layer) -> kbit/s by index
    (True, 3): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 1): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 3): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MPEG_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_seconds(f, size):
    f.seek(0)
    head = f.read(10)
    start = 0
    if head[:3] == b'ID3':
        start = 10 + _syncsafe(head[6:10]) + (10 if head[5] & 0x10 else 0)
    f.seek(start)
    data = f.read(64 * 1024)
    i = 0
    while i + 4 <= len(data):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            b1, b2, b3 = data[i + 1], data[i + 2], data[i + 3]
            version = (b1 >> 3) & 3            # 3: MPEG1, 2: MPEG2, 0: MPEG2.5
            layer_bits = (b1 >> 1) & 3         # 3: layer I, 2: II, 1: III
            bi, ri = b2 >> 4, (b2 >> 2) & 3
            if version != 1 and layer_bits and 0 < bi < 15 and ri < 3:
                layer = {3: 1, 2: 2, 1: 3}[layer_bits]
                rate = _MPEG_RATES[version][ri]
                kbps = _MPEG_BITRATES[(version == 3, {1: 3, 2: 2, 3: 1}[layer])][bi]
                per_frame = 384 if layer == 1 else (1152 if layer == 2 or version == 3 else 576)
                # A VBR header (Xing / Info / VBRI) says how many frames there are.
                mono = (b3 >> 6) == 3
                side = (17 if mono else 32) if version == 3 else (9 if mono else 17)
                x = i + 4 + side
                if data[x:x + 4] in (b'Xing', b'Info'):
                    flags = struct.unpack('>I', data[x + 4:x + 8])[0]
                    if flags & 1:
                        frames = struct.unpack('>I', data[x + 8:x + 12])[0]
                        return frames * per_frame / rate
                v = i + 36
                if data[v:v + 4] == b'VBRI':
                    frames = struct.unpack('>I', data[v + 14:v + 18])[0]
                    return frames * per_frame / rate
                # Constant bitrate: the audio's size over its rate.
                end = size - (128 if _has_id3v1(f) else 0)
                return max(0, end - start - i) * 8 / (kbps * 1000)
        i += 1
    return None


def _has_id3v1(f):
    try:
        f.seek(-128, os.SEEK_END)
        return f.read(3) == b'TAG'
    except OSError:
        return False


def _flac_seconds(f):
    f.seek(0)
    if f.read(4) != b'fLaC':
        return None
    head = f.read(4)
    if len(head) < 4 or head[0] & 0x7F != 0:
        return None
    info = f.read(34)
    bits = int.from_bytes(info[10:18], 'big')
    rate = bits >> 44
    total = bits & ((1 << 36) - 1)
    return total / rate if rate else None


def _wav_seconds(f):
    f.seek(0)
    head = f.read(12)
    if head[:4] != b'RIFF' or head[8:12] != b'WAVE':
        return None
    byte_rate = None
    while True:
        ch = f.read(8)
        if len(ch) < 8:
            return None
        cid, csize = ch[:4], struct.unpack('<I', ch[4:])[0]
        if cid == b'fmt ':
            fmt = f.read(csize)
            byte_rate = struct.unpack('<I', fmt[8:12])[0]
            if csize % 2:
                f.read(1)
        elif cid == b'data':
            return csize / byte_rate if byte_rate else None
        else:
            f.seek(csize + (csize % 2), os.SEEK_CUR)


def _mp4_seconds(f, size):
    """The movie header (moov/mvhd): its duration over its time scale."""
    def find(start, end, name):
        pos = start
        while pos + 8 <= end:
            f.seek(pos)
            head = f.read(8)
            if len(head) < 8:
                return None
            asize, aname = struct.unpack('>I4s', head)
            hdr = 8
            if asize == 1:
                asize = struct.unpack('>Q', f.read(8))[0]
                hdr = 16
            elif asize == 0:
                asize = end - pos
            if asize < hdr:
                return None
            if aname == name:
                return pos + hdr, pos + asize
            pos += asize
        return None
    moov = find(0, size, b'moov')
    if moov is None:
        return None
    mvhd = find(moov[0], moov[1], b'mvhd')
    if mvhd is None:
        return None
    f.seek(mvhd[0])
    version = f.read(4)[0]
    if version == 1:
        f.read(16)
        scale, duration = struct.unpack('>IQ', f.read(12))
    else:
        f.read(8)
        scale, duration = struct.unpack('>II', f.read(8))
    return duration / scale if scale else None


def _ogg_seconds(f, size):
    """Ogg (Vorbis / Opus): the last page's sample position over the rate."""
    f.seek(0)
    first = f.read(4096)
    if first[:4] != b'OggS':
        return None
    rate = None
    v = first.find(b'\x01vorbis')
    if v >= 0:
        rate = struct.unpack('<I', first[v + 12:v + 16])[0]
    elif first.find(b'OpusHead') >= 0:
        rate = 48000            # Opus counts its samples at 48 kHz
    if not rate:
        return None
    f.seek(max(0, size - 65536))
    tail = f.read()
    at = tail.rfind(b'OggS')
    if at < 0 or at + 14 > len(tail):
        return None
    granule = struct.unpack('<q', tail[at + 6:at + 14])[0]
    return granule / rate if granule > 0 else None


@lru_cache(maxsize=8192)
def _seconds(path, mtime):
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            ext = os.path.splitext(path)[1].lower()
            if ext == '.mp3':
                return _mp3_seconds(f, size)
            if ext == '.flac':
                return _flac_seconds(f)
            if ext == '.wav':
                return _wav_seconds(f)
            if ext in ('.m4a', '.m4b', '.mp4', '.aac'):
                got = _mp4_seconds(f, size)
                return got if got is not None else (_mp3_seconds(f, size) if ext == '.aac' else None)
            if ext in ('.ogg', '.opus'):
                return _ogg_seconds(f, size)
    except (OSError, struct.error, ValueError, IndexError, KeyError, ZeroDivisionError):
        pass
    return None


def read_seconds(path):
    """How long the song at *path* lasts, in seconds, or None when its type
    is not read here (wma) or its headers make no sense. Read once per file
    (the grid asks on every repaint)."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    got = _seconds(path, mtime)
    return got if got and 0 < got < 24 * 3600 else None


def format_seconds(seconds):
    """3:45, or 1:02:03 past the hour."""
    s = int(round(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def read_tags(path, cover=False):
    """{'title', and 'artist' / 'album' / 'track' when the file has them,
    'cover': image bytes when *cover* and it has one}."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = 0
    return _read(path, mtime, bool(cover))
