"""Kalshi V2 RSA-PSS request signing."""

from __future__ import annotations

import base64
import time
from typing import Mapping
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def load_private_key(pem_bytes: bytes) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(pem_bytes, password=None, backend=default_backend())
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("Kalshi key must be an RSA private key")
    return key


def sign_path(private_key: RSAPrivateKey, timestamp_ms: str, method: str, path: str) -> str:
    """Sign timestamp + METHOD + path (no query string)."""
    path_without_query = path.split("?", 1)[0]
    message = f"{timestamp_ms}{method.upper()}{path_without_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def signing_path_for_url(base_url: str, relative_path: str) -> str:
    """Full API path from root for signing, e.g. /trade-api/v2/portfolio/balance."""
    if relative_path.startswith("http"):
        return urlparse(relative_path).path
    base_path = urlparse(base_url).path.rstrip("/")
    rel = relative_path if relative_path.startswith("/") else f"/{relative_path}"
    if rel.startswith(base_path):
        return rel.split("?", 1)[0]
    return f"{base_path}{rel}".split("?", 1)[0]


def build_auth_headers(
    *,
    api_key_id: str,
    private_key: RSAPrivateKey,
    method: str,
    base_url: str,
    relative_path: str,
    timestamp_ms: str | None = None,
) -> Mapping[str, str]:
    ts = timestamp_ms or str(int(time.time() * 1000))
    path = signing_path_for_url(base_url, relative_path)
    sig = sign_path(private_key, ts, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
