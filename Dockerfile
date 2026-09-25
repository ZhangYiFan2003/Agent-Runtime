FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra postgres --no-install-project

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres --no-editable

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 axiom \
    && useradd --uid 10001 --gid axiom --create-home --home-dir /home/axiom axiom \
    && mkdir -p /workspace /var/lib/axiom \
    && chown -R axiom:axiom /workspace /var/lib/axiom

WORKDIR /workspace
COPY --from=builder --chown=axiom:axiom /app/.venv /app/.venv

USER axiom
ENTRYPOINT ["axiom"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080", "--task-workers", "0", "--cwd", "/workspace", "--data-dir", "/var/lib/axiom"]
