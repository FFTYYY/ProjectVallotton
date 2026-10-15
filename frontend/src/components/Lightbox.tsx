import { useCallback, useEffect, useRef, useState } from "react";

import type { Attachment } from "../api/types";
import { pushOverlay } from "../lib/overlay";
import { useUi } from "../store/ui";
import {
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CloseIcon,
  DownloadIcon,
  StarIcon,
  PaperclipIcon,
} from "./Icons";
import { t } from "../uiText";

/**
 * The full-screen view of one picture.
 *
 * Zoom and pan follow the input, because the two habits genuinely differ: a
 * mouse expects a click to magnify and the backdrop to dismiss, while a touch
 * screen expects a double-tap to magnify and a single tap to leave. Pointer
 * events carry which one it is, so both live in one set of handlers rather
 * than two code paths that drift.
 *
 * While it is up the page underneath is inert — see lib/overlay: the swipe
 * drawer listens on the window and was happily opening the left rail through
 * what looked like a modal scrim.
 */

/**
 * How far in a zoom goes.
 *
 * A double tap goes straight to the far end and straight back, because that is
 * what a viewer is for. Two fingers go anywhere in between — the same ceiling,
 * reached at whatever pace the hand wants.
 */
const ZOOM = 2.5;
/** below this the picture is at its natural size, whatever rounding says */
const NATURAL = 1.01;
/** movement under this is a tap, not a drag */
const TAP_SLOP = 8;
/** a second tap within this is a double-tap */
const DOUBLE_TAP_MS = 280;
/** how far a finger must travel sideways before it means "the next one" */
const SWIPE_MIN = 60;
/** ...and how much more sideways than up-down, so a scroll is never a swipe */
const SWIPE_RATIO = 1.4;
/** each half of the slide when the picture changes */
const SLIDE_MS = 170;
/**
 * What a drag toward the end of the set does.
 *
 * A fraction alone is not enough: a quarter of a long drag is still a fifth of
 * a phone screen, which reads as a page about to turn. The cap is what makes
 * it a wall — past a couple of centimetres of finger travel the picture simply
 * stops, however hard you push.
 */
const END_RESISTANCE = 0.25;
const END_MAX = 24;

interface LightboxProps {
  images: Attachment[];
  index: number;
  onClose: () => void;
  onIndex: (i: number) => void;
  /** whether this picture is its turn's main one; absent for references */
  isMain?: (att: Attachment) => boolean;
  /** make this picture its turn's main one; absent for references */
  onSetMain?: (att: Attachment) => void;
}

interface View {
  scale: number;
  x: number;
  y: number;
}

const RESET: View = { scale: 1, x: 0, y: 0 };

export function Lightbox({ images, index, onClose, onIndex, isMain, onSetMain }: LightboxProps) {
  const img = images[index];
  const [view, setView] = useState<View>(RESET);
  /** every finger currently down, so two of them can be told from one */
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  /** the gesture in progress, measured from where it started */
  const pinch = useRef<{ dist: number; from: View } | null>(null);
  /** a lifted finger after a pinch is not a tap */
  const pinched = useRef(false);
  /**
   * How far the picture has been dragged sideways in an unzoomed swipe.
   *
   * Separate from `view.x`, which clamp() pins to 0 while the picture fits the
   * frame — correct for panning, and exactly wrong here. Without this the
   * finger moves and nothing does, which reads as a dead screen until you let
   * go and the picture suddenly changes.
   */
  const [swipe, setSwipe] = useState(0);
  /**
   * Is the sideways offset being animated, or written per frame by a finger?
   *
   * A finger must not be transitioned — the picture would lag behind the touch
   * point by the duration, which feels like a stuck screen. Everything else —
   * springing back from a swipe that fell short, and the slide when the
   * picture changes — must be, or it teleports.
   */
  const [animated, setAnimated] = useState(false);
  const [fading, setFading] = useState(false);
  const sliding = useRef(false);
  const slideTimers = useRef<number[]>([]);
  const frameRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  // gesture bookkeeping, all in refs: none of it should cause a render
  const drag = useRef<{ id: number; x: number; y: number; from: View; moved: boolean } | null>(null);
  const lastTap = useRef(0);
  const tapTimer = useRef<number | null>(null);

  const zoomed = view.scale > 1;

  /**
   * Which edge is about to be crossed, if the finger stopped now.
   *
   * A swipe has a threshold, and a threshold you cannot see is a threshold you
   * discover by guessing: you let go, nothing happens, and you have no idea
   * whether you moved too little or the gesture does not exist. So the edge
   * you are about to reach lights up the moment the movement is enough — and
   * stays dark when that direction has nowhere to go, which answers the other
   * half of the question without a word.
   */
  const armed: "prev" | "next" | null =
    swipe > SWIPE_MIN && index > 0
      ? "prev"
      : swipe < -SWIPE_MIN && index < images.length - 1
        ? "next"
        : null;

  // ---- the page underneath stops listening -------------------------------
  useEffect(() => pushOverlay(), []);

  // ---- a new picture starts unzoomed --------------------------------------
  // (the sideways offset is NOT reset here: while a slide is running it is
  // the thing doing the sliding, and index changes half way through one)
  useEffect(() => setView(RESET), [index]);

  /**
   * Which picture the <img> is actually showing.
   *
   * One element whose src changes: until the new file has decoded the browser
   * keeps painting the old one, so the counter said 4/9 over a picture that
   * was still number 3. It only ever showed on a picture not already in cache
   * — which is to say right after opening a conversation, from a thumbnail in
   * the middle of it. Better a moment of nothing than the wrong picture
   * presented as the right one.
   */
  const [readyUrl, setReadyUrl] = useState<string | null>(null);
  /**
   * Which of the two copies is on screen.
   *
   * The small one first — it is almost certainly in the browser's cache
   * already, because the grid that was just tapped drew it — and then the full
   * picture once it has decoded. The alternative is what this used to be: a
   * black frame for as long as 1.3 MB takes to arrive, on the tap that was
   * meant to show the picture.
   */
  const [shown, setShown] = useState<string>(() => img?.thumb_url ?? "");
  /** pictures already sent to the composer during this opening */
  const [added, setAdded] = useState<ReadonlySet<string>>(() => new Set());
  const [sending, setSending] = useState(false);
  const composer = useUi((s) => s.composer);
  useEffect(() => {
    const el = imgRef.current;
    // already decoded (cache, or a re-render of the same picture): onLoad will
    // not fire again, so take it from the element
    if (el && el.complete && el.naturalWidth > 0 && el.currentSrc && shown) {
      if (el.currentSrc.endsWith(shown) || el.currentSrc === shown) setReadyUrl(shown);
    }
  }, [shown]);

  // Show the small one, fetch the full one, swap when it is decoded. Swapping
  // only after `decode()` is what keeps it from flashing: the element never
  // holds a src the browser has not finished with.
  useEffect(() => {
    if (!img) return;
    setShown(img.thumb_url);
    let live = true;
    const full = new Image();
    full.src = img.url;
    full
      .decode()
      .then(() => {
        if (live) setShown(img.url);
      })
      .catch(() => {
        // no full picture: the small one is already on screen and stays
      });
    return () => {
      live = false;
    };
  }, [img?.id, img?.url, img?.thumb_url]);

  // and so that there is usually nothing to wait for: the one on each side is
  // fetched while this one is being looked at
  useEffect(() => {
    for (const near of [images[index + 1], images[index - 1]]) {
      if (!near) continue;
      // the small one, because that is what the next tap will show first
      const pre = new Image();
      pre.src = near.thumb_url;
      const full = new Image();
      full.src = near.url;
    }
  }, [images, index]);

  useEffect(
    () => () => slideTimers.current.forEach(window.clearTimeout),
    [],
  );

  /**
   * Change picture, as a movement rather than a cut.
   *
   * Out to one side, swap, in from the other — the direction says which way
   * you went through the set, which a straight replacement cannot. It runs the
   * same way whichever asked for it: a finger, an arrow, or a key.
   *
   * `sliding` drops anything asked for mid-slide. Queueing would be kinder to
   * someone holding an arrow key down, but a half-finished slide interrupted
   * by another is a picture stuck off-centre, and that is worse than a dropped
   * keypress.
   *
   * Returns whether it took the request. The caller needs to know: a finger
   * that has just been lifted has left the picture off-centre, and if nothing
   * is going to move it, something has to put it back. Saying nothing here is
   * how the picture ended up parked at the edge of the screen — neither turned
   * nor returned — at either end of the set.
   */
  const go = useCallback(
    (to: number): boolean => {
      if (to < 0 || to >= images.length || to === index || sliding.current) {
        return false;
      }
      const width = frameRef.current?.clientWidth ?? 400;
      const dir = to > index ? 1 : -1;      // 1 = onward through the set
      const travel = Math.min(width * 0.55, 420);

      sliding.current = true;
      setAnimated(true);
      setFading(true);
      setSwipe(-dir * travel);

      const after = (ms: number, fn: () => void) =>
        slideTimers.current.push(window.setTimeout(fn, ms));

      after(SLIDE_MS, () => {
        onIndex(to);
        // put it down on the far side without a transition, then let the
        // next frame carry it in
        setAnimated(false);
        setSwipe(dir * travel);
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            setAnimated(true);
            setFading(false);
            setSwipe(0);
            after(SLIDE_MS, () => {
              sliding.current = false;
            });
          }),
        );
      });
      return true;
    },
    [index, images.length, onIndex],
  );

  // ---- keyboard, and the phone's back gesture ----------------------------
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft") go(index - 1);
      if (e.key === "ArrowRight") go(index + 1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, go, onClose]);

  useEffect(
    () => () => {
      if (tapTimer.current !== null) window.clearTimeout(tapTimer.current);
    },
    [],
  );

  /**
   * Keep the picture's own edges from leaving the frame.
   *
   * Panning beyond them strands the viewer on blank ground with no clue which
   * way is back, which is the one thing a drag can get badly wrong.
   */
  const clamp = useCallback((v: View): View => {
    const el = imgRef.current;
    const frame = frameRef.current;
    if (!el || !frame) return v;
    const r = el.getBoundingClientRect();
    // the rect is already scaled, so the slack is half of what the zoom added
    const slackX = Math.max((r.width - frame.clientWidth) / 2, 0);
    const slackY = Math.max((r.height - frame.clientHeight) / 2, 0);
    return {
      scale: v.scale,
      x: Math.min(Math.max(v.x, -slackX), slackX),
      y: Math.min(Math.max(v.y, -slackY), slackY),
    };
  }, []);

  /** Zoom toward a point, so what was under the cursor stays under it. */
  const toggleZoomAt = useCallback(
    (clientX: number, clientY: number) => {
      setView((cur) => {
        // already as far in as it goes: come all the way back. Anywhere short
        // of that — including part-way through a pinch — go to the far end,
        // because that is the thing a second tap is asking for.
        if (cur.scale >= ZOOM - 0.01) return RESET;
        const frame = frameRef.current;
        if (!frame) return { scale: ZOOM, x: 0, y: 0 };
        const r = frame.getBoundingClientRect();
        const dx = clientX - (r.left + r.width / 2);
        const dy = clientY - (r.top + r.height / 2);
        return clamp({ scale: ZOOM, x: -dx * (ZOOM - 1), y: -dy * (ZOOM - 1) });
      });
    },
    [clamp],
  );

  /**
   * Did this land on the picture, or on the ground around it?
   *
   * The frame fills the window so a magnified picture has room to grow, which
   * means it also covers the empty space either side. Dismissing has to go by
   * the picture's own rectangle instead of by which element was hit.
   */
  function onPicture(clientX: number, clientY: number): boolean {
    const r = imgRef.current?.getBoundingClientRect();
    return !!r && clientX >= r.left && clientX <= r.right
      && clientY >= r.top && clientY <= r.bottom;
  }

  // ---- one set of pointer handlers for mouse and touch alike --------------
  function onPointerDown(e: React.PointerEvent) {
    if (e.button && e.button !== 0) return;
    try {
      // keeps the drag alive when the pointer leaves the image mid-pan; some
      // browsers throw on an id they no longer consider active, and losing
      // capture is far better than losing the gesture
      (e.target as Element).setPointerCapture?.(e.pointerId);
    } catch {
      /* not fatal — the move handler works without capture */
    }
    drag.current = { id: e.pointerId, x: e.clientX, y: e.clientY, from: view, moved: false };

    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size === 2) {
      // A pinch is not a drag that happens to have two fingers in it. Whatever
      // the first finger had started — a pan, a swipe toward the next picture
      // — is abandoned here, so the picture does not page out from under a
      // gesture that was asking it to grow.
      const [a, b] = [...pointers.current.values()];
      drag.current = null;
      pinch.current = { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, from: view };
      pinched.current = true;
      setAnimated(false);
      setSwipe(0);
    }
  }

  function onPointerMove(e: React.PointerEvent) {
    if (pointers.current.has(e.pointerId)) {
      pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    }

    const p = pinch.current;
    if (p && pointers.current.size >= 2) {
      e.preventDefault();
      const [a, b] = [...pointers.current.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      const next = Math.min(Math.max((p.from.scale * dist) / p.dist, 1), ZOOM);
      const frame = frameRef.current;
      if (!frame) {
        setView((v) => clamp({ ...v, scale: next }));
        return;
      }
      // Keep whatever is between the fingers between the fingers. Measured
      // from the frame's centre because that is where a scale of 1 puts the
      // picture; moving both hands at once pans as well, which is what a hand
      // expects of a pinch.
      const r = frame.getBoundingClientRect();
      const cx = (a.x + b.x) / 2 - (r.left + r.width / 2);
      const cy = (a.y + b.y) / 2 - (r.top + r.height / 2);
      const k = next / p.from.scale;
      setView(
        clamp({ scale: next, x: cx - (cx - p.from.x) * k, y: cy - (cy - p.from.y) * k }),
      );
      return;
    }

    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) > TAP_SLOP) d.moved = true;
    if (!d.moved) return;
    e.preventDefault();
    if (d.from.scale > 1) {
      // zoomed: the drag pans, and that is the whole of it — this is why a
      // magnified picture does not page sideways
      setView(clamp({ scale: d.from.scale, x: d.from.x + dx, y: d.from.y + dy }));
      return;
    }
    if (e.pointerType === "touch" && images.length > 1 && !sliding.current) {
      setAnimated(false);
      // At either end the picture still moves, but only a quarter as far, and
      // it will spring back. Refusing to move at all reads as a dead screen;
      // moving freely promises a picture that is not there. Resistance is the
      // one answer that says "this is the end of the set" while you are still
      // pushing.
      const toward = dx < 0 ? index + 1 : index - 1;
      const blocked = toward < 0 || toward >= images.length;
      setSwipe(
        blocked
          ? Math.sign(dx) * Math.min(Math.abs(dx) * END_RESISTANCE, END_MAX)
          : dx,
      );
    }
  }

  function onPointerUp(e: React.PointerEvent) {
    pointers.current.delete(e.pointerId);
    if (pinch.current) {
      if (pointers.current.size < 2) {
        pinch.current = null;
        setAnimated(true);
        // pinched back to nothing, or nearly: sit down properly at the
        // natural size rather than a hair above it, where paging would still
        // be refused for no reason anyone could see
        setView((v) => (v.scale <= NATURAL ? RESET : clamp(v)));
      }
      drag.current = null;
      setSwipe(0);
      return;
    }
    const wasPinch = pinched.current;
    pinched.current = pointers.current.size > 0 && pinched.current;
    const d = drag.current;
    drag.current = null;
    if (wasPinch) {
      // the last finger of a pinch coming up — not a tap, and certainly not
      // half of a double one
      setAnimated(true);
      setSwipe(0);
      return;
    }
    if (!d || d.id !== e.pointerId) {
      setAnimated(true);
      setSwipe(0);
      return;
    }

    if (d.moved) {
      // An unzoomed sideways drag on a touch screen asks for the next picture.
      // Zoomed, the same movement is a pan and was already spent as one.
      if (d.from.scale <= 1 && e.pointerType === "touch" && images.length > 1) {
        const dx = e.clientX - d.x;
        const dy = e.clientY - d.y;
        const far = Math.abs(dx) > SWIPE_MIN && Math.abs(dx) > Math.abs(dy) * SWIPE_RATIO;
        // only leave it where it is if something is about to move it
        if (far && go(dx < 0 ? index + 1 : index - 1)) return;
      }
      // nothing took it — a pan, a swipe that fell short, or a swipe at the end
      // of the set. Whichever, the picture is off-centre and has to come back.
      setAnimated(true);
      setSwipe(0);
      return; // a pan or a swipe, either way not a tap
    }

    if (!onPicture(e.clientX, e.clientY)) {
      onClose();  // the ground around the picture, whatever the input
      return;
    }

    if (e.pointerType === "touch") {
      // touch: double-tap magnifies, a single tap leaves
      const now = Date.now();
      if (now - lastTap.current < DOUBLE_TAP_MS) {
        lastTap.current = 0;
        if (tapTimer.current !== null) window.clearTimeout(tapTimer.current);
        tapTimer.current = null;
        toggleZoomAt(e.clientX, e.clientY);
        return;
      }
      lastTap.current = now;
      // hold the close back long enough to see whether a second tap follows
      tapTimer.current = window.setTimeout(onClose, DOUBLE_TAP_MS);
      return;
    }
    // mouse: the picture magnifies, the ground around it dismisses
    toggleZoomAt(e.clientX, e.clientY);
  }

  if (!img) return null;
  const filename = img.url.split("/").pop() ?? "image.png";

  /**
   * Sending the picture down to the composer as a reference.
   *
   * Which ones have gone already is remembered for exactly as long as this is
   * open, because that is the span over which a second press is a slip rather
   * than a wish: the composer is behind the picture and cannot be seen. Close
   * it and the count starts over — out there the reference may well have been
   * taken off again, and asking for it a second time is then a real request.
   */
  const isAdded = added.has(img.id);
  async function addAsReference() {
    if (isAdded || sending) return;
    setSending(true);
    try {
      const blob = await fetch(img.url).then((r) => r.blob());
      const file = new File([blob], filename, {
        type: blob.type || "image/png",
      });
      composer?.attach([file]);
      setAdded((prev) => new Set(prev).add(img.id));
    } catch {
      // nothing reaches the composer, and the button goes on offering
    } finally {
      setSending(false);
    }
  }

  return (
    <div className={`lightbox ${zoomed ? "zoomed" : ""}`}>
      <div
        className="lightbox-frame"
        ref={frameRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={(e) => {
          // a finger the browser took back counts as lifted, or the next
          // gesture starts believing it is still down
          pointers.current.delete(e.pointerId);
          drag.current = null;
          if (pointers.current.size < 2) pinch.current = null;
          if (pointers.current.size === 0) pinched.current = false;
        }}
      >
        <img
          ref={imgRef}
          src={shown}
          alt=""
          draggable={false}
          className={animated ? "" : "dragging"}
          onLoad={() => setReadyUrl(shown)}
          style={{
            transform:
              `translate(${view.x + swipe}px, ${view.y}px) scale(${view.scale})`,
            // either copy counts as ready: the swap from small to full must
            // not blink, and by then there is already a picture on screen
            opacity:
              fading || (readyUrl !== img.thumb_url && readyUrl !== img.url) ? 0 : 1,
          }}
        />
      </div>

      {/* touch only: `swipe` is never set for a mouse */}
      <div className={`lightbox-edge prev ${armed === "prev" ? "on" : ""}`} aria-hidden />
      <div className={`lightbox-edge next ${armed === "next" ? "on" : ""}`} aria-hidden />

      <button className="lightbox-close" aria-label={t.close} onClick={onClose}>
        <CloseIcon size={18} />
      </button>
      {/* Pinned to the very edges of the window rather than beside the
          picture: magnified, the picture fills the frame, and an arrow sitting
          on top of it covers the thing you zoomed in to look at. */}
      {index > 0 && (
        <button className="lightbox-nav prev" aria-label={t.prev} onClick={() => go(index - 1)}>
          <ChevronLeftIcon size={26} />
        </button>
      )}
      {index < images.length - 1 && (
        <button className="lightbox-nav next" aria-label={t.next} onClick={() => go(index + 1)}>
          <ChevronRightIcon size={26} />
        </button>
      )}
      <div className="lightbox-bar">
        <span>
          {index + 1} / {images.length}
        </span>
        {img.width && img.height ? (
          <span>
            {img.width}×{img.height}
          </span>
        ) : null}
        {img.seed !== null && <span>seed {img.seed}</span>}
        {/* Here and not under the picture in the transcript: which picture a
            turn is "about" is decided while looking at it closely, and the
            transcript stays free of controls. */}
        {onSetMain && isMain && (
          <button
            className={`lightbox-main${isMain(img) ? " on" : ""}`}
            title={isMain(img) ? t.mainPicture : t.setMain}
            aria-label={isMain(img) ? t.mainPicture : t.setMain}
            aria-pressed={isMain(img)}
            disabled={isMain(img)}
            onClick={() => onSetMain(img)}
          >
            <StarIcon size={16} />
          </button>
        )}
        <a href={img.url} download={filename} title={t.downloadOriginal}>
          <DownloadIcon size={16} />
        </a>
        <button
          className={`lightbox-add${isAdded ? " on" : ""}`}
          aria-label={isAdded ? t.addedToReferences : t.addToReferences}
          title={isAdded ? t.addedToReferences : t.addToReferences}
          onClick={addAsReference}
        >
          {isAdded ? <CheckIcon size={16} /> : <PaperclipIcon size={16} />}
        </button>
      </div>
    </div>
  );
}
