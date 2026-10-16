import { create } from "zustand";

/**
 * Which colours the app wears: the registry, the choice, and how it is
 * painted.
 *
 * The choice is the browser's, not the account's: it lives in localStorage,
 * and a phone and a desktop logged into the same account may differ. Dark
 * is the default; the system's preference is not consulted. The choice is
 * painted as `data-theme` on <html>, always, which is what
 * src/styles/tokens.css keys its palettes on; the inline script in
 * index.html paints it before the first frame so the page does not open in
 * the other theme.
 *
 * Adding a theme: an entry here, a `:root[data-theme="<id>"]` block in
 * tokens.css, a name in each uiText table's `themeNames`, and the inline
 * script in index.html if it should survive the first paint (a test holds
 * that script to this registry).
 */

export interface Theme {
  id: "light" | "dark";
  /** the colour of the browser's own bars around the page */
  barColor: string;
}

export const THEMES: readonly Theme[] = [
  { id: "light", barColor: "#eff2f8" },
  { id: "dark", barColor: "#151a24" },
];

export type ThemeId = Theme["id"];

export const DEFAULT_THEME: ThemeId = "dark";

export const THEME_STORAGE_KEY = "vallotton.theme";

export function isTheme(id: unknown): id is ThemeId {
  return typeof id === "string" && THEMES.some((th) => th.id === id);
}

export function themeOf(id: ThemeId): Theme {
  return THEMES.find((th) => th.id === id) ?? THEMES[0];
}

/** the theme after `id` in the registry, wrapping round: what one switch press goes to */
export function nextTheme(id: ThemeId): Theme {
  const at = THEMES.findIndex((th) => th.id === id);
  return THEMES[(at + 1) % THEMES.length];
}

function stored(): ThemeId {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(raw) ? raw : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

function remember(id: ThemeId): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, id);
  } catch {
    /* refused or full: the choice lasts for this tab */
  }
}

/** write the theme onto the document: the attribute the stylesheet keys on, and the bar colour */
export function paint(id: ThemeId): void {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", id);
  const meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (meta) meta.content = themeOf(id).barColor;
}

interface ThemeState {
  theme: ThemeId;
  set: (id: ThemeId) => void;
  /** to the next theme in the registry */
  toggle: () => void;
}

export const useTheme = create<ThemeState>((set, get) => ({
  theme: stored(),

  set: (id) => {
    if (!isTheme(id)) return;
    set({ theme: id });
    remember(id);
    paint(id);
  },

  toggle: () => get().set(nextTheme(get().theme).id),
}));

paint(useTheme.getState().theme);
