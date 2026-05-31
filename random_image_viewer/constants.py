import os

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mkv', '.mov', '.webm', '.flv', '.wmv'}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Document-style files supported as single playlist items (each opens in its
# own dedicated viewer mode: PDF, EPUB, or CBR/CBZ comic archive).
DOCUMENT_EXTENSIONS = {'.pdf', '.epub', '.cbr', '.cbz'}

# Anything that can appear in a mixed folder/multi-drop playlist.
PLAYLIST_EXTENSIONS = MEDIA_EXTENSIONS | DOCUMENT_EXTENSIONS

# GPU state - lazy initialized
cl = None
GPU_AVAILABLE = None  # None = not yet checked


def _check_gpu_available():
    """Lazy check for GPU availability - called only when GPU is first needed"""
    try:
        import pyopencl as _cl
        return _cl, True
    except ImportError:
        return None, False
