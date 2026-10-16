import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const API_TARGET = process.env.API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["icons/icon-192.png", "icons/icon-512.png"],
      manifest: {
        name: "Vallotton",
        short_name: "Vallotton",
        description: "Vallotton — AI 绘图工作室",
        lang: "zh-CN",
        display: "standalone",
        orientation: "portrait",
        theme_color: "#151a24",
        background_color: "#151a24",
        icons: [
          { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "/icons/icon-maskable-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
      workbox: {
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/admin/],
        runtimeCaching: [
          {
            // Thumbnails: what the transcript actually draws. Small (~80 KB),
            // so a lot of them fit — and fitting is the point. These used to
            // share one 300-entry bucket with the full pictures, which at
            // 1.3 MB apiece came to roughly 420 MB; a phone will not keep that
            // and evicts the lot, which is why every visit downloaded them all
            // over again. At this size the same number is about 24 MB.
            urlPattern: /^https?:\/\/[^/]+\/api\/media\/[^/]+\/thumb$/,
            handler: "CacheFirst",
            options: {
              cacheName: "vallotton-thumbs",
              expiration: { maxEntries: 1200, maxAgeSeconds: 60 * 60 * 24 * 180 },
              cacheableResponse: { statuses: [200] },
            },
          },
          {
            // Full pictures: only the ones actually opened, and few of them,
            // so that opening one large image cannot evict the whole grid.
            urlPattern: /^https?:\/\/[^/]+\/api\/media\//,
            handler: "CacheFirst",
            options: {
              cacheName: "vallotton-media",
              expiration: { maxEntries: 40, maxAgeSeconds: 60 * 60 * 24 * 30 },
              cacheableResponse: { statuses: [200] },
            },
          },
          {
            urlPattern: /^https?:\/\/[^/]+\/api\//,
            handler: "NetworkOnly",
          },
        ],
      },
    }),
  ],
  server: {
    // A second dev pair can run alongside the first — VITE_PORT / API_TARGET —
    // so the e2e suites get their own frontend and their own database instead
    // of rearranging the one being used.
    port: Number(process.env.VITE_PORT ?? 5173),
    strictPort: true,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: false },
      "/admin": { target: API_TARGET, changeOrigin: false },
      "/django-static": { target: API_TARGET, changeOrigin: false },
    },
  },
  build: {
    sourcemap: false,
  },
});
