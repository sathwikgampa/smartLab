import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const REACT_VENDOR = /[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/;

// The dev server proxies /api to the Flask backend, so the browser sees one origin.
export default defineConfig({
  plugins: [react()],
  build: {
    target: "es2020",
    // Tightened from 800 kB to just above the largest lazy chunk (Markdown + KaTeX, ~435 kB), so a new
    // heavy dependency shows up in the build output.
    chunkSizeWarningLimit: 450,
    rollupOptions: {
      output: {
        // React changes rarely; its own chunk stays cached across app deploys. Markdown/KaTeX and Recharts are
        // already split out by their dynamic imports; forcing them into named chunks here would pull their
        // shared dependencies (react-is, clsx, ...) into the eagerly loaded graph.
        manualChunks(id) {
          if (REACT_VENDOR.test(id)) return "react";
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: { "/api": { target: process.env.SMARTLAB_API ?? "http://127.0.0.1:5000", changeOrigin: true } },
  },
});
