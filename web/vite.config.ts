import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __GROK2API_DEV_API_TARGET__: JSON.stringify(process.env.VITE_DEV_API_TARGET ?? ""),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 43188,
    proxy: {
      "/api": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:43187",
      "/v1": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:43187",
      "/healthz": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:43187",
      "/readyz": process.env.VITE_DEV_API_TARGET ?? "http://127.0.0.1:43187",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
