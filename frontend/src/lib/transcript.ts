import type { Conversation, Message } from "../api/types";

/**
 * The two judgements a transcript makes about itself, out where they can be
 * looked at.
 *
 * Both used to live inside the chat store, and both were wrong in ways no
 * browser test could reach: they are decisions about *state*, not about the
 * screen, and an end-to-end suite can only press things and look. The worst
 * two bugs this app has had lived here.
 */

export function sortMessages(list: Message[]): Message[] {
  return [...list].sort((a, b) => a.seq - b.seq);
}

/**
 * The waterline an incremental poll asks from: the highest seq below which
 * nothing can still change.
 *
 * A failed turn can still change. A continuous run archives it and puts a
 * replacement in its place, so it belongs ABOVE the line — left below it, the
 * poll never asked after it again and the browser kept whatever it last saw.
 */
export function stableSeq(list: Message[]): number {
  let stable = 0;
  for (const m of sortMessages(list)) {
    if (m.local) continue;
    if (m.status === "pending" || m.status === "error") break;
    stable = m.seq;
  }
  return stable;
}

/**
 * Fold a poll's answer into what is already on screen.
 *
 * Three things happen here and only one of them used to: rows are added, rows
 * are updated in place, and — the missing one — **rows the server has archived
 * are let go**. A turn that failed and was replaced stayed in the transcript
 * because the only way this learned about a row was being sent it, and an
 * archived row is not sent again.
 *
 * Local placeholders survive until the server acknowledges them by
 * client_msg_id, so a message still on its way is never dropped.
 */
export function mergeInto(current: Message[], incoming: Message[]): Message[] {
  const acknowledged = new Set(
    incoming.filter((m) => m.client_msg_id).map((m) => m.client_msg_id),
  );
  const gone = new Set(incoming.filter((m) => m.archived).map((m) => m.id));

  const kept = current.filter(
    (m) => !(m.local && m.client_msg_id && acknowledged.has(m.client_msg_id)),
  );
  const keptIds = new Set(kept.map((m) => m.id));
  const merged = kept.map((m) => incoming.find((i) => i.id === m.id) ?? m);
  for (const m of incoming) {
    if (!keptIds.has(m.id) && !m.archived) merged.push(m);
  }
  return sortMessages(merged.filter((m) => !gone.has(m.id)));
}

/**
 * Whether there is still something to wait for, and so still a reason to poll.
 *
 * The run counts, not only the turn. A turn ends a few seconds before the next
 * one is queued — naming a new conversation costs a model call — and a poll
 * landing in that gap used to see nothing pending and stop. Nothing started it
 * again, because the thing that would have is the poll: the transcript froze
 * while the server went on painting, and the composer unlocked only to answer
 * the next message with a 409.
 */
export function isWorking(
  messages: Message[],
  serverPending: boolean,
  conv: Conversation | undefined,
): boolean {
  const local = messages.some(
    (m) => m.status === "pending" || m.local === "sending",
  );
  return local || serverPending || !!conv?.continuous;
}
