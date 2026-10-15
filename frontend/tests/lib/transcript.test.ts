import { describe, expect, it } from "vitest";

import type { Conversation, Message } from "../../src/api/types";

import { isWorking, mergeInto, stableSeq } from "../../src/lib/transcript";

const msg = (over: Partial<Message> & { seq: number }): Message =>
  ({
    id: `m${over.seq}`,
    conversation_id: "c",
    role: "assistant",
    status: "done",
    kind: "text",
    phase: "",
    content: "",
    error: "",
    client_msg_id: "",
    nai_prompts: null,
    notice_kind: "",
    attachments: [],
    ...over,
  }) as Message;

const conv = (over: Partial<Conversation> = {}): Conversation =>
  ({
    id: "c",
    title: "t",
    source: "web",
    is_archived: false,
    folder_id: null,
    folder_path: [],
    position: 0,
    created_at: "",
    updated_at: "",
    ...over,
  }) as Conversation;

describe("stableSeq", () => {
  it("stops below anything still being made", () => {
    expect(stableSeq([msg({ seq: 1 }), msg({ seq: 2, status: "pending" }), msg({ seq: 3 })]))
      .toBe(1);
  });

  it("stops below a failed turn, because a run can still replace it", () => {
    // left below the line, the poll never asked after it again and the browser
    // kept whatever it last saw of it — forever
    expect(stableSeq([msg({ seq: 1 }), msg({ seq: 2, status: "error" }), msg({ seq: 3 })]))
      .toBe(1);
  });

  it("ignores messages that have not reached the server yet", () => {
    const local = { ...msg({ seq: 99 }), local: "sending" } as Message;
    expect(stableSeq([msg({ seq: 1 }), msg({ seq: 2 }), local])).toBe(2);
  });

  it("is 0 when the very first thing is still going", () => {
    expect(stableSeq([msg({ seq: 1, status: "pending" })])).toBe(0);
  });
});

describe("mergeInto", () => {
  it("adds what it has not seen", () => {
    const got = mergeInto([msg({ seq: 1 })], [msg({ seq: 2 })]);
    expect(got.map((m) => m.seq)).toEqual([1, 2]);
  });

  it("updates in place rather than duplicating", () => {
    const got = mergeInto(
      [msg({ seq: 1, status: "pending" })],
      [msg({ seq: 1, status: "done", content: "好了" })],
    );
    expect(got).toHaveLength(1);
    expect(got[0].status).toBe("done");
    expect(got[0].content).toBe("好了");
  });

  it("lets go of a row the server has archived", () => {
    // the bug: a turn that failed and was replaced sat in the transcript
    // beside the picture that replaced it, because merging only ever added
    const got = mergeInto(
      [msg({ seq: 1 }), msg({ seq: 2, status: "error" })],
      [msg({ seq: 2, status: "error", archived: true }), msg({ seq: 3 })],
    );
    expect(got.map((m) => m.seq)).toEqual([1, 3]);
  });

  it("does not take in a row that arrives already archived", () => {
    const got = mergeInto([msg({ seq: 1 })], [msg({ seq: 2, archived: true })]);
    expect(got.map((m) => m.seq)).toEqual([1]);
  });

  it("keeps a local message the server has not acknowledged", () => {
    const local = {
      ...msg({ seq: 98, role: "user" }),
      id: "local-1",
      local: "sending",
      client_msg_id: "abc",
    } as Message;
    const got = mergeInto([local], [msg({ seq: 1 })]);
    expect(got.some((m) => m.id === "local-1")).toBe(true);
  });

  it("drops a local message once the server acknowledges it", () => {
    const local = {
      ...msg({ seq: 98, role: "user" }),
      id: "local-1",
      local: "sending",
      client_msg_id: "abc",
    } as Message;
    const real = msg({ seq: 2, role: "user", client_msg_id: "abc" });
    const got = mergeInto([local], [real]);
    expect(got.map((m) => m.id)).toEqual(["m2"]);
  });

  it("comes back in order whatever order it was given", () => {
    const got = mergeInto([msg({ seq: 3 })], [msg({ seq: 1 }), msg({ seq: 2 })]);
    expect(got.map((m) => m.seq)).toEqual([1, 2, 3]);
  });
});

describe("isWorking", () => {
  it("is true while a turn of its own is pending", () => {
    expect(isWorking([msg({ seq: 1, status: "pending" })], false, conv())).toBe(true);
  });

  it("is true while the server says so", () => {
    expect(isWorking([msg({ seq: 1 })], true, conv())).toBe(true);
  });

  it("is true between two turns of a run, when neither of the others is", () => {
    // the gap: the turn is done, the next is not queued yet, and the server
    // is in the few seconds of follow-up. Polling used to stop here and
    // nothing ever started it again.
    expect(
      isWorking([msg({ seq: 1 })], false, conv({ continuous: { left: 3, of: 4 } })),
    ).toBe(true);
  });

  it("is false with nothing pending and no run", () => {
    expect(isWorking([msg({ seq: 1 })], false, conv())).toBe(false);
  });

  it("is true while a message of yours is still going out", () => {
    const local = { ...msg({ seq: 9 }), local: "sending" } as Message;
    expect(isWorking([local], false, conv())).toBe(true);
  });

  it("survives having no conversation yet", () => {
    expect(isWorking([], false, undefined)).toBe(false);
  });
});
