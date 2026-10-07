"""Short-code generation strategies: Base62 (sequence) and Murmur3 (hash).

Both strategies produce fixed-width, URL-safe codes from a 64-bit value.

Base62
------
A monotonically increasing 64-bit counter (PostgreSQL sequence) is encoded
in base 62. Because the counter is strictly monotonic and unique, the
resulting code is *uniquely* determined by the sequence value -- zero
collisions by construction (the DB unique index is the final safety net).
Trade-off: codes are sequential (``0, 1, 2, ...`` -> ``0, 1, 2, ...`` in
base62), which leaks volume over time. Acceptable for most shorteners; see
``docs/design.md`` for the discussion.

Murmur3
-------
A 128-bit seed (time + counter + process id, Snowflake-style) is hashed
with Murmur3 to a 64-bit value, then base62-encoded. Codes are effectively
random and non-sequential, hiding volume. Collisions are possible (birthday
problem) but astronomically rare at shortener scale; the DB unique index
detects the (rare) collision and the service retries with a new seed.
"""

from __future__ import annotations

import hashlib

from app.config import Settings

#: Alphabet: lowercase, uppercase, digits. 62 chars -> log2(62) ~ 5.95 bits/char.
BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BASE62_ALPHABET_SET = frozenset(BASE62_ALPHABET)
_ALPHABET = {c: i for i, c in enumerate(BASE62_ALPHABET)}

#: Maximum length of a user-supplied custom alias.
MAX_CUSTOM_ALIAS_LEN = 64

_MAX_64BIT = (1 << 64) - 1


def base62_encode(value: int) -> str:
    """Encode a non-negative integer as a base-62 string."""
    if value < 0:
        raise ValueError("base62 encoding requires a non-negative integer")
    if value == 0:
        return "0"
    chars: list[str] = []
    while value:
        value, rem = divmod(value, 62)
        chars.append(BASE62_ALPHABET[rem])
    return "".join(reversed(chars))


def base62_decode(code: str) -> int:
    """Decode a base-62 string back to an integer.

    Raises ``ValueError`` for any character outside the alphabet.
    """
    if not code:
        raise ValueError("empty code")
    value = 0
    for c in code:
        if c not in _BASE62_ALPHABET_SET:
            raise ValueError(f"invalid base62 character: {c!r}")
        value = value * 62 + _ALPHABET[c]
    return value


def validate_custom_alias(alias: str) -> bool:
    """A custom alias must be non-empty, URL-safe, and within length bounds."""
    if not alias:
        return False
    if len(alias) > MAX_CUSTOM_ALIAS_LEN:
        return False
    first = alias[0]
    if not (first.isalnum() and first.isascii()):
        return False
    return all(c.isalnum() or c in "_-" for c in alias[1:])


def _murmur3_x86_32(data: bytes, seed: int = 0) -> int:
    """Pure-Python Murmur3 (x86, 32-bit) -- matches the reference implementation.

    Verified against official test vectors (e.g. ``murmur3(b"test") ==
    0xBA6BD213``). We call it twice with different seeds on the same input
    to obtain a 64-bit value for base62 encoding.
    """
    c1 = 0xCC9E2D51
    c2 = 0x1B873593
    h = seed & 0xFFFFFFFF
    length = len(data)
    nblocks = length // 4

    def _rotl32(x: int, r: int) -> int:
        return ((x << r) | (x >> (32 - r))) & 0xFFFFFFFF

    for i in range(nblocks):
        k = int.from_bytes(data[i * 4 : i * 4 + 4], "little")
        k = (k * c1) & 0xFFFFFFFF
        k = _rotl32(k, 15)
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = _rotl32(h, 13)
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF

    k1 = 0
    tail = data[nblocks * 4 :]
    tail_len = len(tail)
    # Murmur3 x86_32 finalization: fold the 0-3 leftover bytes into k1.
    # (The 3/2/1 thresholds are part of the canonical algorithm.)
    if tail_len >= 3:
        k1 ^= tail[2] << 16
    if tail_len >= 2:
        k1 ^= tail[1] << 8
    if tail_len >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = _rotl32(k1, 15)
        k1 = (k1 * c2) & 0xFFFFFFFF
        h ^= k1

    h ^= length
    # fmix32
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def _murmur3_64(data: bytes) -> int:
    """Combine two differently-seeded 32-bit Murmur3 digests into 64 bits."""
    low = _murmur3_x86_32(data, seed=0)
    high = _murmur3_x86_32(data, seed=0x9747B28C)
    return ((high << 32) | low) & _MAX_64BIT


def _snowflake_seed() -> int:
    """Generate a 64-bit Snowflake-style seed: time | counter | node."""
    import os
    import time

    # 41-bit millisecond timestamp (epoch 2024-01-01), 10-bit node, 13-bit seq.
    epoch_ms = int(time.time() * 1000) - 1_704_067_200_000
    node = os.getpid() & 0x3FF
    seq = int.from_bytes(os.urandom(2), "little") & 0x1FFF
    return ((epoch_ms & 0x1FFFFF) << 23) | (node << 13) | seq


def _pad_or_truncate(code: str, length: int) -> str:
    """Left-pad with '0' (or truncate) to a fixed width."""
    if len(code) > length:
        # Truncation is lossy; only safe for the *hash* strategy where we
        # re-check for collisions against the DB. We never truncate base62
        # sequence output (would break uniqueness), so this is a defensive
        # no-op for that path.
        return code[:length]
    return code.rjust(length, "0")


def generate_code(settings: Settings, sequence_value: int | None = None) -> str:
    """Generate a short code according to the configured strategy.

    Args:
        settings: application settings (code_strategy, code_length).
        sequence_value: for the base62 strategy, the next sequence value.
            If None a murmur3 seed is used instead (hash strategy).
    """
    length = settings.code_length
    if settings.code_strategy == "base62":
        if sequence_value is None:
            raise ValueError("base62 strategy requires a sequence value")
        return base62_encode(sequence_value)

    # murmur3 (hash) strategy
    seed = _snowflake_seed()
    data = seed.to_bytes(8, "little")
    hashed = _murmur3_64(data)
    return _pad_or_truncate(base62_encode(hashed), length)


def next_sequence_value_fallback() -> int:
    """A process-local 64-bit counter used only when the DB sequence is
    unavailable (e.g. tests without a database). Not collision-safe across
    processes -- callers must prefer the DB sequence in production.
    """
    import itertools
    import os

    counter = itertools.count(int.from_bytes(os.urandom(6), "big"))
    return next(counter)


def sha256_hex(data: str) -> str:
    """SHA-256 hex digest (used for deterministic idempotency checks)."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
