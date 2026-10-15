import { describe, expect, it } from "vitest";

import { lookupsText } from "../../src/lib/lookups";

describe("lookupsText", () => {
  it("is empty when nothing was looked up", () => {
    expect(lookupsText([])).toBe("");
    expect(lookupsText(null)).toBe("");
  });

  it("keeps the answer's own lines rather than escaping them", () => {
    const text = lookupsText([
      { queries: ["cat", "hair"], result: "标签\t图片数\t分类\ncat\t300000\tgeneral" },
    ]);
    expect(text).toContain("查：cat，hair");
    expect(text).toContain("cat\t300000\tgeneral");
    expect(text).not.toContain("\\n");
  });

  it("separates one lookup from the next", () => {
    const text = lookupsText([
      { queries: ["a"], result: "A" },
      { queries: ["b"], result: "B" },
    ]);
    expect(text.split("\n\n")).toHaveLength(2);
  });
});
