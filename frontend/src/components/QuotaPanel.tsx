import { useEffect } from "react";

import { llmQuotaLine } from "../lib/quota";
import { useQuota } from "../store/quota";
import { t } from "../uiText";

export function QuotaPanel() {
  const quota = useQuota((s) => s.quota);
  const ensure = useQuota((s) => s.ensure);
  useEffect(() => ensure(), [ensure]);

  if (!quota) return null;

  const nai = quota.novelai;
  const pct = nai.ok && typeof nai.usage_percent === "number" ? nai.usage_percent : null;

  return (
    <div>
      {nai.ok ? (
        <>
          <div className="quota-line">
            <span>NovelAI {nai.tier}</span>
            <span>{pct !== null ? `${pct}%` : nai.active ? t.active : t.inactive}</span>
          </div>
          {pct !== null && (
            <div className="quota-bar" title={t.naiQuotaTitle}>
              <div className="quota-bar-fill" style={{ width: `${Math.min(pct, 100)}%` }} />
            </div>
          )}
        </>
      ) : (
        <div className="quota-line">
          <span>NovelAI</span>
          <span title={nai.error}>{t.statusUnknown}</span>
        </div>
      )}
      {(() => {
        const line = llmQuotaLine(quota.llm);
        return (
          <div className="quota-line">
            <span title={line.title}>{line.label}</span>
            <span>{line.value}</span>
          </div>
        );
      })()}
      <div className="quota-line">
        <span>{t.imagesGenerated}</span>
        <span>
          {quota.images.total}
          {quota.images.today > 0 ? t.todayCount(quota.images.today) : ""}
        </span>
      </div>
    </div>
  );
}
