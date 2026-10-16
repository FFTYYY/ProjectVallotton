import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import type { Attachment, LibraryProposal, Message } from "../api/types";
import { useChat } from "../store/chat";
import { useProjects } from "../store/projects";
import { useGen, useSettings } from "../store/settings";
import { useUi } from "../store/ui";
import { autoLibraryLabel } from "../lib/genSettings";
import { describeCharacter, ownLayer } from "../lib/library";
import { mainPosition } from "../lib/mainPicture";
import { isWorking } from "../lib/transcript";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  CharacterIcon,
  SettingEntryIcon,
} from "./Icons";
import { Tooltip } from "./Tooltip";
import { Composer } from "./Composer";
import { Lightbox } from "./Lightbox";
import { ProposalTurn } from "./ProposalTurn";
import { SettingChips, useHasSettingChips } from "./SettingChips";
import { MessageBubble } from "./MessageBubble";
import { RightSidebar } from "./RightSidebar";
import { t } from "../uiText";

const EMPTY: Message[] = [];
const POLL_MS = 1300;
/** and how often when it is not: enough to notice another device, little else */
const POLL_IDLE_MS = 15_000;
/** how far from an end before the jump buttons are worth showing */
const FAR = 600;
/**
 * How long arriving at a conversation (or pressing "to the bottom") keeps its
 * claim on the bottom while pictures decode. Long enough to cover a decode,
 * short enough that it is over before anyone reads far — and any wheel, touch
 * or key ends it sooner.
 */
const SETTLE_MS = 2500;

/** what this turn will carry into the models, shown right above the input */
function SelectionChips({ convId }: { convId: string }) {
  const selection = useProjects((s) => s.selection[convId]);
  const setRightOpen = useUi((s) => s.setRightOpen);
  const hasSettings = useHasSettingChips();

  const characters = selection?.characters.filter((c) => c.selected) ?? [];
  const settings = selection?.settings.filter((s) => s.selected) ?? [];
  if (!characters.length && !settings.length && !hasSettings) return null;

  return (
    <div className="selection-chips">
      <SettingChips convId={convId} />
      {/* both kinds say the same kind of thing on hover: what this entry
          actually contributes to the turn. A phone has no hover, so there a
          tap says it instead of opening the sidebar. */}
      {characters.map((c) => {
        const chosen = c.variants.filter((v) => v.selected).map((v) => v.title);
        return (
          <Tooltip key={c.id} content={describeCharacter(c)} prefer="vertical" tapToShow>
            <button className={`selection-chip${ownLayer(c) ? " own" : ""}`} onClick={() => setRightOpen(true)}>
              <CharacterIcon size={12} />
              {c.name}
              {chosen.length > 0 && (
                <span className="chip-variant">{chosen.join(t.listSep)}</span>
              )}
            </button>
          </Tooltip>
        );
      })}
      {settings.map((s) => (
        <Tooltip key={s.id} content={s.content} prefer="vertical" tapToShow>
          <button className={`selection-chip setting${ownLayer(s) ? " own" : ""}`} onClick={() => setRightOpen(true)}>
            <SettingEntryIcon size={12} />
            {s.title}
          </button>
        </Tooltip>
      ))}
    </div>
  );
}

export function ChatView() {
  const { convId = "" } = useParams<{ convId: string }>();
  const messages = useChat((s) => s.messages[convId] ?? EMPTY);
  const loaded = useChat((s) => s.loadedConvs[convId] ?? false);
  const loadError = useChat((s) => s.loadError[convId] ?? "");
  const serverPending = useChat((s) => s.serverPending[convId] ?? false);
  const hasMoreBefore = useChat((s) => s.hasMoreBefore[convId] ?? false);
  const isLoadingOlder = useChat((s) => s.loadingOlder[convId] ?? false);
  const conv = useChat((s) => s.convMeta[convId]);
  const pointSettings = useSettings((s) => s.point);
  const openConversation = useChat((s) => s.openConversation);
  const loadOlderMessages = useChat((s) => s.loadOlderMessages);
  const pollConversation = useChat((s) => s.pollConversation);
  const awaitTitle = useChat((s) => s.awaitTitle);
  const sendMessage = useChat((s) => s.sendMessage);
  const cancelGeneration = useChat((s) => s.cancelGeneration);
  const loadConversations = useChat((s) => s.loadConversations);
  const proposals = useProjects((s) => s.proposals[convId]);
  const loadProposals = useProjects((s) => s.loadProposals);
  const loadSelection = useProjects((s) => s.loadSelection);
  const setMain = useChat((s) => s.setMain);

  /**
   * Which picture is open, by identity rather than by position.
   *
   * It used to hold an index into the album, and the album is built from the
   * messages that happen to be loaded. Scrolling back through a conversation
   * loads older ones, and their pictures land at the FRONT — so an index that
   * meant the fourth picture quietly came to mean the third. Page forward one
   * while the album grew one at the front and the counter advances over the
   * same picture, which is the bug this shape removes rather than patches.
   */
  const [lightbox, setLightbox] = useState<
    { kind: "generated" | "uploads"; id: string } | null
  >(null);
  const [farFromBottom, setFarFromBottom] = useState(false);
  const [farFromTop, setFarFromTop] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const columnRef = useRef<HTMLDivElement>(null);
  // scroll anchor for prepending older history without a visual jump
  const anchorRef = useRef<{ prevHeight: number; prevTop: number } | null>(null);

  /**
   * Three things are allowed to move the transcript: arriving at a
   * conversation, the jump buttons, and sending a message. `settling` is how
   * long one of those keeps its claim on the bottom.
   *
   * It needs a window rather than a single assignment because pictures have no
   * height until they decode — "the bottom" keeps moving for a second or two
   * after either action. Any real input gesture ends the claim at once, and
   * nothing re-opens it. Generation does not: a box that scrolls itself while
   * you are reading is a box you cannot read.
   */
  const settling = useRef(false);
  const settleTimer = useRef<number | null>(null);

  const releaseBottom = useCallback(() => {
    settling.current = false;
    if (settleTimer.current !== null) {
      window.clearTimeout(settleTimer.current);
      settleTimer.current = null;
    }
  }, []);

  const claimBottom = useCallback(() => {
    settling.current = true;
    if (settleTimer.current !== null) window.clearTimeout(settleTimer.current);
    settleTimer.current = window.setTimeout(() => {
      settling.current = false;
      settleTimer.current = null;
    }, SETTLE_MS);
  }, []);

  useEffect(() => releaseBottom, [releaseBottom]);

  // The generation settings belong to this conversation, so the controls show
  // this conversation's. Only which one is said here: what they hold is read
  // from the row itself, wherever it is up to — see store/settings.
  useEffect(() => {
    pointSettings(convId);
  }, [pointSettings, convId]);

  // A wheel, a touch or a key is the reader saying where they want to be.
  // Programmatic scrolling fires none of these, so the claim can only be
  // ended by a person.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.addEventListener("wheel", releaseBottom, { passive: true });
    el.addEventListener("touchstart", releaseBottom, { passive: true });
    el.addEventListener("keydown", releaseBottom);
    return () => {
      el.removeEventListener("wheel", releaseBottom);
      el.removeEventListener("touchstart", releaseBottom);
      el.removeEventListener("keydown", releaseBottom);
    };
  }, [releaseBottom]);

  // A generation may be running even when nothing local says so: the send
  // response can be lost, or a 409 told us someone else's job is in flight.
  // Trust the server's has_pending too, or we would stop polling forever.
  // both judgements live in lib/transcript, out where they can be tested
  const hasPending = isWorking(messages, serverPending, conv);
  const pendingMsg = messages.find((m) => m.status === "pending");

  // initial load / conversation switch
  useEffect(() => {
    claimBottom();
    anchorRef.current = null;
    void openConversation(convId);
    void loadProposals(convId);
  }, [convId, claimBottom, openConversation, loadProposals]);

  /**
   * What this conversation has switched on, read once and in one place.
   *
   * The chips over the composer say what the turn will carry, so this cannot
   * wait for the rail to be opened — and the rail must not read it a second
   * time, which is what it used to do: two answers to the same question land
   * in whatever order they like, and the later one undid a chip ticked in
   * between.
   *
   * Moving the conversation to another project changes what is in reach, so
   * that is a reason to read again — a reason, not a second reader.
   */
  useEffect(() => {
    void loadSelection(convId);
  }, [convId, conv?.folder_id, loadSelection]);

  /**
   * The open conversation is polled: quickly while it is working, slowly while
   * it is not.
   *
   * Idle used to mean not at all, which is right about this browser and wrong
   * about the conversation — the phone can start a run, a run can end, a turn
   * can be archived by a 重跑 made somewhere else. None of that reached the
   * screen until something happened to ask, which is what "I refreshed a few
   * times and then it was gone" is. The idle ask is the same incremental poll
   * and carries nothing new almost every time.
   */
  useEffect(() => {
    const every = hasPending ? POLL_MS : POLL_IDLE_MS;
    const t = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void pollConversation(convId);
    }, every);
    return () => window.clearInterval(t);
  }, [convId, hasPending, pollConversation]);

  // when generation finishes, refresh the sidebar (title/preview may change),
  // pick up any library edits the lead model proposed this turn, and re-read
  // the selection: the lead's own notes land in it at once, and a project
  // character it drew by name is switched on by the server
  const prevPending = useRef(hasPending);
  useEffect(() => {
    if (prevPending.current && !hasPending) {
      void loadConversations();
      void loadProposals(convId);
      void loadSelection(convId);
      void awaitTitle(convId);
    }
    prevPending.current = hasPending;
    // `messages` is read for the finished turn's status; depending on it would
    // re-run this on every poll, and the guard above is the edge, not the list
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasPending, loadConversations, loadProposals, loadSelection, awaitTitle, convId]);

  // catch up after the tab was hidden (mobile lock screen, tab switch)
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible") void pollConversation(convId);
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [convId, pollConversation]);

  function loadOlderAnchored() {
    const el = scrollRef.current;
    if (!el || isLoadingOlder || !hasMoreBefore) return;
    anchorRef.current = { prevHeight: el.scrollHeight, prevTop: el.scrollTop };
    void loadOlderMessages(convId);
  }

  // track whether the user is reading history (don't yank them down),
  // and auto-load older pages when they reach the top
  function onScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const fromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setFarFromBottom(fromBottom > FAR);
    setFarFromTop(el.scrollTop > FAR);
    if (el.scrollTop < 80) loadOlderAnchored();
  }

  // restore the anchor after prepending older history — the ONLY thing that
  // moves the transcript on content growth. It puts the reader back exactly
  // where they were; it never goes to the bottom.
  const contentKey = messages
    .map((m) => `${m.id}:${m.status}:${m.attachments.length}:${m.content.length}`)
    .join("|");
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || !anchorRef.current) return;
    el.scrollTop = el.scrollHeight - anchorRef.current.prevHeight + anchorRef.current.prevTop;
    anchorRef.current = null;
  }, [contentKey]);

  // opening a conversation always lands at the newest message
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && loaded) {
      el.scrollTop = el.scrollHeight;
      setFarFromBottom(false);
      setFarFromTop(el.scrollTop > FAR);
    }
  }, [convId, loaded]);

  /**
   * Hold the bottom while the transcript is still settling.
   *
   * Pictures have no height until they decode, so the effect above runs
   * against a column that is still short and lands far from the end — a
   * conversation with a few images opened thousands of pixels up and stayed
   * there, because content growing below the fold fires no scroll event and
   * nothing ever re-pinned it. React re-renders do not help either: the size
   * changes without any state changing. So watch the box itself.
   */
  useEffect(() => {
    const el = scrollRef.current;
    const column = columnRef.current;
    if (!el || !column) return;
    const ro = new ResizeObserver(() => {
      // anchorRef means we are restoring a position after prepending older
      // history, which owns the scroll for that frame
      if (settling.current && !anchorRef.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(column);
    return () => ro.disconnect();
  }, [convId]);

  function jump(to: "top" | "bottom") {
    const el = scrollRef.current;
    if (!el) return;
    // the list is virtual-free but long; an instant jump is what the button
    // promises, and smooth scrolling here trips the load-older threshold
    el.scrollTop = to === "top" ? 0 : el.scrollHeight;
    if (to === "bottom") claimBottom();
    else releaseBottom();
    onScroll();
  }

  /**
   * Opening a picture takes a history entry, so the phone's back gesture
   * leaves the picture rather than the conversation.
   *
   * This lives with the open/closed state rather than inside the Lightbox:
   * an effect there would push on mount and pop on cleanup, and React's
   * development double-mount runs that cleanup immediately — which fired
   * popstate and shut the picture the instant it opened. Event handlers are
   * not double-invoked, so the pair is safe here.
   */
  const closeLightbox = useCallback(() => {
    setLightbox(null);
    if (window.history.state?.lightbox) window.history.back();
  }, []);

  useEffect(() => {
    function onPop() {
      setLightbox(null); // the entry is already gone; do not pop a second one
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  /**
   * Everything in this conversation you can page through, in two piles.
   *
   * Paging is conversation-wide rather than message-wide: a run of pictures is
   * one body of work, and having to close and reopen between turns to compare
   * the fourth with the first is exactly the comparison you want to make.
   *
   * Two piles rather than one because the pictures you uploaded and the
   * pictures she made are not the same sequence — an arrow that walked from
   * your reference photo into her output would be following nothing.
   */
  const albums = useMemo(() => {
    const generated: Attachment[] = [];
    const uploads: Attachment[] = [];
    for (const m of messages) {
      for (const a of m.attachments) {
        (a.kind === "generated" ? generated : uploads).push(a);
      }
    }
    return { generated, uploads };
  }, [messages]);

  const onOpenImage = useCallback((att: Attachment) => {
    const kind = att.kind === "generated" ? "generated" : "uploads";
    window.history.pushState({ lightbox: true }, "");
    setLightbox({ kind, id: att.id });
  }, []);

  /** where the open picture currently sits, which can move under it */
  const lightboxIndex = lightbox
    ? albums[lightbox.kind].findIndex((a) => a.id === lightbox.id)
    : -1;

  // proposals sit with their own turn rather than in a rail nobody opens
  const proposalsByMessage = useMemo(() => {
    const by: Record<string, LibraryProposal[]> = {};
    for (const p of proposals ?? []) (by[p.message_id] ??= []).push(p);
    return by;
  }, [proposals]);

  const lastAssistantId = [...messages]
    .reverse()
    .find((m) => m.role === "assistant" && m.status !== "pending")?.id;

  async function onSend(text: string, files: File[]) {
    // Sending is the third thing allowed to move the transcript — you asked
    // for something, you should see it land. Same settle window as the other
    // two, so the placeholder and any thumbnail it grows stay in view, and
    // the claim is gone long before the generation starts growing the column.
    const el = scrollRef.current;
    claimBottom();
    if (el) el.scrollTop = el.scrollHeight;
    await sendMessage(convId, text, files);
  }

  function onStop() {
    // the server stops the run when the turn is cancelled — see
    // views/chat.cancel_message
    if (pendingMsg) void cancelGeneration(convId, pendingMsg.id);
  }

  return (
    <>
      {conv && (
        <header className="chat-header">
          {conv.folder_path.length > 0 && (
            <span className="chat-header-crumbs">
              {conv.folder_path.map((name, i) => (
                <span key={i}>
                  {name}
                  <span className="crumb-sep">/</span>
                </span>
              ))}
            </span>
          )}
          <span className="chat-header-title">{conv.title}</span>
          {conv.is_archived && <span className="tag-chip muted">{t.archived}</span>}
          <AutoLibraryChip />
        </header>
      )}
      <div className="chat-scroll" ref={scrollRef} onScroll={onScroll}>
        <div className="chat-column" ref={columnRef}>
          {!loaded && !loadError && (
            <div style={{ textAlign: "center", padding: 40 }}>
              <span className="spinner" />
            </div>
          )}
          {!loaded && loadError && (
            <div className="load-failed">
              <span>{loadError}</span>
              <button className="info-action-btn" onClick={() => void openConversation(convId)}>
                {t.retry}
              </button>
            </div>
          )}
          {loaded && hasMoreBefore && (
            <button className="load-earlier" onClick={loadOlderAnchored} disabled={isLoadingOlder}>
              {isLoadingOlder ? t.loading : t.loadOlder}
            </button>
          )}
          {loaded && messages.length === 0 && (
            <div style={{ textAlign: "center", color: "var(--ink-3)", padding: "60px 0" }}>
              {t.freshStart}
            </div>
          )}
          {messages.map((m) => (
            <Fragment key={m.id}>
              <MessageBubble
                message={m}
                isLastAssistant={m.id === lastAssistantId}
                onOpenImage={onOpenImage}
              />
              {/* a library proposal belongs to the turn that raised it, and is
                  read in sequence with it like everything else here */}
              {(proposalsByMessage[m.id] ?? []).map((p) => (
                <ProposalTurn key={p.id} convId={convId} proposal={p} />
              ))}
            </Fragment>
          ))}
        </div>
      </div>
      <div className="scroll-jumps">
        {farFromTop && (
          <button className="scroll-jump" aria-label={t.toTop} title={t.toTop} onClick={() => jump("top")}>
            <ArrowUpIcon size={16} />
          </button>
        )}
        {farFromBottom && (
          <button
            className="scroll-jump"
            aria-label={t.toLatest}
            title={t.toLatest}
            onClick={() => jump("bottom")}
          >
            <ArrowDownIcon size={16} />
          </button>
        )}
      </div>
      <RightSidebar conv={conv} />
      <Composer
        draftKey={convId}
        busy={hasPending}
        onSend={onSend}
        onStop={onStop}
        // during the upload itself there is no server row to cancel yet, so
        // the stop button must show as unavailable instead of doing nothing
        canStop={!!pendingMsg}
        // mid-reflection the picture already exists, so stopping keeps it
        stopKeeps={!!pendingMsg?.phase.startsWith("reflecting")}
        autoFocus
        chips={<SelectionChips convId={convId} />}
      />
      {lightbox && lightboxIndex >= 0 && (
        <Lightbox
          images={albums[lightbox.kind]}
          index={lightboxIndex}
          onClose={closeLightbox}
          isMain={
            lightbox.kind === "generated"
              ? (att) => {
                  const turn = messages.find((m) => m.id === att.message_id);
                  if (!turn) return false;
                  return (
                    mainPosition(
                      turn.attachments.filter((a) => a.kind === "generated"),
                      turn.main_index,
                    ) === att.position
                  );
                }
              : undefined
          }
          onSetMain={
            lightbox.kind === "generated"
              ? (att) => void setMain(convId, att.message_id, att.position)
              : undefined
          }
          onIndex={(i) =>
            setLightbox((lb) => {
              const next = lb ? albums[lb.kind][i] : null;
              return lb && next ? { ...lb, id: next.id } : lb;
            })
          }
        />
      )}
    </>
  );
}

/**
 * Says so while the app is approving library edits on its own.
 *
 * Only while it is — off is the ordinary state and does not need announcing.
 * It reads the same store the rail's control writes to, so the chip and the
 * control cannot disagree about what this conversation is set to.
 */
function AutoLibraryChip() {
  const mode = useGen().autoLibrary;
  if (mode === "off") return null;
  return <span className="tag-chip">{t.autoLibraryChip(autoLibraryLabel(mode))}</span>;
}

