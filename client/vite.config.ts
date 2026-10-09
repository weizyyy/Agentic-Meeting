import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时前端跑在 5173，/api 代理到 Pipecat 应用（默认 7860）。
// 生产时执行 `npm run build`，由 Python 端把 client/dist 当静态文件托管，不再需要代理。
const backend = process.env.AGENTIC_MEETING_BACKEND ?? "http://localhost:7860";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: true, secure: false },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    // Pipecat 的 WebRTC 传输把 daily-js 一并打进来，包体约 700 KB；局域网使用，不做拆包。
    chunkSizeWarningLimit: 1000,
  },
});
