import { create } from "zustand";

import { ApiError, api } from "../api/client";
import type { QuickPhrase } from "../api/types";
import { useToasts } from "./toast";
import { t } from "../uiText";

interface PhrasesState {
  phrases: QuickPhrase[];
  loaded: boolean;
  load: () => Promise<void>;
  create: (title: string, content: string) => Promise<boolean>;
  update: (id: string, title: string, content: string) => Promise<boolean>;
  remove: (id: string) => Promise<void>;
  /**
   * Put them in this order.
   *
   * Shown first, saved second — a drag that snapped back while the request was
   * in flight would feel broken even when it eventually succeeded. The backend
   * answers with the whole list, and that answer replaces what is on screen:
   * it is the one that knows about a phrase another tab just added.
   */
  reorder: (ids: string[]) => Promise<void>;
}

function toastErr(err: unknown) {
  if (err instanceof ApiError) useToasts.getState().push(err.message, "error");
  else useToasts.getState().push(t.networkError, "error");
}

export const usePhrases = create<PhrasesState>((set, get) => ({
  phrases: [],
  loaded: false,

  load: async () => {
    try {
      const resp = await api.get<{ phrases: QuickPhrase[] }>("/api/phrases");
      set({ phrases: resp.phrases, loaded: true });
    } catch (err) {
      toastErr(err);
    }
  },

  create: async (title, content) => {
    try {
      const p = await api.post<QuickPhrase>("/api/phrases", { title, content });
      set((s) => ({ phrases: [...s.phrases, p] }));
      return true;
    } catch (err) {
      toastErr(err);
      return false;
    }
  },

  update: async (id, title, content) => {
    try {
      const p = await api.patch<QuickPhrase>(`/api/phrases/${id}`, { title, content });
      set((s) => ({ phrases: s.phrases.map((x) => (x.id === id ? p : x)) }));
      return true;
    } catch (err) {
      toastErr(err);
      return false;
    }
  },

  reorder: async (ids) => {
    const before = get().phrases;
    const rank = new Map(ids.map((id, i) => [id, i]));
    set({
      phrases: [...before].sort(
        (a, b) => (rank.get(a.id) ?? 0) - (rank.get(b.id) ?? 0),
      ),
    });
    try {
      const got = await api.post<{ phrases: QuickPhrase[] }>("/api/phrases/reorder", {
        ids,
      });
      set({ phrases: got.phrases });
    } catch {
      set({ phrases: before });   // put it back where it was
    }
  },

  remove: async (id) => {
    try {
      await api.delete(`/api/phrases/${id}`);
      set((s) => ({ phrases: s.phrases.filter((x) => x.id !== id) }));
    } catch (err) {
      toastErr(err);
    }
  },
}));
