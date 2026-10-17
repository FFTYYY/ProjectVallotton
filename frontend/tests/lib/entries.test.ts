import { describe, expect, it } from "vitest";

import {
  displayKey,
  isEdit,
  joinTags,
  originOf,
  sameTags,
  splitTags,
  tagList,
  variantTitle,
} from "../../src/lib/entries";
import { zh } from "../../src/uiText.zh";

/**
 * The small readings of an entry that the library page, the rail and the
 * record under a picture share.
 */

describe("splitTags / joinTags", () => {
  it("splits on commas of either width and on line breaks, trimming each", () => {
    expect(splitTags("1girl, red hair，\nlong  hair ")).toEqual(["1girl", "red hair", "long  hair"]);
  });

  it("drops empty pieces and keeps the same tag once", () => {
    expect(splitTags(",, 1girl, ,1GIRL, 1girl,")).toEqual(["1girl"]);
    expect(splitTags("")).toEqual([]);
  });

  it("joins the way the box shows them, and round-trips", () => {
    const tags = ["1girl", "red hair"];
    expect(joinTags(tags)).toBe("1girl, red hair");
    expect(splitTags(joinTags(tags))).toEqual(tags);
  });

  it("sameTags is order-sensitive and exact", () => {
    expect(sameTags(["a", "b"], ["a", "b"])).toBe(true);
    expect(sameTags(["a", "b"], ["b", "a"])).toBe(false);
    expect(sameTags(["a"], ["a", "b"])).toBe(false);
  });
});

describe("tagList", () => {
  it("reads the lead's tags whether written as a string or a list", () => {
    expect(tagList("a, b")).toEqual(["a", "b"]);
    expect(tagList(["a", " b ", ""])).toEqual(["a", "b"]);
  });

  it("reads nothing out of nothing", () => {
    expect(tagList(undefined)).toEqual([]);
    expect(tagList(null)).toEqual([]);
    expect(tagList(3)).toEqual([]);
  });
});

describe("originOf", () => {
  it("a project entry: no conversation, or a receipt that says project", () => {
    expect(originOf({ conversation_id: null, origin_seq: 4 })).toBe("project");
    expect(originOf({ layer: "project", origin_seq: null })).toBe("project");
    expect(originOf({})).toBe("project");
  });

  it("the conversation's own: the lead wrote it at a message, or a person made it", () => {
    expect(originOf({ conversation_id: "c", origin_seq: 7 })).toBe("lead");
    expect(originOf({ conversation_id: "c", origin_seq: null })).toBe("user");
    expect(originOf({ layer: "conversation", origin_seq: 2 })).toBe("lead");
    expect(originOf({ layer: "conversation" })).toBe("user");
  });
});

describe("displayKey / variantTitle", () => {
  it("a key as a person reads it: no project prefix, a variant joined with the interpunct", () => {
    expect(displayKey("@林")).toBe("林");
    expect(displayKey("林")).toBe("林");
    expect(displayKey("@林/校服")).toBe(zh.variantName("林", "校服"));
    expect(displayKey("林/校服")).toBe("林·校服");
    expect(displayKey("林/校服")).not.toContain("/");
  });

  it("an outfit's title is what follows its character", () => {
    expect(variantTitle("林/校服")).toBe("校服");
    expect(variantTitle("校服")).toBe("校服");
    expect(variantTitle("@林/校服")).toBe("校服");
  });
});

describe("isEdit", () => {
  it("a picture that inherits another is an edit of it", () => {
    const brief = { description: "", expose: [], inherits: "ab3", text: "", size: "" };
    expect(isEdit({ draw_brief: brief })).toBe(true);
    expect(isEdit({ draw_brief: { ...brief, inherits: "" } })).toBe(false);
    expect(isEdit({ draw_brief: null })).toBe(false);
  });
});
