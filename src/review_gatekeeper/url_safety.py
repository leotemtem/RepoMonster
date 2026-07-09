from __future__ import annotations

from urllib.parse import urlsplit


def require_http_url(value: str, *, setting_name: str) -> str:
    """Validate deployment-controlled outbound HTTP endpoints."""
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{setting_name} must be an http(s) URL")
    return normalized
