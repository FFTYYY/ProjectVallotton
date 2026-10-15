import { create } from "zustand";

import { readJson, writeJson } from "../lib/storage";

/**
 * What you have half-written, per conversation.
 *
 * The composer used to keep its text in its own state, and the chat view stays
 * mounted when you move between conversations — only the route parameter
 * changes. So a sentence typed in one conversation was still sitting there in
 * the next one, under someone else's transcript, waiting to be sent to the
 * wrong place.
 *
 * Kept here rather than in the composer so it survives the switch, and written
 * to localStorage so it survives a reload too: an unsent line is something you
 * mean to come back to, and a refresh is not a decision to throw it away.
 *
 * The new-chat page deliberately has no key and so no draft. A blank page is
 * what it is for.
 */

const KEY = "vallotton.drafts";

function prune(map: Record<string, string>): Record<string, string> {
  // an emptied box is not a draft, and conversations come and go — without
  // this the store would only ever grow
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(map)) if (v.trim()) out[k] = v;
  return out;
}

interface DraftState {
  byConv: Record<string, string>;
  get: (key: string | undefined) => string;
  set: (key: string | undefined, text: string) => void;
  clear: (key: string | undefined) => void;
}

export const useDrafts = create<DraftState>((set, get) => {
  function save(byConv: Record<string, string>) {
    const kept = prune(byConv);
    set({ byConv: kept });
    writeJson(KEY, kept);
  }

  return {
    byConv: prune(readJson<Record<string, string>>(KEY, {})),
    get: (key) => (key ? (get().byConv[key] ?? "") : ""),
    set: (key, text) => {
      if (!key) return;
      save({ ...get().byConv, [key]: text });
    },
    clear: (key) => {
      if (!key || !(key in get().byConv)) return;
      const byConv = { ...get().byConv };
      delete byConv[key];
      save(byConv);
    },
  };
});
