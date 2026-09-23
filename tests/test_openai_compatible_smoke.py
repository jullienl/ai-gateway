"""End-to-end gateway test using the development OpenAI-compatible server."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

import app
from dev.mock_openai_server import create_app as create_mock_app
from providers.openai_provider import OpenAIProvider


def test_agent_runs_through_openai_compatible_endpoint(monkeypatch):
    async def exercise():
        mock_transport = httpx.ASGITransport(app=create_mock_app())
        mock_client = httpx.AsyncClient(transport=mock_transport, base_url="http://mock/v1")

        monkeypatch.setenv("AI_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "http://mock/v1")
        provider = OpenAIProvider()

        def create_test_client(**kwargs):
            return AsyncOpenAI(**kwargs, http_client=mock_client)

        monkeypatch.setattr(app, "create_provider", lambda name: provider)
        app.state.providers.clear()

        with patch("openai.AsyncOpenAI", side_effect=create_test_client):
            async with app.lifespan(app.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app.app),
                    base_url="http://gateway",
                ) as gateway_client:
                    response = await gateway_client.post(
                        "/agent/com-rca",
                        json={
                            "input": {"event": {"title": "Mock event"}},
                            "model": "openai:mock-model",
                        },
                    )

        await mock_client.aclose()

        assert response.status_code == 200
        assert response.json()["result"] == {
            "summary": "Mock analysis",
            "likely_root_cause": "Mock root cause",
            "evidence": "Mock evidence",
            "confidence": 0.9,
            "recommended_actions": ["Mock action"],
        }

    asyncio.run(exercise())