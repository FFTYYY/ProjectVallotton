import { describe, expect, it } from "vitest";

import { Latest } from "../../src/lib/latest";

describe("Latest", () => {
  it("lets the newest ticket through", () => {
    const l = new Latest();
    const t = l.claim("a");
    expect(l.holds("a", t)).toBe(true);
  });

  it("turns away an answer to a question asked before something changed", () => {
    const l = new Latest();
    const read = l.claim("a");
    l.claim("a"); // a write, or a newer read
    expect(l.holds("a", read)).toBe(false);
  });

  it("keeps keys apart", () => {
    const l = new Latest();
    const a = l.claim("a");
    l.claim("b");
    expect(l.holds("a", a)).toBe(true);
  });

  it("invalidates everything outstanding when a key is dropped", () => {
    const l = new Latest();
    const t = l.claim("a");
    l.drop("a");
    expect(l.holds("a", t)).toBe(false);
  });
});
