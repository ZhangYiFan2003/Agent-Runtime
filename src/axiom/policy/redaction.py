from __future__ import annotations

from typing import Any

_SENSITIVE_FIELDS = {
    "authorization",
    "cookie",
    "setcookie",
    "proxyauthorization",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "password",
    "passwd",
    "secret",
    "token",
    "dsn",
    "postgresqldsn",
    "credential",
    "credentials",
    "privatekey",
    "clientsecret",
    "bearer",
}


def redact_secrets(value: Any) -> Any:
    """Redact known credential-shaped structured fields without hiding metrics."""
    if isinstance(value, dict):
        return {
            str(key): "***" if _is_sensitive_key(key) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    return value


def _is_sensitive_key(key: object) -> bool:
    normalized = "".join(char for char in str(key).casefold() if char.isalnum())
    return normalized in _SENSITIVE_FIELDS
