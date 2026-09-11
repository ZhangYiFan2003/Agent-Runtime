from __future__ import annotations

CONFIG_KEY = "falcon_retry_limit"


class FalconService:
    """Deterministic falcon processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:falcon:" + self.normalize(payload)


def falcon_pipeline(payload: str) -> str:
    return FalconService().run(payload)
