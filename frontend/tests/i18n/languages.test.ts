import fs from "node:fs";
import path from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_LANGUAGE,
  LANGUAGES,
  LANGUAGE_HINT_KEY,
  isLanguage,
  languageOf,
  nextLanguage,
} from "../../src/i18n/languages";

/**
 * The language registry, and the browser's copy of the account's choice.
 *
 * The registry is held to the backend's (backend/core/i18n.py): the same
 * codes in the same order, the same default. A language one side knows and
 * the other does not would be stored by one and refused by the other.
 */

const BACKEND = path.resolve(__dirname, "../../../backend/core/i18n.py");

describe("语言注册表", () => {
  it("语言码不重复，默认语言在表里", () => {
    const codes = LANGUAGES.map((l) => l.code);
    expect(new Set(codes).size).toBe(codes.length);
    expect(codes).toContain(DEFAULT_LANGUAGE);
    for (const l of LANGUAGES) {
      expect(l.label).toBeTruthy();
      expect(l.htmlLang).toBeTruthy();
    }
  });

  it("isLanguage 只认注册表里的码", () => {
    for (const l of LANGUAGES) expect(isLanguage(l.code)).toBe(true);
    expect(isLanguage("xx")).toBe(false);
    expect(isLanguage(null)).toBe(false);
    expect(isLanguage(3)).toBe(false);
  });

  it("nextLanguage 循环走完整张表", () => {
    let at = LANGUAGES[0];
    const seen = [at.code];
    for (let i = 1; i < LANGUAGES.length; i++) {
      at = nextLanguage(at.code);
      seen.push(at.code);
    }
    expect(seen).toEqual(LANGUAGES.map((l) => l.code));
    expect(nextLanguage(at.code).code).toBe(LANGUAGES[0].code);
    expect(languageOf("en").label).toBe("English");
  });

  it("和后端的注册表一致：同样的码、同样的顺序、同样的默认", () => {
    const py = fs.readFileSync(BACKEND, "utf8");
    const backendCodes = [...py.matchAll(/Language\("(\w+)",/g)].map((m) => m[1]);
    expect(backendCodes.length).toBeGreaterThan(0);
    expect(LANGUAGES.map((l) => l.code)).toEqual(backendCodes);
    const backendDefault = /^DEFAULT = "(\w+)"$/m.exec(py)?.[1];
    expect(DEFAULT_LANGUAGE).toBe(backendDefault);
  });
});

/** the smallest thing that behaves like localStorage, since node has none */
function fakeStorage(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
    key: (i: number) => [...map.keys()][i] ?? null,
    get length() {
      return map.size;
    },
    raw: map,
  };
}

function fakeDocument() {
  return { documentElement: { lang: "" } };
}

/** a fresh app, reading whatever is in storage right now */
async function freshApp(storage: ReturnType<typeof fakeStorage>, doc = fakeDocument()) {
  vi.stubGlobal("localStorage", storage);
  vi.stubGlobal("document", doc);
  vi.resetModules();
  const { useLanguage } = await import("../../src/i18n/language");
  return { useLanguage, doc };
}

describe("浏览器记着的语言", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("什么都没听说过的时候说默认语言", async () => {
    const { useLanguage, doc } = await freshApp(fakeStorage());
    expect(useLanguage.getState().language).toBe(DEFAULT_LANGUAGE);
    expect(doc.documentElement.lang).toBe(languageOf(DEFAULT_LANGUAGE).htmlLang);
  });

  it("上次听到的语言在第一帧就说出来", async () => {
    const { useLanguage, doc } = await freshApp(fakeStorage({ [LANGUAGE_HINT_KEY]: "en" }));
    expect(useLanguage.getState().language).toBe("en");
    expect(doc.documentElement.lang).toBe("en");
  });

  it("记错了的东西不算数", async () => {
    const { useLanguage } = await freshApp(fakeStorage({ [LANGUAGE_HINT_KEY]: "klingon" }));
    expect(useLanguage.getState().language).toBe(DEFAULT_LANGUAGE);
  });

  it("adopt：换语言、记下来、改 <html lang>；不认识的码不理", async () => {
    const storage = fakeStorage();
    const { useLanguage, doc } = await freshApp(storage);
    useLanguage.getState().adopt("en");
    expect(useLanguage.getState().language).toBe("en");
    expect(storage.raw.get(LANGUAGE_HINT_KEY)).toBe("en");
    expect(doc.documentElement.lang).toBe("en");
    useLanguage.getState().adopt("xx");
    useLanguage.getState().adopt(undefined);
    expect(useLanguage.getState().language).toBe("en");
  });

  it("存不了就算了，不该让页面打不开", async () => {
    const angry = {
      ...fakeStorage(),
      getItem: () => {
        throw new Error("refused");
      },
      setItem: () => {
        throw new Error("quota");
      },
    } as unknown as ReturnType<typeof fakeStorage>;
    const { useLanguage } = await freshApp(angry);
    expect(useLanguage.getState().language).toBe(DEFAULT_LANGUAGE);
    expect(() => useLanguage.getState().adopt("en")).not.toThrow();
    expect(useLanguage.getState().language).toBe("en");
  });

  it("choose：自己选的语言，服务器还没确认前读回来的旧值不算数", async () => {
    const storage = fakeStorage();
    const { useLanguage } = await freshApp(storage);
    useLanguage.getState().choose("en");
    expect(useLanguage.getState().language).toBe("en");
    expect(useLanguage.getState().pending).toBe(true);
    // the remount asks the server who it is; the server still says zh
    useLanguage.getState().adopt("zh");
    expect(useLanguage.getState().language).toBe("en");
    // the write lands
    useLanguage.getState().settle("en");
    expect(useLanguage.getState().pending).toBe(false);
    expect(useLanguage.getState().language).toBe("en");
    // from now on the server's word counts again
    useLanguage.getState().adopt("zh");
    expect(useLanguage.getState().language).toBe("zh");
  });

  it("settle 拿不到服务器的答案时只是解除挂起", async () => {
    const { useLanguage } = await freshApp(fakeStorage());
    useLanguage.getState().choose("en");
    useLanguage.getState().settle(undefined);
    expect(useLanguage.getState().pending).toBe(false);
    expect(useLanguage.getState().language).toBe("en");
  });

  it("没有 document 也能用（测试、服务端）", async () => {
    vi.stubGlobal("localStorage", fakeStorage());
    vi.stubGlobal("document", undefined);
    vi.resetModules();
    const { useLanguage } = await import("../../src/i18n/language");
    expect(() => useLanguage.getState().adopt("en")).not.toThrow();
  });
});
