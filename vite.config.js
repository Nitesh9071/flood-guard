import { defineConfig } from "vite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: resolve(projectRoot, "frontend"),
  base: "./",
  define: { __FLOODGUARD_NATIVE_APP__: "true" },
  build: {
    outDir: resolve(projectRoot, "mobile", "www"),
    emptyOutDir: true,
    sourcemap: false
  }
});
