import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the API runs on :8000; Vite proxies to it so the app uses relative URLs everywhere.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://127.0.0.1:8000", "/media": "http://127.0.0.1:8000" },
  },
});
