import { useEffect, useRef } from "react";

import { useConfirm } from "../store/confirm";
import { t } from "../uiText";

/**
 * The app's own confirmation. Mounted once at the root.
 *
 * Esc says no, Enter says yes, and focus starts on the confirming button so a
 * keyboard user never has to hunt for it. Clicking the ground behind it is a
 * cancel, like every other overlay here.
 */
export function ConfirmDialog() {
  const pending = useConfirm((s) => s.pending);
  const answer = useConfirm((s) => s.answer);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!pending) return;
    confirmRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        answer(false);
      } else if (e.key === "Enter") {
        e.preventDefault();
        answer(true);
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [pending, answer]);

  if (!pending) return null;

  return (
    <div className="modal-backdrop" onClick={() => answer(false)}>
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title" id="confirm-title">
          {pending.title}
        </div>
        {pending.body && <div className="modal-body">{pending.body}</div>}
        <div className="modal-actions">
          <button className="info-action-btn" onClick={() => answer(false)}>
            {pending.cancelLabel ?? t.cancel}
          </button>
          <button
            ref={confirmRef}
            className={`info-action-btn ${pending.danger ? "danger solid" : "primary"}`}
            onClick={() => answer(true)}
          >
            {pending.confirmLabel ?? t.confirm}
          </button>
        </div>
      </div>
    </div>
  );
}
