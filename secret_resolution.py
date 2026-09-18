"""File-first secret resolution, shared by every provider.

A value is resolved `NAME_FILE` (read the file, strip a trailing newline) ->
`NAME` (plain env var) -> `default`. A file-backed secret is the safe
production default (it doesn't leak via `docker inspect` or
`/proc/<pid>/environ`, and is what any secret manager projects into a
container as); the plain env var is the dev-convenience fallback.

Named `secret_resolution` rather than `secrets` so it never shadows the stdlib
`secrets` module for any code running from this directory.
"""

from __future__ import annotations

import os


def get_secret(name: str, *, default: str | None = None) -> str | None:
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        with open(file_path, encoding="utf-8") as handle:
            return handle.read().rstrip("\n")
    value = os.environ.get(name)
    if value is not None:
        return value
    return default
