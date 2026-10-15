"""Ed25519 signing of policy bundles (ADR A10). The Hub signs; laptops verify with the Hub public key, so a
consultant cannot grant themselves access by editing a cached file."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def generate_keypair(private_file: Path, public_file: Path) -> None:
    key = Ed25519PrivateKey.generate()
    private_file.parent.mkdir(parents=True, exist_ok=True)
    private_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    public_file.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo))


def sign(payload: dict[str, Any], private_pem: bytes) -> dict[str, Any]:
    key = serialization.load_pem_private_key(private_pem, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    sig = key.sign(canonical(payload))
    return {"payload": payload, "signature": base64.b64encode(sig).decode("ascii"), "alg": "Ed25519"}


def verify(bundle: dict[str, Any], public_pem: bytes) -> dict[str, Any]:
    """Return the payload if the signature is valid, else raise ValueError."""
    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Hub public key is not Ed25519")
    try:
        key.verify(base64.b64decode(bundle["signature"]), canonical(bundle["payload"]))
    except (InvalidSignature, KeyError, ValueError) as e:
        raise ValueError(f"policy signature invalid: {e}") from e
    return bundle["payload"]


def fingerprint(public_pem: bytes) -> str:
    """SHA-256 of the DER public key, shown as 16 groups of 4 hex chars - compare it out-of-band when a laptop is
    enrolled (the key file must come from IT, never be trusted just because the Hub served it)."""
    import hashlib
    key = serialization.load_pem_public_key(public_pem)
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 4] for i in range(0, len(h), 4))
