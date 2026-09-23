"""Small development server that implements OpenAI Chat Completions."""

from __future__ import annotations

import argparse
from typing import Any

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="AI Gateway OpenAI-compatible mock")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "mock-completion",
            "object": "chat.completion",
            "created": 0,
            "model": body.get("model", "mock-model"),
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": (
                            '{"summary":"Mock analysis",'
                            '"likely_root_cause":"Mock root cause",'
                            '"evidence":"Mock evidence",'
                            '"confidence":0.9,'
                            '"recommended_actions":["Mock action"]}'
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11434)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)