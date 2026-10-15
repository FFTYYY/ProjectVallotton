/**
 * The 项目 tab: folders and the conversations inside them.
 *
 * The tree renders NESTED, and every row is preceded by a thin drop gap;
 * every open folder gets one more after its children, indented a level
 * deeper, which is "put it at the end of this folder" as distinct from "put
 * it after this folder". The nesting is what gives each gap its indent.
 *
 * The gaps are there ALL the time, a few pixels tall. Picking a row up must
 * not move anything — the row itself only fades — and creating the gaps at
 * drag start made the whole list jump, which reads as if it had already been
 * taken out. The one under the cursor opens into a real slot and pushes the
 * rows below it down: the list making room exactly where the item will land.
 *
 * Dropping ON a folder puts the item at the end of that folder. That row does
 * not move aside; it outlines itself, so what you aimed at stays put.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  DragOverlay,
  MeasuringStrategy,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
} from "@dnd-kit/core";
import { useNavigate, useParams } from "react-router-dom";

import { confirmArchive } from "../lib/archive";
import { middleClickOpens } from "../lib/newtab";
import { useSmoothReorder } from "../lib/flip";
import {
  buildTree,
  descendantFolderIds,
  gapId,
  intoId,
  isNoop,
  resolveDrop,
  type TreeNode,
} from "../lib/tree";
import { useChat } from "../store/chat";
import { useProjects } from "../store/projects";
import {
  ChevronDownIcon,
  ConversationIcon,
  DotsIcon,
  FolderIcon,
  FolderOpenIcon,
  NewFolderIcon,
} from "./Icons";
import { t } from "../uiText";

/* ------------------------------------------------------------- a drop gap */

function DropGap({
  parentId,
  folderIndex,
  convIndex,
  disabled,
  overId,
}: {
  parentId: string | null;
  folderIndex: number;
  convIndex: number;
  disabled: boolean;
  overId: string | null;
}) {
  const id = gapId(parentId, folderIndex, convIndex);
  const { setNodeRef } = useDroppable({ id, disabled });
  return (
    <div
      ref={setNodeRef}
      className={`tree-gap ${overId === id ? "active" : ""}`}
      // what this gap means, spelled out: which folder it inserts into and
      // at which index for each kind
      data-parent={parentId ?? "root"}
      data-fi={folderIndex}
      data-ci={convIndex}
      aria-hidden="true"
    />
  );
}

/* ------------------------------------------------------------------ a row */

interface RowProps {
  node: TreeNode;
  selected: boolean;
  dragging: boolean;
  dropInto: boolean;
  intoDisabled: boolean;
  collapsed: boolean;
  renaming: boolean;
  onOpen: (node: TreeNode) => void;
  onToggle: (id: string) => void;
  onMenu: (node: TreeNode, anchor: HTMLElement) => void;
  onRename: (node: TreeNode, name: string) => void;
}

function Row({
  node,
  selected,
  dragging,
  dropInto,
  intoDisabled,
  collapsed,
  renaming,
  onOpen,
  onToggle,
  onMenu,
  onRename,
}: RowProps) {
  const draggable = useDraggable({ id: node.id, disabled: renaming });
  const droppable = useDroppable({
    id: intoId(node.id),
    disabled: node.kind !== "folder" || intoDisabled,
  });

  if (renaming) {
    return (
      <div className="tree-row renaming">
        <input
          className="conv-rename-input"
          defaultValue={node.name}
          maxLength={120}
          autoFocus
          aria-label={t.rename}
          onBlur={(e) => onRename(node, e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
            else if (e.key === "Escape") {
              e.currentTarget.value = node.name;
              e.currentTarget.blur();
            }
          }}
        />
      </div>
    );
  }

  const isFolder = node.kind === "folder";
  const hasChildren = node.children.length > 0;

  return (
    <div
      ref={(el) => {
        draggable.setNodeRef(el);
        // a folder is also a target: dropping on it means "inside it"
        if (isFolder) droppable.setNodeRef(el);
      }}
      // NB: no transform. The travelling copy is the DragOverlay; moving the
      // source as well would mean the row leaves its place the instant it is
      // picked up, when nothing has actually happened yet.
      className={[
        "tree-row",
        isFolder ? "folder" : "conv",
        selected ? "active" : "",
        dragging ? "dragging" : "",
        dropInto ? "drop-into" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      {...draggable.attributes}
      {...draggable.listeners}
      data-row-id={node.id}
      {...middleClickOpens(isFolder ? `/f/${node.id}` : `/c/${node.id}`)}
      role="treeitem"
      aria-selected={selected}
      aria-expanded={isFolder ? !collapsed : undefined}
      tabIndex={0}
      onClick={() => onOpen(node)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(node);
        }
      }}
    >
      {/*
        An empty folder has nothing to expand, so it gets no twisty — and no
        gap where one would have been either. Keeping the slot reserved (a
        hidden button) lined the names up in a column, but it also left every
        empty folder sitting a notch to the right of nothing, which reads as an
        indent it does not have.
      */}
      {isFolder && hasChildren ? (
        <button
          className="tree-chevron"
          aria-label={collapsed ? t.expand : t.collapse}
          onClick={(e) => {
            e.stopPropagation();
            onToggle(node.id);
          }}
          onPointerDown={(e) => e.stopPropagation()}
        >
          <ChevronDownIcon size={13} className={collapsed ? "" : "open"} />
        </button>
      ) : null}
      {isFolder ? (
        collapsed ? (
          <FolderIcon size={14} />
        ) : (
          <FolderOpenIcon size={14} />
        )
      ) : (
        <ConversationIcon size={13} />
      )}
      <span className="tree-name">{node.name}</span>
      {node.source === "mcp" && <span className="conv-item-badge">MCP</span>}
      <button
        className="tree-menu-btn"
        aria-label={t.moreActions}
        onClick={(e) => {
          e.stopPropagation();
          onMenu(node, e.currentTarget);
        }}
        onPointerDown={(e) => e.stopPropagation()}
      >
        <DotsIcon size={14} />
      </button>
    </div>
  );
}

/* ----------------------------------------------------------------- the tab */

export function ProjectTree() {
  const folders = useProjects((s) => s.folders);
  const conversations = useProjects((s) => s.conversations);
  const treeLoaded = useProjects((s) => s.treeLoaded);
  const treeError = useProjects((s) => s.treeError);
  const collapsed = useProjects((s) => s.collapsed);
  const toggleCollapse = useProjects((s) => s.toggleCollapse);
  const createFolder = useProjects((s) => s.createFolder);
  const renameFolder = useProjects((s) => s.renameFolder);
  const archiveFolder = useProjects((s) => s.archiveFolder);
  const moveItem = useProjects((s) => s.moveItem);
  const loadTree = useProjects((s) => s.loadTree);

  const renameConversation = useChat((s) => s.renameConversation);
  const setArchived = useChat((s) => s.setArchived);
  const navigate = useNavigate();
  const { convId, folderId } = useParams<{ convId: string; folderId: string }>();

  const [activeId, setActiveId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);
  const [menu, setMenu] = useState<{ node: TreeNode; top: number } | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  /**
   * A folder row beats a gap whenever the pointer is on it.
   *
   * pointerWithin reports every droppable under the pointer, and the gaps sit
   * hard against the rows — a gap that lights up also grows from 6px to 30px,
   * which shoves the row you were aiming at out from under the cursor. The
   * result was that dropping ONTO a folder mostly inserted next to it instead,
   * and the fuller the folder the more gaps there were to catch the pointer.
   *
   * Dropping on a folder means "into it". Said here rather than hoped for.
   */
  const collision = useCallback((args: Parameters<typeof pointerWithin>[0]) => {
    const hits = pointerWithin(args);
    const onFolder = hits.find((c) => String(c.id).startsWith("into|"));
    return onFolder ? [onFolder] : hits;
  }, []);

  const sensors = useSensors(
    // a small movement threshold keeps a plain click a click
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    // press-and-hold on touch, so swiping still scrolls the list
    useSensor(TouchSensor, { activationConstraint: { delay: 220, tolerance: 8 } }),
  );

  const roots = useMemo(
    () => buildTree(folders, conversations),
    [folders, conversations],
  );

  const byId = useMemo(() => {
    const map = new Map<string, TreeNode>();
    const walk = (nodes: TreeNode[]) => {
      for (const n of nodes) {
        map.set(n.id, n);
        walk(n.children);
      }
    };
    walk(roots);
    return map;
  }, [roots]);

  const active = activeId ? byId.get(activeId) ?? null : null;

  /** changes exactly when the arrangement does, and never otherwise */
  const arrangement = useMemo(() => {
    const parts: string[] = [];
    const walk = (nodes: TreeNode[]) => {
      for (const n of nodes) {
        parts.push(n.id, collapsed[n.id] ? "-" : "+");
        walk(n.children);
      }
    };
    walk(roots);
    return parts.join(",");
  }, [roots, collapsed]);

  useSmoothReorder(listRef, arrangement, !activeId);

  /** nothing inside the folder being dragged may become its own home */
  const blocked = useMemo(
    () =>
      active?.kind === "folder"
        ? descendantFolderIds(folders, active.id)
        : new Set<string>(),
    [active, folders],
  );

  function onDragStart(e: DragStartEvent) {
    setActiveId(String(e.active.id));
    setOverId(null);
    setMenu(null);
    document.body.classList.add("dragging-tree");
  }

  function onDragOver(e: DragOverEvent) {
    setOverId(e.over ? String(e.over.id) : null);
  }

  function reset() {
    setActiveId(null);
    setOverId(null);
    document.body.classList.remove("dragging-tree");
  }

  function onDragEnd(e: DragEndEvent) {
    const node = active;
    const over = e.over ? String(e.over.id) : null;
    reset();
    if (!node || !over) return;

    if (over.startsWith("into|")) {
      const target = byId.get(over.slice("into|".length));
      if (!target || blocked.has(target.id)) return;
      const index =
        node.kind === "folder" ? target.folderChildren : target.convChildren;
      void moveItem(node.kind, node.id, target.id, index);
      return;
    }

    const target = resolveDrop(over, node);
    if (!target || isNoop(node, target)) return;
    void moveItem(node.kind, node.id, target.parentId, target.index);
  }

  /**
   * A new folder is made immediately under a default name and its workspace
   * opens — the name is one field away there, which beats being asked for it
   * before the thing even exists.
   */
  async function onNewFolder(parentId: string | null) {
    setMenu(null);
    const siblings = folders.filter((f) => f.parent_id === parentId).map((f) => f.name);
    let name: string = t.newFolder;
    for (let i = 2; siblings.includes(name); i++) name = t.newFolderN(i);

    const folder = await createFolder(name, parentId);
    if (!folder) return;
    if (folder.parent_id && collapsed[folder.parent_id]) toggleCollapse(folder.parent_id);
    navigate(`/f/${folder.id}`);
  }

  async function onRemoveFromFolder(node: TreeNode) {
    setMenu(null);
    if (node.parentId) await moveItem("conversation", node.id, null, 0);
  }

  async function commitRename(node: TreeNode, raw: string) {
    setRenamingId(null);
    const name = raw.trim();
    if (!name || name === node.name) return;
    if (node.kind === "folder") await renameFolder(node.id, name);
    else await renameConversation(node.id, name);
  }

  async function onArchive(node: TreeNode) {
    setMenu(null);
    if (!(await confirmArchive(node.kind, node.name))) return;
    if (node.kind === "folder") await archiveFolder(node.id, true);
    else await setArchived(node.id, true);
  }

  function onOpen(node: TreeNode) {
    navigate(node.kind === "folder" ? `/f/${node.id}` : `/c/${node.id}`);
  }

  /**
   * Dismiss the row menu the way the 对话 list does: by noticing a press
   * somewhere else.
   *
   * It used to be a full-screen backdrop, which closes on a click and also
   * swallows every wheel and every finger — so the list would not scroll while
   * a menu was open, which is not what anyone expects of a small menu. Nothing
   * has to be blocked: `top` is measured in the list's own scrolled
   * coordinates, so the menu travels with the row it belongs to.
   */
  useEffect(() => {
    if (!menu) return;
    function onDocPress(e: PointerEvent) {
      const target = e.target as Element | null;
      // The dots are not "somewhere else": pressing them is how the menu is
      // toggled, and closing here would have it shut on the press and open
      // again on the click — which looks exactly like a button that does
      // nothing.
      if (target?.closest?.(".tree-menu-btn")) return;
      if (menuRef.current && !menuRef.current.contains(target as Node)) {
        setMenu(null);
      }
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") setMenu(null);
    }
    document.addEventListener("pointerdown", onDocPress);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("pointerdown", onDocPress);
      document.removeEventListener("keydown", onEsc);
    };
  }, [menu]);

  function openMenu(node: TreeNode, anchor: HTMLElement) {
    // the same button both ways: pressing the dots of the row whose menu is
    // already showing puts it away
    if (menu?.node.id === node.id) {
      setMenu(null);
      return;
    }
    const box = anchor.getBoundingClientRect();
    const listBox = listRef.current?.getBoundingClientRect();
    setMenu({
      node,
      top: box.bottom - (listBox?.top ?? 0) + (listRef.current?.scrollTop ?? 0) + 2,
    });
  }

  const intoTarget = overId?.startsWith("into|") ? overId.slice("into|".length) : null;

  function renderNodes(nodes: TreeNode[], parentId: string | null): React.ReactNode {
    const out: React.ReactNode[] = [];
    let folderCount = 0;
    let convCount = 0;

    for (const node of nodes) {
      out.push(
        <DropGap
          key={`gap-${node.id}`}
          parentId={parentId}
          folderIndex={folderCount}
          convIndex={convCount}
          disabled={!activeId || (parentId !== null && blocked.has(parentId))}
          overId={overId}
        />,
      );
      const isFolder = node.kind === "folder";
      const isCollapsed = !!collapsed[node.id];
      out.push(
        <div className="tree-node" key={node.id}>
          <Row
            node={node}
            selected={node.id === convId || node.id === folderId}
            dragging={node.id === activeId}
            dropInto={intoTarget === node.id}
            intoDisabled={!activeId || blocked.has(node.id)}
            collapsed={isCollapsed}
            renaming={renamingId === node.id}
            onOpen={onOpen}
            onToggle={toggleCollapse}
            onMenu={openMenu}
            onRename={commitRename}
          />
          {/*
            renderNodes closes every level with its own tail gap, which IS
            "the end of this folder" — emitting another one here gave two
            droppables the same id, and then neither of them worked.

            Only when the folder is actually showing its contents, though. A
            folder with nothing open under it offers no gap of its own: there
            is nothing for one to sit at the end of, and a stray blank line
            below a closed folder claims to mean something it does not. The
            way in is to drop on the row itself.
          */}
          {isFolder && !isCollapsed && node.children.length > 0 && (
            <div className="tree-children">{renderNodes(node.children, node.id)}</div>
          )}
        </div>,
      );
      if (isFolder) folderCount += 1;
      else convCount += 1;
    }

    // and the end of this level
    out.push(
      <DropGap
        key="gap-tail"
        parentId={parentId}
        folderIndex={folderCount}
        convIndex={convCount}
        disabled={!activeId || (parentId !== null && blocked.has(parentId))}
        overId={overId}
      />,
    );
    return out;
  }

  if (!treeLoaded) {
    return (
      <div className="conv-list" style={{ textAlign: "center", paddingTop: 24 }}>
        {treeError ? (
          <div className="conv-list-empty">
            {treeError}
            <button
              className="info-action-btn"
              style={{ marginTop: 10 }}
              onClick={() => void loadTree()}
            >
              {t.retry}
            </button>
          </div>
        ) : (
          <span className="spinner" />
        )}
      </div>
    );
  }

  return (
    <div className="project-pane">
      <button className="new-folder-btn" onClick={() => void onNewFolder(null)}>
        <NewFolderIcon size={14} />
        <span>{t.createFolder}</span>
      </button>

      <div className="conv-list tree-list" ref={listRef} role="tree">
        <DndContext
          sensors={sensors}
          collisionDetection={collision}
          // a gap changes size as it opens, so its rectangle has to be re-read
          // rather than measured once at the start of the drag
          measuring={{ droppable: { strategy: MeasuringStrategy.Always } }}
          onDragStart={onDragStart}
          onDragOver={onDragOver}
          onDragEnd={onDragEnd}
          onDragCancel={reset}
        >
          {renderNodes(roots, null)}

          <DragOverlay dropAnimation={null}>
            {active ? (
              <div className="tree-row drag-ghost">
                {active.kind === "folder" ? (
                  <FolderIcon size={14} />
                ) : (
                  <ConversationIcon size={13} />
                )}
                <span className="tree-name">{active.name}</span>
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>

        {roots.length === 0 && <div className="conv-list-empty">{t.noFolders}</div>}

        {menu && (
          <>
            <div ref={menuRef} className="conv-menu tree-menu" style={{ top: menu.top }}>
              {/* the same actions a conversation row offers in the 对话 list,
                  plus the one thing only a folder can do. Opening is what
                  clicking the row already does — it needs no entry. */}
              <button
                onClick={() => {
                  const it = menu.node;
                  setMenu(null);
                  setRenamingId(it.id);
                }}
              >
                {t.rename}
              </button>
              {menu.node.kind === "folder" ? (
                <button onClick={() => void onNewFolder(menu.node.id)}>
                  {t.createSubfolder}
                </button>
              ) : (
                <button
                  onClick={() => void onRemoveFromFolder(menu.node)}
                  disabled={!menu.node.parentId}
                >
                  {t.moveOutOfFolder}
                </button>
              )}
              <button onClick={() => void onArchive(menu.node)}>{t.archive}</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
