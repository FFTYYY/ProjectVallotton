import { describe, expect, it } from "vitest";

import { en } from "../../src/uiText.en";

/**
 * English counts agree with their number: one variant, two variants. The
 * Chinese table has no plurals and is not concerned.
 */
describe("English counts", () => {
  it("says one in the singular and more in the plural", () => {
    expect(en.variantCount(1)).toBe("1 variant");
    expect(en.variantCount(2)).toBe("2 variants");
    expect(en.variantCountShort(1)).toBe("1 variant");
    expect(en.turnsCount(1)).toBe("1 turn");
    expect(en.chars(1)).toBe("1 character");
  });

  it("counts both halves of the reflection line on their own", () => {
    expect(en.reflectionSummary(1, 0)).toBe("Reflection 1 round · redrawn 0 times");
    expect(en.reflectionSummary(2, 1)).toBe("Reflection 2 rounds · redrawn 1 time");
    expect(en.reflectionRoundsTitle(1)).toBe("Reflection · 1 round");
  });

  it("keeps the irregular plurals and the verb in step", () => {
    expect(en.retriesSuffix(1)).toBe(" · 1 retry");
    expect(en.retriesSuffix(3)).toBe(" · 3 retries");
    expect(en.unknownTags(1)).toBe("1 tag is not in the Danbooru tag table: ");
    expect(en.unknownTags(2)).toBe("2 tags are not in the Danbooru tag table: ");
    expect(en.deleteCharacterBody(1)).toBe("Its variant goes with it.");
    expect(en.deleteCharacterBody(3)).toBe("Its 3 variants go with it.");
  });
});
