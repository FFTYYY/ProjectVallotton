import { create } from "zustand";

export interface Toast {
  id: number;
  text: string;
  type: "info" | "error" | "success";
}

let nextId = 1;

interface ToastState {
  toasts: Toast[];
  push: (text: string, type?: Toast["type"]) => void;
  remove: (id: number) => void;
}

export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push: (text, type = "info") => {
    const id = nextId++;
    set((s) => ({ toasts: [...s.toasts, { id, text, type }] }));
    window.setTimeout(() => {
      set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
    }, 4200);
  },
  remove: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));
