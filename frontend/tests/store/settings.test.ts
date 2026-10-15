import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation, GenSettings } from "../../src/api/types";

import { useAppSettings } from "../../src/store/appSettings";
import { useChat } from "../../src/store/chat";
import { genTweaksForNew, shownSettings, useSettings } from "../../src/store/settings";

/**
 * What the controls would show right now. The app has no such function on
 * purpose — see settings.ts — so a test that wants the ambient answer wires
 * it up itself, in one place, where it is plainly a test convenience.
 */
function shown(convId = useSettings.getState().convId) {
  return shownSettings({
    convId,
    row: convId ? useChat.getState().convMeta[convId]?.gen_settings : undefined,
    defaults: useAppSettings.getState().defaults,
    pendingNew: useSettings.getState().pendingNew,
  });
}

/**
 * The browser keeps one copy of a conversation's generation settings — the row
 * — and what the controls show is a pure function of it.
 *
 * The version before this one kept a second copy, seeded on route changes.
 * Every way of arriving at a conversation was a way to seed it from the wrong
 * thing: change a setting in one conversation, switch away, come back, and the
 * controls showed what the row said BEFORE the edit, because the edit had gone
 * to the server and to the copy but never to the row.
 */

const patched: { path: string; body: unknown }[] = [];
let patchReply: (path: string) => unknown = () => ({});
/** answers keyed on what is being written, for tests that hold one in flight */
let patchByBody: (body: unknown) => unknown = () => undefined;
let getReply: (path: string) => unknown = () => ({});
const posted: { path: string; form: unknown }[] = [];

vi.mock("../../src/api/client", () => ({
  ApiError: class extends Error {},
  api: {
    get: vi.fn(async (path: string) => getReply(path)),
    post: vi.fn(async () => ({})),
    postForm: vi.fn(async (path: string, form: unknown) => {
      posted.push({ path, form });
      return { messages: [] };
    }),
    patch: vi.fn(async (path: string, body: unknown) => {
      patched.push({ path, body });
      const keyed = patchByBody(body);
      return keyed === undefined ? patchReply(path) : keyed;
    }),
  },
}));

const wire = (over: Partial<GenSettings> = {}): GenSettings =>
  ({
    n_images: 1,
    reflection_rounds: 0,
    continuous_rounds: 1,
    continuous_phrase_id: null,
    sticky_refs: false,
    ...over,
  }) as GenSettings;

const conv = (id: string, gen: GenSettings, at = "2026-09-14T00:00:00Z"): Conversation =>
  ({
    id,
    title: id,
    source: "web",
    is_archived: false,
    folder_id: null,
    folder_path: [],
    position: 0,
    created_at: "",
    updated_at: at,
    gen_settings: gen,
  }) as Conversation;

const flush = () => new Promise((r) => setTimeout(r, 0));

/**
 * A new conversation starts from the defaults — and the defaults live on the
 * server.
 *
 * The bug this pins: 新对话 pressed on a folder page handed the new
 * conversation whatever the settings store happened to be pointed at, which on
 * a page with no controls is simply the last conversation you looked at. The
 * rule is not "the folder page must remember to do something"; it is that the
 * browser only ever says the one thing it knows and the server does not.
 */
describe("what a new conversation is given", () => {
  beforeEach(() => {
    useAppSettings.setState({
      defaults: {
        nImages: 2,
        reflectionRounds: 0,
        continuousRounds: 1,
        continuousPhraseId: null,
        stickyRefs: false,
      quality: "mid" as const,
      autoLibrary: "off" as const,
      },
      loaded: true,
    });
    useChat.setState({ conversations: [], convMeta: { a: conv("a", wire({ n_images: 1 })) } });
    useSettings.setState({ convId: null, pendingNew: {} });
  });

  it("says nothing at all when standing in another conversation", () => {
    useSettings.getState().point("a");
    expect(genTweaksForNew()).toEqual({});
  });

  it("says nothing on a page that has no controls either", () => {
    // a folder page points at nothing of its own; the pointer is left over
    useSettings.getState().point("a");
    useChat.setState({ convMeta: {} }); // and the row is not even loaded
    expect(genTweaksForNew()).toEqual({});
  });

  it("carries only what was tweaked on the new-chat page", () => {
    useSettings.getState().point(null);
    useSettings.getState().setNImages(3);
    expect(genTweaksForNew()).toEqual({ nImages: 3 });
  });

  it("lets go of that tweak the moment it belongs to a conversation", () => {
    useSettings.getState().point(null);
    useSettings.getState().setNImages(3);
    useSettings.getState().point("a");
    expect(genTweaksForNew()).toEqual({});
  });
});

describe("what a send carries", () => {
  beforeEach(() => {
    useChat.setState({
      conversations: [],
      convMeta: { a: conv("a", wire({ n_images: 4 })), b: conv("b", wire({ n_images: 1 })) },
      messages: {},
      failedSends: {},
    });
    useSettings.setState({ convId: null, pendingNew: {} });
  });

  it("carries no settings at all: they are on the row the server reads", async () => {
    // The settings used to ride along, read from wherever the controls were
    // pointed. A retry after walking to another conversation then sent THAT
    // one's numbers — the same mistake as the folder page, one path along.
    useSettings.getState().point("b");
    await useChat.getState().sendMessage("a", "画一张", []);
    const form = (posted[0]?.form ?? null) as FormData | null;
    expect(form).not.toBeNull();
    for (const key of ["n_images", "reflection_rounds", "sticky_refs", "continuous_rounds"]) {
      expect(form!.get(key)).toBeNull();
    }
    expect(form!.get("text")).toBe("画一张");
  });
});

describe("what the controls show", () => {
  beforeEach(() => {
    patched.length = 0;
    posted.length = 0;
    patchReply = () => ({});
    patchByBody = () => undefined;
    getReply = () => ({ conversations: [], has_more: false });
    useAppSettings.setState({
      defaults: {
        nImages: 2,
        reflectionRounds: 0,
        continuousRounds: 1,
        continuousPhraseId: null,
        stickyRefs: false,
      quality: "mid" as const,
      autoLibrary: "off" as const,
      },
      loaded: true,
    });
    useChat.setState({
      conversations: [],
      convMeta: {
        a: conv("a", wire({ reflection_rounds: 0 })),
        b: conv("b", wire({ reflection_rounds: 5 })),
      },
    });
    useSettings.setState({ convId: null, pendingNew: {} });
  });

  it("is the row of whichever conversation it points at", () => {
    useSettings.getState().point("a");
    expect(shown().reflectionRounds).toBe(0);
    useSettings.getState().point("b");
    expect(shown().reflectionRounds).toBe(5);
  });

  it("survives switching away and back after an edit", () => {
    // the bug this file exists for
    useSettings.getState().point("a");
    useSettings.getState().setReflectionRounds(3);
    expect(shown().reflectionRounds).toBe(3);

    useSettings.getState().point("b");
    expect(shown().reflectionRounds).toBe(5);

    useSettings.getState().point("a");
    expect(shown().reflectionRounds).toBe(3);
  });

  it("writes the edit onto the row, which is the only copy there is", () => {
    useSettings.getState().point("a");
    useSettings.getState().setNImages(4);
    expect(useChat.getState().convMeta.a.gen_settings?.n_images).toBe(4);
  });

  it("sends the change to the server, one control at a time", async () => {
    useSettings.getState().point("a");
    useSettings.getState().setContinuousRounds(6);
    await flush();
    expect(patched).toHaveLength(1);
    expect(patched[0].path).toBe("/api/conversations/a");
    expect(patched[0].body).toEqual({ gen_settings: { continuous_rounds: 6 } });
  });

  it("keeps the server's answer, which is the merged and clamped one", async () => {
    patchReply = () => conv("a", wire({ reflection_rounds: 10 }));
    useSettings.getState().point("a");
    useSettings.getState().setReflectionRounds(99); // clamped to the ceiling
    await flush();
    expect(shown().reflectionRounds).toBe(10);
  });

  it("is not undone by an answer that left before the edit did", async () => {
    // Something else the server says about this conversation — a rename here,
    // a poll in life — carries the row as it was before the edit. Without the
    // guard the control you just moved jumps back, then forwards again when
    // the write lands.
    let land: (v: unknown) => void = () => {};
    const held = new Promise((r) => (land = r));
    // keyed on what is being written, so it does not matter when each call is
    // actually made — the settings write is queued, and queues run late
    patchReply = () => held;
    patchByBody = (body) =>
      body && typeof body === "object" && "title" in body
        ? conv("a", wire({ n_images: 1 }))
        : undefined;

    useSettings.getState().point("a");
    useSettings.getState().setNImages(4);
    await useChat.getState().renameConversation("a", "改个名字");
    expect(shown().nImages).toBe(4);

    land(conv("a", wire({ n_images: 4 })));
    await flush();
    expect(shown().nImages).toBe(4);
  });

  it("is not undone by a listing that was taken before the edit", async () => {
    // The sidebar reloads on a timer and every conversation in that listing
    // carries its settings. On a slow link the listing often leaves before an
    // edit and lands after it — which is the shape of "I moved it and it went
    // back a moment later". The row's own stamp is what tells them apart.
    patchReply = () => conv("a", wire({ n_images: 4 }), "2026-09-14T10:00:00Z");
    useSettings.getState().point("a");
    useSettings.getState().setNImages(4);
    await flush();
    expect(shown().nImages).toBe(4);

    // ...and here comes the listing, taken an hour earlier
    getReply = () => ({
      conversations: [conv("a", wire({ n_images: 1 }), "2026-09-14T09:00:00Z")],
      has_more: false,
    });
    await useChat.getState().loadConversations();
    expect(shown().nImages).toBe(4);
  });

  it("does take a change that really is newer", async () => {
    // the same conversation, changed in another tab or on the phone
    patchReply = () => conv("a", wire({ n_images: 4 }), "2026-09-14T10:00:00Z");
    useSettings.getState().point("a");
    useSettings.getState().setNImages(4);
    await flush();
    useChat.getState().acceptConv(conv("a", wire({ n_images: 2 }), "2026-09-14T11:00:00Z"));
    expect(shown().nImages).toBe(2);
  });

  it("keeps its own answer, which the guard must not turn away", async () => {
    // The guard exists so an answer that left before the edit cannot undo it.
    // Counted off too late it turned away the one answer it was waiting for,
    // and the row never learned what the server actually stored.
    // The server does not only store what it was sent: it merges, clamps, and
    // fills in a continuous phrase when none was chosen. None of that reaches
    // the row if its own answer is turned away, and the browser then shows
    // something the server never agreed to.
    patchReply = () =>
      conv("a", wire({ continuous_rounds: 5, continuous_phrase_id: "p1" }),
           "2026-09-14T10:00:00Z");
    useSettings.getState().point("a");
    useSettings.getState().setContinuousRounds(5);
    await flush();
    expect(shown().continuousPhraseId).toBe("p1");
    expect(useChat.getState().convMeta.a.updated_at).toBe("2026-09-14T10:00:00Z");
  });

  it("four controls moved in a second end up saying what was asked", async () => {
    // Each write answers with the row as it was when THAT one arrived, so the
    // early answers are already out of date when they land. Only the last one
    // describes everything that was asked for.
    patchByBody = (body) => {
      const patch = (body as { gen_settings: Record<string, number> }).gen_settings;
      if ("sticky_refs" in patch) {
        return conv("a", wire({ n_images: 3, reflection_rounds: 4, continuous_rounds: 7, sticky_refs: true }), "2026-09-14T10:00:04Z");
      }
      if ("continuous_rounds" in patch) {
        return conv("a", wire({ reflection_rounds: 4, continuous_rounds: 7 }), "2026-09-14T10:00:02Z");
      }
      return conv("a", wire({ reflection_rounds: 4 }), "2026-09-14T10:00:01Z");
    };
    useSettings.getState().point("a");
    useSettings.getState().setReflectionRounds(4);
    useSettings.getState().setContinuousRounds(7);
    useSettings.getState().setNImages(3);
    useSettings.getState().setStickyRefs(true);
    await flush();
    await flush();
    await flush();
    await flush();
    const got = shown();
    expect([got.reflectionRounds, got.continuousRounds, got.nImages, got.stickyRefs])
      .toEqual([4, 7, 3, true]);
  });

  it("shows the defaults where there is no conversation", () => {
    useSettings.getState().point(null);
    expect(shown().nImages).toBe(2);
  });

  it("follows the defaults when they change under it", () => {
    useSettings.getState().point(null);
    useAppSettings.setState({
      defaults: { ...useAppSettings.getState().defaults, continuousRounds: 6 },
    });
    expect(shown().continuousRounds).toBe(6);
  });

  it("keeps a tweak made before there is a conversation to put it on", () => {
    useSettings.getState().point(null);
    useSettings.getState().setNImages(3);
    expect(shown().nImages).toBe(3);
    expect(patched).toHaveLength(0); // nothing to write to yet
  });

  it("lets go of that tweak once it has a conversation", () => {
    useSettings.getState().point(null);
    useSettings.getState().setNImages(3);
    useSettings.getState().point("b");
    useSettings.getState().point(null);
    expect(shown().nImages).toBe(2);
  });
});
