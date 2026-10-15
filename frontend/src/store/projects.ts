/**
 * Projects: the folder tree, each folder's library, what a conversation has
 * switched on, and the edit proposals the lead model raises.
 *
 * The tree is kept flat here (folders + conversations as two arrays) and
 * assembled for display, so a drag can be applied optimistically by rewriting
 * two fields and confirmed by the server afterwards.
 */

import { create } from "zustand";

import { readJson, writeJson } from "../lib/storage";

import { ApiError, api } from "../api/client";
import { Latest } from "../lib/latest";
import type {
  ArchivePayload,
  Character,
  Folder,
  FolderLibrary,
  LibraryProposal,
  ProjectTree,
  SelectionPayload,
  Setting,
  TreeConversation,
  Variant,
} from "../api/types";
import { useToasts } from "./toast";
import { t } from "../uiText";

/**
 * Tickets for the library selection, per conversation.
 *
 * Both the read and the write take one: the read so a slow answer cannot land
 * on top of a newer truth, the write so it invalidates the read that left
 * before it. See lib/latest.
 */
const selectionReads = new Latest();

/**
 * The same tickets for the project tree and for a folder's library.
 *
 * Both are read on a timer and written by hand, which is the whole hazard: a
 * tree fetched before a row was dragged lands after it and puts the row back.
 * The tree is one thing, so it has one key; a library is per folder.
 */
const treeReads = new Latest();
const libraryReads = new Latest();
const TREE = "tree";

function toastErr(err: unknown, fallback: string = t.networkError) {
  useToasts.getState().push(err instanceof ApiError ? err.message : fallback, "error");
}

export type ItemKind = "folder" | "conversation";

interface ProjectsState {
  folders: Folder[];
  conversations: TreeConversation[];
  treeLoaded: boolean;
  treeError: string;
  /**
   * Folder ids the user has collapsed, kept in this browser.
   *
   * Collapsed rather than expanded, so that "open" is what a folder is by
   * default — a folder created on another device, or by someone who has never
   * touched this tree, shows its contents rather than hiding them.
   *
   * In localStorage and not on the server on purpose: it is not a fact about
   * the project, it is where this person has the tree scrolled to. A phone
   * wants most of it shut and a desktop does not, and neither is wrong.
   */
  collapsed: Record<string, boolean>;

  libraries: Record<string, FolderLibrary>;
  loadingLibrary: Record<string, boolean>;

  /** per-conversation: the whole reachable library with `selected` flags */
  selection: Record<string, SelectionPayload>;
  proposals: Record<string, LibraryProposal[]>;

  /** the 已归档 tab: folders and conversations together, newest first */
  archive: ArchivePayload | null;

  loadTree: () => Promise<void>;
  loadArchive: () => Promise<void>;
  archiveFolder: (id: string, archived: boolean) => Promise<void>;
  toggleCollapse: (folderId: string) => void;

  createFolder: (name: string, parentId: string | null) => Promise<Folder | null>;
  renameFolder: (id: string, name: string) => Promise<void>;
  moveItem: (
    kind: ItemKind,
    id: string,
    parentId: string | null,
    index: number,
  ) => Promise<void>;

  loadLibrary: (folderId: string) => Promise<void>;
  createSetting: (folderId: string, title: string) => Promise<Setting | null>;
  updateSetting: (folderId: string, id: string, patch: Partial<Setting>) => Promise<void>;
  deleteSetting: (folderId: string, id: string) => Promise<void>;
  createCharacter: (folderId: string, name: string) => Promise<Character | null>;
  updateCharacter: (
    folderId: string,
    id: string,
    patch: Partial<Character>,
  ) => Promise<void>;
  deleteCharacter: (folderId: string, id: string) => Promise<void>;
  createVariant: (folderId: string, characterId: string, title: string) => Promise<void>;
  deleteVariant: (folderId: string, characterId: string, id: string) => Promise<void>;
  updateVariant: (
    folderId: string,
    characterId: string,
    id: string,
    patch: Partial<Variant>,
  ) => Promise<void>;

  loadSelection: (convId: string) => Promise<void>;
  setSelection: (
    convId: string,
    settingIds: string[],
    variantIds: string[],
  ) => Promise<void>;
  toggleSetting: (convId: string, settingId: string) => Promise<void>;
  toggleOne: (
    convId: string,
    change: { kind: "setting" | "variant" | "character"; id: string; on: boolean },
  ) => Promise<void>;
  toggleVariant: (convId: string, characterId: string, variantId: string) => Promise<void>;
  toggleCharacter: (convId: string, characterId: string) => Promise<void>;
  /** the conversation layer: drop one of its entries, or move one up into the project */
  removeEntry: (convId: string, kind: "setting" | "character", id: string) => Promise<void>;
  promoteEntry: (convId: string, kind: "setting" | "character", id: string) => Promise<void>;
  editEntry: (
    convId: string,
    kind: "setting" | "character",
    id: string,
    patch: { title?: string; name?: string; content?: string; description?: string },
  ) => Promise<void>;

  loadProposals: (convId: string) => Promise<void>;
  resolveProposal: (
    convId: string,
    proposalId: string,
    approve: boolean,
  ) => Promise<void>;

  /** local bookkeeping after a conversation moves or disappears */
  noteConversation: (conv: TreeConversation) => void;
  forgetConversation: (id: string) => void;
}

const COLLAPSED_KEY = "vallotton.collapsed";

function readCollapsed(): Record<string, boolean> {
  // Only the true entries are kept. A folder the user opened again is simply
  // absent — writing `false` would grow the record forever and say nothing
  // that its absence does not already say.
  const out: Record<string, boolean> = {};
  for (const [id, shut] of Object.entries(
    readJson<Record<string, boolean>>(COLLAPSED_KEY, {}),
  )) {
    if (shut) out[id] = true;
  }
  return out;
}

export const useProjects = create<ProjectsState>((set, get) => ({
  folders: [],
  conversations: [],
  treeLoaded: false,
  treeError: "",
  collapsed: readCollapsed(),
  libraries: {},
  loadingLibrary: {},
  selection: {},
  proposals: {},
  archive: null,

  loadTree: async () => {
    const ticket = treeReads.claim(TREE);
    try {
      const tree = await api.get<ProjectTree>("/api/projects/tree");
      // a tree fetched before something moved must not land on top of it
      if (!treeReads.holds(TREE, ticket)) return;
      set({
        folders: tree.folders,
        conversations: tree.conversations,
        treeLoaded: true,
        treeError: "",
      });
    } catch (err) {
      set({
        treeError: err instanceof ApiError ? err.message : t.projectListFailed,
      });
    }
  },

  loadArchive: async () => {
    try {
      set({ archive: await api.get<ArchivePayload>("/api/archive") });
    } catch (err) {
      toastErr(err);
    }
  },

  archiveFolder: async (id, archived) => {
    try {
      await api.post(`/api/folders/${id}/archive`, { archived });
      // an archived folder takes its contents out of the tree with it
      await get().loadTree();
      await get().loadArchive();
      useToasts.getState().push(archived ? t.archived : t.unarchivedToast, "success");
    } catch (err) {
      toastErr(err);
    }
  },

  toggleCollapse: (folderId) =>
    set((s) => {
      const collapsed = { ...s.collapsed };
      if (collapsed[folderId]) delete collapsed[folderId];
      else collapsed[folderId] = true;
      writeJson(COLLAPSED_KEY, collapsed);
      return { collapsed };
    }),

  createFolder: async (name, parentId) => {
    try {
      const folder = await api.post<Folder>("/api/folders", {
        name,
        parent_id: parentId,
      });
      // a new folder goes to the very top of its level — the server already
      // gave it a position ahead of its siblings, so just merge it in
      set((s) => ({ folders: [...s.folders, folder] }));
      return folder;
    } catch (err) {
      toastErr(err);
      return null;
    }
  },

  renameFolder: async (id, name) => {
    try {
      const folder = await api.patch<Folder>(`/api/folders/${id}`, { name });
      set((s) => ({
        folders: s.folders.map((f) => (f.id === id ? { ...f, ...folder } : f)),
      }));
      // breadcrumbs elsewhere are now stale, but they refresh on next open
    } catch (err) {
      toastErr(err);
    }
  },


  moveItem: async (kind, id, parentId, index) => {
    const before = { folders: get().folders, conversations: get().conversations };
    // whatever was read before this move is now describing the past
    treeReads.claim(TREE);
    // optimistic: reparent now, renumber from the server's answer afterwards
    set((s) =>
      kind === "folder"
        ? { folders: s.folders.map((f) => (f.id === id ? { ...f, parent_id: parentId } : f)) }
        : {
            conversations: s.conversations.map((c) =>
              c.id === id ? { ...c, folder_id: parentId } : c,
            ),
          },
    );
    try {
      await api.post("/api/projects/move", { kind, id, parent_id: parentId, index });
      await get().loadTree();
    } catch (err) {
      set(before); // the move was refused (a cycle, too deep) — snap back
      toastErr(err);
    }
  },

  loadLibrary: async (folderId) => {
    if (get().loadingLibrary[folderId]) return;
    const ticket = libraryReads.claim(folderId);
    set((s) => ({ loadingLibrary: { ...s.loadingLibrary, [folderId]: true } }));
    try {
      const lib = await api.get<FolderLibrary>(`/api/folders/${folderId}/library`);
      // an entry added or removed while this was in the air would be undone
      if (!libraryReads.holds(folderId, ticket)) return;
      set((s) => ({ libraries: { ...s.libraries, [folderId]: lib } }));
    } catch (err) {
      toastErr(err);
    } finally {
      set((s) => ({ loadingLibrary: { ...s.loadingLibrary, [folderId]: false } }));
    }
  },

  createSetting: async (folderId, title) => {
    libraryReads.claim(folderId);
    try {
      const s = await api.post<Setting>(`/api/folders/${folderId}/settings`, { title });
      // the caller needs the id: a new entry opens for editing, and matching
      // on the name would lose it the moment the name is changed
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: { ...lib, settings: [...lib.settings, s] },
          },
        };
      });
      return s;
    } catch (err) {
      toastErr(err);
      return null;
    }
  },

  updateSetting: async (folderId, id, patch) => {
    libraryReads.claim(folderId);
    try {
      const s = await api.patch<Setting>(`/api/settings/${id}`, patch);
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: {
              ...lib,
              settings: lib.settings.map((x) => (x.id === id ? s : x)),
            },
          },
        };
      });
    } catch (err) {
      toastErr(err);
    }
  },

  deleteSetting: async (folderId, id) => {
    libraryReads.claim(folderId);
    try {
      await api.delete(`/api/settings/${id}`);
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: { ...lib, settings: lib.settings.filter((x) => x.id !== id) },
          },
        };
      });
    } catch (err) {
      toastErr(err);
    }
  },

  createCharacter: async (folderId, name) => {
    libraryReads.claim(folderId);
    try {
      const c = await api.post<Character>(`/api/folders/${folderId}/characters`, {
        name,
      });
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: { ...lib, characters: [...lib.characters, c] },
          },
        };
      });
      return c;
    } catch (err) {
      toastErr(err);
      return null;
    }
  },

  updateCharacter: async (folderId, id, patch) => {
    libraryReads.claim(folderId);
    try {
      const c = await api.patch<Character>(`/api/characters/${id}`, patch);
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: {
              ...lib,
              characters: lib.characters.map((x) => (x.id === id ? c : x)),
            },
          },
        };
      });
    } catch (err) {
      toastErr(err);
    }
  },

  deleteCharacter: async (folderId, id) => {
    libraryReads.claim(folderId);
    try {
      await api.delete(`/api/characters/${id}`);
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: {
              ...lib,
              characters: lib.characters.filter((x) => x.id !== id),
            },
          },
        };
      });
    } catch (err) {
      toastErr(err);
    }
  },

  createVariant: async (folderId, characterId, title) => {
    libraryReads.claim(folderId);
    try {
      const v = await api.post<Variant>(`/api/characters/${characterId}/variants`, {
        title,
      });
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: {
              ...lib,
              characters: lib.characters.map((c) =>
                c.id === characterId ? { ...c, variants: [...c.variants, v] } : c,
              ),
            },
          },
        };
      });
    } catch (err) {
      toastErr(err);
    }
  },

  deleteVariant: async (folderId, characterId, id) => {
    libraryReads.claim(folderId);
    try {
      await api.delete(`/api/variants/${id}`);
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: {
              ...lib,
              characters: lib.characters.map((c) =>
                c.id === characterId
                  ? { ...c, variants: c.variants.filter((v) => v.id !== id) }
                  : c,
              ),
            },
          },
        };
      });
    } catch (err) {
      toastErr(err);
    }
  },

  updateVariant: async (folderId, characterId, id, patch) => {
    libraryReads.claim(folderId);
    try {
      const v = await api.patch<Variant>(`/api/variants/${id}`, patch);
      set((st) => {
        const lib = st.libraries[folderId];
        if (!lib) return {};
        return {
          libraries: {
            ...st.libraries,
            [folderId]: {
              ...lib,
              characters: lib.characters.map((c) =>
                c.id === characterId
                  ? { ...c, variants: c.variants.map((x) => (x.id === id ? v : x)) }
                  : c,
              ),
            },
          },
        };
      });
    } catch (err) {
      toastErr(err);
    }
  },

  /**
   * Read what this conversation has switched on.
   *
   * Ticketed, because this read races the very thing it is about: arriving at
   * a conversation starts it, and the first thing a hand does is tick a chip.
   * On a slow link the read lands after the tick and puts the chip back —
   * which looks exactly like the app undoing what you just did. See lib/latest.
   */
  loadSelection: async (convId) => {
    const ticket = selectionReads.claim(convId);
    try {
      const sel = await api.get<SelectionPayload>(
        `/api/conversations/${convId}/selection`,
      );
      if (!selectionReads.holds(convId, ticket)) return; // something changed under it
      set((s) => ({ selection: { ...s.selection, [convId]: sel } }));
    } catch (err) {
      toastErr(err);
    }
  },

  setSelection: async (convId, settingIds, variantIds) => {
    const before = get().selection[convId];
    // a write invalidates every read that left before it, and every earlier
    // write — two chips ticked quickly are two writes, and only the later
    // one's answer is the truth
    const ticket = selectionReads.claim(convId);
    try {
      const sel = await api.post<SelectionPayload>(
        `/api/conversations/${convId}/selection`,
        { setting_ids: settingIds, variant_ids: variantIds },
      );
      if (!selectionReads.holds(convId, ticket)) return;
      set((s) => ({ selection: { ...s.selection, [convId]: sel } }));
    } catch (err) {
      if (before && selectionReads.holds(convId, ticket)) {
        set((s) => ({ selection: { ...s.selection, [convId]: before } }));
      }
      toastErr(err);
    }
  },

  toggleSetting: async (convId, settingId) => {
    const sel = get().selection[convId];
    if (!sel) return;
    const on = !sel.settings.find((x) => x.id === settingId)?.selected;
    // paint the change immediately; the server response replaces it
    set((s) => ({
      selection: {
        ...s.selection,
        [convId]: {
          ...sel,
          settings: sel.settings.map((x) =>
            x.id === settingId ? { ...x, selected: !x.selected } : x,
          ),
        },
      },
    }));
    await get().toggleOne(convId, { kind: "setting", id: settingId, on });
  },

  // One switch, sent as one switch. The whole list used to be rewritten from
  // the cached copy, which quietly switched off whatever the server had
  // switched on during the turn in between.
  toggleOne: async (convId, change) => {
    const before = get().selection[convId];
    const ticket = selectionReads.claim(convId);
    try {
      const sel = await api.post<SelectionPayload>(
        `/api/conversations/${convId}/selection/toggle`,
        change,
      );
      if (!selectionReads.holds(convId, ticket)) return;
      set((s) => ({ selection: { ...s.selection, [convId]: sel } }));
    } catch (err) {
      if (before && selectionReads.holds(convId, ticket)) {
        set((s) => ({ selection: { ...s.selection, [convId]: before } }));
      }
      toastErr(err);
    }
  },

  toggleVariant: async (convId, characterId, variantId) => {
    const sel = get().selection[convId];
    if (!sel) return;
    const on = !sel.characters
      .find((c) => c.id === characterId)
      ?.variants.find((v) => v.id === variantId)?.selected;
    set((s) => ({
      selection: {
        ...s.selection,
        [convId]: {
          ...sel,
          characters: sel.characters.map((c) => {
            if (c.id !== characterId) return c;
            const variants = c.variants.map((v) =>
              v.id === variantId ? { ...v, selected: !v.selected } : v,
            );
            return { ...c, variants, selected: variants.some((v) => v.selected) };
          }),
        },
      },
    }));
    await get().toggleOne(convId, { kind: "variant", id: variantId, on });
  },

  toggleCharacter: async (convId, characterId) => {
    const sel = get().selection[convId];
    if (!sel) return;
    const character = sel.characters.find((c) => c.id === characterId);
    if (!character) return;
    // a character is on exactly when one of its variants is; switching it on
    // takes the first variant, which is the sensible default
    const on = !character.selected;
    set((s) => ({
      selection: {
        ...s.selection,
        [convId]: {
          ...sel,
          characters: sel.characters.map((c) =>
            c.id === characterId
              ? {
                  ...c,
                  selected: !c.selected,
                  variants: c.variants.map((v, i) => ({
                    ...v,
                    selected: c.selected ? false : i === 0,
                  })),
                }
              : c,
          ),
        },
      },
    }));
    await get().toggleOne(convId, { kind: "character", id: characterId, on });
  },

  removeEntry: async (convId, kind, id) => {
    try {
      await api.delete(`/api/${kind === "setting" ? "settings" : "characters"}/${id}`);
      await get().loadSelection(convId);
    } catch (err) {
      toastErr(err);
    }
  },

  promoteEntry: async (convId, kind, id) => {
    try {
      await api.post(`/api/${kind === "setting" ? "settings" : "characters"}/${id}/promote`);
      await get().loadSelection(convId);
    } catch (err) {
      toastErr(err);
    }
  },

  editEntry: async (convId, kind, id, patch) => {
    try {
      await api.patch(`/api/${kind === "setting" ? "settings" : "characters"}/${id}`, patch);
      await get().loadSelection(convId);
    } catch (err) {
      toastErr(err);
    }
  },

  loadProposals: async (convId) => {
    try {
      const resp = await api.get<{ proposals: LibraryProposal[] }>(
        `/api/conversations/${convId}/proposals`,
      );
      set((s) => ({ proposals: { ...s.proposals, [convId]: resp.proposals } }));
    } catch {
      // proposals are an extra: a failure here must not disturb the chat
    }
  },

  resolveProposal: async (convId, proposalId, approve) => {
    try {
      const p = await api.post<LibraryProposal>(
        `/api/proposals/${proposalId}/resolve`,
        { approve },
      );
      set((s) => ({
        proposals: {
          ...s.proposals,
          [convId]: (s.proposals[convId] ?? []).map((x) =>
            x.id === proposalId ? p : x,
          ),
        },
      }));
      if (approve) {
        useToasts.getState().push(p.applied_summary || t.writtenToLibrary, "success");
        // the folder's library changed under us
        set((s) => {
          const libraries = { ...s.libraries };
          for (const key of Object.keys(libraries)) delete libraries[key];
          return { libraries };
        });
        await get().loadSelection(convId);
      }
    } catch (err) {
      toastErr(err);
    }
  },

  noteConversation: (conv) =>
    set((s) => {
      const idx = s.conversations.findIndex((c) => c.id === conv.id);
      if (idx < 0) return { conversations: [...s.conversations, conv] };
      const next = [...s.conversations];
      next[idx] = { ...next[idx], ...conv };
      return { conversations: next };
    }),

  forgetConversation: (id) =>
    set((s) => ({ conversations: s.conversations.filter((c) => c.id !== id) })),
}));
