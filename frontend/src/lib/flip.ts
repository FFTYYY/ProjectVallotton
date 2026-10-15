/**
 * Move rows to their new places instead of teleporting them there.
 *
 * When the tree changes — something dropped somewhere else, a folder opened,
 * an item archived — React simply paints the new arrangement, and everything
 * that moved appears to jump. This measures where each row was, lets the new
 * layout happen, then puts each row back where it started with a transform
 * and animates that away. The browser does the interpolation; nothing is
 * animated that did not actually move.
 *
 * Rows are found by `data-row-id`, so nothing has to thread refs around.
 */

import { useLayoutEffect, useRef, type RefObject } from "react";

const DURATION = 260;
const EASE = "cubic-bezier(0.2, 0.7, 0.2, 1)";

export function useSmoothReorder(
  container: RefObject<HTMLElement | null>,
  /** changes exactly when the arrangement does */
  signature: string,
  /** off during a drag: the gaps are already animating, and a transform here
      would fight the one the drag library is applying */
  enabled: boolean,
) {
  const previous = useRef<Map<string, number>>(new Map());

  useLayoutEffect(() => {
    const root = container.current;
    if (!root) return;

    const rows = Array.from(
      root.querySelectorAll<HTMLElement>("[data-row-id]"),
    );
    const now = new Map<string, number>();
    for (const el of rows) {
      const id = el.dataset.rowId;
      if (id) now.set(id, el.getBoundingClientRect().top);
    }

    if (enabled && previous.current.size > 0) {
      const timers: number[] = [];
      for (const el of rows) {
        const id = el.dataset.rowId;
        if (!id) continue;
        const before = previous.current.get(id);
        const after = now.get(id);
        if (before === undefined || after === undefined) continue;
        const delta = before - after;
        if (Math.abs(delta) < 1) continue;

        el.style.transition = "none";
        el.style.transform = `translateY(${delta}px)`;
        requestAnimationFrame(() => {
          el.style.transition = `transform ${DURATION}ms ${EASE}`;
          el.style.transform = "";
        });
        // hand the element back to the stylesheet once it has arrived
        timers.push(
          window.setTimeout(() => {
            el.style.transition = "";
            el.style.transform = "";
          }, DURATION + 60),
        );
      }
      previous.current = now;
      return () => timers.forEach(window.clearTimeout);
    }

    previous.current = now;
  }, [container, signature, enabled]);
}
