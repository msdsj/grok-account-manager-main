import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backendTarget = "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true,
      },
      "/v1": {
        target: backendTarget,
        changeOrigin: true,
      },
      "/admin/api": {
        target: backendTarget,
        changeOrigin: true,
      },
    },
  },
});
