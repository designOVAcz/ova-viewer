"""In-app log capture.

The packaged exe runs without a console, so every print(), Python traceback
and Qt/FFmpeg warning would otherwise vanish. install() tees all of them into
an in-memory ring buffer (shown by the status-bar log window) and a log file
under %LOCALAPPDATA%\\OvaViewer\\logs, while still passing them through to the
real console when there is one (running from source).
"""
import collections
import datetime
import os
import sys
import threading
import traceback

MAX_LINES = 5000

LEVEL_INFO = 0
LEVEL_WARNING = 1
LEVEL_ERROR = 2

_lock = threading.RLock()
_lines = collections.deque(maxlen=MAX_LINES)  # (text, level)
_listeners = []
_log_file = None
_log_path = None
_installed = False

# Words that mark a plain print() as a problem worth flagging in the UI.
_ERROR_HINTS = ("error", "failed", "exception", "traceback", "❌", "✗")
_WARNING_HINTS = ("warning", "warn:", "⚠", "could not", "cannot", "unable")


def log_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "OvaViewer", "logs")


def log_path():
    return _log_path


def _guess_level(text):
    low = text.lower()
    if any(h in low for h in _ERROR_HINTS):
        return LEVEL_ERROR
    if any(h in low for h in _WARNING_HINTS):
        return LEVEL_WARNING
    return LEVEL_INFO


def add_listener(fn):
    """fn(text, level) is called for every new line, from any thread."""
    with _lock:
        _listeners.append(fn)


def remove_listener(fn):
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def snapshot():
    with _lock:
        return list(_lines)


def clear():
    with _lock:
        _lines.clear()


def log(text, level=None):
    """Record one message (may span several lines)."""
    text = text.rstrip("\n")
    if not text.strip():
        return
    if level is None:
        level = _guess_level(text)
    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with _lock:
        listeners = list(_listeners)
        for i, part in enumerate(text.split("\n")):
            line = f"{stamp}  {part}" if i == 0 else f"              {part}"
            _lines.append((line, level))
            if _log_file is not None:
                try:
                    _log_file.write(line + "\n")
                except Exception:
                    pass
        if _log_file is not None:
            try:
                _log_file.flush()
            except Exception:
                pass
    full = "\n".join(
        (f"{stamp}  {p}" if i == 0 else f"              {p}")
        for i, p in enumerate(text.split("\n")))
    for fn in listeners:
        try:
            fn(full, level)
        except Exception:
            pass


class _TeeStream:
    """File-like stand-in for stdout/stderr that also feeds the log."""

    def __init__(self, original, level):
        self._original = original
        self._level = level
        self._buf = ""
        self.encoding = getattr(original, "encoding", None) or "utf-8"

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        if self._original is not None:
            try:
                self._original.write(s)
            except UnicodeEncodeError:
                try:
                    self._original.write(
                        s.encode(self.encoding, "replace").decode(self.encoding))
                except Exception:
                    pass
            except Exception:
                pass
        with _lock:
            self._buf += s
            if "\n" not in self._buf:
                return len(s)
            complete, self._buf = self._buf.rsplit("\n", 1)
        log(complete, self._level)
        return len(s)

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def fileno(self):
        if self._original is not None:
            return self._original.fileno()
        raise OSError("no underlying stream")


def _qt_message_handler(mode, context, message):
    from PySide6.QtCore import QtMsgType
    if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
        level, tag = LEVEL_ERROR, "Qt critical"
    elif mode == QtMsgType.QtWarningMsg:
        level, tag = LEVEL_WARNING, "Qt warning"
    else:
        level, tag = LEVEL_INFO, "Qt"
    category = getattr(context, "category", None)
    if category and category != "default":
        tag = f"{tag} [{category}]"
    log(f"{tag}: {message}", level)
    if _original_stderr is not None:
        try:
            _original_stderr.write(f"{tag}: {message}\n")
        except Exception:
            pass


def _excepthook(exc_type, exc, tb):
    log("Uncaught exception:\n" + "".join(
        traceback.format_exception(exc_type, exc, tb)).rstrip(), LEVEL_ERROR)


def _thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    log(f"Uncaught exception in thread {name}:\n" + "".join(
        traceback.format_exception(args.exc_type, args.exc_value,
                                   args.exc_traceback)).rstrip(), LEVEL_ERROR)


_original_stderr = None


def install():
    """Start capturing. Safe to call more than once."""
    global _installed, _log_file, _log_path, _original_stderr
    if _installed:
        return
    _installed = True
    _original_stderr = sys.stderr

    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        _log_path = os.path.join(d, "ova_viewer.log")
        # Keep the previous run's log next to the new one: it is usually the
        # interesting one after a crash.
        if os.path.exists(_log_path):
            try:
                os.replace(_log_path, _log_path + ".1")
            except OSError:
                pass
        _log_file = open(_log_path, "w", encoding="utf-8", errors="replace")
    except Exception:
        _log_file = None
        _log_path = None

    sys.stdout = _TeeStream(sys.stdout, None)
    sys.stderr = _TeeStream(sys.stderr, None)
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    try:
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(_qt_message_handler)
    except Exception:
        pass

    frozen = "exe" if getattr(sys, "frozen", False) else "source"
    log(f"Ova Viewer starting ({frozen}), Python {sys.version.split()[0]}, "
        f"{sys.platform}", LEVEL_INFO)
    try:
        import PySide6
        log(f"PySide6 {PySide6.__version__}", LEVEL_INFO)
    except Exception:
        pass
