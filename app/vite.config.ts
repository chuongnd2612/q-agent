import { defineConfig, type PluginOption } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import fs from "node:fs";
import path from "node:path";

// Where this app is mounted in the URL. `/` standalone — on its own hostname it
// owns the root, and that stays the default so nothing changes unless a
// deployment asks otherwise. Behind the suite's shared front door it is served
// under a prefix (`VITE_BASE=/qagent/`), and the router `basename`, every asset
// URL and the API/auth prefixes all derive from this one value at runtime via
// `import.meta.env.BASE_URL`.
//
// Normalised to a trailing slash because Vite concatenates it with asset paths:
// `/qagent` would emit `/qagentassets/index.js`.
const BASE = (() => {
  const raw = (process.env.VITE_BASE || "/").trim();
  return raw.endsWith("/") ? raw : `${raw}/`;
})();

// Dev-only stopgap: serve the Local Agent installers from the repo `downloads/`
// dir at `/downloads/*`, mirroring the production nginx route. Lets the dev
// server (when it's what a Cloudflare tunnel fronts) hand out the installer;
// in real production the docker `web`/nginx container serves this instead.
function serveDownloads(base: string): PluginOption {
  const dir = path.resolve(__dirname, "../downloads");
  // Mirrors the production route, which sits under whatever prefix the app is
  // mounted at — so this has to follow `base` rather than assume the root.
  const prefix = `${base.replace(/\/$/, "")}/downloads/`;
  return {
    name: "serve-downloads",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url || !req.url.startsWith(prefix)) return next();
        const rel = decodeURIComponent(req.url.split("?")[0].slice(prefix.length));
        const filePath = path.join(dir, rel);
        if (filePath !== dir && !filePath.startsWith(dir + path.sep)) {
          res.statusCode = 403;
          return res.end("Forbidden");
        }
        fs.stat(filePath, (err, st) => {
          if (err || !st.isFile()) {
            res.statusCode = 404;
            return res.end("Not found");
          }
          res.setHeader("Content-Type", "application/octet-stream");
          res.setHeader("Content-Disposition", "attachment");
          res.setHeader("Content-Length", String(st.size));
          fs.createReadStream(filePath).pipe(res);
        });
      });
    },
  };
}

// https://vite.dev/config/
export default defineConfig({
  base: BASE,
  plugins: [react(), tailwindcss(), serveDownloads(BASE)],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // Same-origin proxy so `/auth/*` calls carry the httpOnly refresh + CSRF
  // cookies in dev (ADR 0007). Everything else hits API_BASE directly with a
  // bearer token, so only auth + its websockets are proxied here.
  server: {
    // Hosts allowed to reach the dev server (Vite's DNS-rebinding guard).
    // Needed when the dev server is fronted by a tunnel/custom domain
    // (e.g. a Cloudflare tunnel). Use a leading dot to allow all subdomains.
    allowedHosts: ["qagent.chuongnd.click", ".chuongnd.click"],
    proxy: {
      // Same-origin API access. The frontend calls `/api/*` (see API_BASE in
      // lib/api.ts) and Vite forwards to the backend with the `/api` prefix
      // stripped. This keeps everything one origin — so it works behind a
      // single tunnel with no CORS — and the `/api` prefix avoids colliding
      // with the SPA's own client routes (`/runs`, `/projects`, …). `ws: true`
      // also carries the `/api/ws/*` websockets.
      "/api": {
        target: "http://127.0.0.1:8787",
        changeOrigin: true,
        ws: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      // Auth stays same-origin on its own path so the httpOnly refresh + CSRF
      // cookies flow (ADR 0007).
      "/auth": { target: "http://127.0.0.1:8787", changeOrigin: true },
    },
    port: 5173
  },
});
