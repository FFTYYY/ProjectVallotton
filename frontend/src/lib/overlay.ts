/**
 * Is a full-screen overlay open?
 *
 * Window-level gesture handlers — the swipe drawer, above all — have no way to
 * know that something is covering the page. A scrim that only darkens is not a
 * scrim: swiping across the lightbox was still opening the left rail behind
 * it. Overlays announce themselves here and those handlers stand down.
 *
 * A counter, not a boolean: overlays stack (a dialog over a lightbox), and the
 * inner one closing must not re-arm the page while the outer one is still up.
 */

let depth = 0;

export function pushOverlay(): () => void {
  depth += 1;
  let released = false;
  return () => {
    // idempotent: React may run an effect's cleanup more than once
    if (released) return;
    released = true;
    depth = Math.max(depth - 1, 0);
  };
}

export function overlayOpen(): boolean {
  return depth > 0;
}
