import { useEffect } from "react";

import { usePhrases } from "../store/phrases";
import {
  MAX_CONTINUOUS_ROUNDS,
  MAX_REFLECTION_ROUNDS,
} from "../store/settings";
import { useDragOnlyOnTouch } from "../lib/dragSlider";
import {
  AUTO_LIBRARY,
  autoLibraryLabel,
  QUALITIES,
  qualityLabel,
  type AutoLibrary,
  type Quality,
} from "../lib/genSettings";
import { t } from "../uiText";

/**
 * The generation controls, wherever they are being set.
 *
 * Two places set them and they must not drift: the right rail sets this
 * conversation's, and the settings page sets the defaults a new conversation
 * is poured from. Same markup, same ceilings, same touch behaviour — a phone
 * moves these by dragging only, and a settings page that forgot that would be
 * the one place a stray tap still costs you.
 *
 * Deliberately unaware of where the values live. It is handed four numbers and
 * a way to change one; whether that ends up on a conversation or on the app is
 * the caller's business.
 */

export interface GenValues {
  nImages: number;
  quality: Quality;
  autoLibrary: AutoLibrary;
  reflectionRounds: number;
  continuousRounds: number;
  continuousPhraseId: string | null;
  stickyRefs: boolean;
}

interface Props {
  values: GenValues;
  onChange: (patch: Partial<GenValues>) => void;
  /** the phrase picker only makes sense where a run can actually be sent */
  showPhrases?: boolean;
}

export function GenerationControls({ values, onChange, showPhrases = true }: Props) {
  return (
    <>
      <ImageCount value={values.nImages} onChange={(n) => onChange({ nImages: n })} />
      <QualityTier
        value={values.quality}
        onChange={(q) => onChange({ quality: q })}
      />
      <ReflectionRounds
        value={values.reflectionRounds}
        onChange={(n) => onChange({ reflectionRounds: n })}
      />
      <ContinuousRounds
        value={values.continuousRounds}
        phraseId={values.continuousPhraseId}
        onChange={onChange}
        showPhrases={showPhrases}
      />
      <AutoLibraryMode
        value={values.autoLibrary}
        onChange={(m) => onChange({ autoLibrary: m })}
      />
      <StickyRefs
        value={values.stickyRefs}
        onChange={(on) => onChange({ stickyRefs: on })}
      />
    </>
  );
}

function ImageCount({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <div className="setting-row">
      <div className="setting-label">{t.nImages}</div>
      <div className="seg">
        {[1, 2, 3, 4].map((n) => (
          <button
            key={n}
            className={`seg-item ${value === n ? "active" : ""}`}
            onClick={() => onChange(n)}
          >
            {n}
          </button>
        ))}
      </div>
    </div>
  );
}

function AutoLibraryMode({
  value,
  onChange,
}: {
  value: AutoLibrary;
  onChange: (m: AutoLibrary) => void;
}) {
  return (
    <div className="setting-row">
      <div className="setting-label">{t.autoLibrary}</div>
      <div className="seg">
        {AUTO_LIBRARY.map((m) => (
          <button
            key={m}
            className={`seg-item wide ${value === m ? "active" : ""}`}
            onClick={() => onChange(m)}
          >
            {autoLibraryLabel(m)}
          </button>
        ))}
      </div>
    </div>
  );
}

function QualityTier({
  value,
  onChange,
}: {
  value: Quality;
  onChange: (q: Quality) => void;
}) {
  return (
    <div className="setting-row">
      <div className="setting-label">{t.quality}</div>
      <div className="seg">
        {QUALITIES.map((q) => (
          <button
            key={q}
            className={`seg-item ${value === q ? "active" : ""}`}
            onClick={() => onChange(q)}
          >
            {qualityLabel(q)}
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * How many times the picture gets a second look.
 *
 * A slider rather than a switch and a number, because off and "once" and
 * "five times" are the same decision made at different strengths — and 0
 * sitting at the left end says what off costs you in the same gesture.
 */
function ReflectionRounds({
  value,
  onChange,
}: {
  value: number;
  onChange: (n: number) => void;
}) {
  const touch = useDragOnlyOnTouch({
    min: 0,
    max: MAX_REFLECTION_ROUNDS,
    step: 1,
    onChange,
  });
  return (
    <div className="setting-row">
      <div className="setting-label">{t.reflectionRounds}</div>
      <div className="slider-group" {...touch}>
        <input
          type="range"
          className="slider"
          min={0}
          max={MAX_REFLECTION_ROUNDS}
          step={1}
          value={value}
          aria-label={t.reflectionRounds}
          onChange={(e) => onChange(Number(e.target.value))}
        />
        <span className={`setting-value ${value > 0 ? "on" : ""}`}>
          {value === 0 ? t.off : value}
        </span>
      </div>
    </div>
  );
}

/**
 * How many pictures one instruction turns into.
 *
 * Nothing about this reaches a model: when a turn finishes the app types the
 * chosen quick phrase again, and the model reads it exactly as if it had been
 * typed. Which is why it needs a phrase to type — with none defined the
 * control has nothing to send and says so by being unavailable.
 */
function ContinuousRounds({
  value,
  phraseId,
  onChange,
  showPhrases,
}: {
  value: number;
  phraseId: string | null;
  onChange: (patch: Partial<GenValues>) => void;
  showPhrases: boolean;
}) {
  const phrases = usePhrases((s) => s.phrases);
  const usable = phrases.length > 0;
  const chosen = phrases.find((p) => p.id === phraseId);
  const touch = useDragOnlyOnTouch({
    min: 1,
    max: MAX_CONTINUOUS_ROUNDS,
    step: 1,
    onChange: (n) => onChange({ continuousRounds: n }),
  });

  // Turning it on with nothing chosen would arm a run with no message to send.
  // Falling back to the first phrase is the only answer that is never wrong by
  // more than one click, and the radio shows which one it picked.
  useEffect(() => {
    if (showPhrases && value > 1 && usable && !chosen) {
      onChange({ continuousPhraseId: phrases[0].id });
    }
  }, [showPhrases, value, usable, chosen, phrases, onChange]);

  return (
    <div className={`setting-row ${usable || !showPhrases ? "" : "unavailable"}`}>
      <div className="setting-label">{t.continuous}</div>
      <div className="slider-group" {...touch}>
        <input
          type="range"
          className="slider"
          min={1}
          max={MAX_CONTINUOUS_ROUNDS}
          step={1}
          value={value}
          disabled={showPhrases && !usable}
          aria-label={t.continuousCount}
          onChange={(e) => onChange({ continuousRounds: Number(e.target.value) })}
        />
        <span className={`setting-value ${value > 1 ? "on" : ""}`}>{value}</span>
      </div>
    </div>
  );
}

/**
 * Keep every reference picture in view, instead of only the newest turn's.
 *
 * A switch rather than a number, because it is one: either the pictures you
 * gave stay in front of her or they scroll out of sight like everything else.
 * Nothing is copied when it is on — the same picture is simply carried for
 * longer — so turning it off is instantly back to normal and no picture is
 * ever in the context twice.
 */
function StickyRefs({
  value,
  onChange,
}: {
  value: boolean;
  onChange: (on: boolean) => void;
}) {
  return (
    <div className="setting-row">
      <div className="setting-label">{t.stickyRefs}</div>
      <button
        role="switch"
        aria-checked={value}
        aria-label={t.stickyRefs}
        className={`toggle ${value ? "on" : ""}`}
        onClick={() => onChange(!value)}
      >
        <span />
      </button>
    </div>
  );
}
