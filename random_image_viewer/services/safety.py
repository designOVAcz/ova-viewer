"""Not losing a painting: where recovery saves go, which sessions ended
without closing, and a record of what the app was doing when it hung or
crashed.

* recovery_dir(): %LOCALAPPDATA%/OvaViewer/recovery - the autosaved
  paintings (main_window._autosave_tick), one .ova per picture.
* Session locks: a file per running app (its pid), removed when it closes
  properly. One left behind by a process that is gone: that session ended
  without closing (a crash, a hang ended from outside) - its recovery saves
  are offered at the next start.
* HangWatch: the main thread ticks a heartbeat; a background thread that
  sees no tick for HANG_S writes every thread's stack to logs/hang_*.txt.
  A hang left no trace before - the log just stopped.
* crash_log(): Python's faulthandler on, into logs/crash_*.txt - a native
  crash (Qt, the GPU driver, numpy) writes the stacks there.
"""

import faulthandler
import glob
import os
import sys
import threading
import time

HANG_S = 10.0           # no heartbeat this long: the stacks are written
HANG_DUMPS = 3          # ... at most this many times in one hang (10, 30, 60 s)


def _base():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "OvaViewer")


def recovery_dir():
    d = os.path.join(_base(), "recovery")
    os.makedirs(d, exist_ok=True)
    return d


def logs_dir():
    d = os.path.join(_base(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def recovery_path(picture):
    """Where *picture*'s painting is autosaved: one file per picture."""
    import hashlib
    stem = os.path.splitext(os.path.basename(picture))[0][:60] or "painting"
    tag = hashlib.sha1(os.path.normcase(os.path.abspath(picture)).encode('utf-8')).hexdigest()[:8]
    return os.path.join(recovery_dir(), f"{stem}_{tag}.ova")


def prune_recovery(keep=30):
    """Only the newest *keep* recovery saves are kept."""
    files = sorted(glob.glob(os.path.join(recovery_dir(), "*.ova")), key=os.path.getmtime, reverse=True)
    for path in files[keep:]:
        try:
            os.remove(path)
        except OSError:
            pass


# -- sessions -----------------------------------------------------------------

def _alive(pid):
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def _lock_path(pid):
    return os.path.join(recovery_dir(), f"session_{pid}.lock")


def start_session():
    """This app is running (its lock). Returns the sessions that ended
    without closing: [(started, ended)] as times - the last change to
    their recovery saves is within them - and forgets them."""
    ended = []
    for lock in glob.glob(os.path.join(recovery_dir(), "session_*.lock")):
        try:
            pid = int(os.path.basename(lock)[8:-5])
        except ValueError:
            continue
        if pid == os.getpid() or _alive(pid):
            continue
        try:
            started = os.path.getmtime(lock)
            ended.append((started, time.time()))
            os.remove(lock)
        except OSError:
            pass
    try:
        with open(_lock_path(os.getpid()), "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except OSError:
        pass
    return ended


def end_session():
    """Closed properly: no recovery to offer for this one."""
    try:
        os.remove(_lock_path(os.getpid()))
    except OSError:
        pass


def recoverable(ended):
    """The recovery saves made during the sessions *ended* (start_session),
    newest first."""
    if not ended:
        return []
    start = min(s for s, _e in ended)
    files = [p for p in glob.glob(os.path.join(recovery_dir(), "*.ova"))
             if os.path.getmtime(p) >= start - 1]
    return sorted(files, key=os.path.getmtime, reverse=True)


# -- hangs and crashes ----------------------------------------------------------

_crash_file = None


def crash_log():
    """faulthandler on, writing to its own file (kept open: it must still
    work while the process is going down)."""
    global _crash_file
    if _crash_file is not None:
        return
    try:
        path = os.path.join(logs_dir(), f"crash_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.txt")
        _crash_file = open(path, "w", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
        # (an empty one is removed when the app closes, see HangWatch.stop)
        _crash_file._ova_path = path
    except Exception as e:
        print(f"Crash log off: {e}")


class HangWatch:
    """beat() from the main thread (a timer); a daemon thread writes every
    thread's stack when the beats stop for HANG_S."""

    def __init__(self):
        self._beat = time.monotonic()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="OvaHangWatch", daemon=True)

    def start(self):
        self._thread.start()

    def beat(self):
        self._beat = time.monotonic()

    def stop(self):
        self._stop = True
        f = _crash_file
        if f is not None:
            try:
                faulthandler.disable()
                f.close()
                if os.path.getsize(f._ova_path) == 0:
                    os.remove(f._ova_path)
            except Exception:
                pass

    def _run(self):
        dumped, last_beat = 0, self._beat
        while not self._stop:
            time.sleep(1.0)
            beat = self._beat
            if beat != last_beat:
                last_beat, dumped = beat, 0
                continue
            stalled = time.monotonic() - beat
            # at HANG_S, then 30 s and 60 s into the same hang
            due = (HANG_S, 30.0, 60.0)
            if dumped < min(HANG_DUMPS, len(due)) and stalled >= due[dumped]:
                dumped += 1
                self._dump(stalled)

    @staticmethod
    def _dump(stalled):
        path = os.path.join(logs_dir(), f"hang_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Ova Viewer not answering for {stalled:.0f} s - every thread's stack:\n\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
            print(f"⚠ Not answering for {stalled:.0f} s - stacks written to {path}")
        except Exception:
            pass
