import { create } from "zustand";

import { api } from "../api/client";
import type { QuotaStatus } from "../api/types";

/**
 * What the backend knows about limits and spend.
 *
 * Two places need it — the sidebar's usage lines and the context meter in the
 * right rail — and both would otherwise poll the same endpoint on their own.
 * The context window in particular has to come from here rather than be
 * written into the frontend: it is the active provider's, and that changes.
 */

const REFRESH_MS = 10 * 60 * 1000;

interface QuotaState {
  quota: QuotaStatus | null;
  /** fetch once and keep it fresh; safe to call from every mount */
  ensure: () => void;
}

let timer: number | null = null;
let inFlight = false;

export const useQuota = create<QuotaState>((set) => ({
  quota: null,

  ensure: () => {
    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        set({ quota: await api.get<QuotaStatus>("/api/quota") });
      } catch (err) {
        // decorative figures: keep the last good ones and never surface a
        // toast for them — but leave a trace, because a silent catch here
        // made a broken panel look like an empty one
        console.warn("quota fetch failed", err);
      } finally {
        inFlight = false;
      }
    }
    void load();
    if (timer === null) timer = window.setInterval(load, REFRESH_MS);
  },
}));
