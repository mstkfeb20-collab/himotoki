import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = process.env.CODEGRAPH_API || "http://localhost:8765";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/ws": { target: target.replace("http", "ws"), ws: true },
    },
  },
});
