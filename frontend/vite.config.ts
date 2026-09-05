import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite serves the SPA on :5173 and proxies API calls to FastAPI on :8000.
const API_PATHS = [
  "/ask", "/projects", "/starters", "/conversations", "/conversation",
  "/new", "/delete", "/delete_brd", "/rename_brd", "/upload", "/health",
];

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    proxy: Object.fromEntries(
      API_PATHS.map((p) => [p, { target: "http://localhost:8000", changeOrigin: true }]),
    ),
  },
});
