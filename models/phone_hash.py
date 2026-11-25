"""The one place a phone number is allowed to exist, briefly.

``phone_hash`` is HMAC-SHA256(key=PHONE_HASH_KEY, message=<phone>), rendered as 64
lowercase hex characters and truncated to nothing else. HMAC rather than a bare
SHA-256 on purpose: the space of Chinese mobile numbers is small enough to
enumerate, so an unkeyed digest is reversible with a rainbow table. With the key
held only in the environment, a dump of the table is useless without it.

The key never appears in code, config or tests. It is read from the environment
every call, so rotating the key is a deployment concern rather than a code change.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

ENV_VAR = "PHONE_HASH_KEY"
DIGEST_LENGTH = 64
_HEX64 = re.compile(r"[0-9a-f]{64}")


class MissingKeyError(RuntimeError):
    """Raised when PHONE_HASH_KEY is unset and a hash is requested."""


def get_key() -> bytes:
    """Return the HMAC key from the environment.

    Raises MissingKeyError with an actionable message instead of silently hashing
    with an empty key, which would make the digest unkeyed and reversible.
    """
    value = os.environ.get(ENV_VAR)
    if not value:
        raise MissingKeyError(
            f"{ENV_VAR} is not set; refusing to hash a phone number without a key."
        )
    return value.encode("utf-8")


def hash_phone(phone: str | None, *, key: bytes | None = None) -> str | None:
    """Return the 64 character hex digest of *phone*, or None for an absent phone.

    >>> len(hash_phone("19900000001", key=b"demo-key"))
    64
    """
    if phone is None:
        return None
    phone = phone.strip()
    if not phone:
        return None
    if key is None:
        key = get_key()
    return hmac.new(key, phone.encode("utf-8"), hashlib.sha256).hexdigest()


def is_hash(value: str) -> bool:
    """True when *value* is exactly 64 lowercase hex characters."""
    return _HEX64.fullmatch(value) is not None
