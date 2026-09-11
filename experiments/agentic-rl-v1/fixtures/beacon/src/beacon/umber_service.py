from __future__ import annotations

CONFIG_KEY = "umber_retry_limit"


class UmberService:
    """Deterministic umber processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:umber:" + self.normalize(payload)


def umber_pipeline(payload: str) -> str:
    return UmberService().run(payload)
