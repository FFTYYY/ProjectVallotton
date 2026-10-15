import type { QuotaStatus } from "../api/types";
import { t } from "../uiText";

/**
 * What the model line in the sidebar should say.
 *
 * Vendors report different things and the panel has to be honest about which
 * one it got. A balance is the better number when it exists — it is the
 * account's own truth rather than our tally — so it wins. Failing that, the
 * spend we summed ourselves. A vendor that offers neither gets a request
 * count, because a confident "$0.00" would be a lie.
 *
 * Separate from the component so the decision can be tested without a DOM.
 */
export interface QuotaLine {
  label: string;
  value: string;
  /** the tooltip: which model, or why there is no figure */
  title: string;
}

export function llmQuotaLine(llm: QuotaStatus["llm"]): QuotaLine {
  const bal = llm.balance;
  if (bal?.ok && typeof bal.balance === "number") {
    return {
      label: t.balanceLabel(llm.label),
      value: `$${bal.balance.toFixed(2)}`,
      title: llm.model,
    };
  }
  if (llm.priced) {
    const today = llm.today_usd > 0 ? t.todayUsd(llm.today_usd.toFixed(2)) : "";
    return {
      label: t.totalLabel(llm.label),
      value: `$${llm.total_usd.toFixed(2)}${today}`,
      title: llm.model,
    };
  }
  const today = llm.today_requests > 0 ? t.todayCount(llm.today_requests) : "";
  return {
    label: t.callsLabel(llm.label),
    value: `${llm.total_requests}${today}`,
    title: bal?.error ? `${llm.model} · ${bal.error}` : llm.model,
  };
}
