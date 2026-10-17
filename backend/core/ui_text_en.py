"""
Every sentence the backend hands to a person, in English. The same names as
ui_text_zh.py with the same {fields}; see that module's docstring for how the
two are read and held together.

Names of things, so the browser and the backend say the same: lead AI,
drawing AI, library, project, lore entry (设定), character, variant, tag line,
quick phrase, continuous generation, reflection, route, branch.
"""

# ---------------------------------------------------------------- requests
BAD_JSON = "The request is not valid JSON"
BAD_JSON_BODY = "body is not valid JSON"
NOTHING_TO_UPDATE = "Nothing to update"
NOTHING_TO_UPDATE_CONVERSATION = "Nothing to update (title and gen_settings are supported)"
NOTHING_TO_UPDATE_PHRASE = "Nothing to update (title / content are supported)"
BAD_PAGING = "limit/offset are not valid integers"
BAD_SEQ_PAGING = "after_seq/before_seq/limit are not valid integers"
BAD_FOLDER_ID = "folder_id is not a valid UUID"
BAD_PARENT_ID = "parent_id is not a valid UUID"
BAD_ID_OR_PARENT_ID = "id / parent_id is not a valid UUID"
BAD_CONVERSATION_ID = "conversation_id is not a valid UUID"
KIND_NOT_FOLDER_OR_CONVERSATION = "kind must be folder or conversation"
IDS_NOT_STRINGS = "ids is not an array of strings"
GEN_DEFAULTS_NOT_OBJECT = "gen_defaults is not an object"
GEN_SETTINGS_NOT_OBJECT = "gen_settings is not an object"
STEPPED_OVER_NOT_BOOL = "stepped_over is not true or false"
LANGUAGE_UNKNOWN = "language is not one of the supported languages ({codes})"

# ---------------------------------------------------------------- not found
NO_CONVERSATION = "Conversation not found"
NO_FOLDER = "Folder not found"
NO_PARENT_FOLDER = "Parent folder not found"
NO_TARGET_FOLDER = "Target folder not found"
NO_MESSAGE = "Message not found"
NO_SETTING = "Lore entry not found"
NO_CHARACTER = "Character not found"
NO_VARIANT = "Variant not found"
NO_PHRASE = "Phrase not found"
NO_PROPOSAL = "This proposal does not exist"
NO_ROUTE = "No such route"
NO_FILE = "File not found"
FILE_LOST = "The file has been lost"
NO_SUCH_PICTURE = "No such picture"

# ---------------------------------------------------------------- login
NOT_LOGGED_IN = "Not logged in"
BAD_CREDENTIALS = "Wrong username or password"
TOO_MANY_ATTEMPTS = "Too many attempts, try again in {minutes} minutes"

# ---------------------------------------------------------------- names and limits
TITLE_EMPTY = "The title cannot be empty"
TITLE_REQUIRED = "A title is required, at most {max} characters"
NAME_REQUIRED = "A name is required, at most {max} characters"
CONTENT_REQUIRED = "Content is required, at most {max} characters"
TITLE_TAKEN = "This title already exists"
NAME_TAKEN = "This name already exists"
VARIANT_TITLE_TAKEN = "This variant title already exists"
FOLDER_TOO_DEEP = "Folders nest at most {max} levels deep"
FOLDER_INTO_ITSELF = "A folder cannot be dragged into itself or into one of its subfolders"
MESSAGE_EMPTY = "The message cannot be empty"
MESSAGE_OR_IMAGES_REQUIRED = "At least one of message and images is required"
BASE_PROMPT_EMPTY = "base_prompt cannot be empty"

# ---------------------------------------------------------------- pictures coming in
IMAGE_TOO_BIG = "The image is over the {mb}MB limit"
NOT_AN_IMAGE = "Cannot be read as an image"
UNSUPPORTED_IMAGE_FORMAT = "Unsupported format {fmt} (png/jpg/webp/gif/bmp are supported)"
TOO_MANY_IMAGES_UPLOAD = "At most {max} images per upload"
TOO_MANY_IMAGES = "At most {max} images at a time"
FILE_REJECTED = "File {name} was rejected: {why}"
IMAGE_MISSING_DATA = "images[{index}] has no data_base64"
IMAGE_DECODE_FAILED = "images[{index}].data_base64 could not be decoded: {error}"
IMAGE_OVER_LIMIT = "images[{index}] is over the size limit"
IMAGE_INVALID = "images[{index}] is not a valid image: {error}"

# ---------------------------------------------------------------- turns
BUSY_REPLYING = "The previous reply is still being generated; wait for it or stop it first"
BUSY = "A task is already generating"
BUSY_CONVERSATION = "This conversation already has a task generating"
NOTHING_TO_REDRAW = "This message has no picture to redraw"
NO_USER_MESSAGE_YET = "There is no user message in this conversation yet"
NOTHING_TO_BRANCH = "Nothing to branch from"
GENERATION_FAILED = "Generation failed"
JOB_INTERRUPTED = "The task was interrupted (the backend restarted while generating). Please try again."
EDIT_CHANGED_NOTHING = "The drawing AI changed nothing: this picture is word for word the previous one. Try putting it another way."
JOB_INTERRUPTED_MAYBE = "The task was interrupted (the backend may have restarted while generating). Please try again."
#: what the lead's turn says when the model answered with nothing at all
EMPTY_REPLY_FALLBACK = "…Sorry, my mind wandered. Could you say that again?"
MCP_NOT_CONFIGURED = "MCP is not configured"

# ---------------------------------------------------------------- the library
ALREADY_IN_PROJECT = "This one is already in the project"
NO_PROJECT_TO_SAVE_TO = "This conversation is not in any project, so there is nowhere to save to"
PROJECT_HAS_SAME_NAME = "The project already has an entry with this name"
NO_FOLDER_FOR_LIBRARY = "This conversation is not in any folder, so there is nowhere to save library entries"
DEFAULT_VARIANT_TITLE = "Default"

# ---------------------------------------------------------------- names given by the app
NEW_CONVERSATION_TITLE = "New conversation"
CONVERSATION_WORD = "Conversation"
BRANCH_TITLE = "{title} (branch)"
MCP_CONVERSATION_TITLE = "MCP conversation"
MCP_DIRECT_TITLE = "MCP direct generation"
IMAGE_PLACEHOLDER = "[image]"

# ---------------------------------------------------------------- notices in the transcript
#: A token count once it is long: (unit, template) pairs, largest unit first;
#: the count divided by the first unit it reaches goes in as {n}. Below every
#: unit the plain number is shown.
TOKENS_SHORT_UNITS = [(1_000_000, "{n:.1f}M"), (1_000, "{n:.1f}k")]
CONTEXT_NEAR_LIMIT = (
    "This conversation has used about {used} tokens, close to the model's limit ({limit}). "
    "Past it, the earliest messages are dropped."
)
LONG_CONTEXT = "This conversation's context is over {threshold} tokens."
LONG_CONTEXT_PRICED = "This conversation's context is over {threshold} tokens."
REASONING_CLIPPED = "\n…({chars} more characters, cut off)"
#: the lead's routes, as the settings page names them
ROUTE_LABELS = {"deepseek": "DeepSeek official", "openrouter": "OpenRouter fallback"}

# ---------------------------------------------------------------- what a proposal's result says in the card
#: What applying a proposal did, one phrase per entry, joined with "; " and
#: shown on the proposal card. {title} / {name} = the entry's name.
PROPOSAL_CREATED_SETTING = "Created lore entry “{title}”"
PROPOSAL_UPDATED_SETTING = "Updated lore entry “{title}”"
PROPOSAL_CREATED_CHARACTER = "Created character “{name}”"
PROPOSAL_UPDATED_CHARACTER = "Updated character “{name}”"
PROPOSAL_CREATED_VARIANT = "Created variant “{name} · {title}”"
PROPOSAL_UPDATED_VARIANT = "Updated “{name} · {title}”"
#: a character and one of her variants, as a person reads them
ENTRY_VARIANT = "{name} · {title}"
#: A project write refused because the project already has that name: the
#: conversation's entry stays where it is. {name} = the entry's name.
PROPOSAL_NAME_TAKEN = "Not written: the project already has “{name}”"
#: between the phrases of one card's summary
PROPOSAL_SUMMARY_SEP = "; "

# ---------------------------------------------------------------- notes on a turn, as the inspector shows them
#: When the prompt sent is over NovelAI's limit, compressed or not: NovelAI
#: reads the first part of it only. {tokens} = the count, {limit} = the cap.
PROMPT_OVER_LIMIT = "Over NovelAI's prompt limit: {tokens}/{limit} tokens; NovelAI reads only the first part"
#: The lead exposed keys no entry answers to. {keys} = joined with 、.
EXPOSE_UNKNOWN = "No such entries: {keys}"
#: What the lead wrote into this conversation's library. {what} = one phrase
#: per entry, joined with 、.
LIBRARY_WRITTEN = "Written into this conversation's library: {what}"
PROPOSAL_NOT_RECORDED = "The lead AI's library proposal was not recorded: {error}"
PAINT_ONE_FAILED = "One of this batch of {n} pictures was not painted: {error}"
STOPPED_KEPT_PICTURES = "Stopped; the pictures already painted were kept"

# ---------------------------------------------------------------- a model call that failed, as the row's error
#: Thinking and answering share one token budget and this answer ran out of it.
#: {name} = provider label.
MODEL_OUT_OF_TOKENS = "The answer from {name} was cut off"
#: Nothing came back at all. {name} = provider label.
MODEL_EMPTY = "{name} returned nothing"
#: Every attempt came back unusable. {name} = provider label.
MODEL_NOTHING_USABLE = "{name} returned nothing usable several times in a row"
#: One answer ran past the wall-clock cap twice. {name} = the road, {minutes} =
#: settings.LLM_ANSWER_SECONDS in minutes.
MODEL_ANSWER_TOO_LONG = "One answer from {name} ran past {minutes} minutes and was cut short"
#: The answer was not JSON. The answer itself is kept on the row instead, where
#: the inspector can show it. {name} = provider label.
MODEL_UNREADABLE = "The answer from {name} could not be read"

# ---------------------------------------------------------------- continuous generation, as the transcript notes it
#: A run opens with one of these rows and closes with another, always, however
#: it ends. The pair is the point: half a run with no ending reads as a run
#: still going, and the browser is not always there to notice the difference.
CONTINUOUS_START = "Continuous generation started · {of} rounds"
#: Asked for a run with no quick phrase to send again. Said out loud rather
#: than returned from in silence — a run that never started looks exactly like
#: one that was never asked for.
CONTINUOUS_NO_PHRASE = "Continuous generation did not start: there is no quick phrase to send yet"
CONTINUOUS_HEADS = {
    "done": "Continuous generation finished",
    "stopped": "Continuous generation stopped",
    "failed": "Continuous generation interrupted",
    "replaced": "Continuous generation ended",     # a message the user typed took over
    "lost": "Continuous generation broke off",     # the backend stopped partway through
}
CONTINUOUS_SUMMARY = "{head} · {of} rounds · {done} succeeded · {failed} failed"

# ---------------------------------------------------------------- names on the settings page and in the admin
PROVIDER_DEEPSEEK = "DeepSeek"
PROVIDER_GROK = "Grok"
PROVIDER_FAKE = "Fake model (tests only)"
APP_NAME = "Vallotton"
ADMIN_SITE_HEADER = "Vallotton admin"
ADMIN_INDEX_TITLE = "Data"
ADMIN_LLM_USD = "Model spend (USD)"
