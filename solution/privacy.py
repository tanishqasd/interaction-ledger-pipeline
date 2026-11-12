"""Keyed lookup and authenticated encryption for sensitive records."""

import hmac
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_NONCE_SIZE = 12


def _derive_key(master_key: bytes, purpose: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"interaction-ledger/v1/" + purpose,
    ).derive(master_key)


def _lead_context(workflow_id: str, phone_token: bytes) -> bytes:
    return b"lead\0" + workflow_id.encode("utf-8") + b"\0" + phone_token


class Privacy:
    def __init__(self, master_key: bytes):
        self._lookup_key = _derive_key(master_key, b"phone-lookup")
        self._manifest_key = _derive_key(master_key, b"drop-manifest")
        self._cipher = AESGCM(_derive_key(master_key, b"record-encryption"))

    def phone_token(self, workflow_id: str, canonical_phone: str) -> bytes:
        message = workflow_id.encode("utf-8") + b"\0" + canonical_phone.encode("ascii")
        return hmac.digest(self._lookup_key, message, "sha256")

    def manifest_digest(self, manifest: bytes) -> bytes:
        return hmac.digest(self._manifest_key, manifest, "sha256")

    def _encrypt(self, plaintext: bytes, context: bytes) -> bytes:
        nonce = secrets.token_bytes(_NONCE_SIZE)
        return nonce + self._cipher.encrypt(nonce, plaintext, context)

    def _decrypt(self, blob: bytes, context: bytes) -> bytes:
        return self._cipher.decrypt(blob[:_NONCE_SIZE], blob[_NONCE_SIZE:], context)

    def encrypt_lead(self, workflow_id: str, phone_token: bytes, fields: dict[str, str | None]) -> bytes:
        plaintext = json.dumps(fields, ensure_ascii=False).encode("utf-8")
        return self._encrypt(plaintext, _lead_context(workflow_id, phone_token))

    def decrypt_lead(self, workflow_id: str, phone_token: bytes, blob: bytes) -> dict[str, str | None]:
        return json.loads(self._decrypt(blob, _lead_context(workflow_id, phone_token)))

    def encrypt_event(self, event_id: str, body: bytes) -> bytes:
        return self._encrypt(body, b"event\0" + event_id.encode("utf-8"))

    def decrypt_event(self, event_id: str, blob: bytes) -> bytes:
        return self._decrypt(blob, b"event\0" + event_id.encode("utf-8"))
