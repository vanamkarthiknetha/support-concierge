FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies first so the layer caches across code changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

EXPOSE 8010
CMD ["uv", "run", "uvicorn", "concierge.api.main:app", "--host", "0.0.0.0", "--port", "8010"]
