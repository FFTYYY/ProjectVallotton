import React from "react";
import ReactDOM from "react-dom/client";
import { registerSW } from "virtual:pwa-register";

import App from "./App";
import { useLanguage } from "./i18n/language";

/*
 * Latin faces, self-hosted (bundled from npm — no Google Fonts request, which
 * is unreliable from China). Each file carries only the Latin subset with a
 * unicode-range, so Chinese text falls through to PingFang / 微软雅黑 and the
 * mixed setting stays consistent.
 *
 *   Inter               — the interface sans, all controls and (in Chinese) body copy
 *   Cormorant Garamond  — the display serif: wordmark, titles, welcome line
 *   Source Serif 4      — the text serif the conversation is set in when the
 *                         app speaks English (tokens.css: --font-prose)
 *   JetBrains Mono      — tag lines, prompts, code
 */
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/cormorant-garamond/500.css";
import "@fontsource/cormorant-garamond/600.css";
import "@fontsource/source-serif-4/400.css";
import "@fontsource/source-serif-4/400-italic.css";
import "@fontsource/source-serif-4/600.css";
import "@fontsource/jetbrains-mono/400.css";

import "./styles/index.css";

registerSW({ immediate: true });

/*
 * Keyed by the language: components read `t.word` while rendering and
 * subscribe to nothing, so a change of language remounts the app and every
 * word is read again. The stores live outside React and keep their data.
 */
function Root() {
  const language = useLanguage((s) => s.language);
  return <App key={language} />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
