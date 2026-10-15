import { describe, expect, it } from "vitest";

import type { Conversation, Folder } from "../../src/api/types";

import { folderTrail, titleTrail } from "../../src/lib/pageTitle";

const folder = (id: string, name: string, parent: string | null = null): Folder =>
  ({ id, name, parent_id: parent, position: 0, is_archived: false }) as Folder;

const conv = (title: string, path: string[] = []): Conversation =>
  ({ id: "c", title, folder_path: path }) as Conversation;

const TREE = [
  folder("a", "画集"),
  folder("b", "秋天", "a"),
  folder("c", "草稿", "b"),
];

describe("folderTrail", () => {
  it("walks up to the outermost folder", () => {
    expect(folderTrail(TREE, "c")).toEqual(["画集", "秋天", "草稿"]);
  });

  it("is just the one when it sits at the top", () => {
    expect(folderTrail(TREE, "a")).toEqual(["画集"]);
  });

  it("is empty for a folder it has never heard of", () => {
    expect(folderTrail(TREE, "zzz")).toEqual([]);
    expect(folderTrail(TREE, null)).toEqual([]);
  });

  it("stops rather than walking a circle for ever", () => {
    const loop = [folder("x", "一", "y"), folder("y", "二", "x")];
    expect(folderTrail(loop, "x").length).toBeLessThanOrEqual(3);
  });
});

describe("titleTrail", () => {
  const base = {
    pathname: "/",
    conv: undefined,
    folderId: undefined,
    folders: TREE,
    folderPath: undefined,
  };

  it("puts the conversation after the folders it sits in", () => {
    expect(titleTrail({ ...base, conv: conv("黄昏之城", ["画集", "秋天"]) }))
      .toEqual(["画集", "秋天", "黄昏之城"]);
  });

  it("is the conversation alone when it sits in none", () => {
    expect(titleTrail({ ...base, conv: conv("随手画") })).toEqual(["随手画"]);
  });

  it("names a folder page the same way — the folders above it, then it", () => {
    expect(titleTrail({ ...base, pathname: "/f/b", folderId: "b" }))
      .toEqual(["画集", "秋天"]);
  });

  it("takes the page's own trail for a folder the tree leaves out", () => {
    // an archived folder is not in the tree, and its page is still a page
    expect(titleTrail({
      ...base, pathname: "/f/z", folderId: "z", folderPath: ["画集", "去年"],
    })).toEqual(["画集", "去年"]);
  });

  it("says the app's name while it still knows nothing", () => {
    expect(titleTrail({ ...base, pathname: "/f/b", folderId: "b", folders: [] }))
      .toEqual(["Vallotton"]);
    expect(titleTrail(base)).toEqual(["Vallotton"]);
  });

  it("names the settings page, wherever you came from", () => {
    expect(titleTrail({ ...base, pathname: "/settings", conv: conv("随手画") }))
      .toEqual(["设置"]);
  });
});
