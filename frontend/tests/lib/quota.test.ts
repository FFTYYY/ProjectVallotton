import { describe, expect, it } from "vitest";

import type { QuotaStatus } from "../../src/api/types";
import { llmQuotaLine } from "../../src/lib/quota";

type Llm = QuotaStatus["llm"];

const base: Llm = {
  provider: "grok",
  label: "Grok",
  model: "grok-4.6",
  context_limit: 500_000,
  long_context_threshold: 200_000,
  today_requests: 0,
  total_requests: 0,
  priced: true,
  today_usd: 0,
  total_usd: 0,
  balance: null,
};

describe("llmQuotaLine", () => {
  it("prefers the account balance — it is the vendor's own number", () => {
    const line = llmQuotaLine({
      ...base,
      provider: "deepseek",
      label: "DeepSeek",
      model: "deepseek-flash",
      priced: false,
      balance: { ok: true, available: true, currency: "USD", balance: 62.16 },
    });
    expect(line.label).toBe("DeepSeek余额");
    expect(line.value).toBe("$62.16");
    expect(line.title).toBe("deepseek-flash");
  });

  it("falls back to what we summed for a vendor that prices its responses", () => {
    const line = llmQuotaLine({ ...base, total_usd: 4.8538, today_usd: 4.0031 });
    expect(line.label).toBe("Grok累计");
    expect(line.value).toBe("$4.85（今日$4.00）");
  });

  it("leaves today off when nothing was spent today", () => {
    expect(llmQuotaLine({ ...base, total_usd: 1.2 }).value).toBe("$1.20");
  });

  it("a balance still wins over a priced vendor's tally", () => {
    const line = llmQuotaLine({
      ...base,
      total_usd: 9.99,
      balance: { ok: true, currency: "USD", balance: 3 },
    });
    expect(line.value).toBe("$3.00");
  });

  it("counts requests rather than inventing a $0.00", () => {
    const line = llmQuotaLine({
      ...base,
      label: "DeepSeek",
      priced: false,
      total_requests: 182,
      today_requests: 118,
      balance: { ok: false, error: "HTTP 401" },
    });
    expect(line.label).toBe("DeepSeek调用");
    expect(line.value).toBe("182（今日118）");
    // the reason the figure is missing belongs in the tooltip, not on screen
    expect(line.title).toContain("HTTP 401");
  });

  it("a failed balance lookup does not erase a priced vendor's tally", () => {
    const line = llmQuotaLine({
      ...base,
      total_usd: 2.5,
      balance: { ok: false, error: "timeout" },
    });
    expect(line.value).toBe("$2.50");
  });
});
