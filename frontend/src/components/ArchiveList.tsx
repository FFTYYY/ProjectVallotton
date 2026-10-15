/**
 * 已归档 — everything that has been put away, folders and conversations in
 * one list, newest first.
 *
 * Nothing here is hand-ordered, so nothing here drags: the archive is a
 * record, not a workspace.
 */

import { useEffect } from "react";
import { useNavigate } from "react-router-dom";

import { middleClickOpens } from "../lib/newtab";

import { useChat } from "../store/chat";
import { useProjects } from "../store/projects";
import { ArchiveIcon, ConversationIcon, FolderIcon } from "./Icons";
import { t } from "../uiText";

export function ArchiveList() {
  const archive = useProjects((s) => s.archive);
  const loadArchive = useProjects((s) => s.loadArchive);
  const archiveFolder = useProjects((s) => s.archiveFolder);
  const setArchived = useChat((s) => s.setArchived);
  const navigate = useNavigate();

  useEffect(() => {
    void loadArchive();
  }, [loadArchive]);

  if (!archive) {
    return (
      <div className="conv-list" style={{ textAlign: "center", paddingTop: 24 }}>
        <span className="spinner" />
      </div>
    );
  }

  type Row = {
    id: string;
    kind: "folder" | "conversation";
    name: string;
    when: string;
    path: string[];
  };

  const rows: Row[] = [
    ...archive.folders.map((f) => ({
      id: f.id,
      kind: "folder" as const,
      name: f.name,
      when: f.updated_at ?? "",
      path: (f.path ?? []).slice(0, -1),
    })),
    ...archive.conversations.map((c) => ({
      id: c.id,
      kind: "conversation" as const,
      name: c.title,
      when: c.updated_at,
      path: c.folder_path,
    })),
  ].sort((a, b) => b.when.localeCompare(a.when));

  async function unarchive(row: Row) {
    if (row.kind === "folder") await archiveFolder(row.id, false);
    else {
      await setArchived(row.id, false);
      void loadArchive();
    }
  }

  return (
    <div className="conv-list tree-list">
      {rows.map((row) => (
        <div className="tree-row archived" key={row.id}>
          {row.kind === "folder" ? (
            <FolderIcon size={14} />
          ) : (
            <ConversationIcon size={13} />
          )}
          <button
            className="tree-name archive-open"
            {...middleClickOpens(row.kind === "folder" ? `/f/${row.id}` : `/c/${row.id}`)}
            onClick={() => {
                        navigate(row.kind === "folder" ? `/f/${row.id}` : `/c/${row.id}`);
            }}
          >
            {row.name}
            {row.path.length > 0 && (
              <span className="archive-path">{row.path.join(" / ")}</span>
            )}
          </button>
          <button
            className="tree-menu-btn"
            aria-label={t.unarchive}
            title={t.unarchive}
            onClick={() => void unarchive(row)}
          >
            <ArchiveIcon size={14} />
          </button>
        </div>
      ))}
      {rows.length === 0 && <div className="conv-list-empty">{t.archiveListEmpty}</div>}
    </div>
  );
}
