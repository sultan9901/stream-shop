"""Cryptographic primitives: password hashing, HMAC token hashing, signing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

# --------------------------------------------------------------------------
# Password hashing — Argon2id preferred, PBKDF2-HMAC-SHA256 as a portable
# fallback so the app still boots on a host without argon2-cffi wheels.
# --------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError

    _ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
    _ARGON2 = True
except Exception:  # pragma: no cover
    _ph = None
    _ARGON2 = False
    VerifyMismatchError = Exception  # type: ignore[assignment, misc]

_PBKDF2_ROUNDS = 260_000


def _pbkdf2_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def _pbkdf2_verify(password: str, stored: str) -> bool:
    try:
        _, rounds, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def hash_password(password: str) -> str:
    if not password or len(password) < 3:
        raise ValueError("password too short")
    if _ARGON2 and _ph is not None:
        return _ph.hash(password)
    return _pbkdf2_hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    """Constant-time-ish verification. Always runs work to blunt user-enumeration."""
    if not stored_hash:
        # burn comparable time so a missing account is not obviously faster
        _pbkdf2_verify(password or "", _pbkdf2_hash("dummy-value"))
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        return _pbkdf2_verify(password, stored_hash)
    if _ARGON2 and _ph is not None:
        try:
            return _ph.verify(stored_hash, password)
        except VerifyMismatchError:
            return False
        except Exception:
            return False
    return False


def needs_rehash(stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    if _ARGON2 and _ph is not None:
        if stored_hash.startswith("pbkdf2_sha256$"):
            return True
        try:
            return _ph.check_needs_rehash(stored_hash)
        except Exception:
            return False
    return False


# --------------------------------------------------------------------------
# Opaque tokens (sessions, download grants, device ids)
# --------------------------------------------------------------------------
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(raw: str) -> str:
    """Keyed hash — a stolen database dump yields no usable tokens."""
    return hmac.new(settings.secret_key.encode(), raw.encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Signed, timed payloads (OAuth state, device cookie)
# --------------------------------------------------------------------------
def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


def sign_payload(data: dict, salt: str = "generic") -> str:
    return _serializer(salt).dumps(data)


def unsign_payload(token: str, salt: str = "generic", max_age: int = 900) -> dict | None:
    try:
        return _serializer(salt).loads(token, max_age=max_age)
    except (BadSignature, Exception):
        return None
