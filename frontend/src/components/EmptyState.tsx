import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useChat } from "../store/chat";
import { useAppSettings } from "../store/appSettings";
import { useSettings } from "../store/settings";
import { Composer } from "./Composer";
import { RightSidebar } from "./RightSidebar";
import { SettingChips, useHasSettingChips } from "./SettingChips";
import { t } from "../uiText";

export function EmptyState() {
  const createConversation = useChat((s) => s.createConversation);
  const sendMessage = useChat((s) => s.sendMessage);
  const navigate = useNavigate();
  const [preset, setPreset] = useState("");
  const [creating, setCreating] = useState(false);
  // the sliders apply to a conversation's first message as much as its
  // fortieth, so they are worth saying before there is a conversation at all
  const hasChips = useHasSettingChips();
  const pointSettings = useSettings((s) => s.point);
  const loadDefaults = useAppSettings((s) => s.load);

  // A new conversation starts at the defaults from the settings page rather
  // than inheriting whatever the last conversation was set to. Pointing the
  // controls at "no conversation" IS showing the defaults — they are read
  // straight from the settings store, so this needs no second look when they
  // arrive.
  useEffect(() => {
    void loadDefaults();
  }, [loadDefaults]);
  useEffect(() => {
    pointSettings(null);
  }, [pointSettings]);

  async function onSend(text: string, files: File[]) {
    if (creating) return;
    setCreating(true);
    try {
      const conv = await createConversation();
      navigate(`/c/${conv.id}`);
      await sendMessage(conv.id, text, files);
    } finally {
      setCreating(false);
    }
  }

  return (
    <>
      <div className="empty-state">
        <img className="empty-logo" src="/icons/icon-512.png" alt="Vallotton" />
        <h1 className="empty-title">Vallotton</h1>
        <p className="empty-sub">
          {t.emptyStateLede}
        </p>
        <div className="suggestions">
          {t.suggestions.map((s) => (
            <button key={s} className="suggestion-chip" onClick={() => setPreset(s)}>
              {s}
            </button>
          ))}
        </div>
      </div>
      <RightSidebar />
      <Composer
        busy={creating}
        onSend={onSend}
        autoFocus
        presetText={preset}
        onPresetConsumed={() => setPreset("")}
        chips={
          hasChips ? (
            <div className="selection-chips">
              <SettingChips />
            </div>
          ) : null
        }
      />
    </>
  );
}
