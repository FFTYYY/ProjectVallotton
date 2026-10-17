import { describe, expect, it } from "vitest";

import type { Message } from "../../src/api/types";

import { phaseLabel, reflectStage } from "../../src/lib/phase";

const at = (phase: string) => phaseLabel({ phase } as Message);

describe("phaseLabel", () => {
  it("names each stage of an ordinary turn", () => {
    expect(at("thinking")).toContain("构思");
    expect(at("prompting")).toContain("斟酌");
    expect(at("lookup:3")).toBe("瓦洛东小姐正在翻阅资料…");
    expect(at("painting:2/4")).toContain("2/4");
    expect(at("cancelling")).toContain("停止");
  });

  it("says the prompt is being compressed, which only happens over the limit", () => {
    expect(at("compressing")).toContain("压缩");
    expect(at("compressing")).not.toContain("检查");
  });

  it("says a retry is a retry rather than repeating the soothing line", () => {
    expect(at("retry:thinking")).toContain("重试");
    expect(at("retry:prompting")).toContain("重试");
    expect(at("retry:compressing")).toContain("重试");
    expect(at("retry:compressing")).toContain("压缩");
  });

  it("reads a reflection round as one thing with one number", () => {
    // "reflecting:1/2" — the round is the fraction worth showing; a paint
    // counter nested inside it reads as arithmetic
    expect(at("reflecting:1/2")).toBe("瓦洛东小姐正在端详这张画1/2…");
    expect(at("reflecting:1/2:prompting")).toBe("瓦洛东小姐在斟酌怎么改1/2…");
    expect(at("reflecting:2/2:painting:0/1")).toBe("瓦洛东小姐正在重画2/2…");
    expect(at("reflecting:1/3:compressing")).toBe("瓦洛东小姐在压缩提示词1/3…");
  });

  it("a redraw inside a round is not mistaken for an ordinary paint", () => {
    // the bug this shape exists for: left flat, the phase during a reflection
    // redraw was word-for-word an ordinary paint, and the line stopped
    // mentioning reflection halfway through every round
    expect(at("reflecting:2/5:painting:0/1")).not.toBe(at("painting:0/1"));
    expect(at("reflecting:2/5:painting:0/1")).toContain("2/5");
  });

  it("a retry inside a round still says which round", () => {
    expect(at("reflecting:3/4:retry:thinking")).toContain("3/4");
    expect(at("reflecting:3/4:retry:compressing")).toContain("3/4");
  });

  it("falls back rather than showing a raw phase string", () => {
    expect(at("")).not.toContain(":");
    expect(at("something-new")).not.toContain("something-new");
  });
});

describe("reflectStage", () => {
  it("maps every inner phase of a round to her own words", () => {
    expect(reflectStage("prompting")).toContain("斟酌");
    expect(reflectStage("compressing")).toContain("压缩");
    expect(reflectStage("lookup:2")).toContain("翻阅");
    expect(reflectStage("retry:compressing")).toContain("走神");
    expect(reflectStage("painting:0/1")).toContain("重画");
    expect(reflectStage("")).toContain("端详");
  });
});
