import { useEffect, useRef } from "react";
import { Outlet, useLocation, useParams } from "react-router-dom";

import { titleTrail } from "../lib/pageTitle";
import { useChat } from "../store/chat";
import { usePhrases } from "../store/phrases";
import { useProjects } from "../store/projects";
import { useUi } from "../store/ui";
import { useSwipeDrawer } from "../hooks/useSwipeDrawer";
import { CollapseLeftIcon, ExpandRightIcon, MenuIcon } from "./Icons";
import { Sidebar } from "./Sidebar";
import { t } from "../uiText";

export function AppLayout() {
  const loadConversations = useChat((s) => s.loadConversations);
  const convMeta = useChat((s) => s.convMeta);
  const rightOpen = useUi((s) => s.rightOpen);
  const setRightOpen = useUi((s) => s.setRightOpen);
  const setDrawerOpen = useUi((s) => s.setDrawerOpen);
  const setSidebarTab = useUi((s) => s.setSidebarTab);
  const leftOpen = useUi((s) => s.leftOpen);
  const setLeftOpen = useUi((s) => s.setLeftOpen);
  const loadTree = useProjects((s) => s.loadTree);
  const loadPhrases = usePhrases((s) => s.load);
  const folders = useProjects((s) => s.folders);
  const libraries = useProjects((s) => s.libraries);
  const drawer = useSwipeDrawer();
  const { convId, folderId } = useParams<{ convId: string; folderId: string }>();
  const { pathname } = useLocation();

  useEffect(() => {
    void loadConversations();
    // both tabs want the tree: the projects tab draws it, and the conversation
    // list names the folder each conversation sits in
    void loadTree();
    // And the quick phrases, which used to be loaded only by the right rail:
    // the chip above the composer says whether a continuous run can run, and
    // that answer depended on a panel the phone starts with closed. Same shape
    // of bug the server side of this already had — see continuous._instruction.
    void loadPhrases();
  }, [loadConversations, loadTree]);

  // Landing ON a folder — a reload, a bookmark — should show the tab that
  // folder lives in. Only on arrival, though: opening a folder from the
  // archive must not throw you over to 项目 afterwards.
  const tabSettled = useRef(false);
  useEffect(() => {
    if (tabSettled.current) return;
    tabSettled.current = true;
    if (folderId) setSidebarTab("projects");
  }, [folderId, setSidebarTab]);

  // mirror drawer state to the store; opening it also closes the right sidebar
  useEffect(() => {
    setDrawerOpen(drawer.open);
    if (drawer.open) setRightOpen(false);
  }, [drawer.open, setRightOpen, setDrawerOpen]);

  // refresh the list when the tab regains focus
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState === "visible") void loadConversations();
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [loadConversations]);

  // one line, folders then name — on a phone this bar is the only title there
  // is, so it carries the whole trail rather than just the last part of it
  const activeConv = convId ? convMeta[convId] : undefined;
  const trail = titleTrail({
    pathname,
    conv: activeConv,
    folderId,
    folders,
    folderPath: folderId ? libraries[folderId]?.folder.path : undefined,
  });

  // the library and settings pages have no right rail, so the shell must not
  // reserve room for one — an empty 318px column reads as a rendering fault,
  // and it pushes a page that centres its own column off to the left
  const hasRightRail =
    !pathname.startsWith("/f/") && !pathname.startsWith("/settings");

  return (
    <div
      className={`app-shell ${rightOpen && hasRightRail ? "right-open" : ""} ${
        leftOpen ? "" : "left-closed"
      } ${drawer.open ? "drawer-open" : ""}`}
    >
      <Sidebar
        open={drawer.open}
        dragging={drawer.dragging}
        onClose={() => drawer.setOpen(false)}
      />
      <div
        className={`drawer-overlay ${drawer.open ? "visible" : ""}`}
        onClick={() => drawer.setOpen(false)}
      />
      <div className="app-main">
        {/* the left rail's control hugs the edge it folds away to, and its
            chevron points the way it will travel — a different mark from the
            right rail's, so the two are never mistaken for each other */}
        <button
          className={`sidebar-toggle ${leftOpen ? "active" : ""}`}
          aria-label={leftOpen ? t.collapseLeftBar : t.expandLeftBar}
          aria-expanded={leftOpen}
          title={leftOpen ? t.collapse : t.expand}
          onClick={() => setLeftOpen(!leftOpen)}
        >
          {leftOpen ? <CollapseLeftIcon size={18} /> : <ExpandRightIcon size={18} />}
        </button>
        <header className="topbar">
          <button
            className="icon-btn"
            aria-label={t.openMenu}
            onClick={() => drawer.setOpen(true)}
          >
            <MenuIcon size={20} />
          </button>
          {/* the title is a label, not a control — the right sidebar has its
              own toggle, and a tappable title only ever got hit by accident */}
          <div className="topbar-title">
            {trail.length > 1 && (
              <span className="topbar-crumbs">
                {trail.slice(0, -1).map((name, i) => (
                  <span key={i}>
                    {name}
                    <span className="crumb-sep">/</span>
                  </span>
                ))}
              </span>
            )}
            <span className="topbar-name">{trail[trail.length - 1]}</span>
            {activeConv?.is_archived && <span className="tag-chip muted">{t.archived}</span>}
          </div>
          {/* new chat lives in the left drawer, not the hard-to-reach top-right */}
          <span className="topbar-spacer" />
        </header>
        <Outlet />
      </div>
    </div>
  );
}
