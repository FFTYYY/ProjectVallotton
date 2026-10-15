/**
 * localStorage that cannot throw.
 *
 * Reading is guarded because a browser in private mode may refuse the whole
 * API, and writing because the quota can be full. Neither is worth taking the
 * app down over — a remembered preference is allowed to be forgotten.
 */

export function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return { ...fallback, ...(JSON.parse(raw) as Partial<T>) };
  } catch {
    return fallback;
  }
}

export function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* full, or refused — the preference simply will not survive the tab */
  }
}
