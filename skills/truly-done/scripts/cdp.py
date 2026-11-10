"""truly-done: a minimal Chrome DevTools Protocol client on the standard library only.

The verifier needs no pip installs, so this file carries its own small WebSocket client (RFC 6455, client side:
masked frames out, fragmented frames in, ping answered) and a launcher for the Chrome already installed on the
machine. Chrome runs headless with a throwaway profile in a temporary folder: never your own profile, cookies or
sign-ins. Nothing here decides what may be fetched or clicked; that is verify.py's job (its request guard answers
every paused request). The socket only ever connects to 127.0.0.1.
"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse

CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


class CdpError(Exception):
    pass


def find_chrome():
    """The installed Chrome (or Chromium) binary, or None. Never downloads one."""
    for path in CHROME_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


# --- WebSocket (RFC 6455, client side) -------------------------------------------------------------------

def encode_frame(payload: bytes, opcode: int = 1, mask: bytes = None) -> bytes:
    """One final client frame. Clients must mask (RFC 6455 5.3)."""
    mask = mask if mask is not None else os.urandom(4)
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + mask + masked


def decode_frames(buf: bytes):
    """Split complete server frames off the front of buf. Returns ([(fin, opcode, payload)], rest)."""
    frames = []
    while len(buf) >= 2:
        fin, opcode = buf[0] >> 7, buf[0] & 0x0F
        masked, n = buf[1] >> 7, buf[1] & 0x7F
        pos = 2
        if n == 126:
            if len(buf) < 4:
                break
            n, pos = struct.unpack(">H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                break
            n, pos = struct.unpack(">Q", buf[2:10])[0], 10
        mask = b""
        if masked:
            if len(buf) < pos + 4:
                break
            mask, pos = buf[pos:pos + 4], pos + 4
        if len(buf) < pos + n:
            break
        payload = buf[pos:pos + n]
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frames.append((fin, opcode, payload))
        buf = buf[pos + n:]
    return frames, buf


class WebSocket:
    """A blocking WebSocket to a loopback DevTools endpoint."""

    def __init__(self, url: str, timeout: float = 10.0):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "ws" or parts.hostname not in ("127.0.0.1", "localhost"):
            raise CdpError(f"DevTools endpoint must be ws:// on loopback, got {url!r}")  # loopback DevTools has no TLS  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {parts.path or '/'} HTTP/1.1\r\nHost: {parts.hostname}:{parts.port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CdpError("DevTools closed the connection during the handshake")
            head += chunk
        head, self.buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CdpError(f"DevTools refused the WebSocket upgrade: {head[:80]!r}")
        self.parts = []

    def send(self, text: str):
        self.sock.sendall(encode_frame(text.encode()))

    def recv(self, timeout: float):
        """The next complete text message, or None when none arrives within timeout."""
        deadline = time.monotonic() + timeout
        while True:
            frames, self.buf = decode_frames(self.buf)
            for fin, opcode, payload in frames:
                if opcode == 9:          # ping -> pong
                    self.sock.sendall(encode_frame(payload, opcode=10))
                    continue
                if opcode == 8:
                    raise CdpError("DevTools closed the WebSocket")
                if opcode in (0, 1, 2):
                    self.parts.append(payload)
                    if fin:
                        msg, self.parts = b"".join(self.parts), []
                        # put back any later frames already decoded
                        rest = frames[frames.index((fin, opcode, payload)) + 1:]
                        for f in reversed(rest):
                            self.buf = _reframe(f) + self.buf
                        return msg.decode("utf-8", "replace")
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(1 << 20)
            except socket.timeout:
                return None
            if not chunk:
                raise CdpError("DevTools closed the WebSocket")
            self.buf += chunk

    def close(self):
        try:
            self.sock.sendall(encode_frame(b"", opcode=8))
        except OSError:
            pass
        self.sock.close()


def _reframe(frame) -> bytes:
    """Re-encode an already decoded (unmasked) server frame so it can be decoded again later."""
    fin, opcode, payload = frame
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


# --- Chrome + one page session ----------------------------------------------------------------------------

class Browser:
    """Headless Chrome with a throwaway profile, one page, and a flat CDP session to it.

    on_event(method, params) is called for every event while a command is awaited or pump() runs."""

    def __init__(self, chrome: str, on_event, timeout: float = 15.0, extra_args=()):
        self.profile = tempfile.mkdtemp(prefix="truly-done-")
        args = [chrome, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
                "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
                "--metrics-recording-only", "--mute-audio", "--disable-features=Translate,MediaRouter",
                "--window-size=1280,900", *extra_args, "about:blank"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")   # CI containers run as root; Chrome refuses its sandbox there
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.on_event = on_event
        self.next_id = 0
        self.session = None
        port_file = os.path.join(self.profile, "DevToolsActivePort")
        deadline = time.monotonic() + timeout
        while len(_read(port_file).split()) < 2:
            if time.monotonic() > deadline or self.proc.poll() is not None:
                self.close()
                raise CdpError("Chrome did not open its DevTools port")
            time.sleep(0.05)
        port, path = _read(port_file).split()[:2]
        self.ws = WebSocket(f"ws://127.0.0.1:{port}{path}")
        target = self.call("Target.createTarget", {"url": "about:blank"}, browser_level=True)["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True},
                                 browser_level=True)["sessionId"]

    def call(self, method: str, params: dict = None, timeout: float = 30.0, browser_level: bool = False):
        self.next_id += 1
        mid = self.next_id
        msg = {"id": mid, "method": method, "params": params or {}}
        if self.session and not browser_level:
            msg["sessionId"] = self.session
        self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CdpError(f"{method} timed out")
            raw = self.ws.recv(left)
            if raw is None:
                continue
            data = json.loads(raw)
            if data.get("id") == mid:
                if "error" in data:
                    raise CdpError(f"{method}: {data['error'].get('message')}")
                return data.get("result", {})
            if "method" in data:
                self.on_event(data["method"], data.get("params", {}))

    def send(self, method: str, params: dict = None):
        """Fire and forget (answers to paused requests); the reply is dropped when it arrives."""
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}, "sessionId": self.session}
        self.ws.send(json.dumps(msg))

    def pump(self, seconds: float):
        """Handle events for up to `seconds`."""
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            raw = self.ws.recv(min(left, 0.1))
            if raw is None:
                continue
            data = json.loads(raw)
            if "method" in data:
                self.on_event(data["method"], data.get("params", {}))

    def evaluate(self, expression: str, timeout: float = 30.0):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                             "awaitPromise": True}, timeout=timeout)
        if "exceptionDetails" in res:
            raise CdpError(f"page script failed: {res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    def close(self):
        try:
            if getattr(self, "ws", None):
                try:
                    self.call("Browser.close", browser_level=True, timeout=3)
                except Exception:  # noqa: BLE001 - closing anyway
                    pass
                self.ws.close()
        finally:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            shutil.rmtree(self.profile, ignore_errors=True)
