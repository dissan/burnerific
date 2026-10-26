"""A tiny RSA key generator for tests (the boiler uses 512 bit keys)."""

from __future__ import annotations

import base64
import random


def _is_probable_prime(n: int, rng: random.Random, rounds: int = 32) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = rng.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_probable_prime(n, rng):
            return n


def generate(bits: int = 512, seed: int = 7) -> tuple[int, int, int]:
    """Return (n, e, d)."""
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _prime(bits // 2, rng), _prime(bits // 2, rng)
        n = p * q
        phi = (p - 1) * (q - 1)
        if p != q and n.bit_length() == bits and phi % e:
            return n, e, pow(e, -1, phi)


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(value)) + value


def _int(v: int) -> bytes:
    b = v.to_bytes((v.bit_length() + 8) // 8, "big")  # leading zero keeps it positive
    return _tlv(0x02, b)


def pkcs1(n: int, e: int) -> bytes:
    return _tlv(0x30, _int(n) + _int(e))


def spki(n: int, e: int) -> bytes:
    algo = _tlv(0x30, _tlv(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    return _tlv(0x30, algo + _tlv(0x03, b"\x00" + pkcs1(n, e)))


def b64(der: bytes) -> str:
    return base64.b64encode(der).decode()
