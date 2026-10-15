/**
 * Archiving, asked about the same way everywhere.
 *
 * Putting something away is reversible but not nothing — a folder takes its
 * whole subtree with it — so every entry point confirms first, and they all
 * confirm with the same words. Bringing something back asks nothing: it is
 * the undo.
 */

import { confirm } from "../store/confirm";
import { t } from "../uiText";

export async function confirmArchive(
  kind: "folder" | "conversation",
  name: string,
): Promise<boolean> {
  return confirm({
    title: kind === "folder" ? t.archiveFolderTitle(name) : t.archiveConversationTitle(name),
    body:
      kind === "folder"
        ? t.archiveFolderBody
        : t.archiveConversationBody,
    confirmLabel: t.archive,
  });
}
