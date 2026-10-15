import { useEffect, useRef, useState } from "react";

import { useDrafts } from "../store/drafts";
import { useToasts } from "../store/toast";
import { useUi } from "../store/ui";
import { coarsePointer } from "../lib/pointer";
import { PaperclipIcon, SendIcon, StopIcon } from "./Icons";
import { t } from "../uiText";

const MAX_FILES = 6;
const MAX_BYTES = 15 * 1024 * 1024;

interface ComposerProps {
  busy: boolean; // a generation is running → send becomes stop
  onSend: (text: string, files: File[]) => void | Promise<void>;
  onStop?: () => void;
  /** false while the request is still uploading — nothing to cancel yet */
  canStop?: boolean;
  /**
   * Stopping right now keeps what has been made rather than throwing it away —
   * the turn finishes as an ordinary one. The button says so by not looking
   * like the usual hard stop.
   */
  stopKeeps?: boolean;
  autoFocus?: boolean;
  placeholder?: string;
  /** external text injection (suggestion chips) */
  presetText?: string;
  onPresetConsumed?: () => void;
  /** rendered just above the input — the library entries this turn will use */
  chips?: React.ReactNode;
  /**
   * Whose half-written message this is — a conversation id. The chat view
   * stays mounted when you move between conversations, so without this the
   * sentence you left in one was sitting in the composer under the next one.
   * Absent on the new-chat page, which starts blank and keeps nothing.
   */
  draftKey?: string;
}

interface Attached {
  file: File;
  url: string;
}

export function Composer({
  busy,
  onSend,
  onStop,
  canStop = true,
  stopKeeps = false,
  autoFocus,
  placeholder,
  presetText,
  onPresetConsumed,
  chips,
  draftKey,
}: ComposerProps) {
  const [text, setText] = useState(() => useDrafts.getState().get(draftKey));
  const [attached, setAttached] = useState<Attached[]>([]);
  const [dragover, setDragover] = useState(false);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const push = useToasts((s) => s.push);
  const setComposer = useUi((s) => s.setComposer);

  /**
   * Moving to another conversation brings that conversation's half-written
   * line, and leaves this one's behind for when you come back.
   *
   * Attachments are let go rather than carried: a file picked out for one
   * conversation must not follow you into the next, and the object URLs they
   * hold have to be released by the same hand that made them.
   */
  const firstKey = useRef(draftKey);
  useEffect(() => {
    if (firstKey.current === draftKey) return; // the mount already read it
    firstKey.current = draftKey;
    setText(useDrafts.getState().get(draftKey));
    setAttached((prev) => {
      prev.forEach((a) => URL.revokeObjectURL(a.url));
      return [];
    });
  }, [draftKey]);

  /**
   * Say where things can be handed in: a quick phrase from the right rail, a
   * picture from the full-screen view.
   *
   * Registered while this composer is on screen and taken down with it, so a
   * hand-in lands in the composer that was there when it was made — or lands
   * nowhere, which is the right answer when there is no composer. What it
   * cannot do is arrive twice, or in the next conversation.
   */
  useEffect(() => {
    const sink = {
      append(text: string) {
        setText((t) => t + text);
        if (!coarsePointer()) taRef.current?.focus();
      },
      attach(files: File[]) {
        addFiles(files);
      },
    };
    setComposer(sink);
    return () =>
      useUi.setState((s) => (s.composer === sink ? { composer: null } : {}));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setComposer]);

  useEffect(() => {
    if (presetText) {
      setText(presetText);
      taRef.current?.focus();
      onPresetConsumed?.();
    }
  }, [presetText, onPresetConsumed]);

  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    // The cap is read from CSS rather than written here: a phone gets a
    // smaller one (see --composer-max), and the height has to be an inline
    // style to grow with the text, which would otherwise beat the stylesheet.
    const capped = parseFloat(
      getComputedStyle(ta).getPropertyValue("--composer-max"),
    );
    const max = Number.isFinite(capped) && capped > 0 ? capped : 200;
    ta.style.height = "auto";
    const full = ta.scrollHeight;
    ta.style.height = `${Math.min(full, max)}px`;
    // only show the scrollbar once we actually hit the max height — otherwise
    // a single line briefly reports a 1px overflow and flashes a scrollbar
    ta.style.overflowY = full > max ? "auto" : "hidden";
  }, [text]);

  /**
   * Publish this row's height as --composer-h on the shell.
   *
   * On a phone the two floating controls (the rail's and the jump pair) hover
   * over the transcript just above this row. Pinned to a fixed distance from
   * the bottom they end up inside it the moment the text runs to a few lines,
   * so they are positioned off this instead and rise as it grows.
   */
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const wrap = wrapRef.current;
    const shell = wrap?.closest(".app-main") as HTMLElement | null;
    if (!wrap || !shell) return;
    // offsetHeight, not contentRect: the row's own padding — which carries the
    // safe-area inset — is part of the distance the controls have to clear
    const ro = new ResizeObserver(() => {
      shell.style.setProperty("--composer-h", `${wrap.offsetHeight}px`);
    });
    ro.observe(wrap);
    return () => {
      ro.disconnect();
      shell.style.removeProperty("--composer-h");
    };
  }, []);

  // revoke preview object URLs on unmount (ref avoids a stale closure)
  const attachedRef = useRef<Attached[]>([]);
  attachedRef.current = attached;
  useEffect(
    () => () => attachedRef.current.forEach((a) => URL.revokeObjectURL(a.url)),
    [],
  );

  function addFiles(files: FileList | File[]) {
    const list = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (!list.length) return;
    // Build the additions OUTSIDE the updater: creating object URLs and
    // pushing toasts are side effects, and React may run an updater more
    // than once (guaranteed under StrictMode), which would leak URLs and
    // duplicate the toasts.
    const room = MAX_FILES - attachedRef.current.length;
    if (list.length > room) push(t.tooManyFiles(MAX_FILES));
    const accepted: Attached[] = [];
    for (const f of list.slice(0, Math.max(room, 0))) {
      if (f.size > MAX_BYTES) {
        push(t.fileTooBig(f.name));
        continue;
      }
      accepted.push({ file: f, url: URL.createObjectURL(f) });
    }
    if (accepted.length) setAttached((prev) => [...prev, ...accepted]);
  }

  function removeFile(url: string) {
    setAttached((prev) => {
      const target = prev.find((a) => a.url === url);
      if (target) URL.revokeObjectURL(target.url);
      return prev.filter((a) => a.url !== url);
    });
  }

  async function submit() {
    const t = text.trim();
    if (busy) return;
    if (!t && attached.length === 0) return;
    setText("");
    useDrafts.getState().clear(draftKey);
    const files = attached.map((a) => a.file);
    attached.forEach((a) => URL.revokeObjectURL(a.url));
    setAttached([]);
    await onSend(t, files);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !coarsePointer()) {
      if (e.nativeEvent.isComposing) return; // IME composition
      e.preventDefault();
      void submit();
    }
  }

  function onPaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData.files ?? []);
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  }

  return (
    <div className="composer-wrap" ref={wrapRef}>
      {chips}
      <div
        className={`composer ${dragover ? "dragover" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragover(true);
        }}
        onDragLeave={() => setDragover(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragover(false);
          addFiles(e.dataTransfer.files);
        }}
      >
        {attached.length > 0 && (
          <div className="attach-strip">
            {attached.map((a) => (
              <div className="attach-chip" key={a.url}>
                <img src={a.url} alt="" />
                <button className="remove" aria-label={t.remove} onClick={() => removeFile(a.url)}>
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={taRef}
          rows={1}
          value={text}
          placeholder={placeholder ?? t.composerPlaceholder}
          onChange={(e) => {
            setText(e.target.value);
            useDrafts.getState().set(draftKey, e.target.value);
          }}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          autoFocus={autoFocus && !coarsePointer()}
        />
        <div className="composer-bar">
          <button
            className="icon-btn"
            aria-label={t.addReference}
            title={t.addReference}
            onClick={() => fileRef.current?.click()}
          >
            <PaperclipIcon size={18} />
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = "";
            }}
          />
          <div className="composer-hint">{t.composerHint}</div>
          {busy ? (
            <button
              className={`send-btn stop ${stopKeeps ? "keeps" : ""}`}
              aria-label={t.stopGenerating}
              title={
                !canStop
                  ? t.stopAfterUpload
                  : stopKeeps
                    ? t.stopKeepPicture
                    : t.stopGenerating
              }
              disabled={!canStop}
              onClick={onStop}
            >
              <StopIcon size={18} />
            </button>
          ) : (
            <button
              className="send-btn"
              aria-label={t.send}
              disabled={!text.trim() && attached.length === 0}
              onClick={() => void submit()}
            >
              <SendIcon size={18} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
