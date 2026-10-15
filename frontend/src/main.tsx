import React from "react";
import ReactDOM from "react-dom/client";
import { registerSW } from "virtual:pwa-register";

import App from "./App";

/*
 * Latin faces, self-hosted (bundled from npm — no Google Fonts request, which
 * is unreliable from China). Each file carries only the Latin subset with a
 * unicode-range, so Chinese text falls through to PingFang / 微软雅黑 and the
 * mixed setting stays consistent.
 *
 *   Inter               — the interface sans, all controls and body copy
 *   Cormorant Garamond  — the display serif: wordmark, titles, welcome line
 */
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/cormorant-garamond/500.css";
import "@fontsource/cormorant-garamond/600.css";

import "./styles/index.css";

registerSW({ immediate: true });

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
