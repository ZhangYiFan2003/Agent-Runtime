import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  // Dev-only proxy target for the local Axiom Runtime. Override with the
  // AXIOM_RUNTIME_TARGET environment variable (see README). Never a secret:
  // this value lives in the dev server, not in the browser bundle.
  const env = loadEnv(mode, process.cwd(), "");
  const runtimeTarget = env.AXIOM_RUNTIME_TARGET || "http://127.0.0.1:8080";

  return {
    plugins: [react(), tailwindcss()],
    server: {
      proxy: {
        "/v1": { target: runtimeTarget, changeOrigin: true },
        "/health": { target: runtimeTarget, changeOrigin: true },
      },
    },
    test: {
      environment: "node",
      include: ["src/**/*.test.ts"],
    },
  };
});
