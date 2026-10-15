/**
 * Asking before something irreversible — in the app's own voice.
 *
 * `confirm()` returns a promise so a caller reads the same as the browser's
 * dialog did, without the browser's dialog:
 *
 *   if (!(await confirm({ title: "删除？", danger: true }))) return;
 */

import { create } from "zustand";

export interface ConfirmOptions {
  title: string;
  /** the consequence, in one line — say what is lost, not "are you sure" */
  body?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

interface Pending extends ConfirmOptions {
  seq: number;
  resolve: (ok: boolean) => void;
}

interface ConfirmState {
  pending: Pending | null;
  ask: (opts: ConfirmOptions) => Promise<boolean>;
  answer: (ok: boolean) => void;
}

let seq = 0;

export const useConfirm = create<ConfirmState>((set, get) => ({
  pending: null,

  ask: (opts) =>
    new Promise<boolean>((resolve) => {
      // one at a time: a second question supersedes the first, and the first
      // answers "no" so its caller never hangs
      const previous = get().pending;
      previous?.resolve(false);
      set({ pending: { ...opts, seq: ++seq, resolve } });
    }),

  answer: (ok) => {
    const pending = get().pending;
    if (!pending) return;
    set({ pending: null });
    pending.resolve(ok);
  },
}));

/** the shorthand components use */
export const confirm = (opts: ConfirmOptions) => useConfirm.getState().ask(opts);
