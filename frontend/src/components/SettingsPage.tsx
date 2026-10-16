import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useLanguage } from "../i18n/language";
import { LANGUAGES } from "../i18n/languages";
import { confirmLogout } from "../lib/logout";
import { llmQuotaLine } from "../lib/quota";
import { useAppSettings } from "../store/appSettings";
import { useAuth } from "../store/auth";
import { usePhrases } from "../store/phrases";
import { useQuota } from "../store/quota";
import { GenerationControls } from "./GenerationControls";
import { NewPhraseButton, PhraseList } from "./PhraseList";
import { THEMES, useTheme } from "../theme/theme";
import { ActivityIcon, ChevronLeftIcon, LogOutIcon } from "./Icons";
import { t } from "../uiText";

/**
 * Settings: what a new conversation starts from, and how things stand.
 *
 * A page rather than a panel because none of it is about the conversation in
 * front of you. The right rail sets this conversation's generation settings;
 * these are the shape a new one is poured into, so changing them here never
 * reaches back into work that already exists.
 *
 * It also carries the account and the balances. On a phone those used to sit
 * at the foot of the drawer, taking a strip of a screen that has none to spare
 * for something read once a week.
 */
export function SettingsPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const defaults = useAppSettings((s) => s.defaults);
  const apply = useAppSettings((s) => s.apply);
  const running = useAppSettings((s) => s.running);
  const load = useAppSettings((s) => s.load);
  const loadPhrases = usePhrases((s) => s.load);

  useEffect(() => {
    void load();
    void loadPhrases();
  }, [load, loadPhrases]);

  return (
    <div className="settings-page">
      <div className="settings-inner">
        {/* Back to wherever you came from — a conversation, a project — rather
            than to a fixed place: this is a detour, not a destination. On a
            first entry there is nowhere behind it (a bookmark, a reload), and
            react-router calls that entry "default", so it falls back home. */}
        <button
          className="settings-back"
          onClick={() => (location.key === "default" ? navigate("/") : navigate(-1))}
        >
          <ChevronLeftIcon size={15} />
          {t.back}
        </button>
        <h1 className="settings-title">{t.settings}</h1>

        <LanguageThemeSection />

        <section className="settings-section">
          <div className="settings-section-title">{t.defaultGenSettings}</div>
          {/* the same four controls the right rail uses, so the thing you set
              here and the thing you adjust there cannot drift apart */}
          <GenerationControls values={defaults} onChange={apply} />
        </section>

        <PhrasesSection />
        <RoadSection />
        <RunningSection running={running} />
        <BalanceSection />
        <AccountSection onDone={() => navigate("/")} />
      </div>
    </div>
  );
}

/**
 * The language the app speaks and the colours it wears.
 *
 * First on the page, and the only place a phone has them: the desktop rail
 * carries the same two switches beside the username. The language is the
 * account's and goes to the server; the theme is this browser's and stays in
 * it. Both are segmented controls over their registries, so a new language
 * or theme appears here on its own.
 */
function LanguageThemeSection() {
  const language = useLanguage((s) => s.language);
  const chooseLanguage = useAppSettings((s) => s.chooseLanguage);
  const theme = useTheme((s) => s.theme);
  const setTheme = useTheme((s) => s.set);
  return (
    <section className="settings-section">
      <div className="settings-section-title">{t.languageAndTheme}</div>
      <div className="settings-row">
        <span className="settings-row-label">{t.language}</span>
        <div className="seg" role="radiogroup" aria-label={t.language}>
          {LANGUAGES.map((l) => (
            <button
              key={l.code}
              role="radio"
              aria-checked={l.code === language}
              className={`seg-item wide ${l.code === language ? "active" : ""}`}
              onClick={() => chooseLanguage(l.code)}
            >
              {l.label}
            </button>
          ))}
        </div>
      </div>
      <div className="settings-row">
        <span className="settings-row-label">{t.theme}</span>
        <div className="seg" role="radiogroup" aria-label={t.theme}>
          {THEMES.map((th) => (
            <button
              key={th.id}
              role="radio"
              aria-checked={th.id === theme}
              className={`seg-item wide ${th.id === theme ? "active" : ""}`}
              onClick={() => setTheme(th.id)}
            >
              {t.themeNames[th.id]}
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}

/**
 * The ways in, and which one the backend is using.
 *
 * Only shown when there is more than one, so an install with a single endpoint
 * never sees a control it cannot use.
 *
 * The button reports; it does not choose. There is one authority on which
 * roads work — what the backend last found out — and this writes to exactly
 * that, setting the state a request timing out would have set. The probe still
 * runs, and the cheaper road still comes back the moment it answers, whatever
 * was clicked here.
 *
 * What it buys is who noticed. Finding an outage the automatic way costs a
 * full request timeout, and whoever is watching 「思考中」 usually knows first.
 */
function RoadSection() {
  const roads = useAppSettings((s) => s.roads);
  const reportRoad = useAppSettings((s) => s.reportRoad);
  const [busy, setBusy] = useState("");

  if (roads.length < 2) return null;

  const report = (name: string, steppedOver: boolean) => {
    if (busy) return;
    setBusy(name);
    void reportRoad(name, steppedOver).finally(() => setBusy(""));
  };

  return (
    <section className="settings-section">
      <div className="settings-section-title">{t.routes}</div>
      {roads.map((r) => (
        <div className="settings-row" key={r.name}>
          <span className="settings-row-label">{r.label}</span>
          <span className={`road-state ${r.stepped_over ? "out" : r.in_use ? "on" : ""}`}>
            {r.stepped_over ? t.steppedOver : r.in_use ? t.inUse : t.standby}
          </span>
          <button
            className="road-report"
            disabled={!!busy}
            onClick={() => report(r.name, !r.stepped_over)}
          >
            {r.stepped_over ? t.restore : t.reportUnavailable}
          </button>
        </div>
      ))}
    </section>
  );
}

/**
 * The quick phrases, and which one a new conversation sends again.
 *
 * The same list the right rail keeps — it is one set of phrases, not two — but
 * the radio here picks the DEFAULT: what a conversation made from now on will
 * send when it is asked to keep going. An existing conversation's own choice
 * is in the rail, and changing this never reaches back into it.
 */
function PhrasesSection() {
  const defaults = useAppSettings((s) => s.defaults);
  const apply = useAppSettings((s) => s.apply);
  const [creating, setCreating] = useState(false);

  return (
    <section className="settings-section">
      <div className="settings-section-title">
        {t.quickPhrases}
        <NewPhraseButton onToggle={() => setCreating((v) => !v)} />
      </div>
      <PhraseList
        chosenId={defaults.continuousPhraseId}
        onChoose={(id) => apply({ continuousPhraseId: id })}
        chooseHint={t.newConversationPhraseHint}
        creating={creating}
        onCreatingChange={setCreating}
        sortable
      />
    </section>
  );
}

function RunningSection({
  running,
}: {
  running: { turns: number; conversations: number; runs: number };
}) {
  const idle = running.turns === 0 && running.runs === 0;
  return (
    <section className="settings-section">
      <div className="settings-section-title">{t.inProgress}</div>
      {idle ? (
        <div className="settings-row">
          <span className="settings-row-label">
            <ActivityIcon size={14} />
            {t.nothingRunning}
          </span>
        </div>
      ) : (
        <>
          <div className="settings-row">
            <span className="settings-row-label">
              <ActivityIcon size={14} />
              {t.generating}
            </span>
            <span className="settings-row-value on">
              {t.turnsCount(running.turns)}
              {running.conversations > 1 ? t.conversationsSuffix(running.conversations) : ""}
            </span>
          </div>
          {running.runs > 0 && (
            <div className="settings-row">
              <span className="settings-row-label">{t.continuous}</span>
              <span className="settings-row-value on">{t.runsInProgress(running.runs)}</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function BalanceSection() {
  const quota = useQuota((s) => s.quota);
  const ensure = useQuota((s) => s.ensure);
  useEffect(() => ensure(), [ensure]);

  const llm = useMemo(() => (quota ? llmQuotaLine(quota.llm) : null), [quota]);
  if (!quota) return null;
  const nai = quota.novelai;
  const pct = nai.ok && typeof nai.usage_percent === "number" ? nai.usage_percent : null;

  return (
    <section className="settings-section">
      <div className="settings-section-title">{t.balance}</div>
      <div className="settings-row">
        <span className="settings-row-label">NovelAI{nai.ok ? ` ${nai.tier}` : ""}</span>
        <span className="settings-row-value" title={nai.ok ? undefined : nai.error}>
          {nai.ok
            ? pct !== null
              ? `${pct}%`
              : nai.active
                ? t.active
                : t.inactive
            : t.statusUnknown}
        </span>
      </div>
      {pct !== null && (
        <div className="quota-bar" title={t.naiQuotaTitle}>
          <div className="quota-bar-fill" style={{ width: `${Math.min(pct, 100)}%` }} />
        </div>
      )}
      {llm && (
        <div className="settings-row">
          <span className="settings-row-label" title={llm.title}>
            {llm.label}
          </span>
          <span className="settings-row-value">{llm.value}</span>
        </div>
      )}
    </section>
  );
}

function AccountSection({ onDone }: { onDone: () => void }) {
  const username = useAuth((s) => s.username);
  const logout = useAuth((s) => s.logout);
  return (
    <section className="settings-section">
      <div className="settings-section-title">{t.account}</div>
      <div className="settings-row">
        <span className="settings-row-label">{username || t.loggedIn}</span>
        <button
          className="settings-danger"
          onClick={async () => {
            if (!(await confirmLogout())) return;
            await logout();
            onDone();
          }}
        >
          <LogOutIcon size={14} />
          {t.logout}
        </button>
      </div>
    </section>
  );
}
