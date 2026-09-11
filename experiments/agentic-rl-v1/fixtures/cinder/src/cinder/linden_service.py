from __future__ import annotations

CONFIG_KEY = "linden_retry_limit"


class LindenService:
    """Deterministic linden processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "cinder:linden:" + self.normalize(payload)


def linden_pipeline(payload: str) -> str:
    return LindenService().run(payload)
