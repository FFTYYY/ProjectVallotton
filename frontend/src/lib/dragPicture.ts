import type { Attachment } from "../api/types";

/**
 * Dragging a picture out of the app hands over the picture, not the preview.
 *
 * The transcript draws a small copy first, so the `<img>` on screen is often
 * showing the thumbnail — and a drag left to itself carries whatever `src`
 * happens to hold at that moment. Dropping into a folder saved a 150 KB JPEG
 * of a 3.6 MB PNG, and which one you got depended on how long you had been
 * looking at the page.
 *
 * So the drag says what it is carrying rather than letting the element decide:
 *
 *   * `DownloadURL` is what makes a drop onto the desktop or a file manager
 *     fetch the file and save it under a real name. Chromium only, and it
 *     wants exactly `mime:filename:url` with an absolute url;
 *   * `text/uri-list` is the standard one, for anything that takes a link;
 *   * `text/plain` last, for the places that only read text.
 *
 * All three name the full picture, so it no longer matters which copy is on
 * screen when the drag begins.
 */
export function dragPicture(att: Attachment) {
  return (e: React.DragEvent<HTMLImageElement>) => {
    const url = new URL(att.url, window.location.origin).href;
    const name = decodeURIComponent(att.url.split("/").pop() || "") || "image.png";
    const dt = e.dataTransfer;
    if (!dt) return;
    try {
      dt.setData("DownloadURL", `${att.mime || "image/png"}:${name}:${url}`);
    } catch {
      /* not Chromium — the two below still work */
    }
    dt.setData("text/uri-list", url);
    dt.setData("text/plain", url);
    dt.effectAllowed = "copy";
  };
}
