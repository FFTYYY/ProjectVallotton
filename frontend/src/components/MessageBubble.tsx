import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import type { Attachment, Message, NaiPrompts, ReflectionRound } from "../api/types";
import { isLook } from "../api/types";
import { describeCharacter, ownLayer } from "../lib/library";
import { dragPicture } from "../lib/dragPicture";
import { firstLine } from "../lib/firstLine";
import { mainFirst, mainPosition } from "../lib/mainPicture";
import { Collapse } from "./Collapse";
import { FailureDialog } from "./FailureDialog";
import { InspectDialog } from "./InspectDialog";
import { Tooltip } from "./Tooltip";
import { useChat } from "../store/chat";
import { useToasts } from "../store/toast";
import {
  BranchIcon,
  CharacterIcon,
  ChevronDownIcon,
  CopyIcon,
  InfoIcon,
  RedrawIcon,
  InspectIcon,
  RerunIcon,
  SettingEntryIcon,
  ReflectIcon,
  RepeatIcon,
} from "./Icons";
import { Markdown } from "./Markdown";
import { t } from "../uiText";
import { phaseLabel } from "../lib/phase";

function CopyButton({ text }: { text: string }) {
  const push = useToasts((s) => s.push);
  return (
    <button
      className="msg-action"
      title={t.copy}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          push(t.copied, "success");
        } catch {
          push(t.copyFailed, "error");
        }
      }}
    >
      <CopyIcon size={13} /> {t.copy}
    </button>
  );
}

/** Has to match novelai.SIZES, or the slot a picture drops into is the wrong shape. */
const SIZE_RATIO: Record<NaiPrompts["size"], string> = {
  portrait: "832 / 1216",
  landscape: "1216 / 832",
  square: "1 / 1",
};

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hm = d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  if (sameDay) return hm;
  return t.dateTime(d.getMonth() + 1, d.getDate(), hm);
}

function PromptsBox({ prompts, seeds }: { prompts: NaiPrompts; seeds: (number | null)[] }) {
  const [open, setOpen] = useState(false);
  const push = useToasts((s) => s.push);
  const text = useMemo(() => {
    const lines = [t.promptMain(prompts.base_prompt)];
    (prompts.character_prompts ?? []).forEach((c, i) => {
      const pos =
        c.center_x !== null && c.center_y !== null ? ` @(${c.center_x}, ${c.center_y})` : "";
      lines.push(t.promptCharacter(i + 1, pos, c.prompt));
    });
    if (prompts.negative_prompt) lines.push(t.promptNegative(prompts.negative_prompt));
    return lines.join("\n");
  }, [prompts]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      push(t.promptsCopied, "success");
    } catch {
      push(t.copyFailed, "error");
    }
  }

  return (
    <div className="prompts-box">
      <div className="prompts-summary">
        <button
          className={`used-toggle ${open ? "open" : ""}`}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <ChevronDownIcon size={12} />
          NovelAI prompts
        </button>
        <button className="msg-action" style={{ marginLeft: "auto" }} onClick={() => void copy()}>
          <CopyIcon size={13} /> {t.copy}
        </button>
      </div>
      <Collapse open={open}>
      <div className="prompts-content">
        <div className="p-label">{t.mainPromptLabel}</div>
        <div className="p-value">{prompts.base_prompt}</div>
        {(prompts.character_prompts ?? []).map((c, i) => (
          <div key={i}>
            <div className="p-label">
              {t.characterN(i + 1)}
              {c.center_x !== null && c.center_y !== null
                ? t.position(c.center_x, c.center_y)
                : ""}
            </div>
            <div className="p-value">{c.prompt}</div>
          </div>
        ))}
        {prompts.negative_prompt && (
          <>
            <div className="p-label">{t.negativePromptLabel}</div>
            <div className="p-value">{prompts.negative_prompt}</div>
          </>
        )}
        {seeds.length > 0 && (
          <>
            <div className="p-label">seeds</div>
            <div className="p-value">{seeds.map((s) => s ?? "?").join(", ")}</div>
          </>
        )}
      </div>
      </Collapse>
    </div>
  );
}

/**
 * What the picture was drawn from.
 *
 * The selection changes between turns and the library itself gets edited, so
 * an old image would otherwise be unexplainable a month later. This is the
 * receipt: which entries were on, and what they said at the time. Click one
 * open to read the text that was actually used.
 */

/**
 * A footnote on the turn: that reflection ran, and how it ended.
 *
 * Not a message of its own — it is a property of this picture, like the
 * library entries it sits above. What reflection actually said is a paragraph
 * a round and lives under 「检查」 with the rest of the turn's working.
 */
/**
 * A generated picture in the transcript: the small copy, then the real one.
 *
 * The small copy is what makes the grid appear at all — it is about a
 * twentieth of the size and is what the service worker can afford to keep, so
 * on a revisit it is already there and the grid paints instantly.
 *
 * The full picture is then fetched and swapped in, because the small one is a
 * compromise and this is a drawing app. Two things keep that from costing what
 * it sounds like:
 *
 *   * it starts only after the small one has loaded, and the small one is
 *     `loading="lazy"` — so nothing is fetched for a picture that has not been
 *     scrolled to. A long transcript fetches what you actually looked at;
 *   * `decode()` before the swap, so the element never holds a src the browser
 *     has not finished with. A picture already in the cache decodes almost
 *     immediately and swaps in with no visible step at all.
 */
function GridImage({ att }: { att: Attachment }) {
  const [src, setSrc] = useState(att.thumb_url);
  const [small, setSmall] = useState(false);

  useEffect(() => {
    setSrc(att.thumb_url);
    setSmall(false);
  }, [att.thumb_url]);

  useEffect(() => {
    if (!small) return;
    let live = true;
    const full = new Image();
    full.src = att.url;
    full
      .decode()
      .then(() => {
        if (live) setSrc(att.url);
      })
      .catch(() => {
        // the small one is on screen and stays there
      });
    return () => {
      live = false;
    };
  }, [small, att.url]);

  return (
    <img
      src={src}
      alt={t.generatedImageAlt}
      loading="lazy"
      // Either way the small one is done with: a thumbnail that failed is a
      // reason to try the real picture, not a reason to stop.
      onLoad={() => setSmall(true)}
      onError={() => setSmall(true)}
      // dragged out, it is the picture that leaves — not whichever copy
      // happens to be on screen
      onDragStart={dragPicture(att)}
    />
  );
}

function ReflectionNote({ rounds }: { rounds: ReflectionRound[] }) {
  const looks = rounds.filter(isLook);
  if (!looks.length) return null;
  const redraws = looks.filter((r) => r.redrew).length;
  const retries = rounds.length - looks.length;
  // she stopped because she was happy, not because the budget ran out
  const settled = !looks[looks.length - 1].redrew;
  return (
    <div className="reflection-note-line">
      <ReflectIcon size={11} />
      {t.reflectionSummary(looks.length, redraws)}
      {retries > 0 && t.retriesSuffix(retries)} · {settled ? t.settled : t.roundsExhausted}
    </div>
  );
}

function UsedLibrary({ message }: { message: Message }) {
  const [open, setOpen] = useState(false);
  const snap = message.selection_snapshot;
  if (!snap) return null;
  // the picture's own account of what it took, when the row has one; the
  // switched-on selection is the fallback for rows from before it was kept
  const used = message.used_entries ?? [];
  const usedNames = new Set(used.map((u) => u.name));
  const characters = (snap.characters ?? []).filter((c) => !used.length || usedNames.has(c.name));
  const settings = (snap.settings ?? []).filter((s) => !used.length || usedNames.has(s.title));
  const linesOf = (name: string) => used.find((u) => u.name === name)?.lines ?? [];
  if (!characters.length && !settings.length) return null;

  return (
    <div className="used-library">
      <button
        className={`used-toggle ${open ? "open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronDownIcon size={12} />
        {t.materialsUsed}
      </button>
      {!open &&
        characters.map((c) => (
          <Tooltip key={c.id} content={linesOf(c.name).length ? linesOf(c.name).join(t.listSep) : describeCharacter(c)}>
            <span className={`used-chip${ownLayer(c) ? " own" : ""}`}>
              <CharacterIcon size={11} />
              {c.name}
              {linesOf(c.name).length > 0 ? (
                <span className="chip-variant">{t.linesUsed(linesOf(c.name).length)}</span>
              ) : c.variants.length > 0 && (
                <span className="chip-variant">
                  {c.variants.map((v) => v.title).join(t.listSep)}
                </span>
              )}
            </span>
          </Tooltip>
        ))}
      {!open &&
        settings.map((s) => (
          <Tooltip key={s.id} content={linesOf(s.title).length ? linesOf(s.title).join(t.listSep) : (s.content ?? "")}>
            <span className={`used-chip setting${ownLayer(s) ? " own" : ""}`}>
              <SettingEntryIcon size={11} />
              {s.title}
              {linesOf(s.title).length > 0 && (
                <span className="chip-variant">{t.linesUsed(linesOf(s.title).length)}</span>
              )}
            </span>
          </Tooltip>
        ))}
      <Collapse open={open} className="used-collapse">
        <div className="used-detail">
          {characters.map((c) => (
            <div className="used-entry" key={c.id}>
              <div className="used-entry-head">
                <CharacterIcon size={12} />
                {c.name}
              </div>
              {c.description && <div className="used-entry-body">{c.description}</div>}
              {c.variants.map((v) => (
                <div className="used-entry-variant" key={v.id}>
                  <b>{v.title}</b>
                  {v.content}
                </div>
              ))}
            </div>
          ))}
          {settings.map((s) => (
            <div className="used-entry" key={s.id}>
              <div className="used-entry-head">
                <SettingEntryIcon size={12} />
                {s.title}
              </div>
              {s.content && <div className="used-entry-body">{s.content}</div>}
            </div>
          ))}
        </div>
      </Collapse>
    </div>
  );
}

/**
 * What is happening right now.
 *
 * A turn passes through up to four services and any one of them can be the
 * slow one, so each stage says something different. It is still Vallotton
 * doing it, though — naming the models would be reporting on the plumbing.
 */
interface BubbleProps {
  message: Message;
  isLastAssistant: boolean;
  /** the whole conversation is the album; this just says which picture */
  onOpenImage: (att: Attachment) => void;
}

export function MessageBubble({ message: m, isLastAssistant, onOpenImage }: BubbleProps) {
  const [inspecting, setInspecting] = useState(false);
  const redraw = useChat((s) => s.redraw);
  const rerun = useChat((s) => s.rerun);
  const branch = useChat((s) => s.branch);
  const cancelGeneration = useChat((s) => s.cancelGeneration);
  const retrySend = useChat((s) => s.retrySend);
  const discardFailedSend = useChat((s) => s.discardFailedSend);
  const push = useToasts((s) => s.push);
  const navigate = useNavigate();

  // branching makes something rather than destroying it — just do it and say
  // so; the new conversation is right there if it was not what you wanted
  async function onBranch() {
    try {
      const conv = await branch(m.id);
      push(t.branchCreated, "success");
      navigate(`/c/${conv.id}`);
    } catch {
      push(t.branchFailed, "error");
    }
  }
  const [localPreviews] = useState<string[]>(() =>
    m.localFiles ? m.localFiles.map((f) => URL.createObjectURL(f)) : [],
  );
  useEffect(
    () => () => localPreviews.forEach((u) => URL.revokeObjectURL(u)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const convId = m.conversation_id;
  const uploads = m.attachments.filter((a) => a.kind === "upload");
  // the main picture first: it is the one an edit next turn starts from and
  // the one the next picture's seed follows, so it reads as this turn's picture
  const generated = mainFirst(
    m.attachments.filter((a) => a.kind === "generated"),
    m.main_index,
  );
  const mainAt = mainPosition(generated, m.main_index);

  // an app note (context budget, etc.) — the app's own voice, not the AI's
  if (m.role === "system") {
    return (
      <div className="msg-system fade-in">
        <InfoIcon size={14} />
        <span>{m.content}</span>
      </div>
    );
  }

  if (m.role === "user") {
    return (
      <div className="msg-row user fade-in">
        <div className="msg-body">
          {/* the app typed this one, not the user. Said plainly, because a
              message you did not write reading as one you did is the kind of
              thing that makes a transcript untrustworthy. */}
          {m.auto_sent && (
            <div className="auto-sent-note">
              <RepeatIcon size={11} />
              {t.continuousNote(m.auto_sent.round, m.auto_sent.of)}
            </div>
          )}
          {(uploads.length > 0 || localPreviews.length > 0) && (
            <div className="upload-strip">
              {uploads.map((a) => (
                <img
                  key={a.id}
                  className="upload-thumb"
                  src={a.thumb_url}
                  alt=""
                  loading="lazy"
                  onClick={() => onOpenImage(a)}
                  onDragStart={dragPicture(a)}
                />
              ))}
              {uploads.length === 0 &&
                localPreviews.map((u) => (
                  <img key={u} className="upload-thumb" src={u} alt="" />
                ))}
            </div>
          )}
          {m.content && (
            <div className="bubble">
              <div className="md" style={{ whiteSpace: "pre-wrap" }}>{m.content}</div>
            </div>
          )}
          <div className="msg-meta">
            {m.local === "sending" && (
              <>
                <span className="spinner" style={{ width: 12, height: 12 }} />
                <span>{t.sending}</span>
              </>
            )}
            {m.local === "failed" && (
              <>
                <span style={{ color: "var(--danger)" }}>{t.sendFailed}</span>
                <button className="msg-action" onClick={() => void retrySend(convId, m.id)}>
                  {t.retry}
                </button>
                <button className="msg-action" onClick={() => discardFailedSend(convId, m.id)}>
                  {t.discard}
                </button>
              </>
            )}
            {!m.local && (
              <>
                <span className="msg-time">{formatTime(m.created_at)}</span>
                {m.content && <CopyButton text={m.content} />}
                <button className="msg-action" title={t.branchFromHere} onClick={() => void onBranch()}>
                  <BranchIcon size={13} /> {t.branch}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    );
  }

  // ---------------- assistant ----------------
  const pending = m.status === "pending";
  const [showFailure, setShowFailure] = useState(false);
  const nPlanned = m.nai_prompts?.n_images ?? 0;
  const ratio = m.nai_prompts ? SIZE_RATIO[m.nai_prompts.size] : "2 / 3";
  // while painting the phase says how many are in and how many were asked
  // for; a redraw or a reflection round paints into an album that already
  // holds pictures, so "planned minus held" would show nothing coming
  const paintingAt = /^(?:.*:)?painting:(\d+)\/(\d+)$/.exec(m.phase);
  const placeholders = !pending
    ? 0
    : paintingAt
      ? Math.max(Number(paintingAt[2]) - Number(paintingAt[1]), 0)
      : nPlanned > 0
        ? Math.max(nPlanned - generated.length, 0)
        : 0;
  // While reflection runs the picture is already on screen, so the line about
  // looking at it belongs under the picture rather than above it.
  const reflecting = m.phase.startsWith("reflecting");
  const paintingNow = m.phase.startsWith("painting") || m.phase.includes(":painting");
  const working =
    pending && !paintingNow ? (
      <div className={m.content && !reflecting ? "thinking-line" : "bubble"}>
        <div className={`thinking ${m.phase.includes("retry:") ? "warn" : ""}`}>
          <span className="thinking-dots">
            <span />
            <span />
            <span />
          </span>
          {phaseLabel(m)}
        </div>
      </div>
    ) : null;

  return (
    <div className="msg-row assistant fade-in">
      {showFailure && (
        <FailureDialog
          messageId={m.id}
          error={m.error}
          onClose={() => setShowFailure(false)}
        />
      )}
      <img className="msg-avatar" src="/icons/icon-192.png" alt="Vallotton" />
      <div className="msg-body">
        {m.status === "error" ? (
          /* One line, and a door. A failure is not what you came to read, and
             these can run to paragraphs — but the part that says what actually
             broke is usually past where a bubble should stop. */
          <button
            className="bubble error-bubble"
            title={t.openFailure}
            onClick={() => setShowFailure(true)}
          >
            <span className="error-head">{t.generationFailed}</span>
            <span className="error-line">{firstLine(m.error) || t.unknownError}</span>
          </button>
        ) : (
          <>
            {(m.content || (!pending && m.status === "cancelled")) && (
              <div className="bubble">
                {m.content ? <Markdown>{m.content}</Markdown> : null}
                {m.status === "cancelled" && (
                  <div style={{ color: "var(--ink-3)", fontSize: 12.5, marginTop: m.content ? 6 : 0 }}>
                    {t.stopped}
                  </div>
                )}
                {m.status === "done" && !!m.error && (
                  // a note on a finished turn: stopped with its pictures kept
                  <div style={{ color: "var(--ink-3)", fontSize: 12.5, marginTop: m.content ? 6 : 0 }}>
                    {m.error}
                  </div>
                )}
              </div>
            )}
            {/*
              Not `!m.content`: the reply is written the moment the lead model
              returns, which is BEFORE the drawing model runs — gating on an
              empty message meant the 斟酌 stage never appeared at all. Once
              there is a reply the line sits under it, without a bubble.
            */}
            {working && !reflecting && working}
          </>
        )}

        {m.nai_prompts?.draw_id && generated.length > 0 && (
          <div className="draw-id" title={t.drawId}>
            #{m.nai_prompts.draw_id}
            {/* said out loud, so a misjudged "edit" is visible rather than silent */}
            {m.draw_brief?.continues && (
              <span className="draw-continues">{t.continues(m.draw_brief.continues)}</span>
            )}
          </div>
        )}
        {(generated.length > 0 || placeholders > 0) && (
          <div
            className={`gen-grid ${generated.length + placeholders === 1 ? "single" : ""}`}
          >
            {generated.map((a) => (
              <button
                key={a.id}
                className={`gen-item${generated.length > 1 && a.position === mainAt ? " main" : ""}`}
                style={{ ["--ar" as string]: ratio }}
                onClick={() => onOpenImage(a)}
              >
                <GridImage att={a} />
              </button>
            ))}
            {Array.from({ length: placeholders }).map((_, i) => (
              <div
                key={`ph-${i}`}
                className="gen-item placeholder"
                style={{ ["--ar" as string]: ratio }}
              >
                {phaseLabel(m)}
              </div>
            ))}
          </div>
        )}

        {working && reflecting && working}

        {!pending && !!m.reflection_log?.length && (
          <ReflectionNote rounds={m.reflection_log} />
        )}
        {!pending && !!m.paint_errors?.length && (
          <div className="thinking warn">{m.paint_errors.join(t.noteSep)}</div>
        )}
        {!pending && <UsedLibrary message={m} />}

        {m.nai_prompts && !pending && (
          <PromptsBox prompts={m.nai_prompts} seeds={generated.map((g) => g.seed)} />
        )}

        <div className="msg-meta">
          {pending ? (
            <button className="stop-btn" onClick={() => void cancelGeneration(convId, m.id)}>
              {t.stop}
            </button>
          ) : (
            <>
              <span className="msg-time">{formatTime(m.created_at)}</span>
              {m.nai_prompts && (
                <button
                  className="msg-action"
                  title={t.redrawTitle}
                  onClick={() => void redraw(convId, m.id)}
                >
                  <RedrawIcon size={13} /> {t.redraw}
                </button>
              )}
              {m.content && <CopyButton text={m.content} />}
              <button className="msg-action" title={t.branchFromHere} onClick={() => void onBranch()}>
                <BranchIcon size={13} /> {t.branch}
              </button>
              {isLastAssistant && (
                <button
                  className="msg-action"
                  title={t.rerunTitle}
                  onClick={() => void rerun(convId)}
                >
                  <RerunIcon size={13} /> {t.rerun}
                </button>
              )}
              <button
                className="msg-action"
                title={t.inspectTitle}
                onClick={() => setInspecting(true)}
              >
                <InspectIcon size={13} /> {t.inspect}
              </button>
            </>
          )}
        </div>
      </div>
      {inspecting && (
        <InspectDialog messageId={m.id} onClose={() => setInspecting(false)} />
      )}
    </div>
  );
}
