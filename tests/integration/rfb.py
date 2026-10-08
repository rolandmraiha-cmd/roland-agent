"""A small screen-sharing (RFB 3.8, "VNC") client for the tests. Standard library only.

It does what Roland's screen page does at the lowest level: log in with a password, press
keys, move and click the pointer, and read what the server sends back. The tests use it to
check what x11vnc itself enforces, with no noVNC in between: that the view-only password
cannot send input, that a wrong password gets nowhere, and that the screen's clipboard is
never sent out.

VNC passwords are checked with single DES, which Python's standard library doesn't have, so
a plain implementation is included. It is only ever used on test passwords.
"""

from __future__ import annotations

import socket
import struct
import time

# --- DES (FIPS 46-3), encryption of one 8-byte block ---

_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)
_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
)
_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
)
_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
)
_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
)
_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_SBOX = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _permute(value: int, table: tuple[int, ...], width: int) -> int:
    out = 0
    for position in table:
        out = (out << 1) | ((value >> (width - position)) & 1)
    return out


def des_encrypt_block(key: bytes, block: bytes) -> bytes:
    if len(key) != 8 or len(block) != 8:
        raise ValueError("DES takes an 8-byte key and an 8-byte block")
    key56 = _permute(int.from_bytes(key, "big"), _PC1, 64)
    left, right = key56 >> 28, key56 & 0xFFFFFFF
    subkeys = []
    for shift in _SHIFTS:
        left = ((left << shift) | (left >> (28 - shift))) & 0xFFFFFFF
        right = ((right << shift) | (right >> (28 - shift))) & 0xFFFFFFF
        subkeys.append(_permute((left << 28) | right, _PC2, 56))
    data = _permute(int.from_bytes(block, "big"), _IP, 64)
    high, low = data >> 32, data & 0xFFFFFFFF
    for subkey in subkeys:
        mixed = _permute(low, _E, 32) ^ subkey
        boxed = 0
        for index in range(8):
            six = (mixed >> (42 - 6 * index)) & 0x3F
            row, column = ((six >> 4) & 2) | (six & 1), (six >> 1) & 0xF
            boxed = (boxed << 4) | _SBOX[index][row * 16 + column]
        high, low = low, high ^ _permute(boxed, _P, 32)
    return _permute((low << 32) | high, _FP, 64).to_bytes(8, "big")


def vnc_response(password: str, challenge: bytes) -> bytes:
    """The answer to a VNC login challenge: DES with the password as key, each key byte's
    bits in reverse order (a quirk every VNC program shares), on both halves."""
    if len(challenge) != 16:
        raise ValueError("a VNC challenge is 16 bytes")
    raw = password.encode("latin-1")[:8].ljust(8, b"\0")
    key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in raw)
    return des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:])


# --- the client ---

# X11 key codes for the keys the tests press that aren't characters.
ENTER, TAB, DELETE, CONTROL = 0xFF0D, 0xFF09, 0xFFFF, 0xFFE3


class RfbError(Exception):
    """The server refused, or sent something this small client doesn't understand."""


class Rfb:
    """One connection to a VNC server. `Rfb(host, port, password)` logs in or raises RfbError."""

    def __init__(self, host: str, port: int, password: str, *, timeout: float = 8.0, shared: bool = True):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.width = self.height = 0
        self.name = ""
        self.cut_texts: list[bytes] = []
        try:
            self._handshake(password, shared)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Rfb:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _read(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RfbError("the server closed the connection")
            data += chunk
        return data

    def _reason(self) -> str:
        (length,) = struct.unpack(">I", self._read(4))
        return self._read(min(length, 1000)).decode("latin-1", "replace")

    def _handshake(self, password: str, shared: bool) -> None:
        version = self._read(12)
        if not version.startswith(b"RFB 003."):
            raise RfbError("not a VNC server")
        self.sock.sendall(b"RFB 003.008\n")
        (count,) = self._read(1)
        if count == 0:
            raise RfbError("refused: " + self._reason())
        kinds = self._read(count)
        if 2 not in kinds:
            # 1 would mean "no password at all", which the screen must never offer.
            raise RfbError(f"the server offers no password login (types {list(kinds)})")
        self.security_types = list(kinds)
        self.sock.sendall(b"\x02")
        self.sock.sendall(vnc_response(password, self._read(16)))
        (result,) = struct.unpack(">I", self._read(4))
        if result != 0:
            try:
                reason = self._reason()
            except (RfbError, OSError):
                reason = ""
            raise RfbError("login failed" + (f": {reason}" if reason else ""))
        self.sock.sendall(b"\x01" if shared else b"\x00")
        self.width, self.height = struct.unpack(">HH", self._read(4))
        self._read(16)  # pixel format: replaced by ours below
        (length,) = struct.unpack(">I", self._read(4))
        self.name = self._read(min(length, 1000)).decode("latin-1", "replace")
        # 32 bits a pixel, little-endian, blue-green-red-unused; and the plainest encoding.
        self.sock.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        self.sock.sendall(struct.pack(">BxHi", 2, 1, 0))

    # --- what Roland's page can send ---

    def key(self, keysym: int) -> None:
        """Press and let go of one key."""
        self.sock.sendall(struct.pack(">BBxxI", 4, 1, keysym) + struct.pack(">BBxxI", 4, 0, keysym))

    def type(self, text: str) -> None:
        for char in text:
            self.key(ord(char))
            time.sleep(0.03)

    def chord(self, modifier: int, keysym: int) -> None:
        """A key pressed while a modifier is held, such as Control with C."""
        self.sock.sendall(struct.pack(">BBxxI", 4, 1, modifier))
        time.sleep(0.03)
        self.key(keysym)
        time.sleep(0.03)
        self.sock.sendall(struct.pack(">BBxxI", 4, 0, modifier))
        time.sleep(0.05)

    def move(self, x: int, y: int, buttons: int = 0) -> None:
        self.sock.sendall(struct.pack(">BBHH", 5, buttons, x, y))

    def click(self, x: int, y: int) -> None:
        self.move(x, y)
        time.sleep(0.05)
        self.move(x, y, 1)
        time.sleep(0.05)
        self.move(x, y)

    def paste(self, text: str) -> None:
        """Offer text as this client's clipboard (it lands in the screen's clipboard)."""
        data = text.encode("latin-1")
        self.sock.sendall(struct.pack(">BxxxI", 6, len(data)) + data)

    # --- what the server sends back ---

    def picture(self) -> bytes:
        """The whole screen as raw pixels (4 bytes each). Reads until it has arrived."""
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
        pixels = bytearray(self.width * self.height * 4)
        seen = 0
        deadline = time.monotonic() + 20
        while seen < self.width * self.height and time.monotonic() < deadline:
            for x, y, width, height, data in self._next_rectangles():
                for row in range(height):
                    start = ((y + row) * self.width + x) * 4
                    pixels[start:start + width * 4] = data[row * width * 4:(row + 1) * width * 4]
                seen += width * height
        if seen < self.width * self.height:
            raise RfbError("the screen picture did not arrive")
        return bytes(pixels)

    def _next_rectangles(self) -> list[tuple[int, int, int, int, bytes]]:
        while True:
            kind = self._read(1)[0]
            if kind == 0:
                (count,) = struct.unpack(">xH", self._read(3))
                rectangles = []
                for _ in range(count):
                    x, y, width, height, encoding = struct.unpack(">HHHHi", self._read(12))
                    if encoding != 0:
                        raise RfbError(f"unexpected encoding {encoding}")
                    rectangles.append((x, y, width, height, self._read(width * height * 4)))
                return rectangles
            self._other(kind)

    def _other(self, kind: int) -> None:
        if kind == 2:      # bell
            return
        if kind == 3:      # the server's clipboard
            (length,) = struct.unpack(">xxxI", self._read(7))
            self.cut_texts.append(self._read(length))
            return
        if kind == 1:      # colour map
            _first, count = struct.unpack(">xHH", self._read(5))
            self._read(count * 6)
            return
        raise RfbError(f"unexpected message {kind}")

    def listen(self, seconds: float) -> None:
        """Read whatever the server sends for a while (pictures are thrown away)."""
        deadline = time.monotonic() + seconds
        try:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self.sock.settimeout(left)
                try:
                    kind = self.sock.recv(1)
                except TimeoutError:
                    return
                if not kind:
                    raise RfbError("the server closed the connection")
                self.sock.settimeout(8)
                if kind[0] == 0:
                    (count,) = struct.unpack(">xH", self._read(3))
                    for _ in range(count):
                        _x, _y, width, height, encoding = struct.unpack(">HHHHi", self._read(12))
                        if encoding != 0:
                            raise RfbError(f"unexpected encoding {encoding}")
                        self._read(width * height * 4)
                else:
                    self._other(kind[0])
        finally:
            self.sock.settimeout(8)

    def alive(self, wait: float = 1.5) -> bool:
        """False once the server has dropped this connection."""
        try:
            self.listen(wait)
        except (RfbError, OSError):
            return False
        return True
