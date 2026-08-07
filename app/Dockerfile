# syntax=docker/dockerfile:1

# Q-Agent frontend.
#
# Stage 1 builds the Vite/React SPA; stage 2 serves the static bundle with nginx
# on :5174. nginx also reverse-proxies the same-origin /api, /auth and websocket
# paths to the api container (nginx.conf), so the whole app stays one origin
# behind a single Cloudflare tunnel — exactly like the Vite dev proxy.
FROM node:20-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Where the app is mounted in the URL. "/" standalone — on its own hostname it
# owns the root. Behind the suite's shared front door, build with
# `--build-arg VITE_BASE=/qagent/` so every asset URL, the router basename and
# the /api + /auth prefixes come out prefixed. It must match the backend's
# QAGENT_MOUNT_PATH, or the refresh cookie is scoped to a path the browser never
# requests.
#
# A build arg rather than runtime config because Vite bakes `base` into the
# bundle: the emitted `<script src>` in index.html is fixed at build time and
# there is nothing left to configure afterwards.
ARG VITE_BASE=/
ENV VITE_BASE=$VITE_BASE
# VITE_API_BASE is intentionally left unset → the client defaults to the
# same-origin "/api" prefix (under VITE_BASE) that nginx proxies to the backend.
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 5174
