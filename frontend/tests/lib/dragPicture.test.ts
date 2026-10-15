import { beforeAll, describe, expect, it, vi } from "vitest";

import type { Attachment } from "../../src/api/types";

import { dragPicture } from "../../src/lib/dragPicture";

/**
 * What leaves the app when a picture is dragged out of it.
 *
 * The rule is one sentence — it is always the full picture — and the reason it
 * needs a test is that the wrong behaviour looked fine: the transcript shows a
 * thumbnail first and swaps the real picture in behind it, so a drag carried
 * whichever copy happened to be on screen. Dropping the same image into a
 * folder twice could produce two different files, and which one you got
 * depended on how long you had been looking at the page.
 */

const att = (over: Partial<Attachment> = {}): Attachment =>
  ({
    id: "a1",
    kind: "generated",
    url: "/api/media/a1/a1-%E7%81%AF%E5%A1%94.png",
    thumb_url: "/api/media/a1/thumb",
    mime: "image/png",
    width: 1024,
    height: 1536,
    size_bytes: 3_600_000,
    seed: 7,
    position: 0,
    ...over,
  }) as Attachment;

// node has no window; the absolute url is the thing being asserted, so this
// stands in for one rather than the code being made to work without it
beforeAll(() => {
  vi.stubGlobal("window", { location: { origin: "https://example.test" } });
});

function drag(a: Attachment) {
  const written = new Map<string, string>();
  const e = {
    dataTransfer: {
      setData: (k: string, v: string) => void written.set(k, v),
      effectAllowed: "",
    },
  } as unknown as React.DragEvent<HTMLImageElement>;
  dragPicture(a)(e);
  return written;
}

describe("拖出去的是哪一份", () => {
  it("三种写法都指向原图，不是缩略图", () => {
    const w = drag(att());
    for (const [kind, value] of w) {
      expect(value, kind).toContain("/api/media/a1/a1-");
      expect(value, kind).not.toContain("/thumb");
    }
  });

  it("DownloadURL 的格式是 mime:文件名:绝对地址", () => {
    const [mime, name, ...rest] = (drag(att()).get("DownloadURL") ?? "").split(":");
    expect(mime).toBe("image/png");
    expect(name).toBe("a1-灯塔.png");        // 从 URL 里解码回来的真名
    expect(rest.join(":")).toMatch(/^https?:\/\/.+\/api\/media\/a1\//);
  });

  it("地址是绝对的，否则放到别的应用里没有意义", () => {
    expect(drag(att()).get("text/uri-list")).toMatch(/^https?:\/\//);
  });

  it("没有文件名也能拖", () => {
    const [, name] = (drag(att({ url: "/api/media/a1/" })).get("DownloadURL") ?? "").split(
      ":",
    );
    expect(name).toBe("image.png");
  });

  it("没有 mime 就当作 png", () => {
    const [mime] = (drag(att({ mime: "" })).get("DownloadURL") ?? "").split(":");
    expect(mime).toBe("image/png");
  });
});
