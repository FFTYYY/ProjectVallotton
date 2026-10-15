import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "../api/client";
import { CloseIcon, CopyIcon } from "./Icons";
import { useToasts } from "../store/toast";
import { t } from "../uiText";

/**
 * The whole of a failure, on request.
 *
 * A turn that failed says one line in the transcript, because a failure is
 * not the thing you came to read and a wall of it is worse than useless. But
 * the line is an excerpt, and what broke usually turns on something the
 * excerpt cut off — a model's answer fails to parse at ONE character, and
 * which character it was is the whole diagnosis. So the excerpt is a door:
 * the full message, and the answer that could not be read, are behind it.
 *
 * Rendered through a portal for the same reason InspectDialog is: .msg-row
 * carries content-visibility, which makes it a containing block for
 * position:fixed, and a backdrop mounted inside one covers nothing.
 */
export function FailureDialog({
  messageId,
  error,
  onClose,
}: {
  messageId: string;
  error: string;
  onClose: () => void;
}) {
  const [unparsed, setUnparsed] = useState<string | null>(null);
  const push = useToasts((s) => s.push);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Only the transcript's own fields ride along on every poll; an answer kept
  // for reading later is fetched when somebody actually asks to read it.
  useEffect(() => {
    let alive = true;
    void api
      .get<{ unparsed?: string }>(`/api/messages/${messageId}/raw`)
      .then((d) => alive && setUnparsed(d.unparsed || ""))
      .catch(() => alive && setUnparsed(""));
    return () => {
      alive = false;
    };
  }, [messageId]);

  async function copyAll() {
    const text = unparsed ? `${error}\n\n---\n\n${unparsed}` : error;
    try {
      await navigator.clipboard.writeText(text);
      push(t.copied, "success");
    } catch {
      push(t.copyFailed, "error");
    }
  }

  return createPortal(
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="failure-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="inspect-head">
          <div className="modal-title" id="failure-title">
            {t.whyFailed}
          </div>
          <button className="inspect-copy" title={t.copyAll} onClick={() => void copyAll()}>
            <CopyIcon size={13} /> {t.copy}
          </button>
          <button className="phrase-action" title={t.close} onClick={onClose}>
            <CloseIcon size={16} />
          </button>
        </div>

        <div className="inspect-scroll">
          <div className="inspect-block">
            <div className="inspect-block-title">{t.errorHeading}</div>
            <pre className="inspect-pre">{error || t.unknownError}</pre>
          </div>
          {unparsed === null && <span className="spinner" />}
          {!!unparsed && (
            <div className="inspect-block">
              <div className="inspect-block-title">
                {t.unparsedHeading}
                <span className="inspect-block-size">{t.chars(unparsed.length)}</span>
              </div>
              <pre className="inspect-pre">{unparsed}</pre>
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}
