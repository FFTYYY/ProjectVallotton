/**
 * Every sentence the browser shows, in the language the account chose.
 *
 * `t.logout` is the Chinese or the English word according to the language
 * store at the moment it is read. The words themselves are in uiText.zh.ts
 * and uiText.en.ts, one module per language with the same keys; this module
 * only looks a key up in the right one. Components read `t.key` where they
 * use it, never at module level: a `const X = t.key` outside a function is
 * one language for ever.
 *
 * The type of every table is derived from the Chinese one with the literal
 * types widened: a key the English table lacks, or adds, fails to compile.
 *
 * Adding a language: an entry in src/i18n/languages.ts, a uiText.<code>.ts
 * with every key, and its line in TABLES below. tests/i18n/uiText.test.ts
 * says what is missing.
 */

import { useLanguage } from "./i18n/language";
import type { LanguageCode } from "./i18n/languages";
import { en } from "./uiText.en";
import { zh } from "./uiText.zh";

/** the literal types of the Chinese table, widened to what any language fills */
type Widen<T> = T extends string
  ? string
  : T extends readonly string[]
    ? readonly string[]
    : T extends (...args: infer A) => unknown
      ? (...args: A) => string
      : T extends object
        ? { readonly [K in keyof T]: Widen<T[K]> }
        : T;

export type UiText = Widen<typeof zh>;

export const TABLES: Readonly<Record<LanguageCode, UiText>> = { zh, en };

/**
 * What `key` says in each language, for recognising something written down
 * earlier: a conversation titled 新对话 is still untitled after the switch
 * to English.
 */
export function inEveryLanguage<K extends keyof UiText>(key: K): UiText[K][] {
  return Object.values(TABLES).map((table) => table[key]);
}

function current(): UiText {
  return TABLES[useLanguage.getState().language];
}

export const t: UiText = new Proxy({} as UiText, {
  get: (_, key) => current()[key as keyof UiText],
  has: (_, key) => key in current(),
  ownKeys: () => Reflect.ownKeys(current()),
  getOwnPropertyDescriptor: (_, key) => {
    const value = current()[key as keyof UiText];
    return value === undefined ? undefined : { value, enumerable: true, configurable: true, writable: false };
  },
});
