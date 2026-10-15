import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import type { ReflectionRound } from "../api/types";
import { isLook } from "../api/types";
import { ApiError, api } from "../api/client";
import type { Lookup } from "../lib/lookups";
import { lookupsText } from "../lib/lookups";
import { useToasts } from "../store/toast";
import { ChevronDownIcon, CloseIcon, CopyIcon } from "./Icons";
import { Collapse } from "./Collapse";
import { t } from "../uiText";

/**
 * What the two models actually said for one turn.
 *
 * Verbatim JSON rather than a prettied-up summary: this exists to answer
 * "why did it draw that", and a rendering that quietly drops a field is worse
 * than no view at all. It is fetched on open — model_raw is far too big to ride
 * along on every poll.
 *
 * Rendered through a portal: it is mounted from inside a message row, and
 * .msg-row carries content-visibility, which implies paint containment — that
 * makes the row a containing block for position:fixed, so the backdrop would
 * be trapped inside the message instead of covering the page.
 */

interface RawTurn {
  lead: {
    reply?: string | null;
    draw?: unknown;
    edits?: unknown;
    usage?: { retries?: number } & Record<string, unknown>;
    /** the answer as the model gave it, before the app tidied it */
    raw?: unknown;
    /** the drawing the lead itself said it continues */
    continues?: string | null;
  };
  /** the brief behind the pictures that were kept (after reflection, the last round's) */
  draw_brief?: unknown;
  /** pictures of the batch that failed, and what the proposal had to say */
  paint_errors?: string[];
  edit_notes?: string[];
  /** the Danbooru index is not loaded: an empty unknown_tags means nothing */
  tag_index_missing?: boolean;
  drawing: unknown;
  references: {
    index: number;
    size: [number | null, number | null];
    url: string;
    thumb_url: string;
    /** only on turns from when a separate vision model stood in front of it */
    description: string | null;
  }[];
  reasoning: { lead: string; draw: string };
  /** what each model looked up in the tag table; empty on most turns */
  lookups: { lead: Lookup[]; draw: Lookup[] };
  /** the picture in slots: what each subject took from its entry, and how */
  picture: unknown;
  /** on an edit turn, the change list the drawing model handed back */
  patch: unknown;
  /** what the slot check had to say — lines not picked, patches out of bounds */
  notes: string[];
  /** tags in the prompts that the Danbooru index does not have */
  unknown_tags: string[];
  /** an answer that could not be read, kept whole; "" when there was none */
  unparsed: string;
  /** one entry per reflection round; null when reflection did not run */
  reflection: ReflectionRound[] | null;
  selection_snapshot: unknown;
  prompt_tokens: number | null;
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value) && value.length === 0) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return text.trim() ? text : "";
}

/**
 * One labelled slab of the turn.
 *
 * `foldable` is for the chains of thought: tens of thousands of characters
 * each, so leaving them open buries everything else. They stay one click away
 * rather than being left out — the whole point of this dialog is that nothing
 * is quietly dropped.
 */
function Block({
  title,
  value,
  foldable = false,
}: {
  title: string;
  value: unknown;
  foldable?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const text = asText(value);
  if (!text) return null;

  if (!foldable) {
    return (
      <div className="inspect-block">
        <div className="inspect-block-title">{title}</div>
        <pre className="inspect-pre">{text}</pre>
      </div>
    );
  }
  return (
    <div className="inspect-block">
      <button
        className={`inspect-block-title foldable ${open ? "open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <ChevronDownIcon size={12} />
        {title}
        <span className="inspect-block-size">{t.chars(text.length)}</span>
      </button>
      <Collapse open={open}>
        <pre className="inspect-pre">{text}</pre>
      </Collapse>
    </div>
  );
}

export function InspectDialog({ messageId, onClose }: { messageId: string; onClose: () => void }) {
  const [data, setData] = useState<RawTurn | null>(null);
  const [error, setError] = useState("");
  const push = useToasts((s) => s.push);

  useEffect(() => {
    let live = true;
    api
      .get<RawTurn>(`/api/messages/${messageId}/raw`)
      .then((d) => live && setData(d))
      .catch((e) =>
        live && setError(e instanceof ApiError ? e.message : t.readFailed),
      );
    return () => {
      live = false;
    };
  }, [messageId]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  /**
   * Two buttons, because the chains of thought are the bulk of this by far
   * and are rarely what you want to hand to someone. "Output" is the turn
   * without them; "all" is everything, for when the working IS the question.
   */
  async function copy(withReasoning: boolean) {
    if (!data) return;
    const payload = withReasoning
      ? data
      : (({ reasoning: _reasoning, ...rest }) => rest)(data);
    try {
      await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
      push(withReasoning ? t.copiedAll : t.copiedOutput, "success");
    } catch {
      push(t.copyFailed, "error");
    }
  }

  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="inspect-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="inspect-head">
          <div className="modal-title" id="inspect-title">
            {t.rawOutputTitle}
          </div>
          {data && (
            <>
              <button className="inspect-copy" title={t.copyOutputOnly}
                      onClick={() => void copy(false)}>
                <CopyIcon size={13} /> {t.output}
              </button>
              <button className="inspect-copy" title={t.copyWithReasoning}
                      onClick={() => void copy(true)}>
                <CopyIcon size={13} /> {t.all}
              </button>
            </>
          )}
          <button className="phrase-action" title={t.close} onClick={onClose}>
            <CloseIcon size={16} />
          </button>
        </div>

        <div className="inspect-scroll">
          {error && <div className="inspect-note">{error}</div>}
          {!data && !error && <span className="spinner" />}
          {data && (
            <>
              <Block title={t.leadReply} value={data.lead.reply} />
              <Block title={t.leadDraw} value={data.lead.draw} />
              {!!data.draw_brief && asText(data.draw_brief) !== asText(data.lead.draw) && (
                <Block title={t.finalBrief} value={data.draw_brief} />
              )}
              <Block title={t.leadRaw} value={data.lead.raw} />
              <Block title={t.leadEdits} value={data.lead.edits} />
              {!!data.references?.length && (
                <div className="inspect-block">
                  <div className="inspect-block-title">
                    {t.leadReferences(data.references.length)}
                  </div>
                  <div className="inspect-refs">
                    {data.references.map((r) => (
                      <figure key={r.index}>
                        <img src={r.thumb_url} alt={t.referenceAlt(r.index)} loading="lazy" />
                        <figcaption>
                          {r.index}
                          {r.size[0] ? ` · ${r.size[0]}×${r.size[1]}` : ""}
                        </figcaption>
                      </figure>
                    ))}
                  </div>
                  {/* only older turns have one: a separate vision model used to
                      describe the picture and the text went in its place */}
                  {data.references.map((r) =>
                    r.description ? (
                      <Block
                        key={`d-${r.index}`}
                        title={t.oldVisionDescription(r.index)}
                        value={r.description}
                        foldable
                      />
                    ) : null,
                  )}
                </div>
              )}
              {!!data.reflection?.length && (
                <div className="inspect-block">
                  <div className="inspect-block-title">
                    {t.reflectionRoundsTitle(data.reflection.length)}
                  </div>
                  {data.reflection.map((r, i) => (
                    <div className="inspect-round" key={i}>
                      <div className="inspect-round-head">
                        <span>{t.roundN(r.round)}</span>
                        {isLook(r) ? (
                          <span className={r.redrew ? "redrew" : "kept"}>
                            {r.redrew ? t.redrew : t.keptAsIs}
                          </span>
                        ) : (
                          <span className="failed">
                            {r.gave_up ? t.gaveUp : r.paid ? t.retriedPaid : t.retry}
                          </span>
                        )}
                      </div>
                      <div className="inspect-round-said">
                        {isLook(r) ? r.reply : r.failed}
                      </div>
                    </div>
                  ))}
                  {/* the pictures those rounds are talking about are gone: a
                      reflected turn keeps only the last one */}
                </div>
              )}
              {!!data.unparsed && (
                <Block title={t.unparsedTitle} value={data.unparsed} foldable />
              )}
              <Block title={t.drawPrompts} value={data.drawing} />
              {data.tag_index_missing && (
                <div className="inspect-note warn">{t.tagIndexMissing}</div>
              )}
              {!!data.paint_errors?.length && (
                <div className="inspect-note warn">{data.paint_errors.join("；")}</div>
              )}
              {!!data.edit_notes?.length && (
                <div className="inspect-note warn">{data.edit_notes.join("；")}</div>
              )}
              {!!data.unknown_tags?.length && (
                <div className="inspect-note warn">
                  {t.unknownTags(data.unknown_tags.length)}
                  {data.unknown_tags.join("、")}
                </div>
              )}
              <Block title={t.drawPicture} value={data.picture} />
              <Block title={t.drawPatch} value={data.patch} />
              {!!data.notes?.length && (
                <div className="inspect-note warn">{data.notes.join("；")}</div>
              )}
              <Block
                title={t.leadLookups(data.lookups?.lead?.length ?? 0)}
                value={lookupsText(data.lookups?.lead)}
                foldable
              />
              <Block
                title={t.drawLookups(data.lookups?.draw?.length ?? 0)}
                value={lookupsText(data.lookups?.draw)}
                foldable
              />
              <Block title={t.leadReasoning} value={data.reasoning?.lead} foldable />
              <Block title={t.drawReasoning} value={data.reasoning?.draw} foldable />
              <Block title={t.selectionSnapshot} value={data.selection_snapshot} />
              <Block title={t.leadUsage} value={data.lead.usage} />
              {!!data.lead.usage?.retries && (
                <div className="inspect-note warn">
                  {t.retriesNote(data.lead.usage.retries)}
                </div>
              )}
              {data.prompt_tokens !== null && (
                <div className="inspect-note">
                  {t.promptTokens(data.prompt_tokens)}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
