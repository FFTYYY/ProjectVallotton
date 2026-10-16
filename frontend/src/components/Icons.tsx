/**
 * Icon set — Lucide, wrapped so the whole app gets one consistent weight.
 *
 * The visual language calls for hairline linear icons (1.5px), monochrome,
 * inheriting the text colour so hover states are a colour change and nothing
 * else. Lucide ships at stroke 2 by default, so every icon is re-exported
 * through one wrapper instead of being tuned at the call site.
 */

import {
  SlidersHorizontal,
  LogOut,
  Languages,
  Moon,
  Sun,
  Activity,
  Archive,
  ArrowDown,
  ArrowUp,
  BookOpen,
  Check,
  Eye,
  Pin,
  Repeat,
  Feather,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderUp,
  GripVertical,
  MessageSquare,
  UserRound,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Copy,
  Download,
  GitBranch,
  Info,
  Menu,
  MoreHorizontal,
  PanelRight,
  Paperclip,
  Pencil,
  Plus,
  RefreshCw,
  SquarePen,
  RotateCcw,
  ScanSearch,
  Send,
  Square,
  Star,
  Tag,
  Trash2,
  X,
  type LucideIcon,
} from "lucide-react";

interface IconProps {
  size?: number;
  className?: string;
}

const STROKE = 1.5;

function wrap(Icon: LucideIcon, defaultSize = 18) {
  return function Wrapped({ size = defaultSize, className }: IconProps) {
    return (
      <Icon size={size} strokeWidth={STROKE} className={className} aria-hidden="true" />
    );
  };
}

export const PlusIcon = wrap(Plus);
/** starting a conversation is picking up a pen, not adding a row */
export const NewChatIcon = wrap(Feather);
/** the same act, aimed at a project — a folder mark here would read as
    "make a folder", which is the one thing this button does not do */
export const ComposeInIcon = wrap(SquarePen);
export const SendIcon = wrap(Send);
export const PaperclipIcon = wrap(Paperclip);
export const MenuIcon = wrap(Menu);
export const DotsIcon = wrap(MoreHorizontal);
export const CloseIcon = wrap(X);
export const DownloadIcon = wrap(Download);
export const RedrawIcon = wrap(RefreshCw);
/* an entry whose lines are still being rewritten from its text */
export const SyncIcon = wrap(RefreshCw);
export const RerunIcon = wrap(RotateCcw);
/** looking INTO the turn — a magnifier alone reads as "search the chat" */
export const InspectIcon = wrap(ScanSearch);
/** a turn's main picture: the one an edit starts from and the seed follows */
export const StarIcon = wrap(Star);
export const EditIcon = wrap(Pencil);
export const TrashIcon = wrap(Trash2);
/* move a conversation-layer entry up into the project */
export const FolderUpIcon = wrap(FolderUp);
export const ChevronLeftIcon = wrap(ChevronLeft);
export const ChevronRightIcon = wrap(ChevronRight);
export const ChevronDownIcon = wrap(ChevronDown);
export const CopyIcon = wrap(Copy);
export const InfoIcon = wrap(Info);
export const TagIcon = wrap(Tag);
export const ArchiveIcon = wrap(Archive);
export const PanelRightIcon = wrap(PanelRight);
/** the left rail's control points the way it travels: out to the right,
    back to the left. The right rail uses PanelRight, deliberately unlike it. */
export const CollapseLeftIcon = wrap(ChevronsLeft);
export const ExpandRightIcon = wrap(ChevronsRight);
export const BranchIcon = wrap(GitBranch);
export const FolderIcon = wrap(Folder);
export const FolderOpenIcon = wrap(FolderOpen);
export const NewFolderIcon = wrap(FolderPlus);
export const GripIcon = wrap(GripVertical);
export const ConversationIcon = wrap(MessageSquare);
/** 设定 are pages of a world book; 角色 are people */
export const SettingEntryIcon = wrap(BookOpen);
export const GearIcon = wrap(SlidersHorizontal);
export const LogOutIcon = wrap(LogOut);
export const LanguagesIcon = wrap(Languages);
export const MoonIcon = wrap(Moon);
export const SunIcon = wrap(Sun);
export const ActivityIcon = wrap(Activity);
export const CharacterIcon = wrap(UserRound);
export const CheckIcon = wrap(Check);
/** reflection: the model looking back at what it just made */
export const ReflectIcon = wrap(Eye);
/** 常驻参考: the reference pictures stay in view */
export const PinIcon = wrap(Pin);
/** continuous mode: the same instruction, sent again */
export const RepeatIcon = wrap(Repeat);
export const ArrowDownIcon = wrap(ArrowDown);
export const ArrowUpIcon = wrap(ArrowUp);

/** the stop control is the one filled glyph — it must read as a hard stop */
export function StopIcon({ size = 18, className }: IconProps) {
  return (
    <Square
      size={size}
      strokeWidth={STROKE}
      className={className}
      fill="currentColor"
      aria-hidden="true"
    />
  );
}
