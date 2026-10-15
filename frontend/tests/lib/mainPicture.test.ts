import { describe, expect, it } from "vitest";

import type { Attachment } from "../../src/api/types";
import { mainFirst, mainPosition } from "../../src/lib/mainPicture";

function pic(position: number): Attachment {
  return {
    id: `a${position}`,
    message_id: "m",
    kind: "generated",
    url: "",
    thumb_url: "",
    width: 1,
    height: 1,
    seed: position,
    position,
  } as unknown as Attachment;
}

describe("mainPosition", () => {
  it("is the named position when that picture exists", () => {
    expect(mainPosition([pic(0), pic(1)], 1)).toBe(1);
  });

  it("falls back to the first picture left when the named one is gone", () => {
    // a reflection round discarded positions 0 and 1 and kept counting
    expect(mainPosition([pic(2), pic(3)], 0)).toBe(2);
  });

  it("is null without pictures", () => {
    expect(mainPosition([], 0)).toBeNull();
  });
});

describe("mainFirst", () => {
  it("puts the main picture first and keeps the rest in order", () => {
    expect(mainFirst([pic(0), pic(1), pic(2)], 1).map((a) => a.position)).toEqual([1, 0, 2]);
  });

  it("keeps the made order when the named position is gone", () => {
    expect(mainFirst([pic(3), pic(2)], 0).map((a) => a.position)).toEqual([2, 3]);
  });
});
