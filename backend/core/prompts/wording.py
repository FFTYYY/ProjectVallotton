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
# Entries, and the drawing model's message
# ==========================================================================

#: Entries, as both models read them (library.as_prompt_text for the lead,
#: library.exposure_text for the drawing model): one block per entry, the
#: key first, then the fields that are not empty.
ENTRY_NOTE_LABEL = "注释"
ENTRY_POSITIVE_LABEL = "正面"
ENTRY_NEGATIVE_LABEL = "负面"
ENTRY_PROSE_LABEL = "描述"
ENTRY_EMPTY = "（还没有内容）"
ENTRY_KIND_SETTING = "设定"
ENTRY_KIND_CHARACTER = "角色"
ENTRY_KIND_VARIANT = "造型"
#: where an entry came from, beside its kind
ENTRY_FROM_PROJECT = "项目层"
#: the turn's drawing id, as the conversation shows it to the lead
ENTRY_FROM_LEAD = "你在绘图 {id} 那一轮写的"
#: written in a turn that drew nothing
ENTRY_FROM_LEAD_NO_DRAW = "你写的"
ENTRY_FROM_USER = "用户建的"
#: The drawing model's message (llm.format_draw): the lead's description,
#: the text to render, the size, then the exposed entries.
DRAW_DESCRIPTION_HEADING = "【主要描述】"
DRAW_TEXT_HEADING = "【要画进画面的文字】"
DRAW_SIZE_HEADING = "【画幅】"
DRAW_ENTRIES_HEADING = "【暴露的条目】"
EXPOSURE_NONE = "（这一张没有暴露任何条目）"
DRAW_NO_DESCRIPTION = "（主 AI 没有写描述，只暴露了条目）"


# ==========================================================================
# The library, for the lead
# ==========================================================================

#: Rides with the newest user turn in the LEAD model's history, whenever the
#: conversation is in a project. A label only — what outranks what is
#: lead.txt's and drawing.txt's business.
LIBRARY_BLOCK_HEADING = "【本轮用户选中的资料库条目】"

#: Said out loud when the conversation is in a project but nothing is ticked,
#: so "no entries" is a stated fact rather than an absence the model has to
#: infer from a missing section.
LIBRARY_NONE = "【本轮用户没有选中任何资料库条目】"

#: The compressor's user message (llm.format_compress): what the drawing
#: model was asked, as it was asked (the description and the exposed
#: entries), then the limit and the counts, then the prompt as it would go
#: to NovelAI.
REVIEW_LIMIT_HEADING = "【长度】"
REVIEW_LIMIT_LINE = "NovelAI 的上限是 {limit} 个 token，{rule}。现在：{counts}。"
REVIEW_LIMIT_OVER = "超出 {over} 个 token，必须减掉。"
REVIEW_LIMIT_FITS = "没有超出。"
REVIEW_RULE = {
    "total": "整张图的提示词和各主体的提示词合在一起计数，负面提示词单独计数",
    "each": "整张图的提示词、各主体的提示词、负面提示词各自计数",
}
REVIEW_COUNT_BASE = "整张图 {n}"
REVIEW_COUNT_CHAR = "主体「{key}」{n}"
REVIEW_COUNT_NEGATIVE = "负面 {n}"
REVIEW_COUNT_SEP = "、"
REVIEW_PROMPTS_HEADING = "【提示词】"
REVIEW_PROMPT_BASE = "整张图：{text}"
REVIEW_PROMPT_CHAR = "主体「{key}」：{text}"
REVIEW_PROMPT_CHAR_NEGATIVE = "主体「{key}」的负面：{text}"
REVIEW_PROMPT_NEGATIVE = "负面：{text}"

#: The first line of the library block: which layer a proposal may go to.
LIBRARY_IN_PROJECT = (
    "（这次对话属于项目「{project}」。项目层的条目名字前带「@」，这个项目里所有对话都能用；"
    "没有「@」的是这次对话自己的条目）"
)
LIBRARY_NO_PROJECT = "（这次对话不属于任何项目，你记下的条目只存在这次对话里）"
LIBRARY_EMPTY = "（现在还没有任何条目）"

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
    "en": "使用用户的语言（通常是英文）。资料库条目的键和注释也用英文写",
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

#: The app's check of a finished lead answer: keys its draw exposes that the
#: library does not hold and the answer does not write. Sent back once; the
#: model answers again in full.
EXPOSE_MISSING = (
    "【应用检查】（应用发现的问题，不是用户说的话）\n"
    "`expose`里这些条目资料库里没有，这次回答里也没有写：{keys}。"
    "要用就在`settings`或`characters`里把它写出来（造型写在角色的`variants`里）；用不着就从`expose`里去掉。"
    "整份回答重新给一遍，其余照旧。"
)


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
#: …which entries that drawing exposed, by key ({keys} joined with 、), and
#: the tags the drawing model made of that answer.
APP_NOTE_EXPOSED = "这次绘图暴露给绘图AI的条目：{keys}"
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


#: What the patrol is shown (services/library_lines.py): which entry
#: ({title} / {name} = the entry's name, {category} = a setting's category),
#: then its note, then the tags and prose it has now.
FILL_FOR_SETTING = "设定「{title}」（类别：{category}）"
FILL_CATEGORY_OTHER = "其他"
FILL_FOR_CHARACTER = "角色「{name}」（不随衣着改变的样子）"
FILL_FOR_VARIANT = "角色「{name}」的造型「{title}」（角色的变体：姿势、服装、表情等）"
FILL_CURRENT = "现在的内容："
FILL_NONE_YET = "（空）"


# ==========================================================================
# The tag-table lookup's answer
#
# Either model may ask for a lookup through the `lookup` field of its answer
# (llm._ask); the app runs it against core/services/danbooru.py and sends what
# it found back as the next message. These are the words of that message. They
# state facts and no more: what matched, what did not, what was left unqueried.
# When to look anything up is the prompt files' business.
# ==========================================================================

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
# model failures the user sees
# --------------------------------------------------------------------------
#
# A provider's LLMError becomes Message.error and the bubble prints its first
# line, so these are interface text: one short line, Chinese punctuation, no
# spaces around numbers or latin words. Everything a developer would want —
# finish_reason, token counts, which setting to change, the answer that would
# not parse — goes to the log, or onto the row where 「检查」 can show it. None
# of it belongs in front of someone who just wanted a picture.
