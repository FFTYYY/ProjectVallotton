import { useEffect, useState } from "react";

import {
  DndContext,
  MouseSensor,
  TouchSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import { restrictToParentElement, restrictToVerticalAxis } from "@dnd-kit/modifiers";
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

import type { QuickPhrase } from "../api/types";
import { confirm } from "../store/confirm";
import { usePhrases } from "../store/phrases";
import { EditIcon, GripIcon, PlusIcon, TrashIcon } from "./Icons";
import { Tooltip } from "./Tooltip";
import { t } from "../uiText";

/**
 * The quick phrases, wherever they are being kept.
 *
 * Two places show them and they must not drift: the right rail, where one can
 * also be dropped into what you are writing, and the settings page, where the
 * choice being made is which one a NEW conversation will send again.
 *
 * Deliberately unaware of whose choice it is showing. It is handed the id that
 * is currently picked and a way to pick another; whether that ends up on the
 * conversation in front of you or on the app's defaults is the caller's
 * business — the same division as GenerationControls.
 */

interface Props {
  /** which phrase the run sends again, from wherever that is decided */
  chosenId: string | null;
  onChoose: (id: string) => void;
  /** what the radio means here, in a word or two */
  chooseHint: string;
  /** dropping one into what you are writing, where there is something to write in */
  onApply?: (content: string) => void;
  /** the blank editor is opened from the caller's own heading */
  creating: boolean;
  onCreatingChange: (open: boolean) => void;
  /**
   * Whether the order can be changed here.
   *
   * The caller's to decide, like everything else about this list. The order is
   * one order, shared by both places that show it — but rearranging is a thing
   * you sit down to do, not something to trip over while reaching for a phrase
   * mid-sentence in the rail.
   */
  sortable?: boolean;
}

export function PhraseList({
  chosenId,
  onChoose,
  chooseHint,
  onApply,
  creating,
  onCreatingChange,
  sortable = false,
}: Props) {
  const phrases = usePhrases((s) => s.phrases);
  const loaded = usePhrases((s) => s.loaded);
  const load = usePhrases((s) => s.load);
  const create = usePhrases((s) => s.create);
  const update = usePhrases((s) => s.update);
  const remove = usePhrases((s) => s.remove);
  const reorder = usePhrases((s) => s.reorder);

  const [editingId, setEditingId] = useState<string | null>(null);

  const sensors = useSensors(
    // a small movement threshold keeps a plain click a click
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    // press-and-hold on touch, so swiping still scrolls the page
    useSensor(TouchSensor, { activationConstraint: { delay: 220, tolerance: 8 } }),
  );

  function onDragEnd(e: DragEndEvent) {
    const { active, over } = e;
    if (!over || active.id === over.id) return;
    const ids = phrases.map((p) => p.id);
    const from = ids.indexOf(String(active.id));
    const to = ids.indexOf(String(over.id));
    if (from < 0 || to < 0) return;
    ids.splice(to, 0, ...ids.splice(from, 1));
    void reorder(ids);
  }

  useEffect(() => {
    if (!loaded) void load();
  }, [loaded, load]);

  async function onDelete(p: QuickPhrase) {
    const ok = await confirm({
      title: t.deletePhraseTitle(p.title),
      confirmLabel: t.delete,
      danger: true,
    });
    if (ok) await remove(p.id);
  }

  return (
    <>
      {creating && (
        <PhraseEditor onSave={create} onCancel={() => onCreatingChange(false)} />
      )}

      {loaded && phrases.length === 0 && !creating && (
        <div className="rightbar-empty">{t.noPhrases}</div>
      )}

      {/* The drag context only exists where dragging does. In the rail this
          renders the same rows with no sensors attached to them at all, so a
          press on a phrase cannot become a drag by accident. */}
      <Sortable enabled={sortable} ids={phrases.map((p) => p.id)} onDragEnd={onDragEnd}
                sensors={sensors}>
      <div className="phrase-list">
        {phrases.map((p) =>
          editingId === p.id ? (
            <PhraseEditor
              key={p.id}
              initial={p}
              onSave={(t, c) => update(p.id, t, c)}
              onCancel={() => setEditingId(null)}
            />
          ) : (
            <PhraseRow key={p.id} id={p.id} sortable={sortable}>
              {onApply ? (
                <button
                  className="phrase-apply"
                  title={p.content}
                  onClick={() => onApply(p.content)}
                >
                  {p.title}
                </button>
              ) : (
                // nowhere to drop it into here, so it is a label — same slip,
                // without the affordance of one that does something
                <span className="phrase-apply as-label" title={p.content}>
                  {p.title}
                </span>
              )}
              <button
                className="phrase-action"
                aria-label={t.editPhrase}
                onClick={() => {
                  onCreatingChange(false);
                  setEditingId(p.id);
                }}
              >
                <EditIcon size={13} />
              </button>
              <button
                className="phrase-action danger"
                aria-label={t.deletePhrase}
                onClick={() => void onDelete(p)}
              >
                <TrashIcon size={13} />
              </button>
              {/* which phrase continuous mode sends. One of them, so a radio —
                  and it is a button rather than an <input> because the native
                  control cannot be drawn to match anything else here. */}
              <Tooltip content={chooseHint} prefer="left">
                <button
                  role="radio"
                  aria-checked={chosenId === p.id}
                  aria-label={t.labelledQuote(chooseHint, p.title)}
                  className={`phrase-radio ${chosenId === p.id ? "on" : ""}`}
                  onClick={() => onChoose(p.id)}
                >
                  <span />
                </button>
              </Tooltip>
            </PhraseRow>
          ),
        )}
      </div>
      </Sortable>
    </>
  );
}

/** Wraps the rows in a drag context, or in nothing at all. */
function Sortable({
  enabled,
  ids,
  onDragEnd,
  sensors,
  children,
}: {
  enabled: boolean;
  ids: string[];
  onDragEnd: (e: DragEndEvent) => void;
  sensors: ReturnType<typeof useSensors>;
  children: React.ReactNode;
}) {
  if (!enabled) return <>{children}</>;
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      // a phrase can only move up and down its own list
      modifiers={[restrictToVerticalAxis, restrictToParentElement]}
      onDragEnd={onDragEnd}
    >
      <SortableContext items={ids} strategy={verticalListSortingStrategy}>
        {children}
      </SortableContext>
    </DndContext>
  );
}

/**
 * One row, which knows how to be dragged only where that is allowed.
 *
 * The handle is its own control rather than the whole row being draggable:
 * every other thing in here is a button, and a row you can pick up by its
 * delete button is a row that will one day be picked up by its delete button.
 */
function PhraseRow({
  id,
  sortable,
  children,
}: {
  id: string;
  sortable: boolean;
  children: React.ReactNode;
}) {
  const s = useSortable({ id, disabled: !sortable });
  return (
    <div
      ref={s.setNodeRef}
      className={`phrase-item ${s.isDragging ? "dragging" : ""}`}
      style={{
        transform: CSS.Transform.toString(s.transform),
        transition: s.transition,
      }}
    >
      {sortable && (
        <button
          className="phrase-grip"
          aria-label={t.dragToSort}
          ref={s.setActivatorNodeRef}
          {...s.attributes}
          {...s.listeners}
        >
          <GripIcon size={13} />
        </button>
      )}
      {children}
    </div>
  );
}

/** the "+" a caller puts in its own heading, so both headings stay native */
export function NewPhraseButton({ onToggle }: { onToggle: () => void }) {
  return (
    <button className="icon-btn" aria-label={t.newPhrase} title={t.newPhrase} onClick={onToggle}>
      <PlusIcon size={16} />
    </button>
  );
}

export function PhraseEditor({
  initial,
  onSave,
  onCancel,
}: {
  initial?: QuickPhrase;
  onSave: (title: string, content: string) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [title, setTitle] = useState(initial?.title ?? "");
  const [content, setContent] = useState(initial?.content ?? "");
  const [busy, setBusy] = useState(false);

  async function save() {
    if (busy || !title.trim() || !content.trim()) return;
    setBusy(true);
    try {
      if (await onSave(title.trim(), content)) onCancel();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="phrase-editor">
      <input
        className="info-input"
        placeholder={t.phraseTitlePlaceholder}
        value={title}
        maxLength={100}
        autoFocus
        onChange={(e) => setTitle(e.target.value)}
      />
      <textarea
        className="phrase-content-input"
        placeholder={t.phraseContentPlaceholder}
        value={content}
        maxLength={4000}
        rows={4}
        onChange={(e) => setContent(e.target.value)}
      />
      <div className="phrase-editor-actions">
        <button
          className="info-action-btn primary"
          disabled={busy || !title.trim() || !content.trim()}
          onClick={() => void save()}
        >
          {t.save}
        </button>
        <button className="info-action-btn" onClick={onCancel}>
          {t.cancel}
        </button>
      </div>
    </div>
  );
}
