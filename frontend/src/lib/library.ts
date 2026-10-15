/**
 * What a library entry contributes, as one block of text.
 *
 * The rail, the chips above the composer and the record under a picture all
 * want to say the same thing on hover, so they say it from here rather than
 * each keeping their own copy — which is how they drifted apart in the first
 * place.
 */

interface VariantLike {
  title: string;
  content?: string;
}

interface CharacterLike {
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

export function describeCharacter(c: CharacterLike): string {
  const parts = [c.description?.trim()].filter(Boolean) as string[];
  for (const v of c.variants) {
    if (v.content?.trim()) parts.push(`【${v.title}】${v.content.trim()}`);
  }
  return parts.join("\n\n");
}
