/**
 * A folder's workspace: its name, its 设定 and 角色, and the button that
 * deletes it. Everything about a folder is edited here — nothing about it
 * lives behind a browser dialog.
 *
 * New entries are created straight away under a default name and open for
 * editing; naming a thing before it exists is a question the app can answer
 * on its own.
 */

import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import type { Character, Setting, Variant, TagLine } from "../api/types";
import { confirmArchive } from "../lib/archive";
import { STALE_POLL_MS, anyStale, staleHint } from "../lib/linesStale";
import { confirm } from "../store/confirm";
import { useProjects } from "../store/projects";
import { Collapse } from "./Collapse";
import {
  ArchiveIcon,
  CharacterIcon,
  ChevronDownIcon,
  PlusIcon,
  SettingEntryIcon,
  SyncIcon,
  TrashIcon,
} from "./Icons";
import { t } from "../uiText";

/** a textarea as tall as its text, so the words never scroll out of sight */
function fitHeight(el: HTMLTextAreaElement | null) {
  if (!el) return;
  el.style.height = "0";
  el.style.height = `${el.scrollHeight}px`;
}

/** "新设定", then "新设定2" — the server refuses a duplicate title */
function freshName(base: string, taken: string[]): string {
  if (!taken.includes(base)) return base;
  for (let i = 2; i < 500; i++) {
    const candidate = `${base} ${i}`;
    if (!taken.includes(candidate)) return candidate;
  }
  return `${base} ${taken.length + 1}`;
}

/** a labelled field that writes back when it loses focus */
function Field({
  label,
  value,
  placeholder,
  rows,
  maxLength,
  autoFocus,
  onCommit,
}: {
  label?: string;
  value: string;
  placeholder?: string;
  rows?: number;
  maxLength?: number;
  autoFocus?: boolean;
  onCommit: (next: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  function commit() {
    const next = rows ? draft : draft.trim();
    if (next !== value) onCommit(next);
  }

  return (
    <label className="lib-field">
      {label && <span className="info-label">{label}</span>}
      {rows ? (
        <textarea
          className="lib-textarea"
          value={draft}
          rows={rows}
          placeholder={placeholder}
          maxLength={maxLength}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
        />
      ) : (
        <input
          className="info-input"
          value={draft}
          placeholder={placeholder}
          maxLength={maxLength}
          autoFocus={autoFocus}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
          }}
        />
      )}
    </label>
  );
}

/** the lines are behind the text; they catch up on their own */
function StaleMark() {
  return (
    <span className="lib-stale" role="img" aria-label={staleHint()} title={staleHint()}>
      <SyncIcon size={12} />
    </span>
  );
}

/**
 * An entry's tag lines, read-only: an inherited entry is edited where it
 * lives. The lines are what the app splices into prompts byte for byte.
 */
function TagLines({ lines }: { lines?: TagLine[] }) {
  if (!lines?.length) return null;
  return (
    <div className="lib-lines">
      {lines.map((ln) => (
        <div key={ln.id || ln.part}>
          <span className="lib-line-part">{ln.part}</span>
          <code>{ln.tags}</code>
        </div>
      ))}
    </div>
  );
}

/**
 * An entry's tag lines, editable by hand.
 *
 * The lines are normally written by the model — the lead when it records an
 * entry, the line writer after the text is edited — but what they say is the
 * user's to check and correct: a row's id is the app's and stays (pictures
 * already drawn point at it); its part and tags are free text. A row with no
 * tags is not saved. Saving by hand counts the lines as fresh, so the line
 * writer leaves them alone.
 */
function TagLinesEditor({
  lines,
  onCommit,
}: {
  lines?: TagLine[];
  onCommit: (next: TagLine[]) => void;
}) {
  const [draft, setDraft] = useState<TagLine[]>(lines ?? []);
  const stored = JSON.stringify(lines ?? []);
  // the store lands a new copy after every save (ids assigned, rows dropped):
  // follow it, but only when it actually changed, or typing would be undone
  useEffect(() => setDraft(JSON.parse(stored) as TagLine[]), [stored]);

  function clean(rows: TagLine[]): TagLine[] {
    return rows
      .map((ln) => ({ id: ln.id, part: ln.part.trim(), tags: ln.tags.trim() }))
      .filter((ln) => ln.tags);
  }

  function save(rows: TagLine[]) {
    const next = clean(rows);
    const was = clean(lines ?? []);
    const same =
      next.length === was.length &&
      next.every((ln, i) => ln.part === was[i].part && ln.tags === was[i].tags);
    if (!same) onCommit(next);
  }

  function edit(i: number, patch: Partial<TagLine>) {
    setDraft((d) => d.map((ln, j) => (j === i ? { ...ln, ...patch } : ln)));
  }

  function remove(i: number) {
    const next = draft.filter((_, j) => j !== i);
    setDraft(next);
    save(next);
  }

  return (
    <div className="lib-lines lib-lines-edit">
      <div className="lib-lines-head">
        <span className="info-label">{t.tagLines}</span>
        <button
          className="icon-btn"
          aria-label={t.addLine}
          onClick={() => setDraft((d) => [...d, { id: "", part: "", tags: "" }])}
        >
          <PlusIcon size={14} />
        </button>
      </div>
      {draft.map((ln, i) => (
        <div key={ln.id || `new-${i}`} className="lib-line-row">
          <input
            className="info-input"
            value={ln.part}
            placeholder={t.part}
            maxLength={40}
            aria-label={t.part}
            onChange={(e) => edit(i, { part: e.target.value })}
            onBlur={() => save(draft)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
            }}
          />
          <textarea
            className="lib-textarea lib-line-tags"
            value={ln.tags}
            rows={1}
            placeholder="tags"
            aria-label={t.lineTags}
            ref={fitHeight}
            onInput={(e) => fitHeight(e.currentTarget)}
            onChange={(e) => edit(i, { tags: e.target.value })}
            onBlur={() => save(draft)}
          />
          <button
            className="phrase-action danger"
            aria-label={t.deleteLine}
            onClick={() => remove(i)}
          >
            <TrashIcon size={13} />
          </button>
        </div>
      ))}
      {draft.length === 0 && (
        <div className="lib-note">{t.empty}</div>
      )}
    </div>
  );
}

function SettingCard({
  folderId,
  setting,
  readOnly,
  autoOpen,
}: {
  folderId: string;
  setting: Setting;
  readOnly?: boolean;
  autoOpen?: boolean;
}) {
  const updateSetting = useProjects((s) => s.updateSetting);
  const deleteSetting = useProjects((s) => s.deleteSetting);
  const [open, setOpen] = useState(!!autoOpen);
  // the store lands the new row one render before `fresh` is set, so seeding
  // state at mount is too early — open it when the flag actually arrives
  useEffect(() => {
    if (autoOpen) setOpen(true);
  }, [autoOpen]);

  async function onDelete() {
    if (
      await confirm({
        title: t.deleteSettingTitle(setting.title),
        body: t.deleteSettingBody,
        confirmLabel: t.delete,
        danger: true,
      })
    ) {
      await deleteSetting(folderId, setting.id);
    }
  }

  return (
    <div className={`lib-card ${readOnly ? "inherited" : ""}`}>
      <div className="lib-card-head">
        <button className="lib-card-toggle" onClick={() => setOpen((v) => !v)}>
          <ChevronDownIcon size={13} className={open ? "open" : ""} />
          <SettingEntryIcon size={14} />
          <span className="lib-card-title">{setting.title || t.untitledParen}</span>
          {setting.lines_stale && <StaleMark />}
        </button>
        {readOnly ? (
          <span className="lib-inherit-tag">{t.inherited}</span>
        ) : (
          <button
            className="phrase-action danger"
            aria-label={t.deleteSetting}
            onClick={() => void onDelete()}
          >
            <TrashIcon size={13} />
          </button>
        )}
      </div>
      {/* Folded away rather than dropped. Unmounting it the instant the card
          opens made the card briefly SHORTER than it was — the preview gone,
          the body still animating up from nothing — and at the bottom of the
          page that dip and recovery is a visible jump. Two collapses of the
          same duration, one closing while the other opens, only ever add up. */}
      <Collapse open={!open}>
        {setting.content ? (
          <div className="lib-card-preview">{setting.content}</div>
        ) : null}
      </Collapse>
      <Collapse open={open}>
        {readOnly ? (
          <div className="lib-card-body">
            <div className="lib-block">
              <div className="lib-readonly">{setting.content || t.emptyParen}</div>
              <TagLines lines={setting.tags} />
            </div>
          </div>
        ) : (
          <div className="lib-card-body">
            <Field
              label={t.title}
              value={setting.title}
              maxLength={120}
              autoFocus={autoOpen}
              onCommit={(title) =>
                title && void updateSetting(folderId, setting.id, { title })
              }
            />
            <div className="lib-field">
              <span className="info-label">{t.content}</span>
              <div className="lib-block">
                <Field
                  value={setting.content}
                  rows={4}
                  onCommit={(content) => void updateSetting(folderId, setting.id, { content })}
                />
                <TagLinesEditor
                  lines={setting.tags}
                  onCommit={(tags) => void updateSetting(folderId, setting.id, { tags })}
                />
              </div>
            </div>
          </div>
        )}
      </Collapse>
    </div>
  );
}

function VariantRow({
  folderId,
  character,
  variant,
  readOnly,
}: {
  folderId: string;
  character: Character;
  variant: Variant;
  readOnly?: boolean;
}) {
  const updateVariant = useProjects((s) => s.updateVariant);
  const deleteVariant = useProjects((s) => s.deleteVariant);

  async function onDelete() {
    const ok = await confirm({
      title: t.deleteVariantTitle(variant.title || t.untitled),
      confirmLabel: t.delete,
      danger: true,
    });
    if (ok) await deleteVariant(folderId, character.id, variant.id);
  }

  return (
    <div className="lib-variant">
      {readOnly ? (
        <>
          <div className="lib-variant-title">
            {variant.title}
            {variant.lines_stale && <StaleMark />}
          </div>
          <div className="lib-block">
            <div className="lib-readonly">{variant.content || t.emptyParen}</div>
            <TagLines lines={variant.tags} />
          </div>
        </>
      ) : (
        <>
          <div className="lib-variant-head">
            <Field
              value={variant.title}
              maxLength={120}
              onCommit={(title) =>
                title && void updateVariant(folderId, character.id, variant.id, { title })
              }
            />
            {variant.lines_stale && <StaleMark />}
            <button
              className="phrase-action danger"
              aria-label={t.deleteVariant}
              onClick={() => void onDelete()}
            >
              <TrashIcon size={13} />
            </button>
          </div>
          <div className="lib-block">
            <Field
              value={variant.content}
              rows={3}
              onCommit={(content) =>
                void updateVariant(folderId, character.id, variant.id, { content })
              }
            />
            <TagLinesEditor
              lines={variant.tags}
              onCommit={(tags) =>
                void updateVariant(folderId, character.id, variant.id, { tags })
              }
            />
          </div>
        </>
      )}
    </div>
  );
}

function CharacterCard({
  folderId,
  character,
  readOnly,
  autoOpen,
}: {
  folderId: string;
  character: Character;
  readOnly?: boolean;
  autoOpen?: boolean;
}) {
  const updateCharacter = useProjects((s) => s.updateCharacter);
  const deleteCharacter = useProjects((s) => s.deleteCharacter);
  const createVariant = useProjects((s) => s.createVariant);
  const [open, setOpen] = useState(!!autoOpen);
  useEffect(() => {
    if (autoOpen) setOpen(true);
  }, [autoOpen]);

  async function onDelete() {
    if (
      await confirm({
        title: t.deleteCharacterTitle(character.name),
        body: t.deleteCharacterBody(character.variants.length),
        confirmLabel: t.delete,
        danger: true,
      })
    ) {
      await deleteCharacter(folderId, character.id);
    }
  }

  function onAddVariant() {
    void createVariant(
      folderId,
      character.id,
      freshName(t.newVariant, character.variants.map((v) => v.title)),
    );
  }

  return (
    <div className={`lib-card ${readOnly ? "inherited" : ""}`}>
      <div className="lib-card-head">
        <button className="lib-card-toggle" onClick={() => setOpen((v) => !v)}>
          <ChevronDownIcon size={13} className={open ? "open" : ""} />
          <CharacterIcon size={14} />
          <span className="lib-card-title">{character.name}</span>
          {character.lines_stale && <StaleMark />}
          <span className="lib-card-count">{t.variantCount(character.variants.length)}</span>
        </button>
        {readOnly ? (
          <span className="lib-inherit-tag">{t.inherited}</span>
        ) : (
          <button
            className="phrase-action danger"
            aria-label={t.deleteCharacter}
            onClick={() => void onDelete()}
          >
            <TrashIcon size={13} />
          </button>
        )}
      </div>
      <Collapse open={!open}>
        {character.description ? (
          <div className="lib-card-preview">{character.description}</div>
        ) : null}
      </Collapse>
      <Collapse open={open}>
        <div className="lib-card-body">
          {readOnly ? (
            <>
              <div className="lib-block">
                <div className="lib-readonly">{character.description || t.emptyParen}</div>
                <TagLines lines={character.tags} />
              </div>
            </>
          ) : (
            <>
              <Field
                label={t.name}
                value={character.name}
                maxLength={120}
                autoFocus={autoOpen}
                onCommit={(name) =>
                  name && void updateCharacter(folderId, character.id, { name })
                }
              />
              <div className="lib-field">
                <span className="info-label">{t.characterText}</span>
                <div className="lib-block">
                  <Field
                    value={character.description}
                    rows={4}
                    placeholder={t.characterTextPlaceholder}
                    onCommit={(description) =>
                      void updateCharacter(folderId, character.id, { description })
                    }
                  />
                  <TagLinesEditor
                    lines={character.tags}
                    onCommit={(tags) => void updateCharacter(folderId, character.id, { tags })}
                  />
                </div>
              </div>
            </>
          )}
          <div className="lib-subhead">
            {t.variants}
            {!readOnly && (
              <button className="icon-btn" aria-label={t.createVariant} onClick={onAddVariant}>
                <PlusIcon size={15} />
              </button>
            )}
          </div>
          {character.variants.map((v) => (
            <VariantRow
              key={v.id}
              folderId={folderId}
              character={character}
              variant={v}
              readOnly={readOnly}
            />
          ))}
        </div>
      </Collapse>
    </div>
  );
}

export function LibraryView() {
  const { folderId = "" } = useParams<{ folderId: string }>();
  const lib = useProjects((s) => s.libraries[folderId]);
  const loadLibrary = useProjects((s) => s.loadLibrary);
  const createSetting = useProjects((s) => s.createSetting);
  const createCharacter = useProjects((s) => s.createCharacter);
  const renameFolder = useProjects((s) => s.renameFolder);
  const archiveFolder = useProjects((s) => s.archiveFolder);
  const navigate = useNavigate();
  /**
   * The entry just created — it opens with the cursor in its name. Tracked by
   * id, not by name: the first thing you do is rename it, and a name-based
   * match would snap the card shut the moment you did.
   */
  const [fresh, setFresh] = useState<string | null>(null);

  useEffect(() => {
    if (folderId) void loadLibrary(folderId);
  }, [folderId, loadLibrary]);

  // While an entry's lines are being rewritten from its text, ask again now
  // and then, so the chip goes away on its own rather than on a reload.
  const waiting = !!lib && anyStale(lib.settings, lib.characters);
  useEffect(() => {
    if (!waiting || !folderId) return;
    const t = setInterval(() => void loadLibrary(folderId), STALE_POLL_MS);
    return () => clearInterval(t);
  }, [waiting, folderId, loadLibrary]);

  if (!lib) {
    return (
      <div className="chat-scroll">
        <div className="chat-column" style={{ textAlign: "center", padding: 40 }}>
          <span className="spinner" />
        </div>
      </div>
    );
  }

  const path = lib.folder.path ?? [lib.folder.name];

  async function onNewSetting() {
    if (!lib) return;
    const name = freshName(t.newSetting, lib.settings.map((s) => s.title));
    const made = await createSetting(folderId, name);
    if (made) setFresh(made.id);
  }

  async function onNewCharacter() {
    if (!lib) return;
    const name = freshName(t.newCharacter, lib.characters.map((c) => c.name));
    const made = await createCharacter(folderId, name);
    if (made) setFresh(made.id);
  }

  async function onArchiveFolder() {
    if (!lib) return;
    // a folder is put away with everything in it, and can always come back
    if (!(await confirmArchive("folder", lib.folder.name))) return;
    await archiveFolder(folderId, true);
    navigate("/");
  }

  return (
    <>
      <header className="chat-header no-rail">
        {path.length > 1 && (
          <span className="chat-header-crumbs">
            {path.slice(0, -1).map((name, i) => (
              <span key={i}>
                {name}
                <span className="crumb-sep">/</span>
              </span>
            ))}
          </span>
        )}
        <span className="chat-header-title">{path[path.length - 1]}</span>
        <span className="chat-header-kind">{t.library}</span>
        {/* the folder's one action, in the corner — inline with the name it
            crowded a 26px display line with a 13px button */}
        <button
          className="info-action-btn danger header-action"
          title={t.archiveFolder}
          onClick={() => void onArchiveFolder()}
        >
          <ArchiveIcon size={14} />
          {t.archive}
        </button>
      </header>
      <div className="chat-scroll">
        <div className="chat-column library-column">
          <div className="workspace-head">
            <input
              key={lib.folder.id}
              className="workspace-name"
              defaultValue={lib.folder.name}
              maxLength={120}
              aria-label={t.folderName}
              onBlur={(e) => {
                const next = e.target.value.trim();
                if (next && next !== lib.folder.name) void renameFolder(folderId, next);
                else e.target.value = lib.folder.name;
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
              }}
            />
            {/* On a phone the header above is not drawn — the bar at the top of
                the window is the title — so the folder's one action comes down
                here, into the row that names it. Wide, the header has it. */}
            <button
              className="info-action-btn danger workspace-action"
              title={t.archiveFolder}
              onClick={() => void onArchiveFolder()}
            >
              <ArchiveIcon size={14} />
              {t.archive}
            </button>
          </div>

          <section className="library-section">
            <div className="library-section-head">
              <SettingEntryIcon size={15} />
              <h2>{t.settingsHeading}</h2>
              <button className="info-action-btn" onClick={() => void onNewSetting()}>
                <PlusIcon size={14} />
                {t.createSetting}
              </button>
            </div>
            {lib.settings.length === 0 && lib.inherited.settings.length === 0 && (
              <div className="rightbar-empty">{t.noSettings}</div>
            )}
            {lib.settings.map((s) => (
              <SettingCard
                key={s.id}
                folderId={folderId}
                setting={s}
                autoOpen={s.id === fresh}
              />
            ))}
            {lib.inherited.settings.map((s) => (
              <SettingCard key={s.id} folderId={folderId} setting={s} readOnly />
            ))}
          </section>

          <section className="library-section">
            <div className="library-section-head">
              <CharacterIcon size={15} />
              <h2>{t.charactersHeading}</h2>
              <button className="info-action-btn" onClick={() => void onNewCharacter()}>
                <PlusIcon size={14} />
                {t.createCharacter}
              </button>
            </div>
            {lib.characters.length === 0 && lib.inherited.characters.length === 0 && (
              <div className="rightbar-empty">{t.noCharacters}</div>
            )}
            {lib.characters.map((c) => (
              <CharacterCard
                key={c.id}
                folderId={folderId}
                character={c}
                autoOpen={c.id === fresh}
              />
            ))}
            {lib.inherited.characters.map((c) => (
              <CharacterCard key={c.id} folderId={folderId} character={c} readOnly />
            ))}
          </section>
        </div>
      </div>
    </>
  );
}
