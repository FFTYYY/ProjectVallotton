"""
Every sentence the backend hands to a person: error replies, notices, the
names it gives things when nobody named them. Prompt wording lives in
core/prompts/wording.py; this is the other voice, the one the browser shows.

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
EDIT_WITHOUT_PICTURE = "这张图是旧版本画的，没有可改的表征表，改不了。请重新描述整张画面。"
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
TOKENS_TEN_THOUSAND = "{n:.1f} 万"
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
PROPOSAL_CREATED_VARIANT = "新建造型「{name}·{title}」"
PROPOSAL_UPDATED_VARIANT = "更新「{name}·{title}」"
#: The same, for an entry made in the conversation's own layer. It says so:
#: the card would otherwise read the same for both layers, and the two are
#: not the same thing -- one every conversation of the project can use, the
#: other this one alone.
PROPOSAL_CREATED_SETTING_OWN = "新建设定（对话级）「{title}」"
PROPOSAL_CREATED_CHARACTER_OWN = "新建角色（对话级）「{name}」"
PROPOSAL_CREATED_VARIANT_OWN = "新建造型（对话级）「{name}·{title}」"

# ---------------------------------------------------------------- a change list that could not be applied, as the inspector shows it
#: Why a change list was refused. Shown in the log and 「检查」, never to the
#: model: a refused list is retried as the same request.
PATCH_NOT_AN_OBJECT = "{where}的改动不是对象"
PATCH_OP_NOT_ALLOWED = "{where}不允许{op}（只允许{allowed}）"
PATCH_SUBJECT_NOT_AN_OBJECT = "characters的每一项都得是对象"
PATCH_NO_SUCH_SUBJECT = "没有叫「{who}」的主体"
PATCH_NO_SUCH_SUBJECT_SLOT = "主体{who}没有{slot}这一格"
PATCH_PATCHES_SET_ONLY = "{who}.patches只能整组重写（只允许set）"
PATCH_NO_SUCH_SLOT = "没有{slot}这一格"
PATCH_FIELD_HALVES = "{where}的改动只能分成tags和prose两半，没有{key}"
PATCH_SETTINGS_SET_ONLY = "settings只能整组重写（只允许set）"

# ---------------------------------------------------------------- what the prompt check found, as the inspector shows it
#: Shown in 检查 when a character cites a library tag line that is not there.
#: Reaching here means an entry was edited between the two turns, or the model
#: invented a line number; either way the words are silently missing from the
#: picture, so it is worth saying out loud.
#: {who} = the entry name, {lines} = the line ids, joined with 、.
PROMPT_UNKNOWN_LINES = "{who}用了资料库里没有的tag行：{lines}"
#: How a character with no library entry behind it is named in the line above.
PROMPT_NAMELESS = "没有对应条目的主体"
#: A setting pick naming lines the setting does not have. {title} = the entry.
PROMPT_UNKNOWN_SETTING_LINES = "设定「{title}」没有这些行：{lines}"
#: Shown in 检查 when the picture says solo but carries more than one subject.
PROMPT_SOLO_WITH_OTHERS = "人数格写了solo，但画面里有不止一个主体"
#: A library character whose identity line was not picked. {who} = the entry.
PROMPT_IDENTITY_LINE_MISSING = "{who}的身份行{lines}没挑"
#: A patch aimed at a line this picture did not pick. {who} = the subject key.
PROMPT_PATCH_ON_UNPICKED_LINE = "{who}给没挑的行打了补丁：{lines}"
#: A `look` that re-describes something the picked lines already say.
PROMPT_MULTI_WITHOUT_CENTER = "两个以上主体，这几个没给center坐标（位置靠NovelAI自己排）：{who}"
PROPOSAL_LINES_DROPPED = "「{name}」少了{n}行：{parts}"
PROPOSAL_NOT_RECORDED = "主AI的资料库建议没记下来：{error}"
PAINT_ONE_FAILED = "这一批 {n} 张里有一张没画出来：{error}"
STOPPED_KEPT_PICTURES = "已停止；留下了画出来的图"
COPY_FROM_MISSED = "「{name}」的 copy_from 指的图 {picture} 找不到，按主 AI 的记忆记的"
PROMPT_LINE_NOT_ENGLISH = "「{who}」的行{line}里有中文：行只写英文tag，中文描述给绘图AI的brief"
PROMPT_SENTENCE_IN_TAGS = "{where}的tags半边里写了句子（句子该放prose）：{text}"
PROMPT_LOOK_ECHOES_LINES = "{who}的look重复了条目行里的东西：{tags}"

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
