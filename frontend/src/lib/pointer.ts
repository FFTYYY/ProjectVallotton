/**
 * What a tap does when there is no hover.
 *
 * With a mouse a chip's details appear as the pointer arrives, so a click is
 * free to do what the chip is for (open the sidebar). A finger has no hover:
 * the only way to see the details is to tap, so on a touch screen the tap
 * shows them instead of reaching the chip — a second tap puts them away, and
 * so does a tap anywhere else.
 */

export function coarsePointer(): boolean {
  return typeof window !== "undefined" && window.matchMedia("(pointer: coarse)").matches;
}

/**
 * A tap on the anchor of a tooltip that shows on tap: whether it reaches the
 * anchor's own action, and whether the bubble is open afterwards.
 */
export function tapOnAnchor(coarse: boolean, open: boolean): { reaches: boolean; open: boolean } {
  return coarse ? { reaches: false, open: !open } : { reaches: true, open };
}
