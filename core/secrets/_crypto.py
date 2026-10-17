"""Authenticated encryption of Secret Store entries (AES-256-GCM).

Every entry is sealed on its own with a fresh 96-bit nonce. The associated data
binds the ciphertext to its store, its key, its section and its name, so an
entry copied to another name, section, store or key does not open. A sealed
entry is ``base64(nonce || ciphertext || tag)``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
_TAG_BYTES = 16
_AAD_PREFIX = "vbot-secret-store/1"
_KEY_CHECK_SECTION = "key-check"
_KEY_CHECK_PLAINTEXT = b"vbot secret store key check"
_FINGERPRINT_LABEL = b"vbot secret store plaintext fingerprint"


class SealedEntryError(ValueError):
    """A sealed entry is malformed or does not open under this key and name."""


def new_data_key() -> bytes:
    """A fresh random 256-bit data key."""
    return os.urandom(KEY_BYTES)


def new_identifier() -> str:
    """A random identifier for a store or a key (128 bits, hexadecimal)."""
    return secrets.token_hex(16)


def associated_data(store_id: str, key_id: str, section: str, name: str) -> bytes:
    """The associated data that binds one entry to its place."""
    return "\0".join((_AAD_PREFIX, store_id, key_id, section, name)).encode("utf-8")


def seal(key: bytes, plaintext: bytes, aad: bytes) -> str:
    """Encrypt ``plaintext`` with a fresh nonce; the result is base64 text."""
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def open_sealed(key: bytes, sealed: object, aad: bytes) -> bytes:
    """Decrypt one sealed entry; :class:`SealedEntryError` when it does not open."""
    if not isinstance(sealed, str):
        raise SealedEntryError("the entry is not a sealed string")
    try:
        raw = base64.b64decode(sealed.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise SealedEntryError("the entry is not valid base64") from exc
    if len(raw) < NONCE_BYTES + _TAG_BYTES:
        raise SealedEntryError("the entry is too short")
    try:
        return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise SealedEntryError("the entry does not authenticate") from exc


def key_check(key: bytes, store_id: str, key_id: str) -> str:
    """A sealed constant that proves later that a key is this store's key."""
    return seal(
        key, _KEY_CHECK_PLAINTEXT, associated_data(store_id, key_id, _KEY_CHECK_SECTION, "")
    )


def key_matches(key: bytes, store_id: str, key_id: str, check: object) -> bool:
    """Whether ``key`` opens the store's key check."""
    try:
        opened = open_sealed(key, check, associated_data(store_id, key_id, _KEY_CHECK_SECTION, ""))
    except SealedEntryError:
        return False
    return hmac.compare_digest(opened, _KEY_CHECK_PLAINTEXT)


def fingerprint(key: bytes, content: bytes) -> str:
    """A keyed digest of plaintext content; reveals nothing without the data key."""
    subkey = hmac.new(key, _FINGERPRINT_LABEL, hashlib.sha256).digest()
    return hmac.new(subkey, content, hashlib.sha256).hexdigest()


def is_data_key(value: object) -> bool:
    return isinstance(value, bytes) and len(value) == KEY_BYTES
