from __future__ import annotations

CONFIG_KEY = "grove_retry_limit"


class GroveService:
    """Deterministic grove processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:grove:" + self.normalize(payload)


def grove_pipeline(payload: str) -> str:
    return GroveService().run(payload)
