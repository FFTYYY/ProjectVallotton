import { create } from "zustand";

import { readJson, writeJson } from "../lib/storage";

export type SidebarTab = "list" | "projects" | "archived";

/** what the shell looks like when you come back to it tomorrow */
interface Persisted {
  rightOpen: boolean;
  leftOpen: boolean;
  sidebarTab: SidebarTab;
}

/** what the composer can be asked to do from elsewhere on the page */
export interface ComposerSink {
  append: (text: string) => void;
  attach: (files: File[]) => void;
}

interface UiState extends Persisted {
  /** right sidebar (conversation info, library selection, quick phrases) */
  setRightOpen: (open: boolean) => void;
  /** left sidebar, on a wide screen only — on a phone it is the swipe drawer */
  setLeftOpen: (open: boolean) => void;
  /** left drawer open state (mirrored from the swipe hook for cross-component reads) */
  drawerOpen: boolean;
  setDrawerOpen: (open: boolean) => void;
  /** left sidebar tab */
  setSidebarTab: (tab: SidebarTab) => void;
  /**
   * The composer, wherever it happens to be mounted, so the rest of the page
   * can hand it something.
   *
   * A sink that registers itself, not a message left lying in the store. A
   * message has to be remembered as delivered, and whoever remembers that is
   * remounted sooner or later — leave a conversation through a page with no
   * composer, come back to a different one, and the message was delivered a
   * second time. That is how a reference picture added in one conversation
   * turned up in the next. Nothing is kept here between calls, so there is
   * nothing to deliver twice and nothing to deliver to the wrong place.
   */
  composer: ComposerSink | null;
  setComposer: (sink: ComposerSink | null) => void;
}

const KEY = "vallotton.ui";
const TABS: SidebarTab[] = ["list", "projects", "archived"];

/**
 * On a wide screen both rails are part of the layout and start open; on a
 * phone the left one is a swipe drawer and the right one a summoned sheet.
 */
const WIDE = typeof window !== "undefined" && window.matchMedia("(min-width: 900px)").matches;

function load(): Persisted {
  const p = readJson<Persisted>(KEY, { rightOpen: WIDE, leftOpen: true, sidebarTab: "list" });
  return {
    // the phone's rails are overlays, not columns: restoring one open would
    // greet you with a sheet over the conversation you asked for
    rightOpen: WIDE ? p.rightOpen !== false : false,
    leftOpen: p.leftOpen !== false,
    sidebarTab: TABS.includes(p.sidebarTab) ? p.sidebarTab : "list",
  };
}

export const useUi = create<UiState>((set, get) => {
  const remember = () => {
    const { rightOpen, leftOpen, sidebarTab } = get();
    writeJson(KEY, { rightOpen, leftOpen, sidebarTab });
  };
  return {
    ...load(),
    setRightOpen: (open) => {
      set({ rightOpen: open });
      remember();
    },
    setLeftOpen: (open) => {
      set({ leftOpen: open });
      remember();
    },
    drawerOpen: false,
    setDrawerOpen: (open) => set({ drawerOpen: open }),
    setSidebarTab: (tab) => {
      set({ sidebarTab: tab });
      remember();
    },
    composer: null,
    setComposer: (sink) => set({ composer: sink }),
  };
});
