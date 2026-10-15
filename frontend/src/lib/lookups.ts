/**
 * What a model looked up in the tag table, as one block of text for 「检查」.
 *
 * A formatter rather than JSON.stringify because the interesting half of a
 * lookup is the answer, and the answer is a table of tab-separated lines:
 * stringified, every one of them turns into a single line of \t and \n
 * escapes, which is exactly the part nobody can read.
 *
 * Nothing is summarised and nothing is dropped. The dialog exists to show what
 * actually happened, and a formatter that decides which lookups matter is a
 * formatter that hides the one that explains the picture.
 */
import { t } from "../uiText";


export interface Lookup {
  queries: string[];
  result: string;
}

export function lookupsText(lookups: Lookup[] | null | undefined): string {
  if (!lookups?.length) return "";
  return lookups
    .map((run) => [t.lookupQuery((run.queries ?? []).join("，")), run.result].filter(Boolean).join("\n"))
    .join("\n\n");
}
