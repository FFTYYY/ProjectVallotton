/**
 * Small readings of a library entry, shared by the library page, the rail,
 * the proposal card and the record under a picture.
 */

import type { Message } from "../api/types";
import { t } from "../uiText";

/** the key a project entry is written with, in the lead's own notation;
 *  never shown to a person (displayKey) */
export const PROJECT_PREFIX = "@";
/** between a character's key and one of its outfits', in the same notation */
export const VARIANT_SEP = "/";

/**
 * A key as a person reads it: no project prefix, and a variant joined to
 * its character the way the rest of the interface writes it (人物·变体).
 */
export function displayKey(key: string): string {
  const bare = key.startsWith(PROJECT_PREFIX) ? key.slice(PROJECT_PREFIX.length).trim() : key.trim();
  const at = bare.indexOf(VARIANT_SEP);
  return at < 0 ? bare : t.variantName(bare.slice(0, at), bare.slice(at + VARIANT_SEP.length));
}

/**
 * Tags out of what a person typed: split on commas and line breaks, trimmed,
 * empties dropped, the same word kept once. The server dedupes further by
 * its own spelling rules; this is only so the box shows what will be sent.
 */
export function splitTags(text: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  // a comma of either width, or a line break
  for (const raw of text.split(/[,，\n]/)) {
    const tag = raw.trim();
    if (!tag) continue;
    const key = tag.toLowerCase().replace(/\s+/g, " ");
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(tag);
  }
  return out;
}

export function joinTags(tags: readonly string[]): string {
  return tags.join(", ");
}

/** a tag list out of whatever the lead wrote: a string, a list, or nothing */
export function tagList(value: unknown): string[] {
  if (typeof value === "string") return splitTags(value);
  if (Array.isArray(value)) return splitTags(value.map(String).join(","));
  return [];
}

/** whether two tag lists say the same thing, in the same order */
export function sameTags(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((tag, i) => tag === b[i]);
}

export type Origin = "project" | "lead" | "user";

/**
 * Where an entry came from. The selection names its layer by
 * conversation_id, a picture's receipt by layer; in the conversation's own
 * layer, origin_seq is the lead message that wrote it, and none means a
 * person did.
 */
export function originOf(e: {
  conversation_id?: string | null;
  layer?: string;
  origin_seq?: number | null;
}): Origin {
  const own = !!e.conversation_id || e.layer === "conversation";
  if (!own) return "project";
  return e.origin_seq ? "lead" : "user";
}


/** an outfit's own title out of its key, with or without its character */
export function variantTitle(key: string): string {
  const at = key.indexOf(VARIANT_SEP);
  return at < 0 ? key : key.slice(at + VARIANT_SEP.length);
}

/** a picture that changes an earlier drawing rather than starting anew */
export function isEdit(m: Pick<Message, "draw_brief">): boolean {
  return !!m.draw_brief?.inherits;
}
