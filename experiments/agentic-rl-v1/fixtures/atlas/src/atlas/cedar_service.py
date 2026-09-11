from __future__ import annotations

CONFIG_KEY = "cedar_retry_limit"


class CedarService:
    """Deterministic cedar processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:cedar:" + self.normalize(payload)


def cedar_pipeline(payload: str) -> str:
    return CedarService().run(payload)
