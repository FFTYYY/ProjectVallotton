"""
Every word the models read that is NOT in a .txt prompt file.

The system prompts live next door as plain text, where they are easy to review.
Everything else the code assembles — labels, the blocks it splices into the
history, the little system prompt that names a conversation — used to be
scattered across four modules as inline literals. That is how an instruction
nobody asked for ("必须体现在提示词里") ended up overriding what drawing.txt
actually says, invisible to anyone reading the prompt files.

So it all lives here. Two rules for anything added below:

  1. **Label, don't instruct.** These strings say what a block IS. What the
     model should DO about it belongs in the .txt file, which is the thing
     that gets reviewed. A line here that starts telling the model how to
     behave will sooner or later contradict the prompt and win, because it
     sits later in the conversation and reads as more specific.
  2. **Say when it appears.** Each entry's comment names the condition, so a
     reader can tell which of these a given turn actually contained.
  3. **A {placeholder} is a contract with one caller.** Nothing in this file
     declares what fills one in — it is str.format, so the only agreement is
     the `.format(...)` at the call site, and renaming `{fields}` here would
     fail with a KeyError in the middle of a real turn. Every placeholder is
     therefore named in its comment, and AssembledWordingTests checks this
     file's placeholders against the keys the callers actually pass. Rename
     one and the tests go red instead of a turn.
"""

from __future__ import annotations

# ==========================================================================
# The picture description, as the drawing model reads it
#
# format_brief() renders the lead model's `draw` object into labelled lines.
# The key order here is the order they are written in — scene first, then who,
# then how they are posed, then where, from what angle, in what style.
# ==========================================================================

BRIEF_LABELS = {
    "main": "画面内容",
    "character": "人物",
    "pose": "姿势与表情",
    "scene": "场景 / 背景 / 光线 / 时间",
    "camera": "镜头角度",
    "style": "风格",
    "notes": "备注",
}

#: always appended; the value is portrait / landscape / square
BRIEF_SIZE_LABEL = "画幅"

#: The lead model's "这一张", as the drawing model reads it. One block for the
#: picture, one line per subject; what is empty is left out.
BRIEF_HEADING = "【这一张】"
BRIEF_SUBJECTS_LABEL = "主体："
BRIEF_SUBJECT_LABELS = {
    "outfit": "造型",
    "look": "外观",
    "changes": "这一张不同的地方",
    "doing": "在做什么",
    "where": "位置",
}
BRIEF_PICTURE_LABELS = {
    "change": "要改什么（找不到可改的上一张，这一张按新图画，但这段是用户要的）",
    "composition": "画面",
    "camera": "镜头",
    "scene": "场景",
    "light": "光",
    "style": "画风",
    "notes": "备注",
}
#: Heading over the library lines appended to the drawing model's message.
BRIEF_LINES_HEADING = "【条目的行】"
#: Edit turns: the previous picture, slot by slot, then what to change.
CURRENT_PICTURE_HEADING = "【上一张图的提示词，分格列出】"
CURRENT_SUBJECT = "主体「{key}」"
CURRENT_SUBJECT_FROM = "（取自资料库的「{name}」）"
CURRENT_TRUTH_HEADING = "真实表（上一张用了的标「用」）："
CURRENT_TRUTH_USED = "用"
#: Edit turns: the selected settings' lines, marked the same way, so the
#: picture's setting picks are made again rather than inherited.
CURRENT_SETTINGS_HEADING = "设定的行（上一张用了的标「用」）："
CURRENT_SETTING = "设定「{title}」"
CHANGE_HEADING = "【要改什么】"
#: Appended to the drawing model's system prompt on an edit turn.
DRAWING_EDIT_INTRO = (
    "这一轮不是画一张新图，而是改上一张。规则见「改上一张」一节。" + 2 * chr(10)
    + "下面先是上一张图的表征表（分格列出的 tag 和句子、挑了哪些行、打了什么补丁；每个主体旁边列着它真实表的全部行和上一张用了哪些），"
    + "然后是主 AI 写的「要改什么」。你只写要改的那几处，没写到的格子应用会原样保留。" + chr(10)
    + "什么都不用改就回一个空对象 {}。"
)


# ==========================================================================
# The library, for both models
# ==========================================================================

#: heading for the whole character section, when any character is switched on
LIBRARY_CHARACTERS_HEADING = "【角色】"
#: the fixed description of a character
LIBRARY_CHARACTER_DESC_LABEL = "设定"
#: one costume/variant of a character. {title} = the variant's name.
LIBRARY_VARIANT_LABEL = "造型「{title}」"
#: heading for the whole settings section, when any setting is switched on
LIBRARY_SETTINGS_HEADING = "【设定】"

#: Rides with the newest user turn in the LEAD model's history, whenever the
#: conversation is in a project. A label only — what outranks what is
#: lead.txt's and drawing.txt's business.
LIBRARY_BLOCK_HEADING = "【本轮用户选中的资料库条目】"

#: Appended to the DRAWING model's single user message, after the picture
#: description, on the same condition.
LIBRARY_FOR_DRAWING_HEADING = "【用户为这张图选中的资料库条目】"

#: Said out loud when the conversation is in a project but nothing is ticked,
#: so "no entries" is a stated fact rather than an absence the model has to
#: infer from a missing section.
LIBRARY_NONE = "【本轮用户没有选中任何资料库条目】"
LIBRARY_FOR_DRAWING_NONE = "【这张图没有选中任何资料库条目】"

#: The first line of the library block: which layer a proposal may go to.
LIBRARY_IN_PROJECT = (
    "（这次对话属于项目「{project}」。项目层的条目这个项目里所有对话都能用；"
    "标了「本次对话」的只在这次对话里）"
)
LIBRARY_NO_PROJECT = "（这次对话不属于任何项目，新记下的条目只存在这次对话里）"
LIBRARY_EMPTY = "（现在还没有任何条目）"
LIBRARY_NOTHING_ON = "（这次对话还没挂上任何条目）"
#: The rest of the project: entries that exist but are not switched on for
#: this conversation, one line each. A character is switched on by being
#: named in `subjects`; a setting only by the user, in the sidebar.
LIBRARY_OFF_HEADING = (
    "（下面这些条目在资料库里，但这次对话没挂上，所以只列名字。要画其中的角色，"
    "`subjects`里`who`写条目名、`outfit`写造型名，应用会把它挂上、按它的行画，不要为它另建条目；"
    "要用其中的设定，让用户在右边栏挂上）"
)
LIBRARY_OFF_CHARACTER = "◇ 角色「{name}」：{text}"
LIBRARY_OFF_VARIANTS = "造型：{titles}"
LIBRARY_OFF_SETTING = "◇ 设定「{title}」：{text}"
#: One entry's heading. {name} / {title} = the entry's name.
LIBRARY_CHARACTER_HEADING = "角色「{name}」"
LIBRARY_SETTING_HEADING = "设定「{title}」"
#: Appended to a conversation-layer entry's heading.
LIBRARY_CONVERSATION_MARK = "（本次对话）"
#: After a setting's 画风 line in the drawing model's rendering: the app
#: splices these itself, the model is told so it neither writes nor picks them.
LIBRARY_STYLE_LINE_MARK = "（画风行，应用自动拼进 style，不用挑）"
LIBRARY_NO_LINES = "（资料库里还没有带 tag 的条目）"
#: Under an entry that has prose but no lines: the prose is what there is.
LIBRARY_PROSE_LABEL = "正文（没有 tag 行，照着写）"
#: What a tag line's `part` says for the three lines the app itself
#: recognises, by core.i18n code:
#:   style     a setting's style line: goes into every picture's style slot
#:             by itself, never picked
#:   identity  the line that says who this is (`1girl`, a character tag):
#:             picked for every picture whatever the composition
#:   other     what the app writes when the model left the part blank
#: The model is told the current language's word (the {{part_style}} and
#: {{part_identity}} markers in the prompt files); the app recognises the
#: word in every language, since an entry written before the switch keeps
#: its lines. `part_word` says, `is_part` recognises.
LINE_PARTS = {
    "zh": {"style": "画风", "identity": "身份", "other": "其他"},
    "en": {"style": "style", "identity": "identity", "other": "other"},
}
#: tag_lines.txt, after "`part`随意": in English the model is told to write
#: the parts in English too; in Chinese nothing is added (the file is the
#: prompt, byte for byte).
PART_LANGUAGE_HINT = {
    "zh": "",
    "en": "（`part`也用英文写）",
}


def part_word(kind: str) -> str:
    """The current language's word for the `style`, `identity` or `other` part."""
    from core import i18n
    return LINE_PARTS[i18n.current()][kind]


def is_part(part: object, kind: str) -> bool:
    """Whether a line's `part` names the `style`, `identity` or `other` line, in any language."""
    return part in {table[kind] for table in LINE_PARTS.values()}
#: The handle of a subject that is not a library entry: 主体1, 主体2…
SUBJECT_KEY = "主体{n}"

# ==========================================================================
# The person's language, by core.i18n code
#
# The prompts are Chinese whatever the account chose; these are the few
# phrases in them that name the language of what comes OUT — the reply, and
# the prose of a library entry — and they are spliced in where lead.txt and
# patrol.txt carry the marker (core.prompts.REPLY_LANGUAGE_MARK and
# PROSE_LANGUAGE_MARK). One value per language, same keys as i18n.CODES.
# ==========================================================================

#: lead.txt 「你的性格」: the sentence that says what language replies are in
REPLY_LANGUAGE = {
    "zh": "使用用户的语言（通常是中文）",
    "en": "使用用户的语言（通常是英文）。资料库条目的名称、正文和 tag 行的 part 也用英文写",
}
#: lead.txt 「正文」 and patrol.txt: the word for the language of entry prose
PROSE_LANGUAGE = {
    "zh": "中文",
    "en": "英文",
}

#: The tag-table lookup, answered in the model's own history: the heading over
#: the result, and the line added when no further lookups are allowed.
LOOKUP_HEADING = "【标签表查询结果】（应用返回的数据，不是用户说的话）"
LOOKUP_LAST = "这一轮的查询次数已用完，请直接给出正式回答。"


# ==========================================================================
# Blocks spliced into the lead model's history
#
# None of these were written by the user, and none of them are in any prompt
# file — they are most of what the model actually reads.
# ==========================================================================

#: ==========================================================================
#: The app's own turn in the history.
#:
#: Everything here is said by the app, in a message of its own, because none
#: of it was said by the assistant — and a model reads its own past turns as
#: examples of how to answer. It read ours: the picture description used to be
#: appended to the assistant's words as a bracketed block, and five of
#: fifty-eight measured calls came back as that block instead of as JSON.
#: See reports/21-deepseek-failures.html.
#: ==========================================================================

#: How the app announces itself, in one place.
#:
#: There used to be three spellings of this — an opener on ordinary notes, a
#: second one carrying the drawing's id, and a third that reflection used for
#: its own block — so the same kind of message read differently depending on
#: which code path built it. The opener below is shared, and the two endings
#: are the only thing that varies.
SYSTEM_NOTE_OPEN = "[系统：这是一条系统消息。"
SYSTEM_NOTE_PLAIN = SYSTEM_NOTE_OPEN + "]"
#: Names one whole drawing — the brief, the tags made from it, and the pictures
#: that came back — so a later turn can point at an earlier picture and be
#: understood.
SYSTEM_NOTE_DRAW = SYSTEM_NOTE_OPEN + "本次绘图调用的id为 {id}]"
#: The user set a different picture of a turn as its main one. {id} = the
#: drawing's id, {n} = the picture's number, from 1.
SYSTEM_NOTE_MAIN = SYSTEM_NOTE_OPEN + "用户把绘图 {id} 的第{n}张设为主图]"
#: …the tags the drawing model made of that answer.
APP_NOTE_PROMPTS = "绘图AI实际发送给NovelAI的prompts："
#: …and the pictures that came back.
APP_NOTE_IMAGES = "以下是实际生成的图片："
#: A reference the user gave that is too far back to still ride along.
#: {n} = how many of that turn's pictures were left out.
APP_NOTE_REFERENCES_SKIPPED = "用户上一条消息里还有 {n} 张参考图，因为太靠前没有附上。"

#: Inside a reflection round only, carrying the pictures that round produced.
#: A label: what the model should DO about them is lead_reflection.txt's business.


#: A message row with nothing in it at all — should not happen, but the array
#: may not contain an empty string.
EMPTY_USER_MESSAGE = "(空消息)"
EMPTY_ASSISTANT_MESSAGE = "(空)"


# ==========================================================================
# The tag-table lookup's answer
#
# Either model may ask for a lookup through the `lookup` field of its answer
# (llm._ask); the app runs it against core/services/danbooru.py and sends what
# it found back as the next message. These are the words of that message. They
# state facts and no more: what matched, what did not, what was left unqueried.
# When to look anything up is the prompt files' business.
# ==========================================================================

#: What the line writer is shown (services/library_lines.py): which entry,
#: then its text, then the lines it has now.
LINES_FOR_SETTING = "设定「{title}」"
LINES_FOR_CHARACTER = "角色「{name}」（长相的行）"
LINES_FOR_VARIANT = "角色「{name}」的造型「{title}」（这一套衣服配饰的行）"
LINES_CURRENT = "现有的行（编号由应用管，你只给 part 和 tags；每行末尾标着最后是谁改的）："
LINES_NONE_YET = "（还没有行）"
LINE_BY_USER = "〔用户改的〕"
LINE_BY_AI = "〔模型写的〕"

#: First line of every answer: what the three columns are.
TOOL_TAGS_COLUMNS = "标签\t图片数\t分类"
#: One per query, above that query's hits. {query} = the word as it was sent.
TOOL_TAGS_QUERY = "【{query}】"
#: Under a query that matched nothing. Said out loud: an empty section reads as
#: a lookup that broke, and "no such tag" is half of what the answer can say.
TOOL_TAGS_NONE = "（没有匹配的标签）"
#: Above the tags that matched only part of a multi-word query, when there are
#: any. They answer a narrower question than the one asked, and a near miss
#: listed as a hit is how a prompt picks up a tag about something else.
TOOL_TAGS_PARTIAL = "（以下只匹配了其中一部分词）"
#: When more queries arrived than one call carries. {n} = danbooru.MAX_QUERIES.
TOOL_TAGS_TRIMMED = "（只查了前 {n} 个词，后面的没查）"
#: When the call carried no query at all.
TOOL_TAGS_NO_QUERIES = "（这次调用没有给任何查询词）"


# ==========================================================================
# Naming a conversation
#
# Its own small call after the first successful turn, on the cheapest model
# the provider has.
# ==========================================================================

TITLE_SYSTEM = (
    "为下面的对话写一个简短标题，直接输出标题本身，不要引号、"
    "不要标点结尾、不要任何解释。语言与对话一致，最多 12 个字。"
)
TITLE_USER = "用户：{user}\n助手：{reply}"
#: appended to TITLE_SYSTEM: the title in the language the account speaks
#: (the conversation alone did not settle it: an English conversation still
#: came back with a Chinese title). One value per language, like REPLY_LANGUAGE.
TITLE_LANGUAGE = {"zh": "", "en": "标题用英文。"}
#: stand-in when the first user turn was an image with no words
TITLE_IMAGE_PLACEHOLDER = "(图片)"


# ==========================================================================
# Not a prompt: what a finished continuous run leaves in the transcript
#
# A system row, so it is excluded from build_history and no model ever reads
# it. It exists because the run is a server-side thing now and the browser may
# not have been watching when it ended.
# ==========================================================================

# ==========================================================================
# Structured output for a vendor that has none
#
# Appended to the system prompt for providers whose capabilities say
# json_schema=False. The skeleton itself is generated from the very schema the
# strict vendors are handed, so the two cannot drift; only this framing is
# written by hand. DeepSeek's docs require the literal word "json" and a sample
# structure to be present — both are here on purpose.
# ==========================================================================

SCHEMA_PROSE_HEADING = "## 输出格式（严格遵守）"
SCHEMA_PROSE_INTRO = (
    "只输出一个 json 对象，不要输出任何别的内容——没有前言、没有解释、"
    "没有 ``` 代码围栏。字段名和嵌套层级要与下面这个样例一致："
)

#: Printed under the skeleton whenever the schema has array fields. A sample
#: can only draw one element, and "与样例一致" then reads as "exactly one" —
#: which would cap character_prompts at one character and every library
#: proposal at one entry.
#: {fields} = every array path in the schema, joined with 、 — e.g.
#: "edits.settings、edits.characters、edits.characters[].variants".
SCHEMA_PROSE_LISTS = (
    "方括号里是列表：可以放 0 个、1 个或多个长成那样的对象，"
    "样例里只画一个是为了说明形状。这些字段是列表：{fields}。"
)

#: How a capped list is named inside SCHEMA_PROSE_LISTS.
#: {field} = the path, {cap} = the schema's maxItems.
SCHEMA_PROSE_LIST_CAP = "{field}（最多 {cap} 个）"

#: Printed when some field accepts null. For `draw` and `edits` the null case
#: is the ordinary one — no picture this turn, no library changes — and the
#: skeleton draws them as full objects, so without this line the commonest
#: answer looks unavailable.
#: {fields} = every nullable object/array path, joined with 、.
SCHEMA_PROSE_NULLABLE = "这些字段可以直接写 null：{fields}。"
SCHEMA_PROSE_OUTRO = (
    "尖括号是占位说明，替换成真正的值。没有内容的字段，填空字符串或者整个不写"
    "都可以，两种读起来是一样的；但不要改字段名，也不要增加字段。"
)


# --------------------------------------------------------------------------
# prompt slots
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# model failures the user sees
# --------------------------------------------------------------------------
#
# A provider's LLMError becomes Message.error and the bubble prints its first
# line, so these are interface text: one short line, Chinese punctuation, no
# spaces around numbers or latin words. Everything a developer would want —
# finish_reason, token counts, which setting to change, the answer that would
# not parse — goes to the log, or onto the row where 「检查」 can show it. None
# of it belongs in front of someone who just wanted a picture.
