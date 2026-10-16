import fs from "node:fs";
import path from "node:path";

import { beforeEach, describe, expect, it } from "vitest";

import { useLanguage } from "../../src/i18n/language";
import { LANGUAGES } from "../../src/i18n/languages";
import { TABLES, inEveryLanguage, t } from "../../src/uiText";
import { en } from "../../src/uiText.en";
import { zh } from "../../src/uiText.zh";

/**
 * The words the browser shows, held to two things.
 *
 * The Chinese table is held to tests/fixtures/uiText.zh.json: every string
 * and list by value, every function by name and arity. The fixture was
 * written from the table the day the English one was added (2026-10-16), so
 * adding a language did not move a single Chinese word. Change one on
 * purpose and change the fixture with it.
 *
 * Every other table is held to the Chinese one: the same keys, the same
 * shapes, the same arities, and not a Chinese character in it.
 */

const CJK = /[⺀-鿿豈-﫿＀-￯　-〿]/;

type Table = Record<string, unknown>;

/** a table as the fixture records it */
function pin(table: Table): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(table).sort()) {
    const v = table[key];
    out[key] = typeof v === "function" ? { fn: v.length } : v;
  }
  return out;
}

function strings(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(strings);
  if (value && typeof value === "object") return Object.values(value).flatMap(strings);
  return [];
}

describe("中文文案表", () => {
  it("和加英文那天钉下的快照一字不差", () => {
    const fixture = JSON.parse(
      fs.readFileSync(path.resolve(__dirname, "../fixtures/uiText.zh.json"), "utf8"),
    );
    expect(pin(zh as Table)).toEqual(fixture);
  });
});

describe("每种语言的文案表", () => {
  const zhKeys = Object.keys(zh).sort();

  it("注册表里的每种语言都有一张表", () => {
    expect(Object.keys(TABLES).sort()).toEqual(LANGUAGES.map((l) => l.code).sort());
  });

  for (const [code, table] of Object.entries(TABLES)) {
    it(`${code}：和中文表同键、同形、同参数个数`, () => {
      expect(Object.keys(table).sort()).toEqual(zhKeys);
      for (const key of zhKeys) {
        const a = (zh as Table)[key];
        const b = (table as Table)[key];
        expect(typeof b, key).toBe(typeof a);
        if (typeof a === "function") {
          expect((b as Function).length, key).toBe(a.length);
        } else if (Array.isArray(a)) {
          expect(Array.isArray(b), key).toBe(true);
          expect((b as unknown[]).length, key).toBe(a.length);
        } else if (a && typeof a === "object") {
          expect(Object.keys(b as object).sort(), key).toEqual(Object.keys(a).sort());
        }
      }
    });
  }

  it("英文表里没有一个汉字，也没有照抄中文", () => {
    for (const key of zhKeys) {
      const a = (zh as Table)[key];
      const b = (en as Table)[key];
      for (const s of strings(b)) expect(s, key).not.toMatch(CJK);
      if (typeof a !== "function" && strings(a).some((s) => CJK.test(s))) {
        expect(b, key).not.toEqual(a);
      }
    }
  });

  it("英文表里函数拼出来的句子也没有汉字", () => {
    const samples: Record<string, unknown[]> = {};
    for (const key of zhKeys) {
      const fn = (en as Table)[key];
      if (typeof fn !== "function") continue;
      const args = Array.from({ length: fn.length }, (_, i) => (i === 0 ? 3 : "x"));
      // every function takes numbers or strings; try both shapes
      for (const attempt of [args, args.map(() => "x"), args.map(() => 3)]) {
        try {
          const out = (fn as (...a: unknown[]) => unknown)(...attempt);
          samples[key] = [out];
          expect(out, key).not.toMatch(CJK);
        } catch {
          /* a shape the function does not take */
        }
      }
    }
    expect(Object.keys(samples).length).toBeGreaterThan(40);
  });
});

describe("t：按当前语言取词", () => {
  beforeEach(() => useLanguage.getState().adopt("zh"));

  it("默认中文，切到英文就是英文表的词", () => {
    expect(t.logout).toBe(zh.logout);
    useLanguage.getState().adopt("en");
    expect(t.logout).toBe(en.logout);
    expect(t.newConversationIn("x")).toBe(en.newConversationIn("x"));
    expect(t.themeNames.dark).toBe(en.themeNames.dark);
    useLanguage.getState().adopt("zh");
    expect(t.logout).toBe(zh.logout);
  });

  it("不认识的语言码不会把表切走", () => {
    useLanguage.getState().adopt("klingon");
    expect(t.logout).toBe(zh.logout);
  });

  it("像一张普通的表：能列键，能 in", () => {
    expect(Object.keys(t).sort()).toEqual(Object.keys(zh).sort());
    expect("logout" in t).toBe(true);
    expect("nope" in t).toBe(false);
  });

  it("inEveryLanguage 给出每种语言的说法", () => {
    const titles = inEveryLanguage("newConversation");
    expect(titles).toContain(zh.newConversation);
    expect(titles).toContain(en.newConversation);
    expect(titles.length).toBe(LANGUAGES.length);
  });

  it("token 数的缩写按各自语言的单位", () => {
    expect(zh.tokensShort(123456)).toBe("12.3万");
    expect(zh.tokensShort(9999)).toBe("9999");
    expect(en.tokensShort(123456)).toBe("123.5k");
    expect(en.tokensShort(1_000_000)).toBe("1.0M");
    expect(en.tokensShort(999)).toBe("999");
  });
});
