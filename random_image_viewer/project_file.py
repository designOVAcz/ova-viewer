"""💼 Ova projects (.ova): a painting saved to carry on with later.

An .ova file is a ZIP container (as Krita's .kra and OpenRaster are):

    manifest.json   what it is: the mode (◧ Studio, a sketch, a canvas under
                    the picture), the view (zoom, pan, turn, flips), every
                    layer with its settings and every mark on it, in the
                    order they were made, the 💉 palette
    picture/<name>  the picture (or the sketch's canvas) itself, as it was
                    on disk - marks sit in its pixels, so it must load just
                    the same; the project needs nothing outside it
    images/<n>.png  the pixels of image marks (📸 snapshots, Blend / Smear /
                    Push strokes, imported pictures)
    tips/<n>.png    the brush tips the strokes were made with (a tip file
                    moved or gone, the strokes still come out the same)
    thumbnail.png   the view, small - for a glance at the file
    layers/<n>.png  each layer's image as it was (its premultiplied pixels
                    exactly, as a 4-channel PNG) and the patches undo puts back - opened, the
                    layer is that image at once, not laid again stroke by
                    stroke (150 textured strokes: 17 s); undo is as quick as
                    before saving. Used only when it still matches the marks.

Marks are kept as the data they are (a stroke's stamps, a fill's outline),
not as pixels: opened again, they are laid exactly as before, at any zoom,
and undo walks back through them as it did.
"""

import hashlib
import io
import json
import os
import time
import zipfile

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PySide6.QtGui import QColor, QImage

FORMAT = "ova-project"
VERSION = 2         # 2: stamps as rows, layer images kept (1 still opens)
SUFFIX = ".ova"

_LAYER_FIELDS = ('name', 'visible', 'opacity', 'blend', 'effects', 'impasto')


def is_project(path):
    return bool(path) and str(path).lower().endswith(SUFFIX)


# -- marks to JSON and back ---------------------------------------------------

_PLAIN = (int, float, str, bool)
_PATCHES_SAVED = 30     # undo patches kept in the file per layer


def _plain(x):
    """A value a row can hold: a plain one, or a tuple of plain ones."""
    if x is None or type(x) in _PLAIN:
        return True
    return type(x) is tuple and all(y is None or type(y) in _PLAIN for y in x)


def _raw_image(zf, name, image):
    """*image* (premultiplied ARGB) stored exactly: its very bytes as a
    4-channel PNG (OpenCV, in C++) - PNG's row filters pack paint far
    better than deflate alone, and nothing is converted (a premultiplied
    image through an ordinary PNG loses the faint edges of soft strokes)."""
    import cv2
    import numpy as np
    image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    w, h = image.width(), image.height()
    arr = np.frombuffer(image.constBits(), np.uint8).reshape(h, image.bytesPerLine())[:, :w * 4]
    ok, data = cv2.imencode('.png', np.ascontiguousarray(arr.reshape(h, w, 4)),
                            [cv2.IMWRITE_PNG_COMPRESSION, 2])
    if not ok:
        raise ValueError("layer image not encoded")
    zf.writestr(name, data.tobytes(), compress_type=zipfile.ZIP_STORED)
    return {'entry': name, 'w': w, 'h': h, 'png': True}


def _read_raw_image(zf, info):
    import cv2
    import numpy as np
    data = zf.read(info['entry'])
    if info.get('png'):
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        arr = np.ascontiguousarray(arr)
        h, w = arr.shape[:2]
        return QImage(arr.data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()
    image = QImage(data, info['w'], info['h'], info['bpl'], QImage.Format_ARGB32_Premultiplied)
    return image.copy()         # (its own pixels: *data* goes)


class _Writer:
    """Turns marks into JSON-able data; images and tips go into the archive."""

    def __init__(self, zf):
        self.zf = zf
        self.images = 0
        self.tips = {}      # tip path -> entry name

    def png(self, image):
        self.images += 1
        name = f"images/{self.images:05d}.png"
        self.zf.writestr(name, _png_bytes(image))
        return name

    def tip(self, path):
        """The brush tip file stored once in the archive; None for the
        built-in tips (':soft' ...) and for files no longer there."""
        if not path or path.startswith(':'):
            return None
        if path in self.tips:
            return self.tips[path]
        entry = None
        if os.path.isfile(path):
            with open(path, 'rb') as f:
                data = f.read()
            entry = "tips/" + hashlib.sha1(data).hexdigest()[:16] + os.path.splitext(path)[1].lower()
            if entry not in self.zf.namelist():
                self.zf.writestr(entry, data)
        self.tips[path] = entry
        return entry

    def enc(self, o):
        from random_image_viewer.viewer.main_window import Stroke, RulerLine
        if isinstance(o, Stroke):
            return {'$': 'stroke', 'points': self.enc(list(o)), 'thickness': o.thickness,
                    'color': self.enc(o.color), 'texture': o.texture, 'tip': self.tip(o.texture),
                    'stamps': self.enc(o.stamps), 'flat': bool(getattr(o, 'flat', False)),
                    'history': getattr(o, 'history', None), 'erase': bool(getattr(o, 'erase', False)),
                    'pic_width': getattr(o, 'pic_width', None),
                    'clip': self.enc(getattr(o, 'clip', None))}
        if isinstance(o, RulerLine):
            return {'$': 'ruler', 'v': float(o), 'thickness': o.thickness, 'color': self.enc(o.color)}
        if isinstance(o, QColor):
            return {'$': 'color', 'v': o.name(QColor.HexArgb)}
        if isinstance(o, QImage):
            return {'$': 'image', 'entry': self.png(o)}
        if isinstance(o, QPointF):
            return {'$': 'point', 'v': [o.x(), o.y()]}
        if isinstance(o, tuple):
            return {'$': 'tuple', 'v': [self.enc(x) for x in o]}
        if isinstance(o, list):
            if o and all(type(t) is tuple and all(_plain(x) for x in t) for t in o):
                # a stroke's points / stamps: rows of plain numbers (a stamp's
                # speck colours a tuple in it) - kept as rows: one tagged
                # object each read back ~1 M of them, seconds
                return {'$': 'rows', 'v': [[list(x) if type(x) is tuple else x for x in t] for t in o]}
            return [self.enc(x) for x in o]
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if k == 'render':
                    continue        # a cache of how it last looked: made again
                out[str(k)] = self.enc(v)
            return out
        if o is None or isinstance(o, (bool, int, float, str)):
            return o
        raise TypeError(f"cannot save a {type(o).__name__} in a project")


class _Reader:
    def __init__(self, zf, tip_dir):
        self.zf = zf
        self.tip_dir = tip_dir
        self.tip_paths = {}

    def tip(self, path, entry):
        """The tip as saved: its own file when it is still there and the
        same, else the copy from the archive."""
        if not entry:
            return path
        if entry in self.tip_paths:
            return self.tip_paths[entry]
        data = self.zf.read(entry)
        use = path
        if not (path and os.path.isfile(path) and _same_file(path, data)):
            os.makedirs(self.tip_dir, exist_ok=True)
            use = os.path.join(self.tip_dir, os.path.basename(entry))
            if not os.path.isfile(use):
                with open(use, 'wb') as f:
                    f.write(data)
        self.tip_paths[entry] = use
        return use

    def dec(self, o):
        from random_image_viewer.viewer.main_window import Stroke, RulerLine
        if isinstance(o, list):
            return [self.dec(x) for x in o]
        if isinstance(o, dict):
            kind = o.get('$')
            if kind is None:
                return {k: self.dec(v) for k, v in o.items()}
            if kind == 'rows':
                # (a list inside a row was a tuple: rows hold no lists)
                return [tuple(tuple(x) if type(x) is list else x for x in r) for r in o['v']]
            if kind == 'tuple':
                return tuple(self.dec(x) for x in o['v'])
            if kind == 'color':
                return QColor(o['v'])
            if kind == 'point':
                return QPointF(*o['v'])
            if kind == 'image':
                image = QImage()
                image.loadFromData(self.zf.read(o['entry']), 'PNG')
                return image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
            if kind == 'ruler':
                return RulerLine(o['v'], thickness=o.get('thickness'), color=self.dec(o.get('color')))
            if kind == 'stroke':
                stroke = Stroke(self.dec(o['points']), thickness=o.get('thickness'),
                                color=self.dec(o.get('color')),
                                texture=self.tip(o.get('texture'), o.get('tip')),
                                stamps=self.dec(o.get('stamps')), flat=bool(o.get('flat')))
                stroke.history = o.get('history')
                stroke.erase = bool(o.get('erase'))
                stroke.pic_width = o.get('pic_width')
                stroke.clip = self.dec(o.get('clip'))
                return stroke
            raise ValueError(f"unknown mark data: {kind}")
        return o


def _png_bytes(image):
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    image.save(buf, 'PNG')
    buf.close()
    return bytes(data)


def _same_file(path, data):
    try:
        if os.path.getsize(path) != len(data):
            return False
        with open(path, 'rb') as f:
            return f.read() == data
    except OSError:
        return False


# -- the viewer's state ---------------------------------------------------------

def _no_progress(_fraction, _what=None):
    pass


def save_project(viewer, path, progress=None):
    """Write *viewer*'s painting to *path* (.ova). Written to a temporary
    file first and swapped in, so a failed save never spoils the last one.
    *progress*(fraction, step): told how far it got."""
    progress = progress or _no_progress
    picture = viewer.current_image
    if not picture or not os.path.isfile(picture):
        raise ValueError("no picture open to save a project of")
    label = viewer.image_label
    tmp = path + ".saving"
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        w = _Writer(zf)
        pic_entry = "picture/" + os.path.basename(picture)
        progress(0.02, "the picture")
        zf.write(picture, pic_entry, compress_type=zipfile.ZIP_STORED)
        layers = []
        total = max(1, sum(len(items) for layer in viewer.layers for items in layer.marks.values()))
        done = 0
        for n, layer in enumerate(viewer.layers):
            entry = {k: getattr(layer, k, None) for k in _LAYER_FIELDS}
            entry['tag'] = layer.tag.name() if getattr(layer, 'tag', None) is not None else None
            entry['fill'] = layer.fill.name(QColor.HexArgb) if layer.fill is not None else None
            entry['marks'] = {}
            for field, items in layer.marks.items():
                entry['marks'][field] = w.enc(list(items))
                done += len(items)
                progress(0.15 + 0.6 * done / total,
                         f"layer {n + 1} of {len(viewer.layers)} - {layer.name}")
            try:
                entry['raster'] = _save_raster(zf, viewer, layer, n)
            except Exception as e:
                print(f"\U0001F4BC Layer image not kept (laid again on opening): {e}")
            layers.append(entry)
        under = viewer.under_canvas
        manifest = {
            'format': FORMAT, 'version': VERSION,
            'saved': time.strftime("%Y-%m-%dT%H:%M:%S"),
            'app': _app_version(),
            'picture': {'entry': pic_entry, 'name': os.path.basename(picture), 'original_path': picture,
                        'sketch': os.path.basename(picture).startswith("sketch_")},
            'mode': {'studio': bool(viewer.studio_mode), 'image_visible': bool(viewer.image_visible),
                     'under_canvas': under.name(QColor.HexArgb) if under is not None else None,
                     'under_canvas_visible': bool(getattr(viewer, 'under_canvas_visible', True)),
                     'lines_visible': bool(getattr(viewer, 'lines_visible', True)),
                     'guides_visible': bool(getattr(viewer, 'guides_visible', True))},
            'view': {'zoom': float(label.zoom_factor), 'pan': [float(label.pan_offset_x), float(label.pan_offset_y)],
                     'rotation': int(viewer.rotation_angle), 'flip_h': bool(viewer.flipped_h),
                     'flip_v': bool(viewer.flipped_v)},
            'layers': layers,
            'active_layer': viewer.layers.index(viewer.active_layer),
            'layer_count': int(getattr(viewer, '_layer_count', len(layers))),
            'palette': [list(row) for row in getattr(viewer, 'snapped_rows', [])],
        }
        progress(0.78, "packing the marks")
        zf.writestr("manifest.json", json.dumps(manifest, separators=(',', ':')))
        progress(0.92, "the thumbnail")
        try:
            shown = viewer._view_pixmap() if hasattr(viewer, '_view_pixmap') else label.pixmap()
            if shown is not None and not shown.isNull():
                thumb = shown.toImage().scaled(512, 512, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                zf.writestr("thumbnail.png", _png_bytes(thumb))
        except Exception:
            pass
    os.replace(tmp, path)
    progress(1.0, "saved")


def _save_raster(zf, viewer, layer, n):
    """*layer*'s image and its undo patches into the archive, when the
    image holds exactly its marks now; else None (laid again on opening)."""
    raster = getattr(layer, 'raster', None) or {}
    if not raster.get('canvas') or raster.get('image') is None:
        return None
    paint = viewer._layer_paint_marks(layer)
    if raster.get('count') != len(paint) or (paint and raster.get('last') is not paint[-1][1]):
        return None
    rect = raster['rect']
    out = _raw_image(zf, f"layers/{n:03d}.png", raster['image'])
    out.update({'rect': [float(rect.x()), float(rect.y()), float(rect.width()), float(rect.height())],
                'k': float(raster['k']),
                'count': len(paint), 'patches': []})
    # the last few undos quick after opening (each is a piece of the image)
    for i, p in enumerate((getattr(layer, 'canvas_patches', None) or [])[-_PATCHES_SAVED:]):
        if p.get('key') != raster.get('key'):
            continue
        box = p['box']
        info = _raw_image(zf, f"layers/{n:03d}_undo{i:03d}.png", p['image'])
        info.update({'box': [box.x(), box.y(), box.width(), box.height()], 'count': p['count']})
        out['patches'].append(info)
    return out


def _load_raster(zf, entry):
    """The saved layer image (see _save_raster) read back, or None."""
    from PySide6.QtCore import QRect, QRectF
    info = entry.get('raster')
    if not info:
        return None
    try:
        return {'image': _read_raw_image(zf, info), 'rect': QRectF(*info['rect']), 'k': info['k'],
                'count': info['count'],
                'patches': [{'image': _read_raw_image(zf, p), 'box': QRect(*p['box']), 'count': p['count']}
                            for p in info.get('patches', [])]}
    except Exception as e:
        print(f"\U0001F4BC Saved layer image unreadable (laid again): {e}")
        return None


def load_project(viewer, path, progress=None):
    """Open the .ova at *path* in *viewer*: the picture, the mode, the layers
    and every mark, the view. Returns the picture path it opened.
    *progress*(fraction, step): told how far it got (reading is the first
    third; the layers painted again from their marks, the rest)."""
    from random_image_viewer.drawing.layers import Layer, MARK_FIELDS
    progress = progress or _no_progress
    with zipfile.ZipFile(path) as zf:
        progress(0.02, "reading the project")
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get('format') != FORMAT:
            raise ValueError("not an Ova project")
        if int(manifest.get('version', 0)) > VERSION:
            raise ValueError("made by a newer Ova Viewer - update it to open this project")
        progress(0.06, "the picture")
        data = zf.read(manifest['picture']['entry'])
        # The picture: its own file when it is still there and unchanged, else
        # the copy from the project (in the app's data folder, kept by hash).
        picture = manifest['picture'].get('original_path')
        if not (picture and os.path.isfile(picture) and _same_file(picture, data)):
            folder = os.path.join(_projects_dir(), hashlib.sha1(data).hexdigest()[:16])
            os.makedirs(folder, exist_ok=True)
            picture = os.path.join(folder, manifest['picture']['name'])
            if not (os.path.isfile(picture) and _same_file(picture, data)):
                with open(picture, 'wb') as f:
                    f.write(data)
        reader = _Reader(zf, os.path.join(_projects_dir(), "tips"))
        layers = []
        count = len(manifest['layers'])
        for n, entry in enumerate(manifest['layers']):
            progress(0.12 + 0.2 * n / max(1, count), f"reading layer {n + 1} of {count}")
            layer = Layer(entry.get('name') or "Layer")
            for k in ('visible', 'opacity', 'blend', 'effects', 'impasto'):
                if k in entry:
                    setattr(layer, k, entry[k])
            if entry.get('tag'):
                layer.tag = QColor(entry['tag'])
            layer.fill = QColor(entry['fill']) if entry.get('fill') else None
            for field in MARK_FIELDS:
                layer.marks[field] = reader.dec(entry.get('marks', {}).get(field, []))
            layer.saved_raster = _load_raster(zf, entry)      # taken by apply_project
            layers.append(layer)
    progress(0.33, "painting the layers")
    viewer.apply_project(picture, manifest, layers, progress=progress)
    progress(1.0, "opened")
    return picture


def _projects_dir():
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser("~")
    return os.path.join(base, "OvaViewer", "projects")


def _app_version():
    try:
        from random_image_viewer.version import get_version
        return get_version()
    except Exception:
        return None
