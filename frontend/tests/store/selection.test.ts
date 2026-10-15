import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * A chip is one switch. It used to rewrite the whole selection from the
 * cached copy, which quietly switched off whatever the server had switched
 * on during the turn in between (the lead switching a character on by name,
 * an approved proposal). Now a chip sends exactly one change.
 */
const post = vi.fn();
vi.mock("../../src/api/client", () => ({
  api: { post: (...a: unknown[]) => post(...a), get: vi.fn() },
  ApiError: class extends Error {},
}));

const payload = {
  settings: [
    { id: "s1", title: "黄昏", selected: true },
    { id: "s2", title: "雨夜", selected: true },
  ],
  characters: [
    {
      id: "c1",
      name: "林",
      selected: false,
      variants: [
        { id: "v1", title: "校服", selected: false },
        { id: "v2", title: "便装", selected: false },
      ],
    },
  ],
};

async function freshStore() {
  vi.resetModules();
  const { useProjects } = await import("../../src/store/projects");
  // the server's answer is whatever it says; the test only looks at what was sent
  post.mockResolvedValue(payload);
  useProjects.setState({ selection: { conv: payload as never } } as never);
  return useProjects;
}

describe("selection chips", () => {
  beforeEach(() => post.mockReset());

  it("a setting chip sends that one switch", async () => {
    const store = await freshStore();
    await store.getState().toggleSetting("conv", "s1");
    expect(post).toHaveBeenCalledWith("/api/conversations/conv/selection/toggle", {
      kind: "setting", id: "s1", on: false,
    });
  });

  it("a character chip switches the character, not a list of variants", async () => {
    const store = await freshStore();
    await store.getState().toggleCharacter("conv", "c1");
    expect(post).toHaveBeenCalledWith("/api/conversations/conv/selection/toggle", {
      kind: "character", id: "c1", on: true,
    });
  });

  it("a variant chip names the variant", async () => {
    const store = await freshStore();
    await store.getState().toggleVariant("conv", "c1", "v2");
    expect(post).toHaveBeenCalledWith("/api/conversations/conv/selection/toggle", {
      kind: "variant", id: "v2", on: true,
    });
  });
});
