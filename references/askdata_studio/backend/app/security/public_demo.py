"""Explicit, fail-closed settings for the hosted trial; local defaults stay intact."""
from __future__ import annotations

import os
from urllib.parse import urlsplit


def enabled() -> bool:
    return os.environ.get("INSIGHTPILOT_PUBLIC_DEMO", "").lower() in {"1", "true"}


def public_origin() -> str:
    value = (os.environ.get("INSIGHTPILOT_PUBLIC_ORIGIN") or
             (os.environ.get("RENDER_EXTERNAL_URL", "") if enabled() else "")).rstrip("/")
    parsed = urlsplit(value)
    if enabled() and (parsed.scheme != "https" or not parsed.netloc or
                      parsed.path or parsed.query or parsed.fragment or parsed.username):
        raise RuntimeError("公开试用需要配置 HTTPS 站点地址")
    return value or "http://127.0.0.1:5174"


def allowed_origins() -> set[str]:
    if enabled():
        return {public_origin()}
    return {"http://127.0.0.1:5174", "http://localhost:5174",
            "http://127.0.0.1:5175", "http://localhost:5175"}


def signing_key() -> bytes:
    value = os.environ.get("INSIGHTPILOT_VISITOR_SECRET", "")
    if len(value) < 32:
        raise RuntimeError("公开试用尚未配置访客会话签名密钥")
    return value.encode()


DATASOURCE = "homenova_20260831"
ADMIN_USER_ID = "cloud_admin"


def verify_admin_password(password: str) -> bool:
    """Only an explicitly configured salted hash enables the cloud administrator."""
    import hashlib
    import hmac
    encoded = os.environ.get("INSIGHTPILOT_ADMIN_PASSWORD_HASH", "")
    if not password or len(password) > 256:
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != 600_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False
