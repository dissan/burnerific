"""The controller's write encryption: unpadded ("textbook") RSA with its own key.

The boiler hands out its public key on request (``misc.rsa_key``) as base64
DER. Requests that change something carry the PIN and must be encrypted with
that key. There is no padding scheme, so the standard libraries refuse to do
it; it is a single modular exponentiation, which Python does natively. That
keeps this integration free of third party requirements.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

_RSA_OID = bytes.fromhex("2a864886f70d010101")  # 1.2.840.113549.1.1.1


class RsaKeyError(ValueError):
    """The boiler's key could not be understood."""


def _read_tlv(data: bytes, pos: int) -> tuple[int, int, int]:
    """Return (tag, value_start, value_end) of the DER element at ``pos``."""
    if pos + 2 > len(data):
        raise RsaKeyError("truncated DER")
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 4 or pos + count > len(data):
            raise RsaKeyError("bad DER length")
        length = int.from_bytes(data[pos : pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise RsaKeyError("truncated DER")
    return tag, pos, pos + length


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out = []
    while start < end:
        tag, vs, ve = _read_tlv(data, start)
        out.append((tag, vs, ve))
        start = ve
    return out


def _rsa_numbers(der: bytes) -> tuple[int, int]:
    tag, vs, ve = _read_tlv(der, 0)
    if tag != 0x30:
        raise RsaKeyError("key is not a DER sequence")
    items = _children(der, vs, ve)
    # PKCS#1 RSAPublicKey: SEQUENCE { INTEGER n, INTEGER e }
    if len(items) == 2 and items[0][0] == 0x02 and items[1][0] == 0x02:
        n = int.from_bytes(der[items[0][1] : items[0][2]], "big")
        e = int.from_bytes(der[items[1][1] : items[1][2]], "big")
        return n, e
    # SubjectPublicKeyInfo: SEQUENCE { SEQUENCE { OID rsa, NULL }, BIT STRING { RSAPublicKey } }
    if len(items) == 2 and items[0][0] == 0x30 and items[1][0] == 0x03:
        algo = _children(der, items[0][1], items[0][2])
        if not algo or algo[0][0] != 0x06 or der[algo[0][1] : algo[0][2]] != _RSA_OID:
            raise RsaKeyError("not an RSA key")
        bits = der[items[1][1] : items[1][2]]
        if not bits or bits[0] != 0:
            raise RsaKeyError("bad BIT STRING")
        return _rsa_numbers(bits[1:])
    raise RsaKeyError("unknown key layout")


@dataclass(slots=True, frozen=True)
class RsaKey:
    n: int
    e: int

    @property
    def size(self) -> int:
        """Modulus length in bytes, which is also the cipher block length."""
        return (self.n.bit_length() + 7) // 8

    @classmethod
    def from_der(cls, der: bytes) -> RsaKey:
        n, e = _rsa_numbers(der)
        if n < 2**256 or e < 3:
            raise RsaKeyError("implausible RSA key")
        return cls(n, e)

    @classmethod
    def from_payload(cls, value: str) -> RsaKey:
        """Parse the ``rsa_key`` value of a ``misc.rsa_key`` response (base64 DER or PEM)."""
        text = value.strip()
        if "-----BEGIN" in text:
            text = "".join(line for line in text.splitlines() if line and not line.startswith("-----"))
        try:
            der = base64.b64decode(text, validate=False)
        except ValueError as err:
            raise RsaKeyError("key is not base64") from err
        return cls.from_der(der)

    def encrypt(self, block: bytes) -> bytes:
        """Textbook RSA of exactly one block. Raises if the result would need a leading zero.

        The controller strips leading zeros and then expects a full block, so a
        cipher text starting with 0x00 is unusable; callers re-pad and retry.
        """
        if len(block) != self.size:
            raise ValueError(f"block must be {self.size} bytes")
        m = int.from_bytes(block, "big")
        if m >= self.n:
            raise ValueError("block does not fit the modulus")
        c = pow(m, self.e, self.n).to_bytes(self.size, "big")
        if c[0] == 0:
            raise LeadingZero
        return c


class LeadingZero(Exception):
    """Cipher text started with a zero byte; pad differently and try again."""
