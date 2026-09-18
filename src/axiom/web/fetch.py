from __future__ import annotations

import html
import re

import httpx

from axiom.policy.network import NetworkPolicy, NetworkPolicyError


async def fetch_url(url: str, max_length: int = 10_000, timeout: float = 15.0) -> str:
    policy = NetworkPolicy()
    policy.validate_url(url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = None
        current_url = url
        for _ in range(5):
            policy.validate_url(current_url)
            response = await client.get(
                current_url,
                headers={"user-agent": "axiom-agent/0.1.0"},
            )
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location")
            if not location:
                break
            current_url = str(response.url.join(location))
        else:
            raise NetworkPolicyError("too many redirects")
        assert response is not None
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        text = response.text
        if "html" in content_type:
            text = extract_text_from_html(text)
        if len(text) > max_length:
            text = text[:max_length] + "\n... [truncated]"
        return text or "(empty page)"


def extract_text_from_html(raw_html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", raw_html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()
