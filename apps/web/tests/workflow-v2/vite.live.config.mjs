import { defineConfig } from "../../node_modules/vite/dist/node/index.js";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
const root = path.dirname(fileURLToPath(import.meta.url));
const target = process.env.C4C5_API;
if (!target || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(target))
  throw new Error("Explicit loopback API required");
export default defineConfig({
  root,
  plugins: process.env.C4C5_PREVIEW_SESSION
    ? [
        {
          name: "local-test-session",
          configureServer(server) {
            server.middlewares.use((req, res, next) => {
              if (req.url === "/__test_status") {
                const status = path.join(path.dirname(process.env.C4C5_PREVIEW_SESSION), "live-status.json");
                res.setHeader("Content-Type", "application/json");
                res.setHeader("Cache-Control", "no-store");
                res.end(fs.readFileSync(status, "utf8"));
                return;
              }
              if (req.url !== "/" && req.url !== "/full-plane") return next();
              const session = JSON.parse(fs.readFileSync(process.env.C4C5_PREVIEW_SESSION, "utf8"));
              res.setHeader("Set-Cookie", `${session.cookie_name}=${session.session}; Path=/; HttpOnly; SameSite=Lax`);
              res.statusCode = 302;
              res.setHeader(
                "Location",
                req.url === "/full-plane"
                  ? `http://127.0.0.1:3000/${encodeURIComponent(session.workspace)}/projects/${encodeURIComponent(session.project)}/issues/`
                  : `/live.html?workspace=${encodeURIComponent(session.workspace)}&project=${encodeURIComponent(session.project)}`
              );
              res.end();
            });
          },
        },
      ]
    : [],
  define: { "process.env": "{}" },
  resolve: { alias: { "@": path.resolve(root, "../../core") }, dedupe: ["react", "react-dom", "@headlessui/react"] },
  server: {
    host: "127.0.0.1",
    port: Number(process.env.C4C5_UI_PORT || 4329),
    strictPort: true,
    proxy: { "/api": { target, changeOrigin: true } },
  },
});
