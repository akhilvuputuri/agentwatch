# agentwatch server image: the Python app with the built UI, running as a non-root user.
# Base images are pinned by digest; update them deliberately.

FROM node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS ui
WORKDIR /src/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
# Vite writes the UI into ../src/agentwatch/static, which the Python package serves.
RUN mkdir -p /src/src/agentwatch && npm run build

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY --from=ui /src/src/agentwatch/static ./src/agentwatch/static
RUN uv sync --locked --no-dev --no-editable \
    && rm /usr/local/bin/uv \
    && useradd --system --uid 10001 --home-dir /data agentwatch \
    && mkdir /data && chown agentwatch /data
USER agentwatch
ENV PATH=/app/.venv/bin:$PATH AGENTWATCH_DB=/data/agentwatch.duckdb
VOLUME /data
EXPOSE 4318
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4318/healthz', timeout=4)"]
ENTRYPOINT ["agentwatch"]
CMD ["serve", "--host", "0.0.0.0"]
