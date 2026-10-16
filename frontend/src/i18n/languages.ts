/**
 * The languages the browser can speak. The registry and nothing else: the
 * words are in src/uiText.<code>.ts, the choice is in ./language.ts.
 *
 * The codes are the backend's (backend/core/i18n.py); a test holds the two
 * lists to each other. The label is how a language names itself, which is
 * why it is here and not in the text tables: a switch to English is labelled
 * "English" whatever the app is speaking now.
 */

export interface Language {
  code: "zh" | "en";
  /** the language's own name for itself */
  label: string;
  /** what `<html lang>` says while it is spoken */
  htmlLang: string;
}

export const LANGUAGES: readonly Language[] = [
  { code: "zh", label: "中文", htmlLang: "zh-CN" },
  { code: "en", label: "English", htmlLang: "en" },
];

export type LanguageCode = Language["code"];

/** what the browser speaks until it hears otherwise */
export const DEFAULT_LANGUAGE: LanguageCode = "zh";

/** where the last language heard is kept, so the first paint speaks it */
export const LANGUAGE_HINT_KEY = "vallotton.language";

export function isLanguage(code: unknown): code is LanguageCode {
  return typeof code === "string" && LANGUAGES.some((l) => l.code === code);
}

export function languageOf(code: LanguageCode): Language {
  return LANGUAGES.find((l) => l.code === code) ?? LANGUAGES[0];
}

/** the language after `code` in the registry, wrapping round: what one switch press goes to */
export function nextLanguage(code: LanguageCode): Language {
  const at = LANGUAGES.findIndex((l) => l.code === code);
  return LANGUAGES[(at + 1) % LANGUAGES.length];
}
