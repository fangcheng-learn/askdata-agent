"""Explicit, fail-closed settings for the hosted trial; local defaults stay intact."""
from __future__ import annotations

import os
from urllib.parse import urlsplit


def enabled() -> bool:
    return os.environ.get("INSIGHTPILOT_PUBLIC_DEMO", "").lower() in {"1", "true"}


def public_origin() -> str:
    value = os.environ.get("INSIGHTPILOT_PUBLIC_ORIGIN", "").rstrip("/")
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
