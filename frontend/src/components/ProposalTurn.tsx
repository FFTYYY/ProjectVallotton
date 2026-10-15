import { useState } from "react";

import type { LibraryProposal, TagLine } from "../api/types";
import { useProjects } from "../store/projects";

import { CharacterIcon, CheckIcon, SettingEntryIcon } from "./Icons";
import { t } from "../uiText";

/**
 * What the model wants written into the library, said in the conversation.
 *
 * A proposal belongs to the turn that raised it — it knows its `message_id` —
 * so it is shown there, in the transcript, in sequence. It used to live in the
 * right rail, which is closed most of the time, so a proposal could be made
 * and never seen.
 *
 * Answering it does not remove it. An answer is part of what happened in this
 * conversation, so the turn stays and says what was decided, the way the rest
 * of the transcript does.
 */
/**
 * An entry's tag lines, as the model proposed them: one line per thing, the
 * words the app will splice into prompts byte for byte. Shown so what is
 * being approved is what will actually be drawn from.
 */
function TagLinesNote({ lines }: { lines?: TagLine[] }) {
  if (!lines?.length) return null;
  return (
    <div className="proposal-lines">
      {lines.map((ln, i) => (
        <div key={i}>
          <span className="proposal-line-part">{ln.part}</span>
          <code>{ln.tags}</code>
        </div>
      ))}
    </div>
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
            {t.settingEntry(s.title)}
            {s.scope === "conversation" && <span className="sel-layer">{t.thisConversation}</span>}
          </div>
          <div className="proposal-entry-body">{s.content}</div>
          <TagLinesNote lines={s.tags} />
        </div>
      ))}
      {(p.characters ?? []).map((c, i) => (
        <div className="proposal-entry" key={`c${i}`}>
          <div className="proposal-entry-head">
            <CharacterIcon size={13} />
            {t.characterEntry(c.name)}
            {c.scope === "conversation" && <span className="sel-layer">{t.thisConversation}</span>}
          </div>
          {c.description && <div className="proposal-entry-body">{c.description}</div>}
          <TagLinesNote lines={c.tags} />
          {(c.variants ?? []).map((v, j) => (
            <div className="proposal-variant" key={j}>
              <b>{v.title}</b>
              {v.content}
              <TagLinesNote lines={v.tags} />
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
