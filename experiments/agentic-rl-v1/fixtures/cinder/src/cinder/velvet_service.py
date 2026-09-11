from __future__ import annotations

CONFIG_KEY = "velvet_retry_limit"


class VelvetService:
    """Deterministic velvet processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "cinder:velvet:" + self.normalize(payload)


def velvet_pipeline(payload: str) -> str:
    return VelvetService().run(payload)
