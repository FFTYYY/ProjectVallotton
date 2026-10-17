import { useState } from "react";

import type { LibraryProposal, ProposedEntry } from "../api/types";
import { displayKey, joinTags, tagList, variantTitle } from "../lib/entries";
import { useProjects } from "../store/projects";

import { CharacterIcon, CheckIcon, SettingEntryIcon } from "./Icons";
import { t } from "../uiText";

/**
 * What the model wants written into the project's library, said in the
 * conversation.
 *
 * A proposal belongs to the turn that raised it — it knows its `message_id` —
 * so it is shown there, in the transcript, in sequence. It used to live in the
 * right rail, which is closed most of the time, so a proposal could be made
 * and never seen. It is shown only once the turn is done: reflection may
 * rewrite the entries each round, and a card for a draft would be answered
 * before the lead had finished.
 *
 * Answering it does not remove it. An answer is part of what happened in this
 * conversation, so the turn stays and says what was decided, the way the rest
 * of the transcript does.
 */

/**
 * An entry as the lead proposed it: the note, then the tags and prose the
 * drawing model will read. Shown so what is being approved is what will
 * actually be drawn from.
 */
function EntryNote({ entry }: { entry: ProposedEntry }) {
  const positive = tagList(entry.positive);
  const negative = tagList(entry.negative);
  const prose = entry.prose?.trim();
  return (
    <>
      {entry.note && <div className="proposal-entry-body">{entry.note}</div>}
      {(positive.length > 0 || negative.length > 0 || prose) && (
        <div className="proposal-fields">
          {positive.length > 0 && <code>+ {joinTags(positive)}</code>}
          {negative.length > 0 && <code>− {joinTags(negative)}</code>}
          {prose && <span>{prose}</span>}
        </div>
      )}
    </>
  );
}

export function ProposalTurn({
  convId,
  proposal,
}: {
  convId: string;
  proposal: LibraryProposal;
}) {
  const resolveProposal = useProjects((s) => s.resolveProposal);
  const [busy, setBusy] = useState(false);
  const p = proposal.payload ?? { settings: [], characters: [] };

  async function resolve(approve: boolean) {
    setBusy(true);
    try {
      await resolveProposal(convId, proposal.id, approve);
    } finally {
      setBusy(false);
    }
  }

  if (proposal.status !== "pending") {
    const applied = proposal.status === "applied";
    return (
      <div className={`proposal-turn resolved ${applied ? "applied" : "rejected"} fade-in`}>
        <div className="proposal-turn-head">
          {applied
            ? proposal.applied_summary || t.writtenToLibrary
            : t.notWritten}
        </div>
      </div>
    );
  }

  return (
    <div className="proposal-turn fade-in">
      <div className="proposal-turn-head">{t.proposalHead}</div>
      {(p.settings ?? []).map((s, i) => (
        <div className="proposal-entry" key={`s${i}`}>
          <div className="proposal-entry-head">
            <SettingEntryIcon size={13} />
            {t.settingEntry(displayKey(s.key))}
            {s.category && <span className="proposal-category">{s.category}</span>}
          </div>
          <EntryNote entry={s} />
        </div>
      ))}
      {(p.characters ?? []).map((c, i) => (
        <div className="proposal-entry" key={`c${i}`}>
          <div className="proposal-entry-head">
            <CharacterIcon size={13} />
            {t.characterEntry(displayKey(c.key))}
          </div>
          <EntryNote entry={c} />
          {(c.variants ?? []).map((v, j) => (
            <div className="proposal-variant" key={j}>
              <b>{variantTitle(v.key)}</b>
              <EntryNote entry={v} />
            </div>
          ))}
        </div>
      ))}
      {proposal.folder_id ? (
        <div className="proposal-actions">
          <button
            className="info-action-btn primary"
            disabled={busy}
            onClick={() => void resolve(true)}
          >
            <CheckIcon size={14} />
            {t.writeToLibrary}
          </button>
          <button className="info-action-btn" disabled={busy} onClick={() => void resolve(false)}>
            {t.notNow}
          </button>
        </div>
      ) : (
        <>
          {/* Said here rather than when the button is pressed: the answer is
              already known, so making you click to find out is just a slower
              way of telling you. */}
          <div className="proposal-note">
            {t.noProjectForLibrary}
          </div>
          <div className="proposal-actions">
            <button className="info-action-btn" disabled={busy} onClick={() => void resolve(false)}>
              {t.gotIt}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
