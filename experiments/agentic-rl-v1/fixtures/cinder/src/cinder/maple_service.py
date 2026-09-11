from __future__ import annotations

CONFIG_KEY = "maple_retry_limit"


class MapleService:
    """Deterministic maple processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "cinder:maple:" + self.normalize(payload)


def maple_pipeline(payload: str) -> str:
    return MapleService().run(payload)
