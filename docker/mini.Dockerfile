ARG BASE_IMAGE=python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377
FROM ${BASE_IMAGE}

# Alpine needs Python and Bash; keep the musl base
RUN if [ -f /etc/alpine-release ]; then apk add --no-cache bash python3; fi

COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    MSWEA_SILENT_STARTUP=1 \
    MSWEA_GLOBAL_CONFIG_DIR=/tmp/mini-swe-agent \
    PATH="/opt/runtime/.venv/bin:${PATH}"

WORKDIR /opt/runtime
COPY pyproject.toml uv.lock ./
COPY agents/mini-swe-agent/ ./agents/mini-swe-agent/
# Install locked agent dependencies without installing the harness
RUN uv sync --locked --no-install-project --no-dev --python python3 && uv pip check

COPY docker/smoke_test.py ./smoke_test.py
WORKDIR /workspace
CMD ["mini", "--help"]
