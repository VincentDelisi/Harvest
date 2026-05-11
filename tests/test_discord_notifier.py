"""Discord notifier — mocked HTTP, never blocks on failure."""
from __future__ import annotations

import httpx

from engine.notify.discord import (
    COLOR_GREEN,
    COLOR_RED,
    DiscordNotifier,
    NullNotifier,
    build_notifier,
)


def test_null_notifier_is_noop():
    n = NullNotifier()
    assert n.send("t") is True
    assert n.success("t") is True
    assert n.error("t") is True


def test_discord_send_includes_embed_and_color():
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append({
            "url": str(req.url),
            "json": __import__("json").loads(req.content),
        })
        return httpx.Response(204)

    n = DiscordNotifier(
        "https://discord.com/api/webhooks/123/abc",
        transport=httpx.MockTransport(handler),
    )
    ok = n.success("Filled — SPY PUT", "credit 0.40", fields=[{"name": "Qty", "value": "1"}])
    assert ok is True
    assert len(captured) == 1
    body = captured[0]["json"]
    assert body["username"] == "Harvest"
    embed = body["embeds"][0]
    assert embed["title"] == "Filled — SPY PUT"
    assert embed["color"] == COLOR_GREEN
    assert embed["fields"][0]["name"] == "Qty"


def test_discord_swallows_5xx_errors():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,  # don't actually sleep during retry
    )
    # Must NOT raise — notification failure cannot block trading
    assert n.error("kill switch", "test") is False


def test_discord_swallows_network_errors():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    assert n.warn("test") is False


# ----- Retry-with-backoff on transient failures ----------------------------


def test_retry_succeeds_after_transient_503():
    """Two 503s then a 204 — retry must recover the message.

    This is the exact failure mode we saw Friday 4 PM ET on the EOD ping.
    """
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="no healthy upstream")
        return httpx.Response(204)

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    assert n.info("recovers") is True
    assert calls["n"] == 3  # 1st attempt + 2 retries


def test_retry_gives_up_after_max_attempts():
    """All 3 attempts return 503 — returns False, does not raise, does not
    retry forever."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="down")

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    assert n.error("persistent") is False
    assert calls["n"] == DiscordNotifier.MAX_ATTEMPTS


def test_retry_does_not_retry_on_4xx():
    """Client errors (e.g. 400 bad payload, 401 bad token) must NOT retry —
    they're our bug and retrying just spams Discord."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    assert n.info("bad") is False
    assert calls["n"] == 1  # one attempt, no retries


def test_retry_honors_retry_after_on_429():
    """429 with Retry-After header — should retry AND use the header value
    (capped at BACKOFF_MAX_S)."""
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited", headers={"Retry-After": "3"})
        return httpx.Response(204)

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda s: sleeps.append(s),
    )
    assert n.warn("throttled") is True
    assert calls["n"] == 2
    assert sleeps == [3.0]  # honored the header exactly


def test_retry_caps_runaway_retry_after():
    """If Discord ever sends an absurd Retry-After like 3600, cap it so the
    engine doesn't stall for an hour."""
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "3600"})

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda s: sleeps.append(s),
    )
    assert n.warn("capped") is False
    # MAX_ATTEMPTS=3 → 2 sleeps between 3 attempts. Each capped at 8.0s.
    assert len(sleeps) == DiscordNotifier.MAX_ATTEMPTS - 1
    for s in sleeps:
        assert s <= DiscordNotifier.BACKOFF_MAX_S


def test_retry_recovers_from_transient_network_error():
    """First call raises ConnectError, second succeeds — retry covers
    transient network blips, not just HTTP-status failures."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(204)

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )
    assert n.info("recovers-from-network") is True
    assert calls["n"] == 2


def test_first_attempt_success_does_not_sleep():
    """Happy path — single 2xx response, no retry overhead."""
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    n = DiscordNotifier(
        "https://example.com/wh",
        transport=httpx.MockTransport(handler),
        sleep=lambda s: sleeps.append(s),
    )
    assert n.success("first try") is True
    assert sleeps == []  # never slept


def test_build_notifier_returns_null_when_no_webhook(monkeypatch):
    monkeypatch.setattr("engine.notify.discord.CONFIG.discord_webhook_url", "")
    n = build_notifier()
    assert isinstance(n, NullNotifier)


def test_build_notifier_returns_discord_when_webhook_set(monkeypatch):
    monkeypatch.setattr(
        "engine.notify.discord.CONFIG.discord_webhook_url",
        "https://discord.com/api/webhooks/x/y",
    )
    n = build_notifier()
    assert isinstance(n, DiscordNotifier)


def test_long_field_values_are_truncated():
    captured: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(__import__("json").loads(req.content))
        return httpx.Response(204)

    n = DiscordNotifier("https://x", transport=httpx.MockTransport(handler))
    huge = "x" * 5000
    n.info("title-" + huge, huge, fields=[{"name": "n-" + huge, "value": "v-" + huge}])
    embed = captured[0]["embeds"][0]
    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= 4096
    assert len(embed["fields"][0]["name"]) <= 256
    assert len(embed["fields"][0]["value"]) <= 1024
