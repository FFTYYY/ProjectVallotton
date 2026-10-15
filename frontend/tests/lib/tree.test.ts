import { describe, expect, it } from "vitest";

import type { Folder, TreeConversation } from "../../src/api/types";
import {
  buildTree,
  descendantFolderIds,
  gapId,
  isNoop,
  resolveDrop,
  sameKindIndex,
  type TreeNode,
} from "../../src/lib/tree";

function folder(id: string, parent: string | null, position: number): Folder {
  return { id, name: id, parent_id: parent, position, is_archived: false };
}
function conv(id: string, folderId: string | null, position: number): TreeConversation {
  return { id, title: id, source: "web", folder_id: folderId, position };
}

/*
 *  A            folder, root
 *    A1         folder, in A
 *      ca1      conversation, in A1
 *    ca         conversation, in A
 *  B            folder, root (empty)
 *  c1           conversation, root
 *  c2           conversation, root
 */
const FOLDERS = [folder("A", null, 0), folder("A1", "A", 0), folder("B", null, 1)];
const CONVS = [
  conv("ca1", "A1", 0),
  conv("ca", "A", 0),
  conv("c1", null, 0),
  conv("c2", null, 1),
];

const roots = buildTree(FOLDERS, CONVS);

function find(id: string, nodes: TreeNode[] = roots): TreeNode {
  for (const n of nodes) {
    if (n.id === id) return n;
    const hit = n.children.length ? find(id, n.children) : null;
    if (hit) return hit;
  }
  return null as unknown as TreeNode;
}

describe("buildTree", () => {
  it("nests, with folders ahead of conversations at every level", () => {
    expect(roots.map((n) => n.id)).toEqual(["A", "B", "c1", "c2"]);
    expect(find("A").children.map((n) => n.id)).toEqual(["A1", "ca"]);
    expect(find("A1").children.map((n) => n.id)).toEqual(["ca1"]);
    expect(find("B").children).toEqual([]);
  });

  it("records each node's index among its own kind", () => {
    expect(sameKindIndex(find("A"))).toBe(0);
    expect(sameKindIndex(find("B"))).toBe(1);
    expect(sameKindIndex(find("c1"))).toBe(0);
    expect(sameKindIndex(find("c2"))).toBe(1);
  });

  it("says where a folder would go if dropped among conversations", () => {
    // above c1 there are two root folders, so a folder lands after both
    expect(find("c1").folderIndex).toBe(2);
    // and a conversation dropped above a folder goes to the front
    expect(find("A").convIndex).toBe(0);
  });

  it("counts what each folder already holds", () => {
    expect(find("A").folderChildren).toBe(1);
    expect(find("A").convChildren).toBe(1);
    expect(find("B").folderChildren).toBe(0);
    expect(find("B").convChildren).toBe(0);
  });

  it("survives a parent cycle in the data rather than hanging", () => {
    expect(buildTree([folder("X", "Y", 0), folder("Y", "X", 0)], [])).toEqual([]);
  });
});

describe("descendantFolderIds", () => {
  it("includes the folder itself and everything under it", () => {
    expect([...descendantFolderIds(FOLDERS, "A")].sort()).toEqual(["A", "A1"]);
    expect([...descendantFolderIds(FOLDERS, "B")]).toEqual(["B"]);
  });
});

describe("resolveDrop", () => {
  it("reads a gap as its parent and the index for that kind", () => {
    const id = gapId("A1", 0, 1); // end of A1: no folders, one conversation
    expect(resolveDrop(id, find("c1"))).toEqual({ parentId: "A1", index: 1 });
    expect(resolveDrop(id, find("B"))).toEqual({ parentId: "A1", index: 0 });
  });

  it("reads a root gap as a null parent", () => {
    expect(resolveDrop(gapId(null, 2, 0), find("c1"))).toEqual({
      parentId: null,
      index: 0,
    });
  });

  /*
   * The gap indices count the list AS SHOWN, which still contains the row
   * being dragged. The server inserts into the list WITHOUT it, so a move
   * down within one parent has to lose one — the bug this guards against
   * puts every such drop one place too far.
   */
  it("loses one when moving down inside the same parent", () => {
    // c1 (index 0) onto the gap below c2 (index 2 as shown)
    expect(resolveDrop(gapId(null, 2, 2), find("c1"))).toEqual({
      parentId: null,
      index: 1,
    });
  });

  it("keeps the index when moving up inside the same parent", () => {
    // c2 (index 1) onto the gap above c1 (index 0)
    expect(resolveDrop(gapId(null, 2, 0), find("c2"))).toEqual({
      parentId: null,
      index: 0,
    });
  });

  it("keeps the index when the parent changes", () => {
    // ca1 lives in A1; dropping it at the end of the root is index 2 there
    expect(resolveDrop(gapId(null, 2, 2), find("ca1"))).toEqual({
      parentId: null,
      index: 2,
    });
  });

  it("ignores anything that is not a gap", () => {
    expect(resolveDrop("into|A", find("c1"))).toBeNull();
    expect(resolveDrop("nonsense", find("c1"))).toBeNull();
  });
});

describe("isNoop", () => {
  it("spots a drop that changes nothing", () => {
    // c1 is already the first root conversation
    expect(isNoop(find("c1"), { parentId: null, index: 0 })).toBe(true);
    // and the gap just below it resolves to the same place
    const target = resolveDrop(gapId(null, 2, 1), find("c1"))!;
    expect(isNoop(find("c1"), target)).toBe(true);
  });

  it("does not mistake a real move for one", () => {
    expect(isNoop(find("c1"), { parentId: null, index: 1 })).toBe(false);
    expect(isNoop(find("c1"), { parentId: "A", index: 0 })).toBe(false);
  });
});
