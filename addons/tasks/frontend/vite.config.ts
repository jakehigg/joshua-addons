import { defineConfig } from "vite";

// Placeholder. The real UI and the React plugin arrive in P5.5.
export default defineConfig({
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
