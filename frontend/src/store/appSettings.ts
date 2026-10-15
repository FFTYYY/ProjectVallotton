import { create } from "zustand";

import { api } from "../api/client";
import { inOrder } from "../lib/inOrder";
import type { GenSettings } from "../api/types";
import { DEFAULTS, fromWire, toWire, type Values } from "../lib/genSettings";

/**
 * What belongs to the app rather than to any one conversation.
 *
 * So far: the generation settings a new conversation is poured from, and a
 * count of what the backend is busy with. Both live on the server — a default
 * that only applies in the browser that set it is not a default, and a count
 * of running work is precisely what you open after closing the tab.
 */

interface Running {
  /** turns with a row still pending */
  turns: number;
  /** how many conversations those are spread across */
  conversations: number;
  /** continuous runs still armed, most of which are not painting yet */
  runs: number;
}

/** One way to reach the model, as the backend currently finds it. */
export interface Road {
  name: string;
  label: string;
  model: string;
  /** the backend is stepping over this one right now */
  stepped_over: boolean;
  /** how long that lasts without a probe saying otherwise */
  seconds_left: number;
  /** the one a request would take right now */
  in_use: boolean;
}

interface Wire {
  gen_defaults: GenSettings;
  running: Running;
  roads: Road[];
}


interface AppSettingsState {
  defaults: Values;
  running: Running;
  roads: Road[];
  /**
   * Whether what is on screen is authoritative — the server answered, or the
   * user changed something. Either way a later read must not stamp on it.
   */
  loaded: boolean;
  load: () => Promise<void>;
  apply: (patch: Partial<Values>) => void;
  /**
   * Tell the backend a road is out, or that it is back.
   *
   * Not a preference and not applied optimistically. Which roads work is the
   * backend's to know — this only writes what a request timing out would have
   * written, so the answer that comes back is the backend's, not an echo of
   * the click.
   */
  reportRoad: (name: string, steppedOver: boolean) => Promise<void>;
}

export const useAppSettings = create<AppSettingsState>((set, get) => ({
  defaults: { ...DEFAULTS },
  running: { turns: 0, conversations: 0, runs: 0 },
  roads: [],
  loaded: false,

  load: async () => {
    try {
      const got = await api.get<Wire>("/api/app-settings");
      // What is running is always the server's to say. The defaults are not:
      // a control moved while this request was still in flight must win, or
      // the reply lands on top of it and puts the switch back where it was.
      // On a phone that window is long enough to look like a switch that
      // refuses to turn on, and then starts working "after a while".
      const authoritative = get().loaded;
      set({
        running: got.running,
        roads: got.roads,
        loaded: true,
        ...(authoritative ? {} : { defaults: fromWire(got.gen_defaults) }),
      });
    } catch (err) {
      console.warn("app settings fetch failed", err);
    }
  },

  apply: (patch) => {
    // shown immediately, saved behind it: these change nothing that is already
    // running, so there is nothing to roll back if the write loses a race.
    // `loaded` as well — from here on what is on screen is the truth, and a
    // read that was already on its way must not overwrite it.
    set({ defaults: { ...get().defaults, ...patch }, loaded: true });
    inOrder("app-settings", () =>
      api
        .patch<Wire>("/api/app-settings", { gen_defaults: toWire(patch) })
        .then((got) =>
          set({
            defaults: fromWire(got.gen_defaults),
            running: got.running,
            roads: got.roads,
          }),
        ),
    );
  },

  reportRoad: async (name, steppedOver) => {
    const got = await api.post<{ roads: Road[] }>(`/api/roads/${name}`, {
      stepped_over: steppedOver,
    });
    set({ roads: got.roads });
  },
}));
