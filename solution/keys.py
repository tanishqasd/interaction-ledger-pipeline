"""Read the ledger secret without storing it in the database."""

import base64
import hmac
import os

KEY_ENV = "LEDGER_MASTER_KEY"


class KeyConfigurationError(ValueError):
    pass


def load_master_key() -> bytes:
    encoded = os.environ.get(KEY_ENV)
    if not encoded:
        raise KeyConfigurationError(f"Set {KEY_ENV} to a base64-encoded 32-byte secret.")
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise KeyConfigurationError(f"{KEY_ENV} must be a base64-encoded 32-byte secret.") from None
    if len(key) != 32:
        raise KeyConfigurationError(f"{KEY_ENV} must be a base64-encoded 32-byte secret.")
    return key


def key_check(master_key: bytes) -> bytes:
    return hmac.digest(master_key, b"interaction-ledger/v1/key-check", "sha256")
