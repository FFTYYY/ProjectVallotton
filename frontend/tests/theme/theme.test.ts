import fs from "node:fs";
import path from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_THEME, THEMES, THEME_STORAGE_KEY, isTheme, nextTheme, themeOf } from "../../src/theme/theme";
import { zh } from "../../src/uiText.zh";

/**
 * The colours the app wears: the choice is this browser's, dark is the
 * default, the stored choice is painted on <html> as data-theme, and the
 * inline script in index.html paints it again before the first frame.
 */

const ROOT = path.resolve(__dirname, "../..");

describe("配色注册表", () => {
  it("id 不重复，默认是深色，每个配色都有名字", () => {
    const ids = THEMES.map((th) => th.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(DEFAULT_THEME).toBe("dark");
    expect(ids).toContain(DEFAULT_THEME);
    for (const th of THEMES) {
      expect(th.barColor).toMatch(/^#[0-9a-f]{6}$/);
      expect(zh.themeNames[th.id]).toBeTruthy();
    }
    expect(Object.keys(zh.themeNames).sort()).toEqual([...ids].sort());
  });

  it("isTheme / nextTheme / themeOf", () => {
    expect(isTheme("dark")).toBe(true);
    expect(isTheme("sepia")).toBe(false);
    expect(isTheme(null)).toBe(false);
    expect(nextTheme("light").id).toBe("dark");
    expect(nextTheme(THEMES[THEMES.length - 1].id).id).toBe(THEMES[0].id);
    expect(themeOf("dark").id).toBe("dark");
  });

  it("tokens.css 给每个配色都有一块 token：浅色就是 :root 本身，其余各有一块 data-theme", () => {
    const css = fs.readFileSync(path.join(ROOT, "src/styles/tokens.css"), "utf8");
    for (const th of THEMES) {
      if (th.id === "light") continue;
      expect(css).toContain(`:root[data-theme="${th.id}"]`);
    }
  });

  it("index.html 在第一帧前涂上存着的配色，用的是注册表的键和默认值", () => {
    const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
    expect(html).toContain(`localStorage.getItem("${THEME_STORAGE_KEY}")`);
    expect(html).toContain(`var theme = "${DEFAULT_THEME}";`);
    expect(html).toContain(`<meta name="theme-color" content="${themeOf(DEFAULT_THEME).barColor}" />`);
    const vite = fs.readFileSync(path.join(ROOT, "vite.config.ts"), "utf8");
    expect(vite).toContain(`theme_color: "${themeOf(DEFAULT_THEME).barColor}"`);
  });
});

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
  const attrs = new Map<string, string>();
  const meta = { content: "" };
  return {
    attrs,
    meta,
    documentElement: {
      setAttribute: (k: string, v: string) => void attrs.set(k, v),
      removeAttribute: (k: string) => void attrs.delete(k),
    },
    querySelector: (sel: string) => (sel.includes("theme-color") ? meta : null),
  };
}

async function freshApp(storage: ReturnType<typeof fakeStorage>, doc = fakeDocument()) {
  vi.stubGlobal("localStorage", storage);
  vi.stubGlobal("document", doc);
  vi.resetModules();
  const { useTheme } = await import("../../src/theme/theme");
  return { useTheme, doc };
}

describe("浏览器记着的配色", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("默认深色，不看系统偏好；<html> 上总是写着当前配色", async () => {
    const { useTheme, doc } = await freshApp(fakeStorage());
    expect(useTheme.getState().theme).toBe("dark");
    expect(doc.attrs.get("data-theme")).toBe("dark");
    expect(doc.meta.content).toBe(themeOf("dark").barColor);
  });

  it("存着浅色就开成浅色", async () => {
    const { useTheme, doc } = await freshApp(fakeStorage({ [THEME_STORAGE_KEY]: "light" }));
    expect(useTheme.getState().theme).toBe("light");
    expect(doc.attrs.get("data-theme")).toBe("light");
    expect(doc.meta.content).toBe(themeOf("light").barColor);
  });

  it("存了个不认识的就当默认", async () => {
    const { useTheme, doc } = await freshApp(fakeStorage({ [THEME_STORAGE_KEY]: "sepia" }));
    expect(useTheme.getState().theme).toBe("dark");
    expect(doc.attrs.get("data-theme")).toBe("dark");
  });

  it("切换：涂上、记下来；再来一次新的应用还记得", async () => {
    const storage = fakeStorage();
    const first = await freshApp(storage);
    first.useTheme.getState().toggle();
    expect(first.useTheme.getState().theme).toBe("light");
    expect(first.doc.attrs.get("data-theme")).toBe("light");
    expect(storage.raw.get(THEME_STORAGE_KEY)).toBe("light");

    const second = await freshApp(storage);
    expect(second.useTheme.getState().theme).toBe("light");
    second.useTheme.getState().set("dark");
    expect(second.doc.attrs.get("data-theme")).toBe("dark");
    expect(storage.raw.get(THEME_STORAGE_KEY)).toBe("dark");
    second.useTheme.getState().set("sepia" as never);
    expect(second.useTheme.getState().theme).toBe("dark");
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
    const { useTheme } = await freshApp(angry);
    expect(() => useTheme.getState().toggle()).not.toThrow();
    expect(useTheme.getState().theme).toBe("light");
  });
});
