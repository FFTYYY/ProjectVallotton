import type { GenSettings } from "../api/types";
import { t } from "../uiText";

/**
 * The shape of the four generation settings, and the rules about them.
 *
 * Here rather than in either store because both need it: a conversation's
 * settings and the defaults a new conversation is poured from are the same
 * four values with the same ceilings, and the two stores refer to each other —
 * the new-chat page shows the defaults. One of them owning the shape would
 * make that a cycle.
 */

/** the backend's own ceilings; kept in step by hand, and clamped there anyway */
export const MAX_REFLECTION_ROUNDS = 10;
export const MAX_CONTINUOUS_ROUNDS = 20;

/** 低 / 中 / 高. What each costs is the backend's to know. */
export type Quality = "low" | "mid" | "high";
export const QUALITIES: Quality[] = ["low", "mid", "high"];
/** the word for a quality, in the language chosen now (read when shown, not at import) */
export function qualityLabel(q: Quality): string {
  return { low: t.qualityLow, mid: t.qualityMid, high: t.qualityHigh }[q];
}

/** 关闭 / 仅修改 / 全部 —— what the app may approve without asking. */
export type AutoLibrary = "off" | "edits" | "all";
export const AUTO_LIBRARY: AutoLibrary[] = ["off", "edits", "all"];
export function autoLibraryLabel(m: AutoLibrary): string {
  return { off: t.autoOff, edits: t.autoEdits, all: t.autoAll }[m];
}

export interface Values {
  nImages: number; // images per generation, 1..4
  quality: Quality;
  autoLibrary: AutoLibrary;
  reflectionRounds: number; // 0 = off, else how many times it may look again
  continuousRounds: number; // 1 = one picture and stop, i.e. ordinary
  /** which quick phrase gets sent again; null until one is picked */
  continuousPhraseId: string | null;
  /**
   * 常驻参考: keep every reference picture in view instead of only the newest
   * turn's. Nothing is copied — see Message.sticky_refs — so this is a
   * property of each turn as it is sent, not a state the conversation holds.
   */
  stickyRefs: boolean;
}

/** the factory floor, under whatever the settings page holds */
export const DEFAULTS: Values = {
  nImages: 1,
  quality: "mid",
  autoLibrary: "off",
  reflectionRounds: 1,
  continuousRounds: 1,
  continuousPhraseId: null,
  stickyRefs: false,
};

export function clampImages(n: number): number {
  return Number.isFinite(n) ? Math.min(Math.max(Math.round(n), 1), 4) : 1;
}

export function clampRounds(n: number): number {
  return Number.isFinite(n)
    ? Math.min(Math.max(Math.round(n), 0), MAX_REFLECTION_ROUNDS)
    : DEFAULTS.reflectionRounds;
}

export function clampRuns(n: number): number {
  return Number.isFinite(n) ? Math.min(Math.max(Math.round(n), 1), MAX_CONTINUOUS_ROUNDS) : 1;
}

/** the same ceilings, applied to whichever keys a patch happens to carry */
export function clampAll(patch: Partial<Values>): Partial<Values> {
  const out: Partial<Values> = { ...patch };
  if (out.nImages !== undefined) out.nImages = clampImages(out.nImages);
  if (out.reflectionRounds !== undefined) out.reflectionRounds = clampRounds(out.reflectionRounds);
  if (out.continuousRounds !== undefined) out.continuousRounds = clampRuns(out.continuousRounds);
  if (out.stickyRefs !== undefined) out.stickyRefs = !!out.stickyRefs;
  return out;
}

/** the wire shape -> the stores' shape */
export function fromWire(raw: GenSettings | undefined | null): Values {
  if (!raw) return { ...DEFAULTS };
  return {
    nImages: clampImages(raw.n_images),
    reflectionRounds: clampRounds(raw.reflection_rounds),
    continuousRounds: clampRuns(raw.continuous_rounds),
    continuousPhraseId: raw.continuous_phrase_id ?? null,
    stickyRefs: !!raw.sticky_refs,
    quality: cleanQuality(raw.quality),
    autoLibrary: AUTO_LIBRARY.includes(raw.auto_library as AutoLibrary)
      ? (raw.auto_library as AutoLibrary)
      : DEFAULTS.autoLibrary,
  };
}

export function cleanQuality(raw: unknown): Quality {
  return QUALITIES.includes(raw as Quality) ? (raw as Quality) : DEFAULTS.quality;
}

export function toWire(patch: Partial<Values>): Partial<GenSettings> {
  const out: Partial<GenSettings> = {};
  if (patch.nImages !== undefined) out.n_images = patch.nImages;
  if (patch.reflectionRounds !== undefined) out.reflection_rounds = patch.reflectionRounds;
  if (patch.continuousRounds !== undefined) out.continuous_rounds = patch.continuousRounds;
  if (patch.continuousPhraseId !== undefined)
    out.continuous_phrase_id = patch.continuousPhraseId;
  if (patch.stickyRefs !== undefined) out.sticky_refs = patch.stickyRefs;
  if (patch.quality !== undefined) out.quality = patch.quality;
  if (patch.autoLibrary !== undefined) out.auto_library = patch.autoLibrary;
  return out;
}
