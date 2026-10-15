/**
 * A tooltip that appears the moment the pointer arrives, not after the
 * browser's own second-and-a-half pause.
 *
 * It renders through a portal because the rails clip their overflow, so an
 * absolutely-positioned bubble inside one would be cut off at the edge. On a
 * touch screen there is no hover to speak of, so it stays out of the way --
 * unless the anchor opts into `tapToShow`, where a tap opens it in place of
 * the anchor's own action (a chip's details are only reachable that way on a
 * phone), a second tap or a tap anywhere else closes it, and so does a scroll,
 * which would otherwise leave it hanging where the anchor used to be.
 *
 * Two preferences, because the anchors differ in shape. A full-width row wants
 * the bubble beside it ("left" — the rails live on the right, so there is room
 * that way); a chip in a wrapping strip wants it above, where it cannot cover
 * the chip's neighbours. Each falls back the other way when the room is not
 * there.
 */

import { useEffect, useRef, useState, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { coarsePointer, tapOnAnchor } from "../lib/pointer";

interface Placement {
  top: number;
  left: number;
  side: "left" | "below" | "above" | "under";
}

/** horizontal room a bubble opening leftwards needs */
const MIN_ROOM = 260;
/** vertical room one opening upwards needs */
const MIN_ROOM_UP = 160;
/** half of .tooltip's max-width plus the page margin, for the centred sides */
const HALF = 172;

export function Tooltip({
  content,
  children,
  className = "",
  prefer = "left",
  tapToShow = false,
}: {
  content: string;
  children: ReactNode;
  className?: string;
  prefer?: "left" | "vertical";
  /** on a touch screen, a tap opens the bubble instead of reaching the anchor */
  tapToShow?: boolean;
}) {
  const [place, setPlace] = useState<Placement | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  const open = place !== null;

  // a bubble opened by a tap closes on the next tap elsewhere, or on a scroll
  useEffect(() => {
    if (!tapToShow || !open || !coarsePointer()) return;
    const away = (e: Event) => {
      if (e.target instanceof Node && ref.current?.contains(e.target)) return;
      setPlace(null);
    };
    const gone = () => setPlace(null);
    document.addEventListener("pointerdown", away, true);
    window.addEventListener("scroll", gone, true);
    return () => {
      document.removeEventListener("pointerdown", away, true);
      window.removeEventListener("scroll", gone, true);
    };
  }, [tapToShow, open]);

  function tap(e: MouseEvent) {
    // nothing to show: the tap is the anchor's as usual
    if (!tapToShow || !content.trim()) return;
    const next = tapOnAnchor(coarsePointer(), open);
    if (next.reaches) return;
    e.preventDefault();
    e.stopPropagation();
    if (next.open) show(true);
    else setPlace(null);
  }

  function show(byTap = false) {
    const el = ref.current;
    if (!el || !content.trim()) return;
    if (!byTap && coarsePointer()) return;
    // the wrapper is display:contents so it never disturbs the layout it sits
    // in — which also means it has no box of its own to measure
    const box = el.firstElementChild ?? el;
    const r = box.getBoundingClientRect();
    if (prefer === "vertical") {
      // centred on the anchor, clamped so a wide bubble cannot run off either
      // edge of the window
      const cx = Math.min(Math.max(r.left + r.width / 2, HALF), window.innerWidth - HALF);
      setPlace(
        r.top > MIN_ROOM_UP
          ? { top: r.top - 8, left: cx, side: "above" }
          : { top: r.bottom + 8, left: cx, side: "under" },
      );
      return;
    }
    setPlace(
      r.left > MIN_ROOM
        ? { top: r.top + r.height / 2, left: r.left - 10, side: "left" }
        : { top: r.bottom + 8, left: Math.max(12, r.left), side: "below" },
    );
  }

  return (
    <>
      <span
        ref={ref}
        className={`tt-anchor ${className}`}
        onMouseEnter={() => show()}
        onMouseLeave={() => setPlace(null)}
        onClickCapture={tap}
      >
        {children}
      </span>
      {place &&
        createPortal(
          <div
            className={`tooltip ${place.side}`}
            role="tooltip"
            style={{ top: place.top, left: place.left }}
          >
            {content}
          </div>,
          document.body,
        )}
    </>
  );
}
