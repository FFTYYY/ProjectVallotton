"""
Every sentence the backend hands to a person, in Chinese: error replies,
notices, the names it gives things when nobody named them. Prompt wording
lives in core/prompts/wording.py; this is the other voice, the one the
browser shows.

Nothing imports this module by name except the admin and the tests. Code reads
`core.ui_text`, which looks each name up in the table of the language the
account chose (core.i18n); ui_text_en.py is the same names in English, and
tests/tests_i18n.py holds the two to the same names and the same {fields}.
tests/fixtures/ui_text_zh.json is this table as it was when the English one
was added: change a sentence here on purpose and update the fixture with it.

Templates take their values by name: `NAME_REQUIRED.format(max=MAX_NAME)`.
"""

# ---------------------------------------------------------------- requests
BAD_JSON = "请求不是合法 JSON"
BAD_JSON_BODY = "body 不是合法 JSON"
NOTHING_TO_UPDATE = "没有可更新的字段"
NOTHING_TO_UPDATE_CONVERSATION = "没有可更新的字段（支持 title、gen_settings）"
NOTHING_TO_UPDATE_PHRASE = "没有可更新的字段（支持 title / content）"
BAD_PAGING = "limit/offset 不是合法整数"
BAD_SEQ_PAGING = "after_seq/before_seq/limit 不是合法整数"
BAD_FOLDER_ID = "folder_id 不是合法 UUID"
BAD_PARENT_ID = "parent_id 不是合法 UUID"
BAD_ID_OR_PARENT_ID = "id / parent_id 不是合法 UUID"
BAD_CONVERSATION_ID = "conversation_id 不是合法 UUID"
KIND_NOT_FOLDER_OR_CONVERSATION = "kind 必须是 folder 或 conversation"
IDS_NOT_STRINGS = "ids 不是一个字符串数组"
GEN_DEFAULTS_NOT_OBJECT = "gen_defaults 不是一个对象"
GEN_SETTINGS_NOT_OBJECT = "gen_settings 不是一个对象"
STEPPED_OVER_NOT_BOOL = "stepped_over 不是 true 或 false"
LANGUAGE_UNKNOWN = "language 不是支持的语言（{codes}）"

# ---------------------------------------------------------------- not found
NO_CONVERSATION = "对话不存在"
NO_FOLDER = "文件夹不存在"
NO_PARENT_FOLDER = "上级文件夹不存在"
NO_TARGET_FOLDER = "目标文件夹不存在"
NO_MESSAGE = "消息不存在"
NO_SETTING = "设定不存在"
NO_CHARACTER = "角色不存在"
NO_VARIANT = "变体不存在"
NO_PHRASE = "短语不存在"
NO_PROPOSAL = "这条修改建议不存在"
NO_ROUTE = "没有这条线路"
NO_FILE = "文件不存在"
FILE_LOST = "文件已丢失"
NO_SUCH_PICTURE = "没有这一张图"

# ---------------------------------------------------------------- login
NOT_LOGGED_IN = "未登录"
BAD_CREDENTIALS = "用户名或密码错误"
TOO_MANY_ATTEMPTS = "尝试次数过多，请 {minutes} 分钟后再试"

# ---------------------------------------------------------------- names and limits
TITLE_EMPTY = "标题不能为空"
TITLE_REQUIRED = "标题必填，最长 {max} 字"
NAME_REQUIRED = "名称必填，最长 {max} 字"
CONTENT_REQUIRED = "内容必填，最长 {max} 字"
TITLE_TAKEN = "这个标题已经存在"
NAME_TAKEN = "这个名称已经存在"
VARIANT_TITLE_TAKEN = "这个变体标题已经存在"
FOLDER_TOO_DEEP = "文件夹层级最多 {max} 层"
FOLDER_INTO_ITSELF = "不能把文件夹拖进它自己或它的子文件夹里"
MESSAGE_EMPTY = "消息不能为空"
MESSAGE_OR_IMAGES_REQUIRED = "message 和 images 至少要有一个"
BASE_PROMPT_EMPTY = "base_prompt 不能为空"

# ---------------------------------------------------------------- pictures coming in
IMAGE_TOO_BIG = "图片超过 {mb}MB 上限"
NOT_AN_IMAGE = "无法解析为图片"
UNSUPPORTED_IMAGE_FORMAT = "不支持的格式 {fmt}（支持 png/jpg/webp/gif/bmp）"
TOO_MANY_IMAGES_UPLOAD = "一次最多上传 {max} 张图片"
TOO_MANY_IMAGES = "一次最多 {max} 张图片"
FILE_REJECTED = "文件 {name} 被拒绝：{why}"
IMAGE_MISSING_DATA = "images[{index}] 缺少 data_base64"
IMAGE_DECODE_FAILED = "images[{index}].data_base64 解码失败: {error}"
IMAGE_OVER_LIMIT = "images[{index}] 超过大小限制"
IMAGE_INVALID = "images[{index}] 不是合法图片: {error}"

# ---------------------------------------------------------------- turns
BUSY_REPLYING = "上一条回复仍在生成中，请先等待或停止"
BUSY = "已有任务在生成中"
BUSY_CONVERSATION = "该对话已有任务在生成中"
NOTHING_TO_REDRAW = "该消息没有可重绘的图片"
NO_USER_MESSAGE_YET = "对话中还没有用户消息"
NOTHING_TO_BRANCH = "没有可分支的内容"
GENERATION_FAILED = "生成失败"
JOB_INTERRUPTED = "任务中断（后端在生成过程中重启了）。请重试。"
EDIT_CHANGED_NOTHING = "绘图 AI 没有改出任何东西：这一张和上一张一字不差。请换个说法再试。"
JOB_INTERRUPTED_MAYBE = "任务中断（后端可能在生成过程中重启了）。请重试。"
#: what the lead's turn says when the model answered with nothing at all
EMPTY_REPLY_FALLBACK = "……抱歉，我走神了。能再说一遍吗？"
MCP_NOT_CONFIGURED = "MCP 未配置"

# ---------------------------------------------------------------- the library
ALREADY_IN_PROJECT = "这条已经在项目里"
NO_PROJECT_TO_SAVE_TO = "这个对话不在任何项目里，无处可存"
PROJECT_HAS_SAME_NAME = "项目里已经有同名条目"
NO_FOLDER_FOR_LIBRARY = "这个对话不在任何文件夹里，无处保存资料库条目"
DEFAULT_VARIANT_TITLE = "默认"

# ---------------------------------------------------------------- names given by the app
NEW_CONVERSATION_TITLE = "新对话"
CONVERSATION_WORD = "对话"
BRANCH_TITLE = "{title}（分支）"
MCP_CONVERSATION_TITLE = "MCP 对话"
MCP_DIRECT_TITLE = "MCP 直接生成"
IMAGE_PLACEHOLDER = "[图片]"

# ---------------------------------------------------------------- notices in the transcript
#: A token count once it is long: (unit, template) pairs, largest unit first;
#: the count divided by the first unit it reaches goes in as {n}. Below every
#: unit the plain number is shown.
TOKENS_SHORT_UNITS = [(10000, "{n:.1f} 万")]
CONTEXT_NEAR_LIMIT = (
    "这个对话已经用掉约 {used} token，接近模型上限（{limit}）。"
    "超出后最早的消息会被丢弃。"
)
LONG_CONTEXT = "这个对话的上下文已超过 {threshold} token。"
LONG_CONTEXT_PRICED = "这个对话的上下文已超过 {threshold} token。"
REASONING_CLIPPED = "\n…（后面还有 {chars} 字符，已截断）"
#: the lead's routes, as the settings page names them
ROUTE_LABELS = {"deepseek": "DeepSeek 官方", "openrouter": "OpenRouter 备用"}

# ---------------------------------------------------------------- what a proposal's result says in the card
#: What applying a proposal did, one phrase per entry, joined with "；" and
#: shown on the proposal card. {title} / {name} = the entry's name.
PROPOSAL_CREATED_SETTING = "新建设定「{title}」"
PROPOSAL_UPDATED_SETTING = "更新设定「{title}」"
PROPOSAL_CREATED_CHARACTER = "新建角色「{name}」"
PROPOSAL_UPDATED_CHARACTER = "更新角色「{name}」"
PROPOSAL_CREATED_VARIANT = "新建变体「{name}·{title}」"
PROPOSAL_UPDATED_VARIANT = "更新「{name}·{title}」"
#: a character and one of her variants, as a person reads them
ENTRY_VARIANT = "{name}·{title}"
#: A project write refused because the project already has that name: the
#: conversation's entry stays where it is. {name} = the entry's name.
PROPOSAL_NAME_TAKEN = "无法写入，项目里已经有同名的「{name}」"
#: between the phrases of one card's summary
PROPOSAL_SUMMARY_SEP = "；"

# ---------------------------------------------------------------- notes on a turn, as the inspector shows them
#: When the prompt sent is over NovelAI's limit, compressed or not: NovelAI
#: reads the first part of it only. {tokens} = the count, {limit} = the cap.
PROMPT_OVER_LIMIT = "超出 NovelAI 的长度上限：{tokens}/{limit} token，后面的部分 NovelAI 不读"
#: The lead exposed keys no entry answers to. {keys} = joined with 、.
EXPOSE_UNKNOWN = "找不到这些条目：{keys}"
#: What the lead wrote into this conversation's library. {what} = one phrase
#: per entry, joined with 、.
LIBRARY_WRITTEN = "写进本对话的资料库：{what}"
PROPOSAL_NOT_RECORDED = "主AI的资料库建议没记下来：{error}"
PAINT_ONE_FAILED = "这一批 {n} 张里有一张没画出来：{error}"
STOPPED_KEPT_PICTURES = "已停止；留下了画出来的图"

# ---------------------------------------------------------------- a model call that failed, as the row's error
#: Thinking and answering share one token budget and this answer ran out of it.
#: {name} = provider label.
MODEL_OUT_OF_TOKENS = "{name}的回答被截断了"
#: Nothing came back at all. {name} = provider label.
MODEL_EMPTY = "{name}没有返回内容"
#: Every attempt came back unusable. {name} = provider label.
MODEL_NOTHING_USABLE = "{name}连着几次都没返回可用的内容"
#: One answer ran past the wall-clock cap twice. {name} = the road, {minutes} =
#: settings.LLM_ANSWER_SECONDS in minutes.
MODEL_ANSWER_TOO_LONG = "{name}一次回答超过了 {minutes} 分钟，掐断了"
#: The answer was not JSON. The answer itself is kept on the row instead, where
#: 「检查」 can show it. {name} = provider label.
MODEL_UNREADABLE = "{name}的回答读不出来"

# ---------------------------------------------------------------- continuous generation, as the transcript notes it
#: A run opens with one of these rows and closes with another, always, however
#: it ends. The pair is the point: half a run with no ending reads as a run
#: still going, and the browser is not always there to notice the difference.
CONTINUOUS_START = "连续生成开始 · 共{of}轮"
#: Asked for a run with no quick phrase to send again. Said out loud rather
#: than returned from in silence — a run that never started looks exactly like
#: one that was never asked for.
CONTINUOUS_NO_PHRASE = "连续生成没有开始：还没有可发送的快捷短语"
CONTINUOUS_HEADS = {
    "done": "连续生成完成",
    "stopped": "连续生成已停下",
    "failed": "连续生成中断",
    "replaced": "连续生成已结束",   # a message the user typed took over
    "lost": "连续生成已中断",       # the backend stopped partway through
}
CONTINUOUS_SUMMARY = "{head} · 共{of}轮 · 成功{done}轮 · 失败{failed}轮"

# ---------------------------------------------------------------- names on the settings page and in the admin
PROVIDER_DEEPSEEK = "DeepSeek"
PROVIDER_GROK = "Grok"
PROVIDER_FAKE = "假模型（仅测试）"
APP_NAME = "Vallotton"
ADMIN_SITE_HEADER = "Vallotton 管理后台"
ADMIN_INDEX_TITLE = "数据管理"
ADMIN_LLM_USD = "模型花费 (USD)"
