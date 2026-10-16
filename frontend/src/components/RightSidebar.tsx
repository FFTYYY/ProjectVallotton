import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

import type {
  Conversation,
  SelectionPayload,
} from "../api/types";
import { describeCharacter } from "../lib/library";
import { STALE_POLL_MS, anyStale, staleHint } from "../lib/linesStale";
import { Collapse } from "./Collapse";
import { GenerationControls } from "./GenerationControls";
import { NewPhraseButton, PhraseList } from "./PhraseList";
import { Tooltip } from "./Tooltip";
import { useChat } from "../store/chat";
import { confirmArchive } from "../lib/archive";
import { useProjects } from "../store/projects";
import { useQuota } from "../store/quota";
import { useGen, useSettings } from "../store/settings";
import { useUi } from "../store/ui";
import {
  ArchiveIcon,
  CharacterIcon,
  CheckIcon,
  ChevronDownIcon,
  CloseIcon,
  EditIcon,
  FolderUpIcon,
  PanelRightIcon,
  SettingEntryIcon,
  SyncIcon,
  TrashIcon,
} from "./Icons";
import { t } from "../uiText";

/* ---------------------------------------------------------------- conversation */

/**
 * Archiving is the only way out — a conversation is never destroyed — and it
 * is offered from two places at two widths, so the asking lives here once
 * rather than in both.
 */
function useArchiveToggle(conv: Conversation | undefined) {
  const setArchived = useChat((s) => s.setArchived);
  return async function toggle() {
    if (!conv) return;
    // coming back asks nothing — it is the undo
    if (conv.is_archived) return void setArchived(conv.id, false);
    if (await confirmArchive("conversation", conv.title)) {
      await setArchived(conv.id, true);
    }
  };
}

function ConvSection({ conv }: { conv: Conversation }) {
  const renameConversation = useChat((s) => s.renameConversation);
  const setFolder = useChat((s) => s.setFolder);
  const setRightOpen = useUi((s) => s.setRightOpen);
  const navigate = useNavigate();

  const [title, setTitle] = useState(conv.title);
  useEffect(() => setTitle(conv.title), [conv.id, conv.title]);

  async function commitTitle() {
    const t = title.trim();
    if (t && t !== conv.title) await renameConversation(conv.id, t);
    else setTitle(conv.title);
  }

  const onArchiveToggle = useArchiveToggle(conv);

  const created = new Date(conv.created_at);
  const when = `${created.getFullYear()}/${created.getMonth() + 1}/${created.getDate()}`;

  return (
    <section className="rightbar-section">
      <div className="rightbar-section-title">{t.currentConversation}</div>

      {/* every fact in this section is a label on the left and its value on
          the right, the title included — an unlabelled field at the top read
          as "some box", not as the conversation's name */}
      <div className="meta-row">
        <span className="meta-label">{t.title}</span>
        <input
          className="info-input title-field"
          aria-label={t.conversationTitle}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onBlur={() => void commitTitle()}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
          }}
          maxLength={200}
        />
      </div>
      <div className="meta-row">
        <span className="meta-label">{t.project}</span>
        {conv.folder_path.length ? (
          <span className="meta-value">
            {/* the project's name is the way into its 资料库 */}
            <button
              className="crumbs crumb-link"
              title={t.openProjectLibrary}
              onClick={() => {
                if (window.innerWidth < 861) setRightOpen(false);
                if (conv.folder_id) navigate(`/f/${conv.folder_id}`);
              }}
            >
              {conv.folder_path.join(" / ")}
            </button>
            <button className="crumb-clear" onClick={() => void setFolder(conv.id, null)}>
              {t.moveOut}
            </button>
          </span>
        ) : (
          <span className="meta-value muted">—</span>
        )}
      </div>
      <div className="meta-row">
        <span className="meta-label">{t.createdAt}</span>
        <span className="meta-value muted">
          {when}
          {conv.source === "mcp" ? " · MCP" : ""}
          {conv.is_archived ? t.archivedSuffix : ""}
        </span>
      </div>
      <ContextMeter convId={conv.id} />

      {/* on a phone this sits up in the sheet's own title row instead, where
          it costs no height — see the head below */}
      <div className="info-actions conv-archive-row">
        <button
          className="info-action-btn"
          onClick={() => void onArchiveToggle()}
        >
          <ArchiveIcon size={14} />
          {conv.is_archived ? t.unarchive : t.archive}
        </button>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------------- library selection */

function CharacterRow({
  convId,
  character,
  actions,
}: {
  convId: string;
  character: SelectionPayload["characters"][number];
  /** a conversation-layer entry's own controls, at the end of the row */
  actions?: ReactNode;
}) {
  const toggleVariant = useProjects((s) => s.toggleVariant);
  const [open, setOpen] = useState(false);
  const chosen = character.variants.filter((v) => v.selected);

  return (
    <div className={`sel-item ${character.selected ? "on" : ""}`}>
      {/* a character is on exactly when one of its variants is, so the row
          itself only opens and closes — you pick the costume, not the person */}
      <div className="sel-head">
      <Tooltip content={describeCharacter(character)} className="sel-label-wrap">
        <button className="sel-row" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
          <CharacterIcon size={13} />
          <span className="sel-name">{character.name}</span>
          {(character.lines_stale || character.variants.some((v) => v.lines_stale)) && (
            <span className="sel-stale" role="img" aria-label={staleHint()} title={staleHint()}><SyncIcon size={12} /></span>
          )}
          <span className="sel-sub">
            {chosen.length
              ? chosen.map((v) => v.title).join(t.listSep)
              : t.variantCountShort(character.variants.length)}
          </span>
          <ChevronDownIcon size={12} className={open ? "open" : ""} />
        </button>
      </Tooltip>
      {actions}
      </div>
      <Collapse open={open}>
        <div className="sel-variants">
          {character.variants.map((v) => (
            <Tooltip key={v.id} content={v.content} prefer="vertical">
              <button
                className={`sel-variant ${v.selected ? "on" : ""}`}
                role="switch"
                aria-checked={!!v.selected}
                onClick={() => void toggleVariant(convId, character.id, v.id)}
              >
                {v.title}
              </button>
            </Tooltip>
          ))}
        </div>
      </Collapse>
    </div>
  );
}

/**
 * A conversation-layer entry with its own controls: edit it here, move it up
 * into the project, or drop it. Project entries are edited on the project
 * page; these live and die with the conversation, so they are edited where
 * they are. `row` draws the entry's row and takes the controls, which sit at
 * its end; the edit form opens underneath and the controls leave while it is
 * open.
 */
function OwnEntry({
  conv,
  kind,
  entry,
  row,
}: {
  conv: Conversation;
  kind: "setting" | "character";
  entry: { id: string; title?: string; name?: string; content?: string; description?: string };
  row: (actions: ReactNode) => ReactNode;
}) {
  const removeEntry = useProjects((s) => s.removeEntry);
  const promoteEntry = useProjects((s) => s.promoteEntry);
  const editEntry = useProjects((s) => s.editEntry);
  const [editing, setEditing] = useState(false);
  const label = entry.title ?? entry.name ?? "";
  const body = entry.content ?? entry.description ?? "";
  const [draftLabel, setDraftLabel] = useState(label);
  const [draftBody, setDraftBody] = useState(body);

  async function save() {
    // only what changed: text sent back as it was would read as an edit of
    // the text, and the lines would be rewritten from it for nothing
    const patch: Record<string, string> = {};
    const nextLabel = draftLabel.trim();
    if (nextLabel && nextLabel !== label) patch[kind === "setting" ? "title" : "name"] = nextLabel;
    if (draftBody !== body) patch[kind === "setting" ? "content" : "description"] = draftBody;
    if (Object.keys(patch).length) await editEntry(conv.id, kind, entry.id, patch);
    setEditing(false);
  }

  const actions = (
    <div className="sel-own-actions">
      <button
        className="icon-btn"
        aria-label={t.edit}
        title={t.edit}
        onClick={() => { setDraftLabel(label); setDraftBody(body); setEditing(true); }}
      >
        <EditIcon size={13} />
      </button>
      {conv.folder_id && (
        <button
          className="icon-btn"
          aria-label={t.saveToProject}
          title={t.saveToProjectHint}
          onClick={() => void promoteEntry(conv.id, kind, entry.id)}
        >
          <FolderUpIcon size={13} />
        </button>
      )}
      <button
        className="icon-btn danger"
        aria-label={t.delete}
        title={t.delete}
        onClick={() => void removeEntry(conv.id, kind, entry.id)}
      >
        <TrashIcon size={13} />
      </button>
    </div>
  );
  return (
    <>
      {row(editing ? null : actions)}
      {editing && (
        <div className="sel-own-edit">
          <input
            className="info-input"
            value={draftLabel}
            onChange={(e) => setDraftLabel(e.target.value)}
            maxLength={120}
          />
          <textarea
            className="info-input"
            rows={3}
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
          />
          <div className="sel-own-actions">
            <button className="icon-btn" aria-label={t.save} title={t.save} onClick={() => void save()}>
              <CheckIcon size={14} />
            </button>
            <button className="icon-btn" aria-label={t.cancel} title={t.cancel} onClick={() => setEditing(false)}>
              <CloseIcon size={14} />
            </button>
          </div>
        </div>
      )}
    </>
  );
}

function LibrarySection({ conv }: { conv: Conversation }) {
  const selection = useProjects((s) => s.selection[conv.id]);
  const toggleSetting = useProjects((s) => s.toggleSetting);
  const loadSelection = useProjects((s) => s.loadSelection);

  // An entry whose lines are being rewritten says so with a chip; ask again
  // now and then while one is showing, so the chip clears on its own.
  const waiting = !!selection && anyStale(selection.settings, selection.characters);
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(() => void loadSelection(conv.id), STALE_POLL_MS);
    return () => clearInterval(t);
  }, [waiting, conv.id, loadSelection]);

  // Reading it belongs to ChatView, which does it for the chips as well and
  // does it once. Asking a second time from here was not extra safety: two
  // answers to the same question land in whatever order they like, and the
  // later one used to undo a chip ticked in between. The folder can change
  // while this is on screen, which is a real reason to read again — so that
  // is a reason ChatView is given, not a second reader. See its effect.

  const empty =
    selection && !selection.settings.length && !selection.characters.length;
  // The conversation's own entries first, above the project's: they are the
  // same kind of thing in a nearer layer, and one of them with a project
  // entry's name stands in for it.
  const ownCharacters = selection?.characters.filter((c) => c.conversation_id) ?? [];
  const ownSettings = selection?.settings.filter((s) => s.conversation_id) ?? [];
  const projectCharacters = selection?.characters.filter((c) => !c.conversation_id) ?? [];
  const projectSettings = selection?.settings.filter((s) => !s.conversation_id) ?? [];

  const settingHead = (s: SelectionPayload["settings"][number], actions: ReactNode) => (
    <div className="sel-head">
      <Tooltip content={s.content} className="sel-label-wrap">
        <button
          className="sel-row"
          role="switch"
          aria-checked={!!s.selected}
          onClick={() => void toggleSetting(conv.id, s.id)}
        >
          <SettingEntryIcon size={13} />
          <span className="sel-name">{s.title}</span>
          {s.lines_stale && (
            <span className="sel-stale" role="img" aria-label={staleHint()} title={staleHint()}><SyncIcon size={12} /></span>
          )}
        </button>
      </Tooltip>
      {actions}
    </div>
  );
  const settingRow = (s: SelectionPayload["settings"][number]) => (
    <div key={s.id} className={`sel-item ${s.selected ? "on" : ""}`}>
      {s.conversation_id
        ? <OwnEntry conv={conv} kind="setting" entry={s} row={(a) => settingHead(s, a)} />
        : settingHead(s, null)}
    </div>
  );

  return (
    <section className="rightbar-section">
      <div className="rightbar-section-title">{t.library}</div>
      {!selection && (
        <div style={{ textAlign: "center", padding: 8 }}>
          <span className="spinner" />
        </div>
      )}
      {empty && <div className="rightbar-empty">{t.noEntries}</div>}
      {(ownCharacters.length > 0 || ownSettings.length > 0) && (
        <div className="sel-group-label">{t.thisConversation}</div>
      )}
      {ownCharacters.map((c) => (
        <OwnEntry
          key={c.id}
          conv={conv}
          kind="character"
          entry={c}
          row={(a) => (
            <CharacterRow convId={conv.id} character={c} actions={a} />
          )}
        />
      ))}
      {ownSettings.map(settingRow)}
      {!!projectCharacters.length && (
        <>
          <div className="sel-group-label">{t.charactersHeading}</div>
          {projectCharacters.map((c) => (
            <CharacterRow key={c.id} convId={conv.id} character={c} />
          ))}
        </>
      )}
      {!!projectSettings.length && (
        <>
          <div className="sel-group-label">{t.settingsHeading}</div>
          {projectSettings.map(settingRow)}
        </>
      )}
    </section>
  );
}

/* ---------------------------------------------------------------- quick phrases */

function PhrasesSection() {
  const composer = useUi((s) => s.composer);
  const continuousPhraseId = useGen().continuousPhraseId;
  const setContinuousPhraseId = useSettings((s) => s.setContinuousPhraseId);
  const [creating, setCreating] = useState(false);

  return (
    <section className="rightbar-section">
      <div className="rightbar-section-title">
        {t.quickPhrases}
        <NewPhraseButton onToggle={() => setCreating((v) => !v)} />
      </div>
      {/* the same list the settings page keeps, pointed at this conversation:
          here the radio says what THIS run sends again, there it says what a
          new conversation starts out sending */}
      <PhraseList
        chosenId={continuousPhraseId}
        onChoose={setContinuousPhraseId}
        chooseHint={t.continuousPhraseHint}
        onApply={(content) => composer?.append(content)}
        creating={creating}
        onCreatingChange={setCreating}
      />
    </section>
  );
}

/* ---------------------------------------------------------------- context meter */

function ContextMeter({ convId }: { convId: string }) {
  const messages = useChat((s) => s.messages[convId]);
  // the window belongs to whichever provider is configured — 500k on Grok,
  // 1M on DeepSeek — so it is served, not written down here
  const llm = useQuota((s) => s.quota?.llm);
  const ensure = useQuota((s) => s.ensure);
  useEffect(() => ensure(), [ensure]);

  // the newest assistant turn carries what the model actually consumed
  const used = (messages ?? [])
    .filter((m) => m.role === "assistant" && m.prompt_tokens)
    .slice(-1)[0]?.prompt_tokens;
  if (!used || !llm?.context_limit) return null;

  const limit = llm.context_limit;
  const pct = Math.min((used / limit) * 100, 100);
  const fmt = (n: number) => t.tokensShort(n);

  return (
    <div className="meta-row">
      <span className="meta-label">{t.context}</span>
      <span className="meta-value muted" title={`${llm.label} · ${llm.model}`}>
        {fmt(used)} / {fmt(limit)}
        {pct >= 50 ? t.percentParen(Math.round(pct)) : ""}
      </span>
    </div>
  );
}

/* ---------------------------------------------------------------- settings */

function SettingsSection() {
  // field by field, not one selector returning an object: zustand compares
  // with Object.is, and a fresh object every render never compares equal
  const {
    nImages, quality, autoLibrary, reflectionRounds, continuousRounds,
    continuousPhraseId, stickyRefs,
  } = useGen();
  const apply = useSettings((s) => s.apply);
  const values = useMemo(
    () => ({
      nImages, quality, autoLibrary, reflectionRounds, continuousRounds,
      continuousPhraseId, stickyRefs,
    }),
    [nImages, quality, autoLibrary, reflectionRounds, continuousRounds,
     continuousPhraseId, stickyRefs],
  );
  return (
    <section className="rightbar-section">
      <div className="rightbar-section-title">{t.generationSettings}</div>
      {/* the same controls the settings page uses to set the defaults —
          see GenerationControls, which is where they actually live */}
      <GenerationControls values={values} onChange={apply} />
    </section>
  );
}


/* ---------------------------------------------------------------- shell */

export function RightSidebar({ conv }: { conv?: Conversation }) {
  const onArchiveToggle = useArchiveToggle(conv);
  const rightOpen = useUi((s) => s.rightOpen);
  const setRightOpen = useUi((s) => s.setRightOpen);
  const drawerOpen = useUi((s) => s.drawerOpen);
  // keep the panel mounted through its exit animation before unmounting
  const [render, setRender] = useState(rightOpen);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    if (rightOpen) {
      setClosing(false);
      setRender(true);
    } else if (render) {
      setClosing(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rightOpen]);

  return (
    <>
      {/* One control, one corner: it shows the rail it folds, and inverts
          while that rail is open. While the left drawer is open it is dimmed
          and inert (it sits under the drawer, so it must not be tappable). */}
      <button
        className={`rightbar-toggle ${rightOpen ? "active" : ""} ${drawerOpen ? "inert" : ""}`}
        aria-label={rightOpen ? t.collapseRightBar : t.openRightBar}
        aria-expanded={rightOpen}
        aria-hidden={drawerOpen}
        tabIndex={drawerOpen ? -1 : 0}
        disabled={drawerOpen}
        title={rightOpen ? t.collapse : t.conversationSettingsAndLibrary}
        onClick={() => setRightOpen(!rightOpen)}
      >
        <PanelRightIcon size={18} />
      </button>
      {render && (
        <aside
          className={`rightbar ${closing ? "closing" : ""}`}
          onAnimationEnd={(e) => {
            if (e.target === e.currentTarget && closing) {
              setRender(false);
              setClosing(false);
            }
          }}
        >
          <div className="rightbar-head">
            <span className="rightbar-title">{t.conversationSettings}</span>
            {/* Beside the title and nowhere near the corner: on a phone the
                only other thing in this row is the control that closes the
                sheet, and the two must not be reachable by one thumb. */}
            {conv && (
              <button
                className="info-action-btn danger head-archive"
                onClick={() => void onArchiveToggle()}
              >
                <ArchiveIcon size={13} />
                {conv.is_archived ? t.unarchive : t.archive}
              </button>
            )}
            {/* On a phone the rail is a sheet with no room beside it, so the
                floating control would land on top of its own contents. The
                sheet closes from its own corner instead; on a wide screen
                this is hidden and the pinned control does the job. */}
            <button
              className="rightbar-close"
              aria-label={t.collapse}
              onClick={() => setRightOpen(false)}
            >
              <CloseIcon size={17} />
            </button>
          </div>
          <div className="rightbar-scroll">
            {conv ? (
              <>
                <ConvSection conv={conv} />
                <LibrarySection conv={conv} />
              </>
            ) : (
              <section className="rightbar-section">
                <div className="rightbar-section-title">{t.currentConversation}</div>
                <div className="rightbar-empty">{t.noConversationSelected}</div>
              </section>
            )}
            <SettingsSection />
            <PhrasesSection />
          </div>
        </aside>
      )}
    </>
  );
}
