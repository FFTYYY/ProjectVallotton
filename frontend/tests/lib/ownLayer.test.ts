import { describe, expect, it } from "vitest";

import { ownLayer } from "../../src/lib/library";

/**
 * A chip from the conversation's own layer is coloured apart from the
 * project's. The chips above the composer come from the selection, which
 * names the layer by conversation_id; the chips under a picture come from
 * that turn's receipt, which names it by layer.
 */
describe("ownLayer", () => {
  it("reads the selection's conversation_id", () => {
    expect(ownLayer({ conversation_id: "c1" })).toBe(true);
    expect(ownLayer({ conversation_id: null })).toBe(false);
  });

  it("reads a receipt's layer, and an old receipt without one as the project's", () => {
    expect(ownLayer({ layer: "conversation" })).toBe(true);
    expect(ownLayer({ layer: "project" })).toBe(false);
    expect(ownLayer({})).toBe(false);
  });
});
