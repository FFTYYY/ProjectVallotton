/**
 * Height animation for anything that folds open.
 *
 * Uses the grid 0fr→1fr trick rather than a measured max-height: the row
 * resolves to the content's real height, so the transition is correct for any
 * content and never clips or overshoots the way a guessed max-height does.
 *
 * Children stay mounted while closed — the inputs inside keep their state,
 * and opening again is instant.
 */

import type { ReactNode } from "react";

export function Collapse({
  open,
  children,
  className = "",
}: {
  open: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`collapse ${open ? "open" : ""} ${className}`} aria-hidden={!open}>
      <div className="collapse-inner">{children}</div>
    </div>
  );
}
