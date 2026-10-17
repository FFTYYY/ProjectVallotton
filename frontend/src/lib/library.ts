import { t } from "../uiText";

import { joinTags } from "./entries";

/**
 * What a library entry contributes, as one block of text.
 *
 * The rail and the record under a picture want to say the same thing on
 * hover, so they say it from here rather than each keeping their own copy —
 * which is how they drifted apart in the first place.
 */

interface EntryLike {
  positive?: readonly string[];
  negative?: readonly string[];
  prose?: string;
}

interface VariantLike extends EntryLike {
  title: string;
  content?: string;
}

interface CharacterLike extends EntryLike {
  description?: string;
  variants: VariantLike[];
}

/**
 * Whether an entry lives in the conversation's own layer rather than the
 * project's. The selection says so by conversation_id; a turn's receipt (the
 * snapshot) by layer. A receipt from before layers were recorded says
 * neither, and reads as the project's.
 */
export function ownLayer(e: { conversation_id?: string | null; layer?: string }): boolean {
  return !!e.conversation_id || e.layer === "conversation";
}

/** an entry's note, tags and prose, one line each; "" when it has none */
export function describeEntry(note: string | undefined, e: EntryLike): string {
  const lines = [note?.trim()];
  if (e.positive?.length) lines.push(`+ ${joinTags(e.positive)}`);
  if (e.negative?.length) lines.push(`− ${joinTags(e.negative)}`);
  if (e.prose?.trim()) lines.push(e.prose.trim());
  return lines.filter(Boolean).join("\n");
}

export function describeCharacter(c: CharacterLike): string {
  const parts = [describeEntry(c.description, c)];
  for (const v of c.variants) {
    const said = describeEntry(v.content, v);
    if (said) parts.push(t.variantBlock(v.title, said));
  }
  return parts.filter(Boolean).join("\n\n");
}
