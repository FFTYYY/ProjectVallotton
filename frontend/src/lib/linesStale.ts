/**
 * Whether an entry's tag lines are waiting to be rewritten from its text.
 *
 * A person edits text, never tags; the app rewrites the lines from the text
 * a little later, off the request (backend services/library_lines.py). Until
 * then the entry carries a small turning mark, and a page that shows one
 * keeps asking so the mark goes away on its own.
 */

import type { Character, Setting } from "../api/types";
import { t } from "../uiText";

/** read when shown, not at import: it is in the language chosen now */
export function staleHint(): string {
  return t.staleHint;
}
/** how often to ask again while something is waiting */
export const STALE_POLL_MS = 20_000;

export function anyStale(settings: Setting[], characters: Character[]): boolean {
  return (
    settings.some((s) => s.lines_stale) ||
    characters.some((c) => c.lines_stale || c.variants.some((v) => v.lines_stale))
  );
}
