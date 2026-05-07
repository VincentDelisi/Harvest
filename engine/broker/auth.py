"""Public.com access-token manager.

Public uses a stateful auth flow:
    POST /userapiauthservice/personal/access-tokens
        body: { "secret": "...", "validityInMinutes": 60 }
        returns: { "accessToken": "..." }

Tokens are short-lived (minutes to ~1 hour). This module mints a token,
caches it in memory, and refreshes proactively before expiry.

Source: https://public.com/api/docs/templates/get-access-token
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from engine.utils.config import CONFIG
from engine.utils.logging import get_logger

log = get_logger(__name__)

# Default token lifetime; we refresh ~60s before expiry.
DEFAULT_VALIDITY_MIN = 60
REFRESH_LEEWAY_SEC = 90

_AUTH_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _is_retryable_auth(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _AUTH_RETRYABLE_STATUS
    return isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError))


@dataclass
class _CachedToken:
    token: str
    expires_at_epoch: float


class TokenManager:
    """Thread-safe access-token cache with proactive refresh."""

    def __init__(
        self,
        secret: str | None = None,
        base_url: str | None = None,
        validity_minutes: int = DEFAULT_VALIDITY_MIN,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._secret = secret or CONFIG.public_secret
        self._base = base_url or CONFIG.public_base_url
        self._validity_min = validity_minutes
        self._transport = transport  # injectable for tests
        self._lock = threading.Lock()
        self._cached: _CachedToken | None = None

    def get_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._cached is None or self._cached.expires_at_epoch - now < REFRESH_LEEWAY_SEC:
                self._cached = self._mint()
            return self._cached.token

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.1, max=2))
    def _mint(self) -> _CachedToken:
        if not self._secret:
            raise RuntimeError(
                "PUBLIC_COM_SECRET is not set in .env; cannot mint access token."
            )
        log.info("Minting Public access token (validity=%d min)", self._validity_min)
        client_kwargs: dict = {"timeout": 15.0}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(
                f"{self._base}/userapiauthservice/personal/access-tokens",
                json={
                    "validityInMinutes": self._validity_min,
                    "secret": self._secret,
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            payload = resp.json()
        token = payload.get("accessToken")
        if not token:
            raise RuntimeError(f"Public auth response missing accessToken: {payload}")
        expires_at = time.time() + (self._validity_min * 60)
        return _CachedToken(token=token, expires_at_epoch=expires_at)
