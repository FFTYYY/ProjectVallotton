import { useCallback, useRef } from "react";

/**
 * On a phone, a slider moves only when a finger travels.
 *
 * A native range input jumps to wherever it was touched. With a mouse that is
 * the whole point — you aim at a spot and land on it. With a thumb it is a
 * liability: these two sliders sit in a scrolling sidebar, they are narrow,
 * and one stray tap while reaching for something else silently arms five
 * reflection rounds or ten pictures. The setting is invisible once set, so a
 * mis-tap is not something you notice until it costs you.
 *
 * So touch gets a different contract: nothing happens until the finger has
 * moved SLOP pixels, and from then on the value follows it.
 *
 * **The handlers go on the wrapper, not on the input**, and CSS gives the
 * input `pointer-events: none` under `(pointer: coarse)`. Cancelling the
 * input's own pointerdown is not enough — on a touch screen Chrome drives the
 * native thumb from the touch events directly, and the tap still landed. The
 * only reliable answer is for the control not to see the gesture at all.
 *
 * Mouse and keyboard are untouched: at `(pointer: fine)` the input keeps its
 * pointer events and its native behaviour, and everything here returns early
 * for any pointerType that is not touch.
 */

/** How far a finger travels before this is a drag rather than a tap. */
const SLOP = 8;

type Range = {
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
};

export function useDragOnlyOnTouch({ min, max, step, onChange }: Range) {
  const drag = useRef<{ startX: number; moving: boolean } | null>(null);

  const valueAt = useCallback(
    (input: HTMLInputElement, clientX: number) => {
      const box = input.getBoundingClientRect();
      if (box.width <= 0) return min;
      const along = Math.min(1, Math.max(0, (clientX - box.left) / box.width));
      const snapped = Math.round((min + along * (max - min)) / step) * step;
      return Math.min(max, Math.max(min, snapped));
    },
    [min, max, step],
  );

  const input = (el: HTMLElement) =>
    el.querySelector<HTMLInputElement>("input.slider");

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (e.pointerType !== "touch") return;
    const el = input(e.currentTarget);
    if (!el || el.disabled) return;
    e.preventDefault();
    drag.current = { startX: e.clientX, moving: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      const state = drag.current;
      if (!state || e.pointerType !== "touch") return;
      if (!state.moving && Math.abs(e.clientX - state.startX) < SLOP) return;
      const el = input(e.currentTarget);
      if (!el) return;
      state.moving = true;
      onChange(valueAt(el, e.clientX));
    },
    [onChange, valueAt],
  );

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (drag.current && e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    drag.current = null;
  }, []);

  return { onPointerDown, onPointerMove, onPointerUp, onPointerCancel: onPointerUp };
}
