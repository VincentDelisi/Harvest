"""Discord webhook notifier — fills, exits, kill-switch alerts.

Sends rich-embed messages to a Discord webhook URL. Falls back to a no-op
NullNotifier when DISCORD_WEBHOOK_URL is unset (so DRY_RUN works offline).

All sends are best-effort and time-boxed: a notification failure must NEVER
block trading logic. Errors are logged and swallowed.
"""
from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional, Protocol

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

    # Retry settings: ~7s worst-case total (1 + 2 + 4 + jitter). Cheap insurance
    # against Discord's transient 5xx and 429 storms, never long enough to
    # stall the 30s engine tick.
    MAX_ATTEMPTS = 3
    BACKOFF_BASE_S = 1.0
    BACKOFF_MAX_S = 8.0
    RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        webhook_url: str,
        *,
        username: str = "Harvest",
        timeout: float = 5.0,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not webhook_url:
            raise ValueError("DiscordNotifier requires a non-empty webhook_url")
        self.webhook_url = webhook_url
        self.username = username
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.Client(**client_kwargs)
        # Injectable for tests so we don't actually sleep during retry tests.
        self._sleep = sleep

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
        return self._post_with_retry(payload)

    def _post_with_retry(self, payload: dict[str, Any]) -> bool:
        """POST with exponential backoff on transient failures.

        Retries on: network exceptions, 5xx, and 429 (rate-limit — honors
        Discord's `Retry-After` header when present).
        Does NOT retry on: 4xx (client errors are our bug; retrying just spams).
        """
        last_status: Optional[int] = None
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                resp = self._http.post(self.webhook_url, json=payload)
            except Exception as exc:  # noqa: BLE001 — network/timeout
                last_exc = exc
                last_status = None
                if attempt < self.MAX_ATTEMPTS:
                    delay = self._backoff_delay(attempt)
                    log.warning(
                        "Discord notify network error (attempt %d/%d): %s — retrying in %.1fs",
                        attempt, self.MAX_ATTEMPTS, exc, delay,
                    )
                    self._sleep(delay)
                    continue
                break

            if 200 <= resp.status_code < 300:
                if attempt > 1:
                    log.info(
                        "Discord notify succeeded on attempt %d/%d",
                        attempt, self.MAX_ATTEMPTS,
                    )
                return True

            last_status = resp.status_code
            if resp.status_code in self.RETRYABLE_STATUS and attempt < self.MAX_ATTEMPTS:
                # Honor Retry-After (seconds) if Discord sent one (mostly on 429).
                retry_after = self._parse_retry_after(resp)
                delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
                log.warning(
                    "Discord webhook returned %d (attempt %d/%d) — retrying in %.1fs",
                    resp.status_code, attempt, self.MAX_ATTEMPTS, delay,
                )
                self._sleep(delay)
                continue

            # Non-retryable (4xx other than 429), or exhausted retries
            log.warning(
                "Discord webhook returned %d: %s",
                resp.status_code, resp.text[:200],
            )
            return False

        if last_status is not None:
            log.warning(
                "Discord notify failed after %d attempts — last status %d",
                self.MAX_ATTEMPTS, last_status,
            )
        elif last_exc is not None:
            log.warning(
                "Discord notify failed after %d attempts — %s",
                self.MAX_ATTEMPTS, last_exc,
            )
        return False

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with 0–25% jitter: 1s, 2s, 4s base (capped)."""
        base = min(self.BACKOFF_BASE_S * (2 ** (attempt - 1)), self.BACKOFF_MAX_S)
        return base * (1.0 + random.uniform(0.0, 0.25))

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> Optional[float]:
        """Parse `Retry-After` header. Discord sends seconds as a number.
        Caps at BACKOFF_MAX_S so a runaway header doesn't stall the engine."""
        raw = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        if not raw:
            return None
        try:
            return min(float(raw), DiscordNotifier.BACKOFF_MAX_S)
        except (TypeError, ValueError):
            return None

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
