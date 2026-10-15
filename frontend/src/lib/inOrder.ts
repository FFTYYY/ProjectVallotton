const chains = new Map<string, Promise<unknown>>();

/**
 * Send these one after another, per key.
 *
 * The right rail sends one request per control, and a hand can move four of
 * them in a second. Four writes to the same row arriving together is a lost
 * update at best; against SQLite — which is what a local backend runs on — it
 * is a 500 with `database is locked`, and the value the user just set silently
 * reverts on the next load. Nothing about these is urgent enough to be worth
 * racing: a queue is both correct and indistinguishable from instant.
 *
 * A failure is logged rather than surfaced. The control already shows the new
 * value and the next send carries it anyway, so a toast would be noise — but
 * a write that fails with no trace at all is how the value that mysteriously
 * did not stick stayed a mystery.
 */
export function inOrder(key: string, run: () => Promise<unknown>): void {
  const prev = chains.get(key) ?? Promise.resolve();
  // run whichever way the one before it went: one failure must not stall the
  // queue behind it
  const next = prev.then(run, run).catch((err) => {
    console.warn(`write failed (${key})`, err);
  });
  chains.set(key, next);
  void next.then(() => {
    if (chains.get(key) === next) chains.delete(key);
  });
}
