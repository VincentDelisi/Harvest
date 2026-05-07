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
        "https://example.com/wh", transport=httpx.MockTransport(handler)
    )
    # Must NOT raise — notification failure cannot block trading
    assert n.error("kill switch", "test") is False


def test_discord_swallows_network_errors():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    n = DiscordNotifier(
        "https://example.com/wh", transport=httpx.MockTransport(handler)
    )
    assert n.warn("test") is False


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
