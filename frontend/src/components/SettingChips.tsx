import { usePhrases } from "../store/phrases";
import { useChat } from "../store/chat";
import { useGen } from "../store/settings";
import { useUi } from "../store/ui";

import { PinIcon, ReflectIcon, RepeatIcon } from "./Icons";
import { t } from "../uiText";

/**
 * What this turn will do differently, above the composer.
 *
 * These are settings rather than content — they say the turn will behave
 * differently, not what it will carry — so they are brass rather than the
 * library's blue, and they belong anywhere the composer does. That includes
 * the new-chat page: the sliders apply to the first message of a conversation
 * exactly as much as to the fortieth, and a chip that appeared only after the
 * conversation existed would be telling you a moment too late.
 *
 * `convId` is optional for that reason — it is only needed to count the
 * reference pictures a sticky run is carrying.
 *
 * The continuous chip says what the NEXT send will ask for, not how far the
 * run on the server has got. Those are different numbers on purpose: moving
 * the slider while a run is in flight leaves that run alone — it carries the
 * settings it was armed with — so a chip counting the run would sit there
 * contradicting the slider directly above it.
 */
/**
 * Which chips this turn earns — one answer, read by the row and by the caller
 * that decides whether to draw the row at all. Two copies of this rule drifted
 * apart once already.
 *
 * Whether a run can run is "is there anything to send again", and that is the
 * server's rule too: the chosen phrase is a preference, and with none chosen —
 * or one that has since been deleted — the first phrase is what runs. See
 * continuous._instruction. Asking for an exact match here said "no run" while
 * the server would happily have run one.
 */
function useChips() {
  const { reflectionRounds, continuousRounds, stickyRefs } = useGen();
  const hasPhrase = usePhrases((s) => s.phrases.length > 0);
  return {
    sticky: stickyRefs,
    reflection: reflectionRounds,
    continuous: continuousRounds > 1 && hasPhrase ? continuousRounds : 0,
  };
}

export function SettingChips({ convId }: { convId?: string }) {
  const setRightOpen = useUi((s) => s.setRightOpen);
  const { sticky: stickyRefs, reflection: reflectionRounds, continuous } = useChips();
  const pinned = useChat((s) =>
    convId
      ? (s.messages[convId] ?? []).reduce(
          (n, m) => n + m.attachments.filter((a) => a.kind === "upload").length,
          0,
        )
      : 0,
  );


  return (
    <>
      {stickyRefs && (
        <button className="selection-chip sticky" onClick={() => setRightOpen(true)}>
          <PinIcon size={12} />
          {t.stickyRefsChip(pinned)}
        </button>
      )}
      {continuous > 0 && (
        <button className="selection-chip continuous" onClick={() => setRightOpen(true)}>
          <RepeatIcon size={12} />
          {t.continuousChip(continuous)}
        </button>
      )}
      {reflectionRounds > 0 && (
        <button className="selection-chip reflection" onClick={() => setRightOpen(true)}>
          <ReflectIcon size={12} />
          {t.reflectionChip(reflectionRounds)}
        </button>
      )}
    </>
  );
}

/** Whether any of them would render, so a caller can skip the empty row. */
export function useHasSettingChips(): boolean {
  const { sticky, reflection, continuous } = useChips();
  return sticky || reflection > 0 || continuous > 0;
}
