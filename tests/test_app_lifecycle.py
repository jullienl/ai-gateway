"""Tests for selecting the AI gateway's startup provider."""

from __future__ import annotations

import asyncio

import app


class _FakeProvider:
    def __init__(self, name: str):
        self.name = name
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def test_lifespan_starts_configured_provider_without_copilot(monkeypatch):
    provider = _FakeProvider("openai")
    created: list[str] = []

    monkeypatch.setenv("AI_PROVIDER", "openai")

    def fake_create_provider(name: str):
        created.append(name)
        return provider

    monkeypatch.setattr(app, "create_provider", fake_create_provider)
    app.state.providers.clear()

    async def exercise_lifespan():
        async with app.lifespan(app.app):
            assert created == ["openai"]
            assert provider.started is True
            assert app.state.providers == {"openai": provider}

    asyncio.run(exercise_lifespan())
    assert provider.stopped is True
    assert app.state.providers == {}