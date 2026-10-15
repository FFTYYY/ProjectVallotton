/**
 * The one line of a long message that a folded view can show.
 *
 * A model's failure arrives as a sentence and then, often, the whole answer it
 * could not read — several lines of it. The transcript shows the sentence; the
 * rest is behind the door. Splitting rather than slicing keeps the line a whole
 * thought instead of a cut-off one, and the cap is there because an answer with
 * no line breaks at all is still one line.
 */
export function firstLine(text: string | null | undefined, cap = 90): string {
  const line =
    (text || "")
      .split("\n")
      .map((s) => s.trim())
      .find(Boolean) || "";
  return line.length > cap ? line.slice(0, cap) + "…" : line;
}
