import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { NavLink } from "react-router-dom";

import type { Conversation } from "../api/types";
import { confirmArchive } from "../lib/archive";
import { useAuth } from "../store/auth";
import { useChat } from "../store/chat";
import { useProjects } from "../store/projects";
import { useUi } from "../store/ui";
import { ArchiveList } from "./ArchiveList";
import { ComposeInIcon, DotsIcon, FolderIcon, GearIcon, NewChatIcon } from "./Icons";
import { ProjectTree } from "./ProjectTree";
import { QuotaPanel } from "./QuotaPanel";
import { t } from "../uiText";

interface SidebarProps {
  open: boolean;
  dragging: boolean;
  onClose: () => void;
}

function ConvItem({ conv, active }: { conv: Conversation; active: boolean }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const renameConversation = useChat((s) => s.renameConversation);
  const setArchived = useChat((s) => s.setArchived);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setMenuOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [menuOpen]);

  function onRename() {
    setMenuOpen(false);
    setRenaming(true);
  }

  async function commitRename(next: string) {
    setRenaming(false);
    const title = next.trim();
    if (title && title !== conv.title) await renameConversation(conv.id, title);
  }

  async function onArchiveToggle() {
    setMenuOpen(false);
    if (conv.is_archived) return void setArchived(conv.id, false);
    if (await confirmArchive("conversation", conv.title)) {
      await setArchived(conv.id, true);
    }
  }

  if (renaming) {
    return (
      <div className="conv-item renaming">
        <input
          className="conv-rename-input"
          defaultValue={conv.title}
          maxLength={200}
          autoFocus
          aria-label={t.renameConversation}
          onBlur={(e) => void commitRename(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) e.currentTarget.blur();
            else if (e.key === "Escape") {
              e.currentTarget.value = conv.title;
              e.currentTarget.blur();
            }
          }}
        />
      </div>
    );
  }

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <NavLink
        to={`/c/${conv.id}`}
        className={`conv-item ${active ? "active" : ""} ${menuOpen ? "menu-open" : ""}`}
      >
        <div className="conv-item-title">
          {conv.source === "mcp" && <span className="conv-item-badge">MCP</span>}
          {conv.folder_path.length > 0 && (
            <span className="conv-item-folder">
              <FolderIcon size={11} />
              {conv.folder_path[conv.folder_path.length - 1]}
            </span>
          )}
          {conv.title}
        </div>
        {conv.preview ? <div className="conv-item-preview">{conv.preview}</div> : null}
        <button
          className="conv-item-menu-btn"
          aria-label={t.conversationMenu}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setMenuOpen((v) => !v);
          }}
        >
          <DotsIcon size={15} />
        </button>
      </NavLink>
      {menuOpen && (
        <div className="conv-menu">
          <button onClick={onRename}>{t.rename}</button>
          <button onClick={onArchiveToggle}>
            {conv.is_archived ? t.unarchive : t.archive}
          </button>
        </div>
      )}
    </div>
  );
}

function ConversationList() {
  const conversations = useChat((s) => s.conversations);
  const conversationsLoaded = useChat((s) => s.conversationsLoaded);
  const hasMoreConvs = useChat((s) => s.hasMoreConvs);
  const loadingConvs = useChat((s) => s.loadingConvs);
  const loadMoreConversations = useChat((s) => s.loadMoreConversations);
  const { convId } = useParams<{ convId: string }>();

  function onScroll(e: React.UIEvent<HTMLElement>) {
    const el = e.currentTarget;
    if (hasMoreConvs && el.scrollHeight - el.scrollTop - el.clientHeight < 120) {
      void loadMoreConversations();
    }
  }

  return (
    <nav className="conv-list" onScroll={onScroll}>
        {conversations.map((c) => (
          <ConvItem key={c.id} conv={c} active={c.id === convId} />
        ))}
      {conversations.length === 0 && (
        <div className="conv-list-empty">
          {conversationsLoaded ? t.noConversations : t.conversationListFailed}
        </div>
      )}
      {loadingConvs && (
        <div style={{ textAlign: "center", padding: 8 }}>
          <span className="spinner" />
        </div>
      )}
    </nav>
  );
}

/**
 * How often the rail re-reads the backend.
 *
 * Long on purpose. What it catches is a conversation renamed, archived or
 * moved somewhere else — another tab, another device, the model naming a chat
 * it just finished. None of that is worth a request every few seconds, and the
 * open conversation already refreshes on its own.
 */
const REFRESH_MS = 45_000;

export function Sidebar({ open, dragging, onClose }: SidebarProps) {
  const username = useAuth((s) => s.username);
  const logout = useAuth((s) => s.logout);
  const sidebarTab = useUi((s) => s.sidebarTab);
  const setSidebarTab = useUi((s) => s.setSidebarTab);
  const createConversation = useChat((s) => s.createConversation);
  const loadConversations = useChat((s) => s.loadConversations);
  const loadTree = useProjects((s) => s.loadTree);
  const loadArchive = useProjects((s) => s.loadArchive);
  const convMeta = useChat((s) => s.convMeta);
  const folders = useProjects((s) => s.folders);
  const libraries = useProjects((s) => s.libraries);
  const navigate = useNavigate();
  const onSettings = useLocation().pathname === "/settings";

  // Only the tab on screen, and only while there is someone to see it. A drag
  // is the one moment a reload would be felt — the tree would be replaced
  // under the row being carried — and it already marks itself on the body.
  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (document.body.classList.contains("dragging-tree")) return;
      if (sidebarTab === "list") void loadConversations();
      else if (sidebarTab === "projects") void loadTree();
      else void loadArchive();
    }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [sidebarTab, loadConversations, loadTree, loadArchive]);
  const { convId, folderId } = useParams<{ convId: string; folderId: string }>();

  // which folder a "new chat in here" would land in: the folder being viewed,
  // otherwise the folder of the conversation being read
  const targetFolderId = folderId ?? convMeta[convId ?? ""]?.folder_id ?? null;
  const targetFolder = folders.find((f) => f.id === targetFolderId) ?? null;
  const targetName =
    targetFolder?.name ??
    libraries[targetFolderId ?? ""]?.folder.name ??
    convMeta[convId ?? ""]?.folder_path.slice(-1)[0] ??
    "";

  // "new chat" = deselect any conversation → the centered composer at "/"
  // (sending there auto-creates the conversation, ChatGPT-style)
  function onNewChat() {
    onClose();
    navigate("/");
  }

  async function onNewChatInFolder() {
    if (!targetFolderId) return;
    onClose();
    const conv = await createConversation(targetFolderId);
    navigate(`/c/${conv.id}`);
  }

  async function onLogout() {
    await logout();
    navigate("/login");
  }

  return (
    <aside className={`sidebar ${open ? "open" : ""} ${dragging ? "dragging" : ""}`}>
      <div className="sidebar-head">
        <img className="sidebar-logo" src="/icons/icon-192.png" alt="" />
        <div className="sidebar-title">Vallotton</div>
      </div>
      {/* Above 新对话 because it is the rarer errand and the eye lands on the
          top of a rail: putting it at the foot is what made the balance and
          the logout button eat a strip of every phone screen. */}
      <a
        className={`sidebar-settings ${onSettings ? "active" : ""}`}
        href="/settings"
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          navigate("/settings");
          onClose();
        }}
      >
        <GearIcon size={14} />
        <span>{t.settings}</span>
      </a>
      {/* An anchor, not a button: a new conversation has a real address, so
          middle-click and ctrl-click should open it in a tab the way every
          other link does. The click handler keeps the ordinary case a SPA
          navigation — and stays out of the way of the modified ones. */}
      <a
        className="new-chat-btn"
        href="/"
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          onNewChat();
        }}
      >
        <NewChatIcon size={15} />
        <span>{t.newConversation}</span>
      </a>
      <button
        className="new-chat-btn in-folder"
        onClick={() => void onNewChatInFolder()}
        disabled={!targetFolderId}
        title={
          targetFolderId
            ? t.newConversationIn(targetName || t.folder)
            : t.pickFolderFirst
        }
      >
        <ComposeInIcon size={15} />
        <span>{t.newConversationInFolder(targetName || t.folder)}</span>
      </button>
      <div className={`sidebar-tabs tab-${sidebarTab}`} role="tablist">
        {/* one bar that slides between the two tabs, rather than a border
            appearing and disappearing under each */}
        <span className="tab-indicator" aria-hidden="true" />
        <button
          role="tab"
          aria-selected={sidebarTab === "list"}
          className={sidebarTab === "list" ? "active" : ""}
          onClick={() => setSidebarTab("list")}
        >
          {t.conversations}
        </button>
        <button
          role="tab"
          aria-selected={sidebarTab === "projects"}
          className={sidebarTab === "projects" ? "active" : ""}
          onClick={() => setSidebarTab("projects")}
        >
          {t.projects}
        </button>
        <button
          role="tab"
          aria-selected={sidebarTab === "archived"}
          className={sidebarTab === "archived" ? "active" : ""}
          onClick={() => setSidebarTab("archived")}
        >
          {t.archived}
        </button>
      </div>
      {/* keyed so switching tabs re-mounts and the panel fades in */}
      <div className="tab-panel" key={sidebarTab}>
        {sidebarTab === "list" && <ConversationList />}
        {sidebarTab === "projects" && <ProjectTree />}
        {sidebarTab === "archived" && <ArchiveList />}
      </div>
      <div className="sidebar-foot">
        <QuotaPanel />
        <div className="sidebar-user">
          <span>{username}</span>
          <button className="logout-btn" onClick={onLogout}>
            {t.logout}
          </button>
        </div>
      </div>
    </aside>
  );
}
