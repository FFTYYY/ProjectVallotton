import { create } from "zustand";

import { api } from "../api/client";
import type { Conversation, GenSettings } from "../api/types";
import {
  DEFAULTS,
  clampAll,
  clampImages,
  clampRounds,
  clampRuns,
  fromWire,
  toWire,
  type Values,
} from "../lib/genSettings";
import { inOrder } from "../lib/inOrder";
import { useAppSettings } from "./appSettings";
import { useChat } from "./chat";

/**
 * The generation settings, which belong to a conversation.
 *
 * 出图数量, 反思轮数, 连续生成, 常驻参考. They are decisions about a piece of
 * work, and a piece of work is a conversation — so the server holds them
 * (Conversation.gen_settings), which is also what makes them survive a closed
 * browser: a rerun or a continuous run started from another device reads the
 * same numbers.
 *
 * THE BROWSER KEEPS ONE COPY OF THEM, and it is not this store: it is the
 * conversation row in useChat.convMeta, which every listing and every poll
 * already refreshes. What the controls show is a pure function of that row —
 * see useGen — so there is nothing here to go stale, and nothing to keep in
 * step.
 *
 * That is worth saying plainly, because the version before this one did keep a
 * second copy, seeded on route changes and defended by a "已经读过" latch. Each
 * new way of arriving at a conversation was a new way for the copy to be
 * seeded from the wrong thing, and every one of them looked like a different
 * bug: settings changed in one conversation came back stale after switching
 * away and back, because the edit was written to the server and to the copy
 * but never to the row the copy was later re-seeded from.
 *
 * Three things are genuinely state, and they are all that is left here:
 *
 *   convId      which conversation is on screen, so the controls know whose
 *               settings they are showing. Nothing but the route sets it.
 *   pendingNew  tweaks made on the new-chat page, which has no row to write
 *               to yet. They go onto the conversation it creates.
 *   writing     conversations with an edit of ours still in flight, so a poll
 *               that answers with the pre-edit row cannot undo it. Counted
 *               rather than flagged: two controls moved in a second are two
 *               writes. See useChat.rememberConv, which is where the guard is
 *               applied — beside the row it guards.
 */

export { MAX_CONTINUOUS_ROUNDS, MAX_REFLECTION_ROUNDS } from "../lib/genSettings";

interface SettingsState {
  /** whose settings the controls are showing; null on the new-chat page */
  convId: string | null;
  /** edits made before there is a conversation to put them on */
  pendingNew: Partial<Values>;

  /** point the controls at a conversation (or at the new-chat page) */
  point: (convId: string | null) => void;
  /** change one or more at once — what the shared controls call */
  apply: (patch: Partial<Values>) => void;
  setNImages: (n: number) => void;
  setReflectionRounds: (n: number) => void;
  setContinuousRounds: (n: number) => void;
  setContinuousPhraseId: (id: string | null) => void;
  setStickyRefs: (on: boolean) => void;
}

/**
 * There is deliberately no "the settings, generally".
 *
 * There was one, and it read the conversation the CONTROLS were pointed at.
 * Two callers then used it to mean something else — what a new conversation
 * should start from, and what the conversation being sent to is set to — and
 * both are only the same thing on one page. On a folder page, which has no
 * controls at all, the pointer is simply left over from wherever you were
 * last, and a conversation created there came out with a stranger's settings.
 *
 * A function that answers without being asked "whose?" cannot be used
 * carelessly if it does not exist. What is left is one hook for the controls
 * (useGen — which really is about what is on screen) and one function for the
 * one fact a browser holds that the server does not (genTweaksForNew). Nobody
 * else needs to know: a conversation's settings are on its row, and it is the
 * server that reads them.
 */

/**
 * What a conversation created right now should be given ON TOP of the
 * defaults — which is usually nothing.
 *
 * Only the new-chat page can add anything: its controls are the ones a hand
 * can move before the conversation exists. Everywhere else, what is on screen
 * belongs to some OTHER conversation — or to nothing at all, on a page with no
 * composer, where `convId` is simply left over from wherever you were last.
 * Pressing 新对话 on a folder page used to hand the new conversation those
 * values, which is how it ended up with neither its own settings nor the
 * defaults.
 *
 * Empty means "say nothing at all", and that is the point: the defaults live
 * on the server and the server is the only place that knows them for certain.
 * A browser that has never opened the settings page has never been told what
 * they are, so it must not answer a question it cannot answer.
 */
export function genTweaksForNew(): Partial<Values> {
  const { convId, pendingNew } = useSettings.getState();
  return convId ? {} : { ...pendingNew };
}

/**
 * What controls pointed at one conversation show — everything they depend on
 * passed in, nothing read from the air.
 *
 * That is the whole difference between this and the function that used to sit
 * here: a caller must say which conversation it means and hand over the row,
 * so there is no way to get "the settings" without first knowing whose.
 */
export function shownSettings(args: {
  convId: string | null;
  row: GenSettings | undefined;
  defaults: Values;
  pendingNew: Partial<Values>;
}): Values {
  const { convId, row, defaults, pendingNew } = args;
  // the new-chat page: the defaults, with whatever has been tweaked on top,
  // because that is what the conversation it is about to create will be given
  if (!convId) return { ...defaults, ...pendingNew };
  // A conversation whose row has not arrived yet: the defaults are a better
  // guess than the factory floor, and the window is one tick — every listing
  // carries gen_settings, and the shell loads the listing at start-up.
  return row ? fromWire(row) : { ...defaults };
}

/** the same answer, wired to the stores, so the controls re-render with it */
export function useGen(): Values {
  const convId = useSettings((s) => s.convId);
  const pendingNew = useSettings((s) => s.pendingNew);
  const defaults = useAppSettings((s) => s.defaults);
  const row = useChat((s) => (convId ? s.convMeta[convId]?.gen_settings : undefined));
  return shownSettings({ convId, row, defaults, pendingNew });
}

export const useSettings = create<SettingsState>((set, get) => {
  function change(patch: Partial<Values>) {
    const id = get().convId;
    if (!id) {
      // No conversation yet: these are carried by the send that creates one,
      // and written onto it there — see useChat.createConversation.
      set({ pendingNew: { ...get().pendingNew, ...patch } });
      return;
    }
    const chat = useChat.getState();
    // Shown at once by writing the row itself, which is the only copy there
    // is. The guard around it keeps a poll from answering with the pre-edit
    // row while this is still on its way.
    chat.noteSettingsWrite(id, +1);
    chat.mergeGenSettings(id, toWire(patch));
    // queued per conversation so four controls moved in a second do not arrive
    // as four concurrent writes to the same row — see lib/inOrder
    inOrder(`conv:${id}`, async () => {
      let conv: Conversation | null = null;
      try {
        conv = await api.patch<Conversation>(`/api/conversations/${id}`, {
          gen_settings: toWire(patch),
        });
      } finally {
        // Counted off BEFORE its own answer is applied, or the guard turns
        // away the one answer it exists to wait for — and what the server
        // actually stored (it merges, clamps, and fills in a phrase of its
        // own) never reaches the row. If another control was moved meanwhile
        // the count is still above zero and this answer is genuinely out of
        // date, which is exactly what should happen to it.
        useChat.getState().noteSettingsWrite(id, -1);
      }
      // It goes in whole rather than as settings alone, because the row
      // carries the moment it was written. That stamp is how a snapshot taken
      // earlier is recognised when it lands, long after every guard has let
      // go. See useChat.withLocalSettings.
      if (conv) useChat.getState().acceptConv(conv);
    });
  }

  return {
    convId: null,
    pendingNew: {},

    point: (convId) => {
      if (get().convId === convId) return;
      // leaving the new-chat page lets go of anything tweaked there: it has
      // either been carried onto the conversation just created, or abandoned
      set({ convId, pendingNew: {} });
    },
    apply: (patch) => change(clampAll(patch)),
    setNImages: (n) => change({ nImages: clampImages(n) }),
    setReflectionRounds: (n) => change({ reflectionRounds: clampRounds(n) }),
    setContinuousRounds: (n) => change({ continuousRounds: clampRuns(n) }),
    setContinuousPhraseId: (id) => change({ continuousPhraseId: id }),
    setStickyRefs: (on) => change({ stickyRefs: !!on }),
  };
});

export { DEFAULTS };
