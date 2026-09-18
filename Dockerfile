# AI Gateway — container image.
#
# Build context is the repository root.
#
#   docker build -t ghcr.io/<your-org>/ai-gateway:1.0.1 .
#
# Pin a version tag rather than :latest — a tag records WHICH BUILD shipped, and
# a managed runtime will not reliably re-pull a mutated tag. Changed the source?
# push a new, unused tag.
#
# IMPORTANT: the base image must be glibc (Debian slim), NOT Alpine/musl. The
# Copilot SDK loads a native `runtime.node` addon that is built against glibc.

FROM python:3.13-slim

WORKDIR /app

# TLS roots for the SDK's HTTPS calls (model API + runtime download).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the Copilot CLI runtime at BUILD time into a fixed cache dir, so
# the container starts fast and does not need GitHub Releases egress at runtime.
# COPILOT_CLI_EXTRACT_DIR must be set identically at build and run time (the SDK
# uses it for both writing and lookup).
ENV COPILOT_CLI_EXTRACT_DIR=/opt/copilot-cli
RUN python -m copilot download-runtime --in-process \
    && chmod -R a+rX /opt/copilot-cli

# Application code.
COPY app.py agents.py secret_resolution.py ./
COPY providers ./providers

# Run as a non-root user.
RUN useradd --create-home --uid 10001 gateway
USER gateway

EXPOSE 8000

# Auth in a container is headless. Copilot (default provider): set
# COPILOT_GITHUB_TOKEN (or _FILE) to a per-user fine-grained PAT. OpenAI /
# Anthropic providers: set OPENAI_API_KEY(_FILE) / ANTHROPIC_API_KEY(_FILE).
# See GUIDE.md section 9 and README.md's "Providers" section.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
