from __future__ import annotations

CONFIG_KEY = "timber_retry_limit"


class TimberService:
    """Deterministic timber processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "beacon:timber:" + self.normalize(payload)


def timber_pipeline(payload: str) -> str:
    return TimberService().run(payload)
