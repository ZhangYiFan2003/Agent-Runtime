from __future__ import annotations

CONFIG_KEY = "kestrel_retry_limit"


class KestrelService:
    """Deterministic kestrel processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "cinder:kestrel:" + self.normalize(payload)


def kestrel_pipeline(payload: str) -> str:
    return KestrelService().run(payload)
