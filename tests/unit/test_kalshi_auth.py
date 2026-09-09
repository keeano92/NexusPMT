from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.app.kalshi.auth import build_auth_headers, sign_path, signing_path_for_url


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signing_path_strips_query():
    path = signing_path_for_url(
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "/portfolio/orders?limit=5",
    )
    assert path == "/trade-api/v2/portfolio/orders"


def test_sign_and_headers_stable_shape():
    key = _key()
    ts = "1703123456789"
    # RSA-PSS salt is random — signatures are not byte-identical across calls
    sig = sign_path(key, ts, "GET", "/trade-api/v2/portfolio/balance")
    assert isinstance(sig, str) and len(sig) > 20
    headers = build_auth_headers(
        api_key_id="test-key",
        private_key=key,
        method="GET",
        base_url="https://external-api.demo.kalshi.co/trade-api/v2",
        relative_path="/portfolio/balance",
        timestamp_ms=ts,
    )
    assert headers["KALSHI-ACCESS-KEY"] == "test-key"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == ts
    assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 20
    assert headers["Content-Type"] == "application/json"


def test_pem_roundtrip_loads():
    key = _key()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert b"BEGIN" in pem
