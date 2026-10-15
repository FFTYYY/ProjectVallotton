import { useEffect, useRef, useState } from "react";

import { overlayOpen } from "../lib/overlay";

/**
 * Mobile drawer gesture: a horizontal swipe ANYWHERE on the page opens
 * (left→right) or closes (right→left) the left drawer. A direction lock
 * leaves vertical scrolling untouched — once a gesture is judged vertical
 * it is ignored for the rest of that touch.
 *
 * The listener is deliberately NOT passive. A horizontal swipe is also what
 * the browser reads as back/forward navigation, and a passive listener is
 * forbidden from calling preventDefault — so the drawer would open and the
 * page would navigate away underneath it. Once a touch is judged horizontal
 * every move in it is cancelled, for the whole touch and not just up to the
 * threshold: letting go early hands the rest of the gesture back.
 */
export function useSwipeDrawer() {
  const [open, setOpen] = useState(false);
  const startX = useRef(0);
  const startY = useRef(0);
  /** idle: undecided · vertical/native: hands off · horizontal: ours */
  const phase = useRef<"idle" | "vertical" | "horizontal" | "native">("idle");
  const fired = useRef(false);
  /** did this touch begin in the strip the browser watches for navigation? */
  const nearEdge = useRef(false);
  const openRef = useRef(open);
  openRef.current = open;

  useEffect(() => {
    const THRESHOLD = 55; // horizontal px to trigger
    const EDGE = 28; // px from either side the browser treats as its own

    /**
     * Does this touch begin on something that owns the horizontal axis?
     *
     * Two ways of owning it. A wide code block or table scrolls sideways, and
     * swiping inside it must scroll the content. And a control can claim the
     * axis outright with `touch-action: pan-y` — that declaration means "the
     * browser may pan me vertically, the page handles the rest", which is
     * exactly what the generation sliders say so a finger can drag them. Read
     * from the style rather than from a class name, so anything that makes the
     * same claim is honoured without this hook being told about it: dragging a
     * slider used to slide the whole rail shut instead.
     */
    function ownsTheHorizontal(target: EventTarget | null): boolean {
      let el = target instanceof Element ? target : null;
      while (el && el !== document.body) {
        const style = getComputedStyle(el);
        if (style.touchAction.split(/\s+/).includes("pan-y")) return true;
        if (el.scrollWidth > el.clientWidth + 4) {
          if (style.overflowX === "auto" || style.overflowX === "scroll") return true;
        }
        el = el.parentElement;
      }
      return false;
    }

    function onTouchStart(e: TouchEvent) {
      // something full-screen is up (the lightbox, a dialog): the page behind
      // it does not get to react. A scrim that only darkens is not a scrim.
      if (overlayOpen()) {
        phase.current = "native";
        return;
      }
      const t = e.touches[0];
      startX.current = t.clientX;
      startY.current = t.clientY;
      fired.current = false;
      nearEdge.current = t.clientX < EDGE || t.clientX > window.innerWidth - EDGE;
      phase.current = ownsTheHorizontal(e.target) ? "native" : "idle";
    }

    function onTouchMove(e: TouchEvent) {
      if (phase.current === "native" || phase.current === "vertical") return;

      if (phase.current === "idle") {
        const t = e.touches[0];
        const dx = t.clientX - startX.current;
        const dy = t.clientY - startY.current;
        // The browser commits to its edge swipe early, so a touch that began
        // near a screen edge gets judged on a much shorter run — 12px of
        // undecided slack there is enough for navigation to win the race.
        const slack = nearEdge.current ? 4 : 12;
        if (Math.abs(dx) < slack && Math.abs(dy) < slack) {
          if (nearEdge.current && e.cancelable) e.preventDefault();
          return; // too small to judge
        }
        if (Math.abs(dy) > Math.abs(dx)) {
          phase.current = "vertical"; // it's a scroll, leave it alone
          return;
        }
        phase.current = "horizontal";
      }

      // ours now: keep it away from the browser's edge-swipe navigation
      if (e.cancelable) e.preventDefault();
      if (fired.current) return;

      const dx = e.touches[0].clientX - startX.current;
      if (dx > THRESHOLD && !openRef.current) {
        setOpen(true);
        fired.current = true;
      } else if (dx < -THRESHOLD && openRef.current) {
        setOpen(false);
        fired.current = true;
      }
    }

    function onTouchEnd() {
      phase.current = "idle";
      fired.current = false;
    }

    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchmove", onTouchMove, { passive: false });
    window.addEventListener("touchend", onTouchEnd, { passive: true });
    window.addEventListener("touchcancel", onTouchEnd, { passive: true });
    return () => {
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("touchend", onTouchEnd);
      window.removeEventListener("touchcancel", onTouchEnd);
    };
  }, []);

  return { open, setOpen, dragging: false };
}
