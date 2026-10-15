import { create } from "zustand";

import { api, ensureCsrf, setUnauthorizedHandler } from "../api/client";

interface AuthState {
  checked: boolean;
  authenticated: boolean;
  username: string;
  check: () => Promise<void>;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const useAuth = create<AuthState>((set) => ({
  checked: false,
  authenticated: false,
  username: "",

  check: async () => {
    try {
      await ensureCsrf();
      const me = await api.get<{ authenticated: boolean; username?: string }>(
        "/api/auth/me",
      );
      set({
        checked: true,
        authenticated: me.authenticated,
        username: me.username ?? "",
      });
    } catch {
      set({ checked: true, authenticated: false, username: "" });
    }
  },

  login: async (username, password) => {
    const resp = await api.post<{ ok: boolean; username: string }>(
      "/api/auth/login",
      { username, password },
    );
    set({ authenticated: true, username: resp.username, checked: true });
  },

  logout: async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      set({ authenticated: false, username: "" });
    }
  },
}));

setUnauthorizedHandler(() => {
  const s = useAuth.getState();
  if (s.authenticated) useAuth.setState({ authenticated: false });
});
