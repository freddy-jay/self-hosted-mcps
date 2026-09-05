# Universal MCP runtime: node + python + uv + the stdio->HTTP bridge.
ARG NODE_IMAGE=docker.io/library/node:22-bookworm-slim
FROM ${NODE_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ca-certificates curl python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY package.json package-lock.json /opt/mcps-bridge/
RUN npm ci --prefix /opt/mcps-bridge --omit=dev --ignore-scripts

COPY gateway.js /usr/local/lib/mcps-gateway.js

ENV UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_PORT=8080 \
    PATH=/opt/mcps-bridge/node_modules/.bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /app
