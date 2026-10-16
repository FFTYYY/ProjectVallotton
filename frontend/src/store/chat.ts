/**
 * Chat state. Handles the tricky parts of the conversation lifecycle:
 *
 * - sending with an idempotency key (a network retry can never duplicate)
 * - optimistic "sending" placeholder that survives failure with a retry path
 * - incremental polling that always refetches non-terminal messages
 *   (so phase/attachments of a pending generation stream in)
 * - busy-409 handling (server enforces one generation at a time per conv)
 * - windowed history: only the last N messages load initially; older pages
 *   are fetched on demand (before_seq) so huge conversations stay light
 * - paginated conversation list, with an archived view
 */

import { create } from "zustand";
import { inEveryLanguage, t } from "../uiText";

import { ApiError, api } from "../api/client";
import { Latest } from "../lib/latest";
import { mergeInto, sortMessages, stableSeq } from "../lib/transcript";
import {
  type Conversation,
  type ConversationsResponse,
  type GenSettings,
  type Message,
  type MessagesResponse,
} from "../api/types";
import { toWire } from "../lib/genSettings";
import { useProjects } from "./projects";
import { genTweaksForNew } from "./settings";
import { useToasts } from "./toast";

const LOCAL_SEQ_BASE = 1_000_000_000;
const CONV_PAGE = 50;
const MSG_PAGE = 60;
let localCounter = 0;
/** per-conversation request counter, so a stale open() can't clobber fresh state */
const reads = new Latest();

interface PendingSend {
  clientMsgId: string;
  text: string;
  files: File[];
}

interface ChatState {
  conversations: Conversation[];
  conversationsLoaded: boolean;
  hasMoreConvs: boolean;
  loadingConvs: boolean;
  /** meta for any conversation we've opened (covers archived ones too) */
  convMeta: Record<string, Conversation>;
  /**
   * Merge generation settings into a conversation's row. The row is the only
   * copy the browser keeps of them — see store/settings — so an edit is shown
   * by writing it here, and the server's answer is written here too.
   */
  mergeGenSettings: (convId: string, patch: Partial<GenSettings>) => void;
  /** count an edit of ours out and back; see the guard in rememberConv */
  noteSettingsWrite: (convId: string, delta: 1 | -1) => void;
  /** take in a conversation the server has just described, through the gate */
  acceptConv: (conv: Conversation) => void;

  messages: Record<string, Message[]>;
  loadedConvs: Record<string, boolean>;
  /** conversations we tried to open but couldn't (so we can offer a retry) */
  loadError: Record<string, string>;
  /**
   * The server's own answer to "is a generation running here?". Local
   * messages alone are not enough: a send can be accepted while the client
   * loses the response, or a 409 tells us someone else's job is running.
   * Polling keys off this OR local pending state.
   */
  serverPending: Record<string, boolean>;
  hasMoreBefore: Record<string, boolean>;
  loadingOlder: Record<string, boolean>;
  failedSends: Record<string, PendingSend>;

  loadConversations: () => Promise<void>;
  loadMoreConversations: () => Promise<void>;
  createConversation: (folderId?: string | null) => Promise<Conversation>;
  renameConversation: (id: string, title: string) => Promise<void>;
  setArchived: (id: string, archived: boolean) => Promise<void>;
  setFolder: (id: string, folderId: string | null) => Promise<void>;

  openConversation: (id: string) => Promise<void>;
  loadOlderMessages: (id: string) => Promise<void>;
  pollConversation: (id: string) => Promise<void>;
  /** poll a few more times for a title that lands after the turn does */
  awaitTitle: (id: string) => Promise<void>;
  sendMessage: (convId: string, text: string, files: File[]) => Promise<void>;
  retrySend: (convId: string, placeholderId: string) => Promise<void>;
  discardFailedSend: (convId: string, placeholderId: string) => void;
  cancelGeneration: (convId: string, messageId: string) => Promise<void>;
  redraw: (convId: string, messageId: string) => Promise<void>;
  /** make one of a turn's pictures its main one */
  setMain: (convId: string, messageId: string, index: number) => Promise<void>;
  rerun: (convId: string) => Promise<void>;
  branch: (messageId: string) => Promise<Conversation>;

  hasPending: (convId: string) => boolean;
}

function toast(text: string, type: "info" | "error" | "success" = "error") {
  useToasts.getState().push(text, type);
}

/** every store action reports failures the same way — never fail silently */
function toastErr(err: unknown, fallback: string = t.networkError) {
  toast(err instanceof ApiError ? err.message : fallback, "error");
}

export const useChat = create<ChatState>((set, get) => {
  /**
   * Edits of ours still on their way to the server, per conversation.
   *
   * Counted, because two controls moved in a second are two writes.
   */
  const settingsGoingOut: Record<string, number> = {};

  /**
   * What to store for a conversation the server has just described.
   *
   * EVERY answer carrying a conversation row comes through here: the message
   * poll, the sidebar's listing, a rename, an archive, our own settings write.
   * They are snapshots taken at different moments and they arrive in whatever
   * order the network feels like, so which one wins has to be decided rather
   * than assumed — the last one to land is not the newest one.
   *
   * Only the settings are defended, and for a reason: they are the one part
   * of this row the browser writes. The rest — the title the model gave it,
   * the run in flight — only the server writes, and it writes some of them
   * without moving updated_at, so judging the whole row by that stamp would
   * freeze things that are perfectly fresh.
   *
   * Two ways a snapshot can be out of date, and both happen with a slow link:
   *
   *   - it left before our edit did. The edit is not in it, and its stamp is
   *     older than the one our write came back with.
   *   - it is in the air right now, while our edit is too. Nothing it says
   *     about the settings can be worth anything yet.
   */
  function withLocalSettings(prev: Conversation | undefined, conv: Conversation): Conversation {
    if (!prev?.gen_settings) return conv;
    const ours = settingsGoingOut[conv.id] > 0;
    const older = Date.parse(conv.updated_at) < Date.parse(prev.updated_at);
    if (!ours && !older) return conv;
    return { ...conv, gen_settings: prev.gen_settings };
  }

  function rememberConv(incoming: Conversation) {
    set((s) => {
      const conv = withLocalSettings(s.convMeta[incoming.id], incoming);
      const idx = s.conversations.findIndex((c) => c.id === conv.id);
      const next = [...s.conversations];
      if (idx >= 0) next[idx] = { ...next[idx], ...conv, preview: next[idx].preview };
      return {
        conversations: next,
        convMeta: { ...s.convMeta, [conv.id]: conv },
      };
    });
    // The projects tab keeps its own tree, and this runs on every poll — which
    // is how a conversation learns the title the model gave it mid-turn. Miss
    // this and the tree still says 新对话 until the page is reloaded.
    const conv = incoming; // the tree cares about none of the guarded fields
    if (!conv.is_archived) {
      useProjects.getState().noteConversation({
        id: conv.id,
        title: conv.title,
        source: conv.source,
        folder_id: conv.folder_id,
        position: conv.position,
      });
    }
  }

  /** apply an updated conversation everywhere; handles archive show/hide */
  function applyConvUpdate(incoming: Conversation) {
    set((s) => {
      const conv = withLocalSettings(s.convMeta[incoming.id], incoming);
      // this list is the active one; an archived conversation drops out of it
      // the moment it is archived, and reappears when it comes back
      const belongs = !conv.is_archived;
      let next = s.conversations.filter((c) => c.id !== conv.id);
      if (belongs) {
        const old = s.conversations.find((c) => c.id === conv.id);
        const merged = { ...(old ?? conv), ...conv, preview: old?.preview };
        // keep list ordering: reinsert at previous position, or at top
        const idx = old ? s.conversations.findIndex((c) => c.id === conv.id) : 0;
        next = [...next.slice(0, idx < 0 ? 0 : idx), merged, ...next.slice(idx < 0 ? 0 : idx)];
      }
      return { conversations: next, convMeta: { ...s.convMeta, [conv.id]: conv } };
    });
    // the projects page draws from its own tree — keep it in step
    const conv = incoming;
    const projects = useProjects.getState();
    if (conv.is_archived) projects.forgetConversation(conv.id);
    else {
      projects.noteConversation({
        id: conv.id,
        title: conv.title,
        source: conv.source,
        folder_id: conv.folder_id,
        position: conv.position,
      });
    }
  }

  function mergeMessages(convId: string, incoming: Message[]) {
    // the folding itself is in lib/transcript, where it can be tested without
    // a browser — see the note there
    set((s) => ({
      messages: {
        ...s.messages,
        [convId]: mergeInto(s.messages[convId] ?? [], incoming),
      },
    }));
  }

  function replaceMessages(convId: string, incoming: Message[]) {
    set((s) => {
      // keep local placeholders (still sending, or failed awaiting retry) that
      // the server hasn't acknowledged yet
      const incomingClientIds = new Set(
        incoming.filter((m) => m.client_msg_id).map((m) => m.client_msg_id),
      );
      const locals = (s.messages[convId] ?? []).filter(
        (m) => m.local && !(m.client_msg_id && incomingClientIds.has(m.client_msg_id)),
      );
      return {
        messages: { ...s.messages, [convId]: sortMessages([...incoming, ...locals]) },
        loadedConvs: { ...s.loadedConvs, [convId]: true },
      };
    });
  }

  async function doSend(convId: string, send: PendingSend, placeholderId: string) {
    const form = new FormData();
    form.set("text", send.text);
    form.set("client_msg_id", send.clientMsgId);
    // Nothing about the settings rides along. They are on the conversation's
    // row, the server reads them from there, and a browser that restated them
    // would be restating a fact it can be wrong about: a retry after you have
    // walked to another conversation used to carry THAT one's numbers.
    for (const f of send.files) form.append("images", f, f.name);

    try {
      const resp = await api.postForm<{ messages: Message[] }>(
        `/api/conversations/${convId}/messages`,
        form,
      );
      set((s) => {
        const rest = { ...s.failedSends };
        delete rest[placeholderId];
        return {
          failedSends: rest,
          messages: {
            ...s.messages,
            [convId]: (s.messages[convId] ?? []).filter((m) => m.id !== placeholderId),
          },
        };
      });
      mergeMessages(convId, resp.messages);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) throw err;
      if (err instanceof ApiError && (err.code === "busy" || err.status === 409)) {
        // the server says a generation is running here — remember that, or we
        // would drop the placeholder and never poll for the result
        set((s) => ({
          serverPending: { ...s.serverPending, [convId]: true },
          messages: {
            ...s.messages,
            [convId]: (s.messages[convId] ?? []).filter((m) => m.id !== placeholderId),
          },
        }));
        toast(t.stillGenerating);
        return;
      }
      if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
        set((s) => ({
          messages: {
            ...s.messages,
            [convId]: (s.messages[convId] ?? []).filter((m) => m.id !== placeholderId),
          },
        }));
        toast(err.message);
        return;
      }
      // network / 5xx: keep the placeholder, allow retry with the SAME client_msg_id
      set((s) => ({
        failedSends: { ...s.failedSends, [placeholderId]: send },
        messages: {
          ...s.messages,
          [convId]: (s.messages[convId] ?? []).map((m) =>
            m.id === placeholderId ? { ...m, local: "failed" as const } : m,
          ),
        },
      }));
      toast(t.sendFailedNetwork);
    }
  }

  return {
    conversations: [],
    conversationsLoaded: false,
    hasMoreConvs: false,
    loadingConvs: false,
    convMeta: {},
    messages: {},
    loadedConvs: {},
    loadError: {},
    serverPending: {},
    hasMoreBefore: {},
    loadingOlder: {},
    failedSends: {},

    loadConversations: async () => {
      try {
        const resp = await api.get<ConversationsResponse>(
          `/api/conversations?limit=${CONV_PAGE}`,
        );
        set((s) => ({
          conversations: resp.conversations.map((c) =>
            withLocalSettings(s.convMeta[c.id], c),
          ),
          conversationsLoaded: true,
          hasMoreConvs: resp.has_more,
          // through the same gate as every other answer: this listing is a
          // snapshot too, and on a slow link it is often an old one
          convMeta: {
            ...s.convMeta,
            ...Object.fromEntries(
              resp.conversations.map((c) => [c.id, withLocalSettings(s.convMeta[c.id], c)]),
            ),
          },
        }));
      } catch (err) {
        // leave conversationsLoaded false so the sidebar can say "load failed"
        // instead of the misleading "you have no conversations yet"
        toastErr(err);
      }
    },

    loadMoreConversations: async () => {
      const { loadingConvs, hasMoreConvs, conversations } = get();
      if (loadingConvs || !hasMoreConvs) return;
      set({ loadingConvs: true });
      try {
        const resp = await api.get<ConversationsResponse>(
          `/api/conversations?limit=${CONV_PAGE}&offset=${conversations.length}`,
        );
        set((s) => {
          const known = new Set(s.conversations.map((c) => c.id));
          const fresh = resp.conversations.filter((c) => !known.has(c.id));
          return {
            conversations: [...s.conversations, ...fresh],
            hasMoreConvs: resp.has_more,
          };
        });
      } catch (err) {
        toastErr(err);
      } finally {
        set({ loadingConvs: false });
      }
    },

    mergeGenSettings: (convId, patch) =>
      set((s) => {
        const conv = s.convMeta[convId];
        if (!conv) return {};
        const merged = { ...conv, gen_settings: { ...conv.gen_settings, ...patch } };
        return {
          convMeta: { ...s.convMeta, [convId]: merged as Conversation },
          conversations: s.conversations.map((c) =>
            c.id === convId ? ({ ...c, gen_settings: merged.gen_settings } as Conversation) : c,
          ),
        };
      }),

    acceptConv: (conv) => applyConvUpdate(conv),

    noteSettingsWrite: (convId, delta) => {
      const now = (settingsGoingOut[convId] ?? 0) + delta;
      if (now > 0) settingsGoingOut[convId] = now;
      else delete settingsGoingOut[convId];
    },

    createConversation: async (folderId = null) => {
      // Only what this browser knows that the server does not: a tweak made on
      // the new-chat page before there was a conversation to put it on. The
      // defaults themselves are the server's, and asking it to use them is
      // done by saying nothing — see genTweaksForNew.
      const tweaks = toWire(genTweaksForNew());
      const conv = await api.post<Conversation>("/api/conversations", {
        folder_id: folderId,
        ...(Object.keys(tweaks).length ? { gen_settings: tweaks } : {}),
      });
      useProjects.getState().noteConversation({
        id: conv.id,
        title: conv.title,
        source: conv.source,
        folder_id: conv.folder_id,
        position: conv.position,
      });
      set((s) => ({
        conversations: [conv, ...s.conversations],
        convMeta: { ...s.convMeta, [conv.id]: conv },
        messages: { ...s.messages, [conv.id]: [] },
        loadedConvs: { ...s.loadedConvs, [conv.id]: true },
        hasMoreBefore: { ...s.hasMoreBefore, [conv.id]: false },
      }));
      return conv;
    },

    renameConversation: async (id, title) => {
      try {
        const conv = await api.patch<Conversation>(`/api/conversations/${id}`, { title });
        applyConvUpdate(conv);
      } catch (err) {
        toastErr(err);
      }
    },

    setFolder: async (id, folderId) => {
      try {
        const conv = await api.post<Conversation>(`/api/conversations/${id}/folder`, {
          folder_id: folderId,
        });
        applyConvUpdate(conv);
        // moving folders changes which library entries are in reach
        await useProjects.getState().loadSelection(id);
      } catch (err) {
        toastErr(err);
      }
    },

    setArchived: async (id, archived) => {
      try {
        const conv = await api.post<Conversation>(`/api/conversations/${id}/archive`, {
          archived,
        });
        applyConvUpdate(conv);
        toast(archived ? t.archivedToast : t.unarchivedToast, "success");
      } catch (err) {
        toastErr(err);
      }
    },

    openConversation: async (id) => {
      // Stamp the request: a slower earlier response must not overwrite a
      // fresher one (e.g. the first send races the initial open and would
      // otherwise wipe the just-created messages, leaving nothing to poll).
      const ticket = reads.claim(id);
      try {
        const resp = await api.get<MessagesResponse>(
          `/api/conversations/${id}/messages?limit=${MSG_PAGE}`,
        );
        if (!reads.holds(id, ticket)) return; // superseded
        replaceMessages(id, resp.messages);
        rememberConv(resp.conversation);
        set((s) => ({
          hasMoreBefore: { ...s.hasMoreBefore, [id]: resp.has_more_before },
          serverPending: { ...s.serverPending, [id]: resp.has_pending },
          loadError: { ...s.loadError, [id]: "" },
        }));
      } catch (err) {
        if (!reads.holds(id, ticket)) return;
        const msg =
          err instanceof ApiError
            ? err.status === 404
              ? t.conversationGone
              : err.message
            : t.loadFailedNetwork;
        set((s) => ({ loadError: { ...s.loadError, [id]: msg } }));
        if (err instanceof ApiError && err.status === 404) {
          set((s) => ({ conversations: s.conversations.filter((c) => c.id !== id) }));
        }
      }
    },

    loadOlderMessages: async (id) => {
      const { loadingOlder, hasMoreBefore, messages } = get();
      if (loadingOlder[id] || !hasMoreBefore[id]) return;
      const serverMsgs = (messages[id] ?? []).filter((m) => !m.local);
      if (!serverMsgs.length) return;
      const firstSeq = serverMsgs[0].seq;
      set((s) => ({ loadingOlder: { ...s.loadingOlder, [id]: true } }));
      try {
        const resp = await api.get<MessagesResponse>(
          `/api/conversations/${id}/messages?before_seq=${firstSeq}&limit=${MSG_PAGE}`,
        );
        mergeMessages(id, resp.messages);
        set((s) => ({
          hasMoreBefore: { ...s.hasMoreBefore, [id]: resp.has_more_before },
        }));
      } catch (err) {
        if (err instanceof ApiError) toast(err.message);
      } finally {
        set((s) => ({ loadingOlder: { ...s.loadingOlder, [id]: false } }));
      }
    },

    /**
     * The title is written AFTER the turn is marked done — it costs its own
     * model call, and holding "done" back for a cosmetic rename would keep the
     * composer disabled that whole time. So the refresh that runs when a turn
     * ends routinely beats the title to the row. Chase it, but only while
     * there is something to chase: this stops the moment a title lands, and
     * gives up after a few tries.
     */
    awaitTitle: async (id) => {
      for (let i = 0; i < 5; i += 1) {
        // untitled in whichever language it was made in
        if (!inEveryLanguage("newConversation").includes(get().convMeta[id]?.title ?? "")) return;
        await new Promise((r) => setTimeout(r, 1400));
        await get().pollConversation(id);
      }
    },

    pollConversation: async (id) => {
      const current = get().messages[id] ?? [];
      const after = stableSeq(current);
      try {
        const resp = await api.get<MessagesResponse>(
          `/api/conversations/${id}/messages?after_seq=${after}`,
        );
        mergeMessages(id, resp.messages);
        rememberConv(resp.conversation);
        set((s) => ({ serverPending: { ...s.serverPending, [id]: resp.has_pending } }));
        // An incremental poll is only told about rows newer than the waterline
        // it asked from, so a finished turn archived below it — which is what
        // 重跑 does to the answer it is replacing — would sit on screen for
        // ever, next to the answer that replaced it. Being told how many rows
        // are live needs no path to announce itself: holding more than exist
        // means one went, and the window is read again. Only that direction
        // is a fault; holding fewer is just a window.
        const held = (get().messages[id] ?? []).filter((m) => !m.local).length;
        if (resp.active_count !== undefined && held > resp.active_count) {
          await get().openConversation(id);
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          set((s) => ({
            conversations: s.conversations.filter((c) => c.id !== id),
          }));
        }
        // network errors during poll: silent, next tick will retry
      }
    },

    sendMessage: async (convId, text, files) => {
      const clientMsgId =
        typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `c-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const placeholderId = `local-${++localCounter}`;
      const placeholder: Message = {
        id: placeholderId,
        conversation_id: convId,
        seq: LOCAL_SEQ_BASE + localCounter,
        role: "user",
        status: "done",
        kind: "text",
        phase: "",
        content: text,
        error: "",
        client_msg_id: clientMsgId,
        nai_prompts: null,
        draw_brief: null,
        main_index: 0,
        selection_snapshot: null,
        prompt_tokens: null,
        reflection_log: null,
        notice_kind: "",
        created_at: new Date().toISOString(),
        auto_sent: null,
        attachments: [],
        local: "sending",
        localFiles: files,
        localText: text,
      };
      set((s) => ({
        messages: {
          ...s.messages,
          [convId]: [...(s.messages[convId] ?? []), placeholder],
        },
      }));
      await doSend(convId, { clientMsgId, text, files }, placeholderId);
    },

    retrySend: async (convId, placeholderId) => {
      const send = get().failedSends[placeholderId];
      if (!send) return;
      set((s) => ({
        messages: {
          ...s.messages,
          [convId]: (s.messages[convId] ?? []).map((m) =>
            m.id === placeholderId ? { ...m, local: "sending" as const } : m,
          ),
        },
      }));
      await doSend(convId, send, placeholderId);
    },

    discardFailedSend: (convId, placeholderId) => {
      set((s) => {
        const rest = { ...s.failedSends };
        delete rest[placeholderId];
        return {
          failedSends: rest,
          messages: {
            ...s.messages,
            [convId]: (s.messages[convId] ?? []).filter(
              (m) => m.id !== placeholderId,
            ),
          },
        };
      });
    },

    cancelGeneration: async (convId, messageId) => {
      try {
        await api.post(`/api/messages/${messageId}/cancel`);
        set((s) => ({
          messages: {
            ...s.messages,
            [convId]: (s.messages[convId] ?? []).map((m) =>
              m.id === messageId ? { ...m, phase: "cancelling" } : m,
            ),
          },
        }));
      } catch (err) {
        if (err instanceof ApiError) toast(err.message);
      }
    },

    redraw: async (convId, messageId) => {
      try {
        const resp = await api.post<{ messages: Message[] }>(
          `/api/messages/${messageId}/redraw`,
        );
        mergeMessages(convId, resp.messages);
      } catch (err) {
        if (err instanceof ApiError) toast(err.message);
      }
    },

    setMain: async (convId, messageId, index) => {
      try {
        const resp = await api.post<{ messages: Message[] }>(
          `/api/messages/${messageId}/main`,
          { index },
        );
        mergeMessages(convId, resp.messages);
      } catch (err) {
        if (err instanceof ApiError) toast(err.message);
      }
    },

    rerun: async (convId) => {
      try {
        // the same values a fresh send would carry — 出图数量 and 反思轮数
        // apply to a rerun too, and the server has no other way to know them
        // the same silence as a send: 重跑 is this conversation asked again,
        // and what it is set to is on its own row
        await api.post(`/api/conversations/${convId}/rerun`, {});
        // archived messages disappear — take the server's word wholesale
        await get().openConversation(convId);
      } catch (err) {
        if (err instanceof ApiError) toast(err.message);
      }
    },

    branch: async (messageId) => {
      const conv = await api.post<Conversation>(`/api/messages/${messageId}/branch`);
      set((s) => ({
        conversations: [conv, ...s.conversations.filter((c) => c.id !== conv.id)],
        convMeta: { ...s.convMeta, [conv.id]: conv },
      }));
      // a branch keeps its source's folder, so the tree gains a row
      useProjects.getState().noteConversation({
        id: conv.id,
        title: conv.title,
        source: conv.source,
        folder_id: conv.folder_id,
        position: conv.position,
      });
      return conv;
    },

    hasPending: (convId) => {
      const list = get().messages[convId] ?? [];
      return list.some((m) => m.status === "pending" || m.local === "sending");
    },
  };
});
