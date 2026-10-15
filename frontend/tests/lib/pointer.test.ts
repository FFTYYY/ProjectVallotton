import { describe, expect, it } from "vitest";

import { tapOnAnchor } from "../../src/lib/pointer";

/**
 * The chips above the composer explain themselves on hover. A phone has no
 * hover, so there a tap on a chip shows the explanation instead of opening
 * the sidebar; with a mouse the click still opens the sidebar and the bubble
 * is left to the pointer.
 */
describe("tapOnAnchor", () => {
  it("on a touch screen the tap shows the details instead of reaching the chip", () => {
    expect(tapOnAnchor(true, false)).toEqual({ reaches: false, open: true });
  });

  it("a second tap puts the details away, and still does not reach the chip", () => {
    expect(tapOnAnchor(true, true)).toEqual({ reaches: false, open: false });
  });

  it("with a mouse the click reaches the chip and leaves the bubble to hover", () => {
    expect(tapOnAnchor(false, false)).toEqual({ reaches: true, open: false });
    expect(tapOnAnchor(false, true)).toEqual({ reaches: true, open: true });
  });
});
