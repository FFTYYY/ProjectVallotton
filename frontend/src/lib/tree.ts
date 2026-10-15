/**
 * The projects tree, and the drop targets a drag can aim at.
 *
 * The tree is built NESTED rather than flattened, because the nesting is what
 * produces the indentation and, with it, the two things that have to be told
 * apart at the end of a folder:
 *
 *   ┌ folder A
 *   │   ── gap: before A's first child, inside A
 *   │   row
 *   │   ── gap: after that child, still inside A   (indented)
 *   └
 *     ── gap: after A, outside it                  (less indented)
 *
 * Those are separate elements at different indents, which is the only way to
 * say which one you mean.
 *
 * A gap knows the parent it inserts into and the index it inserts at — one
 * index per kind, because folders always sort ahead of conversations, so
 * "third thing here" means something different for each. All of it is encoded
 * in the droppable's id, so resolving a drop needs no lookup table.
 */

import type { Folder, TreeConversation } from "../api/types";

export type ItemKind = "folder" | "conversation";

export interface TreeNode {
  id: string;
  kind: ItemKind;
  name: string;
  source?: "web" | "mcp";
  parentId: string | null;
  /** where a drop just ABOVE this row would land, per kind */
  folderIndex: number;
  convIndex: number;
  children: TreeNode[];
  /** what this folder already holds, i.e. where "at the end" is */
  folderChildren: number;
  convChildren: number;
}

/** matches MAX_DEPTH in the backend's projects view */
export const MAX_DEPTH = 8;

function byPosition<T extends { position: number; id: string }>(a: T, b: T) {
  return a.position - b.position || a.id.localeCompare(b.id);
}

/**
 * Folders first, then conversations, at every level — the ordering the
 * backend keeps and the page has to show.
 */
export function buildTree(
  folders: Folder[],
  conversations: TreeConversation[],
): TreeNode[] {
  const foldersByParent = new Map<string | null, Folder[]>();
  for (const f of folders) {
    const list = foldersByParent.get(f.parent_id);
    if (list) list.push(f);
    else foldersByParent.set(f.parent_id, [f]);
  }
  const convsByParent = new Map<string | null, TreeConversation[]>();
  for (const c of conversations) {
    const list = convsByParent.get(c.folder_id);
    if (list) list.push(c);
    else convsByParent.set(c.folder_id, [c]);
  }

  const seen = new Set<string>();

  function build(parentId: string | null): TreeNode[] {
    const childFolders = [...(foldersByParent.get(parentId) ?? [])].sort(byPosition);
    const childConvs = [...(convsByParent.get(parentId) ?? [])].sort(byPosition);
    const out: TreeNode[] = [];

    childFolders.forEach((f, i) => {
      if (seen.has(f.id)) return; // a cycle in the data must not hang the page
      seen.add(f.id);
      const kids = build(f.id);
      out.push({
        id: f.id,
        kind: "folder",
        name: f.name,
        parentId,
        folderIndex: i,
        convIndex: 0, // a conversation dropped among folders goes first
        children: kids,
        folderChildren: (foldersByParent.get(f.id) ?? []).length,
        convChildren: (convsByParent.get(f.id) ?? []).length,
      });
    });

    childConvs.forEach((c, i) => {
      out.push({
        id: c.id,
        kind: "conversation",
        name: c.title,
        source: c.source,
        parentId,
        // a folder dropped among conversations lands after the last folder
        folderIndex: childFolders.length,
        convIndex: i,
        children: [],
        folderChildren: 0,
        convChildren: 0,
      });
    });

    return out;
  }

  return build(null);
}

/** every folder at or below `folderId`, so a drag can't target its own subtree */
export function descendantFolderIds(folders: Folder[], folderId: string): Set<string> {
  const out = new Set<string>([folderId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const f of folders) {
      if (f.parent_id && out.has(f.parent_id) && !out.has(f.id)) {
        out.add(f.id);
        grew = true;
      }
    }
  }
  return out;
}

/* ------------------------------------------------------------- drop targets */

export const GAP = "gap";
export const INTO = "into";
const SEP = "|";

/** a gap's whole meaning, encoded so a drop needs no lookup */
export function gapId(
  parentId: string | null,
  folderIndex: number,
  convIndex: number,
): string {
  return [GAP, parentId ?? "", folderIndex, convIndex].join(SEP);
}

export function intoId(folderId: string): string {
  return [INTO, folderId].join(SEP);
}

export interface DropTarget {
  parentId: string | null;
  index: number;
}

export function sameKindIndex(node: TreeNode): number {
  return node.kind === "folder" ? node.folderIndex : node.convIndex;
}

/**
 * What dropping `active` on the gap `overId` would do.
 *
 * A gap's index is its position in the list AS SHOWN, which still contains
 * the row being dragged. The server inserts into the list WITHOUT it, so a
 * move within the same parent that travels downwards has to lose one — miss
 * this and every such drop lands one place too far.
 */
export function resolveDrop(overId: string, active: TreeNode): DropTarget | null {
  const parts = overId.split(SEP);
  if (parts[0] !== GAP || parts.length !== 4) return null;
  const [, parent, f, c] = parts;
  const parentId = parent === "" ? null : parent;
  let index = Number(active.kind === "folder" ? f : c);
  if (parentId === active.parentId && sameKindIndex(active) < index) index -= 1;
  return { parentId, index };
}

/** true when the drop would leave everything exactly where it already is */
export function isNoop(node: TreeNode, target: DropTarget): boolean {
  return node.parentId === target.parentId && sameKindIndex(node) === target.index;
}
