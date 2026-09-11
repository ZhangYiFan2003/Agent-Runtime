from __future__ import annotations

CONFIG_KEY = "river_retry_limit"


class RiverService:
    """Deterministic river processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "cinder:river:" + self.normalize(payload)


def river_pipeline(payload: str) -> str:
    return RiverService().run(payload)
