from __future__ import annotations

from typing import Any

import requests


class DiscordNotifierError(Exception):
    pass


class DiscordNotifier:
    def __init__(self, webhook_url: str, *, timeout_seconds: float = 10.0, username: str = "Bitget Position Bot") -> None:
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self.username = username

    def send_embeds(self, embeds: list[dict[str, Any]]) -> None:
        payload: dict[str, Any] = {
            "username": self.username,
            "embeds": embeds,
        }
        self._post(payload)

    def send_text(self, content: str) -> None:
        payload = {
            "username": self.username,
            "content": content,
        }
        self._post(payload)

    def _post(self, payload: dict[str, Any]) -> None:
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise DiscordNotifierError(f"Discord webhook request failed: {exc}") from exc

        if response.status_code not in (200, 204):
            raise DiscordNotifierError(
                f"Discord webhook returned HTTP {response.status_code}: {response.text[:1000]}"
            )
