/**
 * Logging out, asked about the same way everywhere.
 *
 * Not irreversible, but a misplaced tap on a small red button should not
 * throw someone out to the login page in the middle of a conversation, so
 * every entry point asks first, with the same words.
 */

import { confirm } from "../store/confirm";
import { t } from "../uiText";

export function confirmLogout(): Promise<boolean> {
  return confirm({
    title: t.logoutConfirmTitle,
    body: t.logoutConfirmBody,
    confirmLabel: t.logout,
    danger: true,
  });
}
