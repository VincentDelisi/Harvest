"""Discord webhook notifier — fills, exits, kill-switch alerts.

Sends rich-embed messages to a Discord webhook URL. Falls back to a no-op
NullNotifier when DISCORD_WEBHOOK_URL is unset (so DRY_RUN works offline).

All sends are best-effort and time-boxed: a notification failure must NEVER
block trading logic. Errors are logged and swallowed.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

import httpx

from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)


# Discord embed color codes (decimal, not hex)
COLOR_GREEN = 0x2ECC71   # success / fill / profit
COLOR_RED = 0xE74C3C     # loss / kill switch
COLOR_YELLOW = 0xF1C40F  # warning / heads-up
COLOR_BLUE = 0x3498DB    # informational
COLOR_GREY = 0x95A5A6    # debug / heartbeat


class Notifier(Protocol):
    """Protocol — anything callable as the notifier interface."""

    def send(
        self,
        title: str,
        description: str = "",
        *,
        color: int = COLOR_BLUE,
        fields: Optional[list[dict[str, Any]]] = None,
    ) -> bool: ...

    def info(self, title: str, description: str = "", **kwargs: Any) -> bool: ...
    def success(self, title: str, description: str = "", **kwargs: Any) -> bool: ...
    def warn(self, title: str, description: str = "", **kwargs: Any) -> bool: ...
    def error(self, title: str, description: str = "", **kwargs: Any) -> bool: ...


class NullNotifier:
    """No-op notifier for tests and when no webhook is configured."""

    def send(self, *args: Any, **kwargs: Any) -> bool:  # noqa: D401
        return True

    def info(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def success(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def warn(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def error(self, *args: Any, **kwargs: Any) -> bool:
        return True


class DiscordNotifier:
    """Posts rich embeds to a Discord webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        username: str = "Harvest",
        timeout: float = 5.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not webhook_url:
            raise ValueError("DiscordNotifier requires a non-empty webhook_url")
        self.webhook_url = webhook_url
        self.username = username
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.Client(**client_kwargs)

    def close(self) -> None:
        self._http.close()

    def send(
        self,
        title: str,
        description: str = "",
        *,
        color: int = COLOR_BLUE,
        fields: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        """Send a single embed. Returns True on 2xx, False otherwise — never raises."""
        embed: dict[str, Any] = {
            "title": title[:256],
            "description": description[:4096] if description else "",
            "color": color,
        }
        if fields:
            # Discord limits 25 fields per embed
            embed["fields"] = [
                {
                    "name": str(f.get("name", ""))[:256],
                    "value": str(f.get("value", ""))[:1024],
                    "inline": bool(f.get("inline", False)),
                }
                for f in fields[:25]
            ]
        payload = {"username": self.username, "embeds": [embed]}
        try:
            resp = self._http.post(self.webhook_url, json=payload)
            if 200 <= resp.status_code < 300:
                return True
            log.warning(
                "Discord webhook returned %d: %s", resp.status_code, resp.text[:200]
            )
            return False
        except Exception as exc:  # noqa: BLE001 — never block trading on notify failure
            log.warning("Discord notify failed: %s", exc)
            return False

    def info(self, title: str, description: str = "", **kwargs: Any) -> bool:
        return self.send(title, description, color=COLOR_BLUE, **kwargs)

    def success(self, title: str, description: str = "", **kwargs: Any) -> bool:
        return self.send(title, description, color=COLOR_GREEN, **kwargs)

    def warn(self, title: str, description: str = "", **kwargs: Any) -> bool:
        return self.send(title, description, color=COLOR_YELLOW, **kwargs)

    def error(self, title: str, description: str = "", **kwargs: Any) -> bool:
        return self.send(title, description, color=COLOR_RED, **kwargs)


def build_notifier(webhook_url: Optional[str] = None) -> Notifier:
    """Factory — returns DiscordNotifier if a webhook is configured, else NullNotifier."""
    url = webhook_url if webhook_url is not None else CONFIG.discord_webhook_url
    if url:
        return DiscordNotifier(url)
    log.info("No Discord webhook configured — using NullNotifier")
    return NullNotifier()
