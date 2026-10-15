/**
 * Middle-click opens things in a tab, everywhere.
 *
 * The 对话 list gets this for free: its rows are real anchors. The tree and the
 * archived shelf cannot be — the tree row is a drag handle with buttons and a
 * context menu inside it, and turning it into a link would fight dnd-kit — so
 * they ask for the behaviour explicitly, and this is the one place that says
 * what it is.
 */
export function openInNewTab(path: string): void {
  window.open(path, "_blank", "noopener");
}

/**
 * Handlers for a row that is not an anchor but should behave like one.
 *
 * Deliberately `onMouseDownCapture` rather than `onMouseDown`: the tree row
 * already carries dnd-kit's own `onMouseDown`, and spreading a second one over
 * it replaced the drag sensor's activator — dragging stopped working
 * altogether, which the drag suites caught and nothing else would have. A
 * capture handler is a different prop, so both survive.
 */
export function middleClickOpens(path: string) {
  return {
    onAuxClick: (e: React.MouseEvent) => {
      if (e.button !== 1) return;
      e.preventDefault();
      e.stopPropagation();
      openInNewTab(path);
    },
    // Chrome starts its autoscroll on a middle press; nothing here wants it
    onMouseDownCapture: (e: React.MouseEvent) => {
      if (e.button === 1) e.preventDefault();
    },
  };
}
