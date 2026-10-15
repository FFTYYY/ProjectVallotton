import { beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Which folders are shut, and the fact that it survives a reload.
 *
 * The bug this is about: every folder was open, every time. The state existed
 * and worked within a session; it simply started empty on every load, so a
 * tree someone had tidied came back fully expanded.
 *
 * Tested through a real load of the module rather than by calling the toggle
 * and reading the store back. That would pass without any of this: the point
 * is not that toggling changes something, it is that a NEW copy of the app
 * reads what the old one wrote.
 */

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
    /** for the assertions */
    raw: map,
  };
}

const KEY = "vallotton.collapsed";

/** a fresh app, reading whatever is in storage right now */
async function freshApp(storage: ReturnType<typeof fakeStorage>) {
  vi.stubGlobal("localStorage", storage);
  vi.resetModules();
  const { useProjects } = await import("../../src/store/projects");
  return useProjects;
}

describe("哪些文件夹是收起的", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("默认展开：没有记录过的文件夹是打开的", async () => {
    const store = await freshApp(fakeStorage());
    expect(store.getState().collapsed).toEqual({});
  });

  it("收起之后，新开的一次应用仍然记得", async () => {
    const storage = fakeStorage();
    const first = await freshApp(storage);
    first.getState().toggleCollapse("f1");
    expect(first.getState().collapsed).toEqual({ f1: true });

    // 同一份 storage，全新的模块实例 —— 这是刷新
    const second = await freshApp(storage);
    expect(second.getState().collapsed).toEqual({ f1: true });
  });

  it("再打开就不再记着它，而不是记一个 false", async () => {
    const storage = fakeStorage();
    const store = await freshApp(storage);
    store.getState().toggleCollapse("f1");
    store.getState().toggleCollapse("f1");

    expect(store.getState().collapsed).toEqual({});
    // 记 false 会让这份记录只增不减，而它说的事情「不在里面」已经说了
    expect(JSON.parse(storage.raw.get(KEY) ?? "{}")).toEqual({});
  });

  it("旧记录里的 false 不会被当成收起", async () => {
    const storage = fakeStorage({ [KEY]: JSON.stringify({ a: true, b: false }) });
    const store = await freshApp(storage);
    expect(store.getState().collapsed).toEqual({ a: true });
  });

  it("存不了就算了，不该让页面打不开", async () => {
    const angry = {
      ...fakeStorage(),
      setItem: () => {
        throw new Error("quota");
      },
    } as unknown as ReturnType<typeof fakeStorage>;
    const store = await freshApp(angry);
    expect(() => store.getState().toggleCollapse("f1")).not.toThrow();
    expect(store.getState().collapsed).toEqual({ f1: true });
  });

  it("读不了就当作全部展开", async () => {
    const broken = {
      ...fakeStorage(),
      getItem: () => {
        throw new Error("blocked in private mode");
      },
    } as unknown as ReturnType<typeof fakeStorage>;
    const store = await freshApp(broken);
    expect(store.getState().collapsed).toEqual({});
  });
});
