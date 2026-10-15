/**
 * Which of a turn's pictures is its main one.
 *
 * `main_index` names a position. A reflection round throws away the pictures
 * it replaced and keeps counting positions, so the named position can be gone;
 * the backend then treats the first picture left as the main one
 * (pipeline.main_seed), and the transcript has to agree with it, or the seed
 * would follow a picture nobody can see marked.
 */

import type { Attachment } from "../api/types";

/** the position of the main picture, or null when there are no pictures */
export function mainPosition(pictures: Attachment[], mainIndex: number): number | null {
  const sorted = [...pictures].sort((a, b) => a.position - b.position);
  if (sorted.length === 0) return null;
  const named = sorted.find((a) => a.position === mainIndex);
  return (named ?? sorted[0]).position;
}

/** the main picture first, the rest in the order they were made */
export function mainFirst(pictures: Attachment[], mainIndex: number): Attachment[] {
  const sorted = [...pictures].sort((a, b) => a.position - b.position);
  const i = sorted.findIndex((a) => a.position === mainIndex);
  if (i <= 0) return sorted;
  return [sorted[i], ...sorted.slice(0, i), ...sorted.slice(i + 1)];
}
