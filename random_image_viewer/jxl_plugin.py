"""JPEG XL (.jxl) for Pillow, decoded by imagecodecs (libjxl).

Neither Qt nor Pillow reads JPEG XL. Every place the viewer opens a picture
Qt cannot read falls back to Pillow (image_utils._load_pixmap_via_pillow,
read_image_region, thumbnails._decode_with_pillow), so registering a Pillow
format here makes .jxl work in all of them: viewing, thumbnails, crops.

imagecodecs is imported only when a .jxl is opened. Without it, opening one
fails with a message saying what to install.
"""

import numpy as np
from PIL import Image, ImageFile

# A bare codestream, or the ISO box container
_CODESTREAM = b'\xff\x0a'
_CONTAINER = b'\x00\x00\x00\x0cJXL \x0d\x0a\x87\x0a'

_registered = False


def _accept(prefix):
    return prefix[:2] == _CODESTREAM or prefix[:12] == _CONTAINER


def _to_8bit(a):
    """Decoded pixels as uint8: 16-bit and float (HDR) pictures scaled down."""
    if a.dtype == np.uint8:
        return a
    if a.dtype == np.uint16:
        return (a >> 8).astype(np.uint8)
    if np.issubdtype(a.dtype, np.floating):
        return (np.clip(a, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return np.clip(a, 0, 255).astype(np.uint8)


def decode(data):
    """A .jxl's bytes as a PIL Image (the first frame of an animation)."""
    try:
        import imagecodecs
    except ImportError as e:
        raise OSError("JPEG XL needs the imagecodecs package (pip install imagecodecs)") from e
    try:
        a = imagecodecs.jpegxl_decode(data, index=0)
    except TypeError:                       # (an imagecodecs without index=)
        a = imagecodecs.jpegxl_decode(data)
    a = np.asarray(a)
    if a.ndim == 4:                         # frames, h, w, channels: the first
        a = a[0]
    a = _to_8bit(a)
    if a.ndim == 3 and a.shape[2] == 1:
        a = a[..., 0]
    if a.ndim == 2:
        mode = 'L'
    elif a.ndim == 3:
        mode = {2: 'LA', 3: 'RGB', 4: 'RGBA'}.get(a.shape[2])
    else:
        mode = None
    if mode is None:
        raise OSError(f"JPEG XL: pixels of shape {a.shape} not understood")
    return Image.fromarray(np.ascontiguousarray(a), mode)


class JxlImageFile(ImageFile.ImageFile):
    format = 'JXL'
    format_description = 'JPEG XL'

    def _open(self):
        if not _accept(self.fp.read(12)):
            raise SyntaxError("not a JPEG XL file")
        self.fp.seek(0)
        image = decode(self.fp.read())
        image.load()
        self.im = image.im                  # decoded whole: no tiles to read later
        self._mode = image.mode
        self._size = image.size
        self.tile = []


def register():
    """Teach Pillow .jxl (once)."""
    global _registered
    if _registered:
        return
    _registered = True
    Image.register_open(JxlImageFile.format, JxlImageFile, _accept)
    Image.register_extension(JxlImageFile.format, '.jxl')
    Image.register_mime(JxlImageFile.format, 'image/jxl')
