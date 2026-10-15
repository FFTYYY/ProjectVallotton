import type { Message } from "../api/types";
import { t } from "../uiText";

/**
 * What she is doing inside a reflection round.
 *
 * Her own words, not the generic ones: inside a round she is looking at a
 * picture she already made, and 正在构思 is what she does before there is one.
 * One number, not two — the round is the fraction worth showing, and a paint
 * counter nested inside it reads as arithmetic.
 */
export function reflectStage(inner: string): string {
  if (inner === "prompting") return t.reflectPrompting;
  if (inner.startsWith("lookup")) return t.reflectLookup;
  if (inner.startsWith("retry:")) return t.reflectRetry;
  if (inner.startsWith("painting")) return t.reflectPainting;
  return t.reflectLooking;
}

function stageLabel(phase: string): string {
  if (phase === "prompting") return t.phasePrompting;
  // A tool round is two full model responses — one to ask, one to use the
  // answer — so it is usually the longest part of a turn. Left unlabelled the
  // spinner claimed she was still writing tags while she was reading the table.
  if (phase.startsWith("lookup")) return t.phaseLookup;
  // a retry is not a stage, it is something going wrong — say so plainly
  // rather than letting the same soothing line sit there twice as long
  if (phase === "retry:thinking") return t.phaseRetryThinking;
  if (phase === "retry:prompting") return t.phaseRetryPrompting;
  if (phase.startsWith("painting")) {
    const part = phase.split(":")[1];
    return part ? t.phasePaintingPart(part) : t.phasePainting;
  }
  return t.phaseThinking;
}

/**
 * What is happening right now.
 *
 * A turn passes through up to four services and any one of them can be the
 * slow one, so each stage says something different. A reflection round runs
 * those same stages a second time, so the phase is composite —
 * "reflecting:2/5:painting:0/1" — and reads as one thing inside another. Left
 * flat, a reflection redraw was word-for-word an ordinary paint and the line
 * stopped mentioning reflection halfway through every round.
 */
export function phaseLabel(msg: Message): string {
  if (msg.phase === "cancelling") return t.phaseCancelling;
  if (msg.phase.startsWith("reflecting:")) {
    const [, progress, ...rest] = msg.phase.split(":");
    return t.reflectLine(reflectStage(rest.join(":")), progress);
  }
  return t.phaseLine(stageLabel(msg.phase));
}
