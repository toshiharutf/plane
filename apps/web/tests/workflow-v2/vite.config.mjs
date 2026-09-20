import { defineConfig } from "../../node_modules/vite/dist/node/index.js";
import path from "node:path";
import { fileURLToPath } from "node:url";
const root = path.dirname(fileURLToPath(import.meta.url));
export default defineConfig({
  root,
  define: { "process.env": "{}" },
  resolve: { alias: { "@": path.resolve(root, "../../core") }, dedupe: ["react", "react-dom", "@headlessui/react"] },
  server: { host: "127.0.0.1", port: 4317, strictPort: true },
  build: { outDir: "/tmp/plane-delivery-fixture-build", emptyOutDir: true },
});
