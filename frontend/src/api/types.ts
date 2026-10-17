export interface Attachment {
  id: string;
  /** the turn it belongs to */
  message_id: string;
  kind: "upload" | "generated";
  /** The picture itself. What the lightbox opens. */
  url: string;
  /**
   * A small JPEG of the same picture. What anything that draws it small should
   * ask for: `url` is 1.3 MB at p95 and a grid draws it at 410px at the widest.
   */
  thumb_url: string;
  mime: string;
  width: number | null;
  height: number | null;
  size_bytes: number;
  seed: number | null;
  position: number;
}

export interface CharacterPrompt {
  /** the character entry this prompt was assembled for */
  key: string;
  prompt: string;
  /** this character's own negative: its entry's and its outfits' */
  negative: string;
  center_x: number | null;
  center_y: number | null;
}

export interface NaiPrompts {
  n_images: number;
  size: "portrait" | "landscape" | "square";
  /** names this whole drawing, so it can be referred to later by name */
  draw_id?: string;
  base_prompt: string;
  character_prompts: CharacterPrompt[];
  negative_prompt: string;
}

/**
 * What the lead model asked for: the description the drawing model is handed,
 * the entries it exposes, and the drawing this one edits (`inherits`, by id;
 * empty for a new picture).
 */
export interface DrawBrief {
  description: string;
  expose: string[];
  inherits: string;
  text: string;
  size: string;
}

/** which library an entry lives in: the project's, or this conversation's own */
export type EntryLayer = "conversation" | "project";
export type EntryKind = "setting" | "character" | "variant";

/**
 * One exposed entry as the drawing model read it, copied at the time: later
 * edits to the library must not rewrite what an old picture says it came
 * from. A variant's key is `character/outfit` and names its character.
 */
export interface SnapshotEntry {
  id: string;
  kind: EntryKind;
  key: string;
  layer: EntryLayer;
  /** the key as the lead writes it: project entries carry `@` */
  shown_key: string;
  note: string;
  positive: string[];
  negative: string[];
  prose: string;
  /** the lead message that wrote it; null for one the user made */
  origin_seq: number | null;
  category?: string;
  character?: string;
  /** a project entry exposed in the form the lead proposed this turn */
  edited_this_turn?: boolean;
}

/** what a picture exposed, in the lead's order, and the keys to inherit */
export interface SelectionSnapshot {
  entries: SnapshotEntry[];
  keys: string[];
}

/** an exposed entry and what the drawing model made of it for this picture */
export interface UsedEntry {
  key: string;
  kind: EntryKind;
  /** false when the drawing model left it out of this picture */
  used: boolean;
  positive: string[];
  negative: string[];
  prose: string;
}

export type MessageStatus = "done" | "pending" | "error" | "cancelled";

export interface Message {
  id: string;
  conversation_id: string;
  seq: number;
  /** "system" rows are notes the app writes into the transcript */
  role: "user" | "assistant" | "system";
  status: MessageStatus;
  kind: "text" | "image";
  phase: string;
  content: string;
  error: string;
  client_msg_id: string;
  nai_prompts: NaiPrompts | null;
  draw_brief: DrawBrief | null;
  /** which of this turn's pictures is the main one, by position */
  main_index: number;
  selection_snapshot: SelectionSnapshot | null;
  /** what the picture actually took from the library: each exposed entry and what it came to */
  used_entries?: UsedEntry[];
  /** pictures of this batch that failed to paint, one line each */
  paint_errors?: string[];
  /** assistant rows: prompt tokens this turn consumed (context usage) */
  prompt_tokens: number | null;
  /** what reflection did on this turn; shown under it, never sent to a model */
  reflection_log: ReflectionRound[] | null;
  /** set when the app sent this user message itself, in continuous mode */
  auto_sent: { round: number; of: number } | null;
  /** system rows: which app note this is */
  notice_kind: string;
  /** the server has taken this row out of the transcript */
  archived?: boolean;
  created_at: string;
  attachments: Attachment[];
  /** local-only: message not yet accepted by the server */
  local?: "sending" | "failed";
  localFiles?: File[];
  localText?: string;
}

/** 出图数量 / 反思轮数 / 连续生成 / 常驻参考, as this conversation is set. */
export interface GenSettings {
  n_images: number;
  reflection_rounds: number;
  continuous_rounds: number;
  continuous_phrase_id: string | null;
  sticky_refs: boolean;
  quality: string;
  auto_library: string;
}

export interface Conversation {
  /** a continuous run in flight on the server, or null. Read-only here. */
  continuous?: { left: number; of: number } | null;
  /** always complete and in range, even for conversations older than it */
  gen_settings?: GenSettings;
  id: string;
  title: string;
  source: "web" | "mcp";
  is_archived: boolean;
  folder_id: string | null;
  /** every folder name from the root down, for the header breadcrumb */
  folder_path: string[];
  position: number;
  created_at: string;
  updated_at: string;
  preview?: string;
}

export interface ConversationsResponse {
  conversations: Conversation[];
  total: number;
  has_more: boolean;
}

/* ------------------------------------------------------------- projects */

export interface Folder {
  id: string;
  name: string;
  parent_id: string | null;
  position: number;
  is_archived: boolean;
  /** only present on single-folder responses */
  path?: string[];
  /** only in the archive listing */
  updated_at?: string;
}

/** what the 已归档 tab shows: both kinds, together, newest first */
export interface ArchivePayload {
  folders: Folder[];
  conversations: Conversation[];
}

/** the trimmed conversation the projects page draws (title only) */
export interface TreeConversation {
  id: string;
  title: string;
  source: "web" | "mcp";
  folder_id: string | null;
  position: number;
}

export interface ProjectTree {
  folders: Folder[];
  conversations: TreeConversation[];
}

/* ------------------------------------------------------------- library */

/**
 * What every entry carries besides its note: the tags, the prose, and where
 * it came from. The note is the person's words; the tags and prose are what
 * the drawing model reads, written by the lead or by the patrol from the note.
 */
export interface EntryFields {
  positive: string[];
  negative: string[];
  prose: string;
  /** the lead message that wrote it; null for one the user made */
  origin_seq: number | null;
  /** the note was edited and the tags have not been rewritten from it yet */
  lines_stale?: boolean;
}

/** what a PATCH of an entry may carry; tags go as lists */
export interface EntryPatch {
  title?: string;
  name?: string;
  content?: string;
  description?: string;
  category?: string;
  positive?: string[];
  negative?: string[];
  prose?: string;
}

export interface Variant extends EntryFields {
  id: string;
  title: string;
  content: string;
  position: number;
  /** only in selection payloads */
  selected?: boolean;
}

export interface Character extends EntryFields {
  id: string;
  folder_id: string | null;
  /** set on an entry that belongs to one conversation and shadows the project's */
  conversation_id: string | null;
  name: string;
  description: string;
  position: number;
  variants: Variant[];
  /** only in selection payloads: true when at least one variant is on */
  selected?: boolean;
}

export interface Setting extends EntryFields {
  id: string;
  folder_id: string | null;
  conversation_id: string | null;
  title: string;
  content: string;
  /** what kind of setting: style, camera, scene… as the lead named it */
  category: string;
  position: number;
  selected?: boolean;
}

export interface FolderLibrary {
  folder: Folder;
  settings: Setting[];
  characters: Character[];
  /** entries this folder inherits from its ancestors — read-only here */
  inherited: { settings: Setting[]; characters: Character[] };
}

/** what a conversation may draw on, and what of it is switched on */
export interface SelectionPayload {
  settings: Setting[];
  characters: Character[];
}

/* ------------------------------------------------------------- proposals */

/**
 * An entry as the lead proposed it for the project. The tags come as the
 * lead wrote them — a comma-separated string or a list; lib/entries.tagList
 * reads either.
 */
export interface ProposedEntry {
  key: string;
  note?: string;
  positive?: string | string[];
  negative?: string | string[];
  prose?: string;
}

export interface ProposedSetting extends ProposedEntry {
  category?: string;
}

export interface ProposedCharacter extends ProposedEntry {
  /** an outfit's key is its title, or `character/title` */
  variants?: ProposedEntry[];
}

export interface ProposalPayload {
  settings: ProposedSetting[];
  characters: ProposedCharacter[];
}

/**
 * One entry in a turn's reflection log — either a look at the picture, or an
 * attempt at redrawing it that failed. Both carry a round number; only one of
 * them has anything to say.
 */
export type ReflectionRound =
  | { round: number; reply: string; redrew: boolean }
  | { round: number; failed: string; paid: boolean; gave_up?: boolean };

export function isLook(
  r: ReflectionRound,
): r is { round: number; reply: string; redrew: boolean } {
  return "reply" in r;
}

export interface LibraryProposal {
  id: string;
  message_id: string;
  status: "pending" | "applied" | "rejected";
  payload: ProposalPayload;
  /** the folder it would be written into; null means the conversation has none */
  folder_id: string | null;
  applied_summary: string;
  created_at: string;
}

/* ------------------------------------------------------------- misc */

export interface QuotaStatus {
  novelai: {
    ok: boolean;
    tier?: string;
    active?: boolean;
    expires_at?: number;
    usage_percent?: number | null;
    seconds_until_next_percent?: number | null;
    error?: string;
  };
  /**
   * Whatever the configured provider can actually report. Vendors differ:
   * some price every response (priced), some only expose an account balance.
   * The panel shows what exists rather than assuming one shape.
   */
  llm: {
    provider: string;
    label: string;
    model: string;
    /** the active provider's window, and the mark past which it is "long" */
    context_limit: number;
    long_context_threshold: number;
    today_requests: number;
    total_requests: number;
    priced: boolean;
    today_usd: number;
    total_usd: number;
    balance: {
      ok: boolean;
      available?: boolean;
      currency?: string;
      balance?: number;
      error?: string;
    } | null;
  };
  images: { today: number; total: number };
}

export interface QuickPhrase {
  id: string;
  title: string;
  content: string;
  created_at: string;
  position: number;
}

export interface MessagesResponse {
  conversation: Conversation;
  messages: Message[];
  has_pending: boolean;
  has_more_before: boolean;
  /** how many rows are live; see the note where the poll compares it */
  active_count?: number;
}
