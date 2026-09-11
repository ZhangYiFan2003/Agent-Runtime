from __future__ import annotations

CONFIG_KEY = "nova_retry_limit"


class NovaService:
    """Deterministic nova processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "cinder:nova:" + self.normalize(payload)


def nova_pipeline(payload: str) -> str:
    return NovaService().run(payload)
