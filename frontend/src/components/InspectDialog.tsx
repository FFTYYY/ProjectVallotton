import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import type {
  DrawBrief,
  NaiPrompts,
  ReflectionRound,
  SelectionSnapshot,
  SnapshotEntry,
  UsedEntry,
} from "../api/types";
import { isLook } from "../api/types";
import { ApiError, api } from "../api/client";
import { displayKey, joinTags, originOf } from "../lib/entries";
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

/** every piece of a prompt with its token count (backend novelai.prompt_tokens) */
interface TokenCounts {
  base: number;
  characters: { key: string; tokens: number }[];
  negative: number;
  /** the base and the characters together: what the limit is measured on */
  positive: number;
}

/**
 * How the prompt was made (message.prompt): the lead's description, the
 * exposed entries as they read, the drawing model's answer and what each
 * entry came to.
 */
interface Picture {
  description: string;
  text: string;
  size: string;
  inherits: string;
  exposed_keys: string[];
  exposed: SnapshotEntry[];
  answer: {
    entries: Record<string, unknown>;
    positive: string[];
    negative: string[];
    prose: string;
    subjects: { key: string; center_x: number | null; center_y: number | null }[];
    size: string;
  };
  assembly: UsedEntry[];
}

interface RawTurn {
  lead: {
    reply?: string | null;
    draw?: DrawBrief | null;
    /** the entries it wrote into this conversation's library */
    settings?: unknown[];
    characters?: unknown[];
    /** its edits to the project's entries, raised as a proposal */
    edits?: unknown;
    usage?: { retries?: number } & Record<string, unknown>;
    /** the answer as the model gave it, before the app tidied it */
    raw?: unknown;
    /** what the app sent back about an answer that did not hold together, and that answer */
    checks?: { note: string; answer: unknown }[];
  };
  /** the brief behind the pictures that were kept (after reflection, the last round's) */
  draw_brief?: DrawBrief | null;
  /** pictures of the batch that failed, and what the proposal had to say */
  paint_errors?: string[];
  edit_notes?: string[];
  /** the Danbooru index is not loaded: an empty unknown_tags means nothing */
  tag_index_missing?: boolean;
  drawing: NaiPrompts | null;
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
  /** the compressor's pass, only when the prompt was over NovelAI's limit */
  compress: {
    before: unknown;
    counts: { before: TokenCounts; after: TokenCounts };
    changed: boolean;
    fits: boolean;
    reasoning: string;
    usage?: unknown;
  } | null;
  /** rows from before entries were kept carry an older shape; shown verbatim then */
  picture: Picture | Record<string, unknown> | null;
  /** what the lint had to say: foreign words, separators, the limit */
  notes: string[];
  /** tags in the prompts that the Danbooru index does not have */
  unknown_tags: string[];
  /** an answer that could not be read, kept whole; "" when there was none */
  unparsed: string;
  /** one entry per reflection round; null when reflection did not run */
  reflection: ReflectionRound[] | null;
  selection_snapshot: SelectionSnapshot | null;
  prompt_tokens: number | null;
}

function isPicture(value: RawTurn["picture"]): value is Picture {
  return !!value && Array.isArray((value as Picture).assembly) && Array.isArray((value as Picture).exposed);
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

/** an entry's tags and prose, one line each, as a picture had them */
function TagLines({ positive, negative, prose }: { positive: string[]; negative: string[]; prose: string }) {
  return (
    <>
      {positive.length > 0 && <code className="inspect-entry-tags">+ {joinTags(positive)}</code>}
      {negative.length > 0 && <code className="inspect-entry-tags">− {joinTags(negative)}</code>}
      {prose && <div className="inspect-entry-note">{prose}</div>}
    </>
  );
}

/**
 * How this picture was made: the description the drawing model was handed,
 * each exposed entry with whether it was used and what it came to, and what
 * the drawing model added of its own.
 */
function PictureSection({ picture }: { picture: Picture }) {
  const answer = picture.answer ?? { positive: [], negative: [], prose: "", subjects: [] };
  const subjects = (answer.subjects ?? [])
    .map((s) => `${s.key}${s.center_x !== null && s.center_y !== null ? t.position(s.center_x, s.center_y) : ""}`)
    .join(t.listSep);
  const own = [
    [t.positiveTags, joinTags(answer.positive ?? [])],
    [t.negativeTags, joinTags(answer.negative ?? [])],
    [t.prose, answer.prose ?? ""],
    [t.subjects, subjects],
  ].filter(([, value]) => value);
  return (
    <>
      <div className="inspect-block">
        <div className="inspect-block-title">{t.pictureDescription}</div>
        <div className="inspect-said">{picture.description || t.empty}</div>
        <div className="inspect-meta">
          {picture.size && <span>{t.sizeLabel} · {picture.size}</span>}
          {picture.text && <span>{t.textLabel} · {picture.text}</span>}
          {picture.inherits && <span>{t.editOf(picture.inherits)}</span>}
        </div>
      </div>
      <div className="inspect-block">
        <div className="inspect-block-title">{t.exposedEntries(picture.assembly.length)}</div>
        {picture.assembly.length === 0 && <div className="inspect-said">{t.empty}</div>}
        {picture.assembly.map((a) => {
          const e = picture.exposed.find((x) => x.key === a.key);
          return (
            <div className={`inspect-entry${a.used ? "" : " unused"}`} key={a.key}>
              <div className="inspect-entry-head">
                <span className="inspect-entry-key">{displayKey(e?.shown_key ?? a.key)}</span>
                <span className="inspect-entry-kind">
                  {t.kindLabels[a.kind]}
                  {e?.category ? ` · ${e.category}` : ""}
                </span>
                {e && originOf(e) === "user" && <span className="inspect-entry-kind">{t.fromUser}</span>}
                {!a.used && <span className="inspect-entry-unused">{t.unused}</span>}
              </div>
              {e?.note && <div className="inspect-entry-note">{e.note}</div>}
              <TagLines positive={a.positive} negative={a.negative} prose={a.prose} />
            </div>
          );
        })}
      </div>
      <div className="inspect-block">
        <div className="inspect-block-title">{t.drawOwn}</div>
        {own.length === 0 && <div className="inspect-said">{t.empty}</div>}
        {own.length > 0 && (
          <div className="inspect-kv">
            {own.map(([label, value]) => (
              <div key={label}>
                <span>{label}</span>
                <code>{value}</code>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
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

  const picture = data && isPicture(data.picture) ? data.picture : null;
  const wrote = !!(data?.lead.settings?.length || data?.lead.characters?.length);

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
              {wrote && (
                <Block
                  title={t.leadEntries}
                  value={{ settings: data.lead.settings ?? [], characters: data.lead.characters ?? [] }}
                />
              )}
              <Block title={t.leadEdits} value={data.lead.edits} />
              <Block title={t.leadRaw} value={data.lead.raw} />
              {!!data.lead.checks?.length && <Block title={t.leadChecks} value={data.lead.checks} />}
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
              {picture && <PictureSection picture={picture} />}
              {/* the prompts as sent: base, each character with its own
                  negative and position, and the picture's negative */}
              <Block title={t.drawPrompts} value={data.drawing} />
              {data.tag_index_missing && (
                <div className="inspect-note warn">{t.tagIndexMissing}</div>
              )}
              {!!data.paint_errors?.length && (
                <div className="inspect-note warn">{data.paint_errors.join(t.noteSep)}</div>
              )}
              {!!data.edit_notes?.length && (
                <div className="inspect-note warn">{data.edit_notes.join(t.noteSep)}</div>
              )}
              {!!data.unknown_tags?.length && (
                <div className="inspect-note warn">
                  {t.unknownTags(data.unknown_tags.length)}
                  {data.unknown_tags.join(t.listSep)}
                </div>
              )}
              {!!data.notes?.length && (
                <div className="inspect-note warn">{data.notes.join(t.noteSep)}</div>
              )}
              {data.compress && (
                <>
                  <div className={`inspect-note${data.compress.fits ? "" : " warn"}`}>
                    {data.compress.changed
                      ? t.compressNote(
                          data.compress.counts?.before?.positive ?? 0,
                          data.compress.counts?.after?.positive ?? 0,
                        )
                      : t.compressUnchanged}
                    {!data.compress.fits && ` · ${t.compressStillOver}`}
                  </div>
                  {data.compress.changed && (
                    <Block title={t.compressBefore} value={data.compress.before} foldable />
                  )}
                  <Block title={t.compressReasoning} value={data.compress.reasoning} foldable />
                </>
              )}
              {/* the whole record, for a field the sections above do not show */}
              <Block title={t.pictureRecord} value={data.picture} foldable />
              {!picture && (
                <Block title={t.exposedSnapshot} value={data.selection_snapshot} foldable />
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
