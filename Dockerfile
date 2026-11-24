FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev
USER nobody
CMD ["/app/.venv/bin/sandcastle-liquidator", "run"]
