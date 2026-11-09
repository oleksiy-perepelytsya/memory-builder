# Memory Builder: FastAPI web app with a private OpenCode server.
#
# The container talks to a BaryGraph MCP endpoint and to OpenCode providers.
# Runtime state lives in mounted volumes (see docker-compose.yml); the
# committed image stays clean.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/root \
    PATH="/root/.opencode/bin:${PATH}" \
    MEMORY_BUILDER_HOST=0.0.0.0 \
    MEMORY_BUILDER_PORT=8765

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        tar \
    && rm -rf /var/lib/apt/lists/*

# Install the OpenCode v2 CLI. Set OPENCODE_VERSION (for example 2.1.2) when a
# reproducible build is needed; the default tracks the latest v2 release.
ARG OPENCODE_VERSION=""
RUN if [ -n "$OPENCODE_VERSION" ]; then \
        curl -fsSL https://opencode.ai/v2/install | bash -s -- --version "$OPENCODE_VERSION"; \
    else \
        curl -fsSL https://opencode.ai/v2/install | bash; \
    fi

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x run.sh docker/entrypoint.sh

EXPOSE 8765

CMD ["/app/docker/entrypoint.sh"]
