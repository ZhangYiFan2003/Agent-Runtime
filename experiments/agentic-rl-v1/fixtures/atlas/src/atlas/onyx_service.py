from __future__ import annotations

CONFIG_KEY = "onyx_retry_limit"


class OnyxService:
    """Deterministic onyx processing service."""

    def normalize(self, payload: str) -> str:
        return payload.strip().lower()

    def run(self, payload: str) -> str:
        return "atlas:onyx:" + self.normalize(payload)


def onyx_pipeline(payload: str) -> str:
    return OnyxService().run(payload)
