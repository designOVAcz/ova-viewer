"""🐧 Colour sync with ColorPenguin (and anything else that talks to Rebelle).

ColorPenguin can send the colour you pick to Rebelle over Rebelle's WebSocket
server: it connects to ws://127.0.0.1:8265/ and sends one JSON event per
colour, e.g. {"event_type":"SET_BRUSH_COLOR","color":{"r":242,"g":124,"b":0}}
(the Rebelle Motion IO events). Ova Viewer answers on that same port, so with
ColorPenguin's sync set to Rebelle the colour you pick there becomes the brush
colour here - nothing to set up in ColorPenguin.

Only this computer can connect (127.0.0.1). While Rebelle itself listens on
the port, the colours stay Rebelle's: Ova waits and takes over once it closes
(see ColorSyncServer).
"""

import json

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtNetwork import QHostAddress, QTcpSocket

try:
    from PySide6.QtWebSockets import QWebSocketServer
except Exception:       # pragma: no cover - a Qt build without WebSockets
    QWebSocketServer = None

PORT = 8265             # Rebelle's default WebSocket port, which ColorPenguin uses


_SECOND_WORDS = ('SECOND', 'BACKGROUND', 'ALT')      # in an event_type / slot name
_SECOND_KEYS = ('secondary_color', 'second_color', 'background_color', 'bg_color',
                'secondaryColor', 'secondColor', 'backgroundColor')


def _qcolour(colour):
    """{'r','g','b'} (0..255, or 0..1 floats) or '#rrggbb' as a QColor, or None."""
    if isinstance(colour, str):
        c = QColor(colour)
        return c if c.isValid() else None
    if not isinstance(colour, dict):
        return None
    try:
        rgb = [colour[k] for k in ('r', 'g', 'b')]
        if all(isinstance(v, float) and 0.0 <= v <= 1.0 for v in rgb):
            rgb = [v * 255.0 for v in rgb]      # 0..1 floats, should anyone send them
        r, g, b = (max(0, min(255, int(round(float(v))))) for v in rgb)
    except (KeyError, TypeError, ValueError):
        return None
    return QColor(r, g, b)


def _is_second_slot(event):
    """An event marked as for the second (secondary / background) colour."""
    kind = str(event.get('event_type') or '').upper()
    if any(w in kind for w in _SECOND_WORDS):
        return True
    if event.get('secondary') is True or event.get('second') is True:
        return True
    slot = event.get('slot', event.get('target', event.get('index')))
    if isinstance(slot, str):
        return any(w in slot.upper() for w in _SECOND_WORDS)
    return isinstance(slot, int) and not isinstance(slot, bool) and slot == 1


def colours_from_event(text):
    """[(slot, QColor)] a Rebelle event sets, slot 'pen' or 'second'. Takes
    SET_BRUSH_COLOR and SET_BRUSH with a colour, alone or in a list (as a
    Motion IO frame holds them). The second colour, should a sender pass one:
    an event named for it (…SECONDARY… / …BACKGROUND…), one marked as its slot,
    or a secondary_color / background_color beside the colour."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    if isinstance(data, dict) and isinstance(data.get('events'), list):
        data = data['events']
    events = data if isinstance(data, list) else [data]
    found = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get('event_type') or '').upper()
        if 'COLOR' not in kind and 'COLOUR' not in kind and kind != 'SET_BRUSH':
            continue
        colour = _qcolour(event.get('color', event.get('colour')))
        if colour is not None:
            found.append(('second' if _is_second_slot(event) else 'pen', colour))
        for key in _SECOND_KEYS:
            second = _qcolour(event.get(key))
            if second is not None:
                found.append(('second', second))
                break
    return found


def colour_from_event(text):
    """The pen colour a Rebelle event sets, as a QColor, or None (the last
    one wins). See colours_from_event for the second colour too."""
    pens = [c for slot, c in colours_from_event(text) if slot == 'pen']
    return pens[-1] if pens else None


def event_shape(text):
    """A short key for what kind of message *text* is (its event types and
    their fields), so each kind is logged once - see ColorSyncServer.seen."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return 'not json'
    if isinstance(data, dict) and isinstance(data.get('events'), list):
        data = data['events']
    events = data if isinstance(data, list) else [data]
    return '|'.join(
        f"{e.get('event_type')}:{','.join(sorted(e))}" if isinstance(e, dict) else type(e).__name__
        for e in events[:6])


def _answers(address, port, wait_ms=150):
    """Whether something accepts a connection on *address*:*port* (local:
    refused at once when nothing listens)."""
    sock = QTcpSocket()
    sock.connectToHost(address, port)
    ok = sock.waitForConnected(wait_ms)
    sock.abort()
    return ok


class ColorSyncServer(QObject):
    """Listens on 127.0.0.1:PORT; emits colour_received(QColor) per colour.

    Gives way to another app on the port: Rebelle listens there itself (on
    every address), and next to it a listener on 127.0.0.1 took ColorPenguin's
    next connection - Rebelle silently stopped getting colours. So it does not
    start while something answers there, steps aside when something appears
    (seen on the IPv6 loopback, where this one does not listen), and comes
    back once the port is free. status_changed says which."""

    colour_received = Signal(QColor)
    second_received = Signal(QColor)    # the second colour, when the sender marks one
    status_changed = Signal(str)        # 'listening', 'giving_way', 'off'

    CHECK_MS = 5000

    def __init__(self, parent=None, port=PORT):
        super().__init__(parent)
        self.port = port
        self._server = None
        self._clients = []
        self.error = ""
        self.state = 'off'
        self._seen = set()          # kinds of message logged once (event_shape)
        self._watch = QTimer(self)
        self._watch.setInterval(self.CHECK_MS)
        self._watch.timeout.connect(self._check)

    def is_running(self):
        return self._server is not None and self._server.isListening()

    def has_clients(self):
        """ColorPenguin (or another app) is connected right now."""
        return self.is_running() and bool(self._clients)

    def start(self):
        """Switch the sync on: listen now if the port is free, else wait for
        it. True when listening."""
        self._watch.start()
        return self._listen()

    def _listen(self):
        if self.is_running():
            return True
        if QWebSocketServer is None:
            self.error = "this Qt has no WebSockets"
            return False
        if (_answers(QHostAddress(QHostAddress.LocalHost), self.port)
                or _answers(QHostAddress(QHostAddress.LocalHostIPv6), self.port)):
            self.error = f"another app (Rebelle?) is using port {self.port}"
            self._set_state('giving_way')
            return False
        server = QWebSocketServer("Ova Viewer colour sync", QWebSocketServer.NonSecureMode, self)
        if not server.listen(QHostAddress.LocalHost, self.port):
            self.error = server.errorString() or f"port {self.port} is in use"
            server.deleteLater()
            self._set_state('giving_way')
            return False
        server.newConnection.connect(self._on_connection)
        self._server, self.error = server, ""
        self._set_state('listening')
        return True

    def _check(self):
        if self.is_running():
            if _answers(QHostAddress(QHostAddress.LocalHostIPv6), self.port):
                self._close()           # another app took the port too: its turn
                self.error = f"another app (Rebelle?) is using port {self.port}"
                self._set_state('giving_way')
        else:
            self._listen()

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            self.status_changed.emit(state)

    def stop(self):
        """Switch the sync off."""
        self._watch.stop()
        self._close()
        self._set_state('off')

    def _close(self):
        clients, self._clients = self._clients, []
        for client in clients:
            try:
                client.close()
            except RuntimeError:
                pass
        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None

    def _on_connection(self):
        while self._server is not None and self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            self._clients.append(client)
            client.textMessageReceived.connect(self._on_text)
            client.binaryMessageReceived.connect(
                lambda data: self._on_text(bytes(data).decode('utf-8', 'replace')))
            client.disconnected.connect(lambda c=client: self._drop(c))

    def _drop(self, client):
        if client in self._clients:
            self._clients.remove(client)
        try:
            client.deleteLater()
        except RuntimeError:
            pass                # went with the server (closed with it)

    def _on_text(self, text):
        # Each kind of message once into the log (Show Log): what the sender
        # really sends, e.g. for its second colour.
        shape = event_shape(text)
        if shape not in self._seen:
            self._seen.add(shape)
            print(f"🐧 Colour sync: new kind of message: {text[:400]}")
        for slot, colour in colours_from_event(text):
            if slot == 'second':
                self.second_received.emit(colour)
            else:
                self.colour_received.emit(colour)
