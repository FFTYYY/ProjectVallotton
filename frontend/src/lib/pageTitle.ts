import type { Conversation, Folder } from "../api/types";
import { t } from "../uiText";

/**
 * What the bar at the top of a phone says.
 *
 * One line, read left to right: the folders a thing sits in, then the thing.
 * The bar used to carry the last part only, while a line underneath carried
 * the trail — two rows of a small screen spent on one fact.
 */

/** the folders above one folder, outermost first, then the folder itself */
export function folderTrail(folders: Folder[], id: string | null | undefined): string[] {
  const by = new Map(folders.map((f) => [f.id, f]));
  const out: string[] = [];
  let at = id ? by.get(id) : undefined;
  // A folder inside itself cannot be made from the UI, but a trail that walks
  // for ever takes the whole shell with it — count the steps rather than
  // trust the shape.
  for (let step = 0; at && step <= folders.length; step++) {
    out.unshift(at.name);
    at = at.parent_id ? by.get(at.parent_id) : undefined;
  }
  return out;
}

export function titleTrail(args: {
  pathname: string;
  conv: Conversation | undefined;
  folderId: string | undefined;
  folders: Folder[];
  /** the trail the open folder's own page knows, which the tree may not */
  folderPath: string[] | undefined;
}): string[] {
  const { pathname, conv, folderId, folders, folderPath } = args;
  if (pathname.startsWith("/settings")) return [t.settings];
  if (conv) return [...conv.folder_path, conv.title];
  if (folderId) {
    // The page's own answer first: the tree leaves out archived folders, and
    // an archived folder still has a page you can stand on.
    if (folderPath?.length) return folderPath;
    // the tree arrives a moment after the route does, and a bar reading
    // nothing at all looks like a fault
    const trail = folderTrail(folders, folderId);
    if (trail.length) return trail;
  }
  return ["Vallotton"];
}
