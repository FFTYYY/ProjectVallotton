/**
 * Thin fetch wrapper: CSRF handling, JSON decoding, typed errors.
 * A 401 anywhere flips the auth store to logged-out (handled via callback).
 */
import { t } from "../uiText";


export class ApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code = "") {
    super(message);
    this.status = status;
    this.code = code || String(status);
  }
}

let csrfToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function ensureCsrf(): Promise<void> {
  if (csrfToken) return;
  const resp = await fetch("/api/csrf", { credentials: "same-origin" });
  if (resp.ok) {
    const data = await resp.json();
    csrfToken = data.csrfToken;
  }
}

async function rawRequest(
  method: string,
  url: string,
  body?: BodyInit | null,
  headers: Record<string, string> = {},
): Promise<Response> {
  if (method !== "GET") {
    await ensureCsrf();
    if (csrfToken) headers["X-CSRFToken"] = csrfToken;
  }
  return fetch(url, {
    method,
    credentials: "same-origin",
    headers,
    body: body ?? undefined,
  });
}

async function request<T>(
  method: string,
  url: string,
  body?: BodyInit | null,
  headers: Record<string, string> = {},
): Promise<T> {
  let resp = await rawRequest(method, url, body, { ...headers });
  // CSRF token may have rotated (e.g. after login) — refresh once and retry
  if (resp.status === 403 && method !== "GET") {
    csrfToken = null;
    resp = await rawRequest(method, url, body, { ...headers });
  }
  if (resp.status === 401) {
    onUnauthorized?.();
    throw new ApiError(t.notLoggedIn, 401, "unauthenticated");
  }
  let data: unknown = null;
  const text = await resp.text();
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* non-JSON error body */
  }
  if (!resp.ok) {
    const err = (data ?? {}) as { error?: string; code?: string };
    throw new ApiError(err.error || t.requestFailed(resp.status), resp.status, err.code);
  }
  return data as T;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) =>
    request<T>(
      "POST",
      url,
      body === undefined ? null : JSON.stringify(body),
      body === undefined ? {} : { "Content-Type": "application/json" },
    ),
  postForm: <T>(url: string, form: FormData) => request<T>("POST", url, form),
  patch: <T>(url: string, body: unknown) =>
    request<T>("PATCH", url, JSON.stringify(body), {
      "Content-Type": "application/json",
    }),
  delete: <T>(url: string) => request<T>("DELETE", url),
};
