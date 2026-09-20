# syntax=docker/dockerfile:1

# Q-Agent backend (FastAPI).
#
# Bakes in Python 3.13 + uv, the Node 20 runtime + the Claude Code CLI (AI runs
# server-side; auth comes from in-app uploaded credentials, not the host), and
# git (project-bootstrap clones application repos). It also ships chromium +
# browser-harness AND Playwright's own `cli` subcommand (#875, `browserDriver`
# setting) so live spec-authoring (#400) can drive a real browser server-side
# either way. It still ships NO `@playwright/test` — spec *execution* is offloaded
# to the paired Local Agent (set Execution target = Local Agent).
#
# Build context is the REPO ROOT (see docker-compose.yml) so it can also copy
# the dedicated `skills/` the backend injects as Claude system prompts.
FROM python:3.13-slim-bookworm

# uv (pinned to match the uv.lock format, revision 3) — fast, reproducible
# installs straight from api/uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11.13 /uv /uvx /usr/local/bin/

# Node 20 runtime copied from the official image (the Claude Code CLI is a Node
# package), plus git + ca-certificates for repo cloning and outbound HTTPS.
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates chromium \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @anthropic-ai/claude-code \
 && npm cache clean --force

# browser-harness CLI (#400 live-authoring): Claude drives it against a real
# browser to author specs. Installed to /usr/local/bin so it is on PATH; the
# server launches a headless chromium (QAGENT_CHROME_BIN) for it to attach to
# over CDP via BU_CDP_URL. Isolated venv via uv tool — no clash with app deps.
RUN UV_TOOL_BIN_DIR=/usr/local/bin uv tool install browser-harness

# Playwright's own scriptable `cli` subcommand (#875) — the alternative
# `browserDriver` to browser-harness: Claude drives real locators/refs directly
# instead of raw CDP coordinate clicks. `playwright` (>= 1.63, which ships the
# `cli` subcommand) is installed globally alongside the Node runtime above, into
# the SAME global node_modules Claude Code just landed in — `playwright_cli_js`
# (app/config.py) resolves `<QAGENT_PLAYWRIGHT_NODE_MODULES>/playwright/cli.js`
# from it, so live_authoring_service can `node` it directly with no PATH shim.
RUN npm install -g playwright@^1.63.0 \
 && npm cache clean --force

ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/api/.venv \
    PATH="/app/api/.venv/bin:${PATH}" \
    QAGENT_PLAYWRIGHT_NODE_MODULES=/usr/local/lib/node_modules

WORKDIR /app/api

# Install Python deps first so this layer is cached until the lockfile changes.
# `[tool.uv] package = false` means only dependencies are installed (no project
# build), so the lock files alone are enough here.
COPY api/pyproject.toml api/uv.lock ./
RUN uv sync --frozen --no-dev

# Backend source (includes alembic.ini + migrations/, applied at boot) and the
# dedicated Q-Agent skills. api/.dockerignore keeps .venv/workspace/.env out.
COPY api/ ./
COPY skills/ /app/skills/

# Bind on all interfaces so the web (nginx) container can reach the API. Secrets,
# auth flags and the admin seed are supplied via the environment (compose).
ENV QAGENT_HOST=0.0.0.0 \
    QAGENT_PORT=8787 \
    QAGENT_CHROME_BIN=/usr/bin/chromium

EXPOSE 8787
# `python -m uvicorn` puts the working dir on sys.path so `app.main` imports.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
