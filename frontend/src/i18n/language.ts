import { create } from "zustand";

import {
  DEFAULT_LANGUAGE,
  LANGUAGE_HINT_KEY,
  isLanguage,
  languageOf,
  type LanguageCode,
} from "./languages";

/**
 * Which language the browser speaks right now.
 *
 * The choice belongs to the account: the backend keeps it
 * (AppSettings.language) and says it in /api/auth/me and /api/app-settings.
 * This store is the browser's copy of that answer, and `t` (src/uiText.ts)
 * reads from it. Nothing here talks to the server; the stores that do
 * (auth, appSettings) call `adopt` with what the server said, and `choose`
 * in appSettings writes a new choice back.
 *
 * One thing is kept in the browser: the last language heard, as a hint for
 * the first paint, so a reload does not flash Chinese before the server has
 * answered. The server's answer always wins over the hint.
 *
 * No dependency on src/uiText.ts, on purpose: that module reads this one.
 */

interface LanguageState {
  language: LanguageCode;
  /**
   * A choice made in this browser that the server has not confirmed yet.
   * While it stands, what a read says is ignored: the switch remounts the
   * app, the app asks the server who it is, and the server still says the
   * old language for a moment. Taking that answer made the screen flicker
   * between the two languages until the write landed.
   */
  pending: boolean;
  /** what the server says: speak it, unless a choice is pending */
  adopt: (code: unknown) => void;
  /** what the person chose: speak it now, and hold it until `settle` */
  choose: (code: LanguageCode) => void;
  /** the server's answer to the choice (or nothing, if the write failed) */
  settle: (code: unknown) => void;
}

function hinted(): LanguageCode {
  try {
    const raw = localStorage.getItem(LANGUAGE_HINT_KEY);
    return isLanguage(raw) ? raw : DEFAULT_LANGUAGE;
  } catch {
    return DEFAULT_LANGUAGE;
  }
}

function remember(code: LanguageCode): void {
  try {
    localStorage.setItem(LANGUAGE_HINT_KEY, code);
  } catch {
    /* refused or full: the next load starts from the default, then catches up */
  }
}

function markDocument(code: LanguageCode): void {
  if (typeof document === "undefined") return;
  document.documentElement.lang = languageOf(code).htmlLang;
}

export const useLanguage = create<LanguageState>((set, get) => {
  const speak = (code: unknown) => {
    if (!isLanguage(code)) return;
    if (code !== get().language) set({ language: code });
    remember(code);
    markDocument(code);
  };
  return {
    language: hinted(),
    pending: false,

    adopt: (code) => {
      if (get().pending) return;
      speak(code);
    },

    choose: (code) => {
      set({ pending: true });
      speak(code);
    },

    settle: (code) => {
      set({ pending: false });
      speak(code);
    },
  };
});

markDocument(useLanguage.getState().language);
