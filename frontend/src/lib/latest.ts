/**
 * Which answer is still worth applying.
 *
 * Every store here has the same shape of hazard, and it has bitten in every
 * one of them: a read leaves, something changes under it, and the read lands
 * afterwards carrying the world as it was before. The control springs back,
 * the chip un-ticks, the row reappears. On a fast link the window is a few
 * milliseconds and it looks like nothing; on a real one it is most of a
 * second, which is exactly long enough to move a slider in.
 *
 * The rule is the same everywhere, so it lives here rather than being
 * rewritten per store: **take a ticket before you ask, and throw the answer
 * away if the ticket is no longer the latest one.** Anything that changes the
 * thing locally takes a ticket too, which is what makes a write beat a read
 * that left before it.
 *
 * Keyed, because these things are per conversation, per folder, per whatever —
 * one slow read about A must not discard a fresh answer about B.
 */
export class Latest {
  private at: Record<string, number> = {};

  /** take a ticket: for a read, to check later; for a write, to invalidate reads */
  claim(key: string): number {
    this.at[key] = (this.at[key] ?? 0) + 1;
    return this.at[key];
  }

  /** is this ticket still the newest one for its key? */
  holds(key: string, ticket: number): boolean {
    return (this.at[key] ?? 0) === ticket;
  }

  /** forget a key entirely (nothing outstanding can hold a ticket after this) */
  drop(key: string): void {
    delete this.at[key];
  }
}
