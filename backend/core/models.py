import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from django.db import models
from core import i18n, ui_text

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager


def attachment_upload_path(instance: "Attachment", filename: str) -> str:
    return f"conversations/{instance.message.conversation_id}/{instance.id}-{filename}"


def attachment_thumb_path(instance: "Attachment", filename: str) -> str:
    """
    Beside the original, so one conversation's files stay in one place.

    The name is the caller's — imaging.thumb_name puts the size in it, and a
    stored thumbnail whose name no longer matches is how the app knows to make
    it again. Overwriting the name here would throw that away, quietly, and
    leave every existing picture on whatever size it was first made at.
    """
    return f"conversations/{instance.message.conversation_id}/thumbs/{filename}"


class WithLines:
    """
    What a setting, a character and a costume share: they are entries.

    An entry is a note (what the person said, in their language, never sent
    to NovelAI), a positive tag list, a negative tag list and a prose line
    (English, for NovelAI). The note is the person's; the other three a model
    fills in from it (services/library_lines.py) and the person may edit.

    Whether a model still has to look at the entry is a question of time, not
    of content: the note changed after a model last filled the tags. It is
    never judged by whether there are tags -- an entry may have none on
    purpose, or some a person wrote by hand.
    """

    @property
    def lines_text(self) -> str:
        """the note"""
        return self.description if isinstance(self, Character) else self.content  # type: ignore[attr-defined]

    @property
    def key(self) -> str:
        """the entry's name, which is its identity within its layer"""
        return self.name if isinstance(self, Character) else self.title  # type: ignore[attr-defined]

    @property
    def lines_stale(self) -> bool:
        text_at: datetime | None = getattr(self, "text_edited_at")
        ai_at: datetime | None = getattr(self, "lines_ai_at")
        if not self.lines_text.strip() or text_at is None:
            return False
        return ai_at is None or ai_at < text_at


class Folder(models.Model):
    """
    A project folder. Folders nest, hold conversations, and — the real point —
    carry a library: 设定 (world entries) and 角色 (characters with variants),
    which a conversation inside the folder may draw on. A child folder sees its
    ancestors' library too.

    `position` orders siblings by hand (drag and drop); it is only meaningful
    within one parent.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    parent: "models.ForeignKey[Folder | None]" = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    position = models.IntegerField(default=0)
    # a folder is archived, never deleted — the same deal its conversations
    # get, so nothing a picture was drawn from can quietly disappear
    is_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        parent_id: uuid.UUID | None
        children: "RelatedManager[Folder]"
        conversations: "RelatedManager[Conversation]"
        settings: "RelatedManager[Setting]"
        characters: "RelatedManager[Character]"

    class Meta:
        ordering = ["position", "created_at"]

    def __str__(self) -> str:
        return self.name

    def ancestors(self) -> list["Folder"]:
        """Root-first chain up to and including this folder."""
        chain: list[Folder] = []
        node: Folder | None = self
        seen: set[uuid.UUID] = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)          # a cycle would otherwise hang the request
            chain.append(node)
            node = node.parent
        return list(reversed(chain))

    def breadcrumb(self) -> list[str]:
        return [f.name for f in self.ancestors()]


class Setting(WithLines, models.Model):
    """
    A 世界书 entry: a titled paragraph of natural language (pose, style, scene…),
    plus its tag lines.

    An entry lives in one of two layers. In a folder it is the project's: every
    conversation under that folder reads it. On a conversation it is that
    conversation's alone — the rain that only falls in this story, the costume
    she only wears this once — and it shadows a project entry of the same
    title. Same table, same shape; which layer is which foreign key is set.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.CASCADE, related_name="settings"
    )
    conversation = models.ForeignKey(
        "Conversation", null=True, blank=True, on_delete=models.CASCADE,
        related_name="own_settings",
    )
    title = models.CharField(max_length=120)
    content = models.TextField(blank=True, default="")
    positive = models.JSONField(default=list, blank=True)
    negative = models.JSONField(default=list, blank=True)
    prose = models.TextField(blank=True, default="")
    origin_seq = models.BigIntegerField(null=True, blank=True)
    #: What this setting is about -- 画风, 镜头, 场景, 光, 人物比例 or
    #: whatever the lead names; blank means 其他. A label for people and for
    #: the lead's own ordering; the app attaches no behaviour to it.
    category = models.CharField(max_length=40, blank=True, default="")
    #: when the text last changed, by anyone
    text_edited_at = models.DateTimeField(null=True, blank=True)
    #: when a model last wrote the lines, or read them and let them stand. A
    #: person's edit of the lines leaves this alone, so the line writer
    #: (services/library_lines.py) still gets to see what they did.
    lines_ai_at = models.DateTimeField(null=True, blank=True)
    #: when the line writer last tried this entry, so one that keeps failing
    #: is rested rather than retried every sweep
    lines_tried_at = models.DateTimeField(null=True, blank=True)
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        folder_id: uuid.UUID | None
        conversation_id: uuid.UUID | None

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            # one owner: a project entry has a folder, a conversation's own
            # entry has a conversation. Never both — the folder a conversation
            # entry was written in is the conversation's to know, and a copy
            # of it on the entry went stale the moment the conversation moved
            models.CheckConstraint(
                condition=models.Q(conversation__isnull=True) | models.Q(folder__isnull=True),
                name="setting_one_owner",
            ),
            # the title is the identity: the model edits by title, and an
            # unseen title means "create" — unique within a layer
            models.UniqueConstraint(
                fields=["folder", "title"], condition=models.Q(conversation__isnull=True),
                name="uniq_folder_setting_title",
            ),
            models.UniqueConstraint(
                fields=["conversation", "title"],
                condition=models.Q(conversation__isnull=False),
                name="uniq_conversation_setting_title",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class Character(WithLines, models.Model):
    """
    A 角色书 entry: a name, a description, and variants (outfits, poses…).
    A character is only ever used through one or more of its variants.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.CASCADE, related_name="characters"
    )
    #: Set on a conversation-layer character; see Setting.
    conversation = models.ForeignKey(
        "Conversation", null=True, blank=True, on_delete=models.CASCADE,
        related_name="own_characters",
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    #: what does not change with the outfit: identity, hair, eyes, skin, marks
    positive = models.JSONField(default=list, blank=True)
    negative = models.JSONField(default=list, blank=True)
    prose = models.TextField(blank=True, default="")
    origin_seq = models.BigIntegerField(null=True, blank=True)
    #: when the text last changed, by anyone
    text_edited_at = models.DateTimeField(null=True, blank=True)
    #: when a model last wrote the lines, or read them and let them stand. A
    #: person's edit of the lines leaves this alone, so the line writer
    #: (services/library_lines.py) still gets to see what they did.
    lines_ai_at = models.DateTimeField(null=True, blank=True)
    #: when the line writer last tried this entry, so one that keeps failing
    #: is rested rather than retried every sweep
    lines_tried_at = models.DateTimeField(null=True, blank=True)
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        folder_id: uuid.UUID | None
        conversation_id: uuid.UUID | None
        variants: "RelatedManager[CharacterVariant]"

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            # one owner, as for Setting
            models.CheckConstraint(
                condition=models.Q(conversation__isnull=True) | models.Q(folder__isnull=True),
                name="character_one_owner",
            ),
            models.UniqueConstraint(
                fields=["folder", "name"], condition=models.Q(conversation__isnull=True),
                name="uniq_folder_character_name",
            ),
            models.UniqueConstraint(
                fields=["conversation", "name"],
                condition=models.Q(conversation__isnull=False),
                name="uniq_conversation_character_name",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class CharacterVariant(WithLines, models.Model):
    """One look for a character. Variants are added and edited, never deleted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="variants"
    )
    title = models.CharField(max_length=120)
    content = models.TextField(blank=True, default="")
    #: this outfit: every garment and accessory
    positive = models.JSONField(default=list, blank=True)
    negative = models.JSONField(default=list, blank=True)
    prose = models.TextField(blank=True, default="")
    origin_seq = models.BigIntegerField(null=True, blank=True)
    #: when the text last changed, by anyone
    text_edited_at = models.DateTimeField(null=True, blank=True)
    #: when a model last wrote the lines, or read them and let them stand. A
    #: person's edit of the lines leaves this alone, so the line writer
    #: (services/library_lines.py) still gets to see what they did.
    lines_ai_at = models.DateTimeField(null=True, blank=True)
    #: when the line writer last tried this entry, so one that keeps failing
    #: is rested rather than retried every sweep
    lines_tried_at = models.DateTimeField(null=True, blank=True)
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        character_id: uuid.UUID

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["character", "title"], name="uniq_character_variant_title"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.character_id}:{self.title}"


class AppSettings(models.Model):
    """
    One row, holding what belongs to the app rather than to any conversation.

    That is the generation defaults a new conversation starts from, and the
    language the app speaks (core.i18n). They are a row and not a settings.py
    constant because the user sets them from the settings page, and not
    localStorage because a default that only applies in the browser that set
    it is not a default.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    gen_defaults = models.JSONField(null=True, blank=True)
    #: a code from core.i18n.CODES; anything else reads as the default
    language = models.CharField(max_length=8, default=i18n.DEFAULT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "app settings"

    def __str__(self) -> str:
        return "app settings"

    @classmethod
    def get(cls) -> "AppSettings":
        row, _ = cls.objects.get_or_create(id=1)
        return row


class Conversation(models.Model):
    SOURCE_CHOICES = [("web", "web"), ("mcp", "mcp")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200, default=ui_text.new_conversation_title)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="web")
    # Hidden from the normal list, never auto-deleted.
    is_archived = models.BooleanField(default=False)
    # Project placement. null folder = loose at the root of the projects page.
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="conversations",
    )
    position = models.IntegerField(default=0)
    # What this conversation draws on. A character counts as selected when at
    # least one of its variants is — which is exactly the "pick ≥1 variant"
    # rule, expressed in the data instead of in validation.
    selected_settings = models.ManyToManyField(Setting, blank=True, related_name="+")
    selected_variants = models.ManyToManyField(CharacterVariant, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    #: A continuous run in progress, or null: {left, of, text, free, done,
    #: failed}. It lives here rather than in the browser because a run of ten
    #: pictures is exactly the kind of thing you start and walk away from —
    #: kept client-side, a refresh silently ended it half way through.
    continuous_run = models.JSONField(null=True, blank=True)
    #: 出图数量 / 反思轮数 / 连续生成 / 常驻参考, for this conversation alone.
    #: They were one set of values in the browser, shared by every conversation
    #: at once; they are decisions about a piece of work. Null on rows written
    #: before this existed — core.services.gensettings is what turns that into
    #: the defaults, and nothing else may read this column raw.
    gen_settings = models.JSONField(null=True, blank=True)

    if TYPE_CHECKING:  # reverse relation, for the type checker only
        messages: "RelatedManager[Message]"
        own_settings: "RelatedManager[Setting]"
        own_characters: "RelatedManager[Character]"
        folder_id: uuid.UUID | None

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return f"{self.title} ({str(self.id)[:8]})"


class Message(models.Model):
    # "system" rows are notes the APP writes into the transcript (e.g. the
    # context-budget warning). They are shown to the user but never sent to
    # the model and never copied into a branch.
    ROLE_CHOICES = [("user", "user"), ("assistant", "assistant"), ("system", "system")]
    STATUS_CHOICES = [
        ("done", "done"),
        ("pending", "pending"),
        ("error", "error"),
        ("cancelled", "cancelled"),
    ]
    KIND_CHOICES = [("text", "text"), ("image", "image")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    # Monotonic per-conversation ordering key (assigned under row lock).
    seq = models.BigIntegerField()
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="done")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="text")
    # UI progress hint while pending: "thinking" (lead) / "painting" (novelai)
    # composite while reflection runs: "reflecting:2/5:painting:0/1"
    phase = models.CharField(max_length=64, blank=True, default="")
    content = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")
    # Client-generated idempotency key so a network retry can't duplicate a send.
    client_msg_id = models.CharField(max_length=64, blank=True, default="")
    # True when superseded by a rerun (hidden from the transcript).
    archived = models.BooleanField(default=False)
    #: the lead model's whole answer, as it came back. Named for what it is
    #: rather than for whoever returned it — the vendor is a setting.
    model_raw = models.JSONField(null=True, blank=True)
    # {"base_prompt": ..., "character_prompts": [...], "negative_prompt": ...,
    #  "size": ..., "n_images": ...} — what was (or will be) sent to NovelAI.
    nai_prompts = models.JSONField(null=True, blank=True)
    #: The same prompt before assembly, in slots — see services/prompt.py.
    #: nai_prompts is what was sent and stays the thing 重绘 replays; this is
    #: what it was made of, which is what an edit next turn has to work on.
    #: Null on every row written before slots existed, and on 重绘 of one.
    prompt = models.JSONField(null=True, blank=True)
    #: Which of this turn's pictures is the main one, as the attachment's
    #: `position` (not a list index: a reflection round can discard the first
    #: pictures, and positions keep counting).
    #: Everything that refers to "the picture of this turn" — an edit next
    #: turn, the seed the next picture follows — means this one. The first
    #: picture by default; 重绘 appends and does not move it.
    main_index = models.IntegerField(default=0)
    cancel_requested = models.BooleanField(default=False)
    # how many images to paint; the app decides this, never a model
    force_n_images = models.IntegerField(null=True, blank=True)
    #: Whether 常驻参考 was on when this turn was sent. Nothing is copied when
    #: it is — the flag only widens which of the conversation's reference
    #: pictures this turn's history carries, so turning it off goes straight
    #: back to normal and no picture is ever in the context twice.
    sticky_refs = models.BooleanField(default=False)
    #: How many reflection rounds this turn was allowed, from the slider at the
    #: moment it was sent. 0 or null means the feature was off. Carried on the
    #: row rather than read live so a 重跑 replays the turn it is replacing.
    force_reflection_rounds = models.IntegerField(null=True, blank=True)
    #: What reflection actually did: one entry per round, each {round, reply,
    #: redrew}. Shown under the turn and never sent to a model — the whole
    #: point of reflection is that it stays out of the conversation.
    reflection_log = models.JSONField(null=True, blank=True)
    #: On a USER row the app sent on the user's behalf, in continuous mode:
    #: {"round": 2, "of": 5}. The model is told nothing about it — it reads the
    #: same words it would have read if they had been typed — so this exists
    #: only so the transcript can say who actually pressed send.
    auto_sent = models.JSONField(null=True, blank=True)
    # assistant rows: prompt tokens the model actually consumed for this turn
    # (i.e. how much of the context window this conversation now occupies)
    prompt_tokens = models.IntegerField(null=True, blank=True)
    # system rows: which note this is, so each kind is only ever emitted once
    notice_kind = models.CharField(max_length=32, blank=True, default="")
    # system rows: what happened, as a key and the numbers (core.services.notices),
    # so the sentence can be made in the reader's language when the row is read.
    # `content` keeps the sentence as it was written; rows older than this
    # column have only that.
    notice = models.JSONField(null=True, blank=True)
    # what the lead model briefed the drawing model with, in natural language
    # ({main, pose, scene, camera, style, notes}) — kept so the transcript can
    # show the intent, not just the resulting tags
    draw_brief = models.JSONField(null=True, blank=True)
    # which library entries were selected when this turn ran, so an image can
    # always say what it was drawn from even after the selection changes
    selection_snapshot = models.JSONField(null=True, blank=True)
    # This conversation's own library entries as they stood after this turn,
    # every one of them, lines and ids included. A branch made at this message
    # rebuilds the layer from it, so the branch starts with the entries this
    # point in the story had -- not the ones added later, and not none. Never
    # sent to the browser: it is history for branching, not for reading.
    library_snapshot = models.JSONField(null=True, blank=True)
    #: assistant rows: the conversation's own layer as this turn FOUND it --
    #: before the lead's edits of this turn, after whatever the user changed by
    #: hand since the previous turn. A branch made at the previous message
    #: starts from this: the layer as it stood when the user would have sent
    #: the next message. Never sent to the browser.
    library_before = models.JSONField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    #: assistant rows: this turn has already been counted by the continuous run
    #: it belongs to. Two callers can reach that accounting for the same turn —
    #: the worker that finished it, and the reaper that gave up on it — and
    #: before this flag both were served: the run spent two rounds for one turn
    #: and queued two replacements, which then painted side by side.
    run_settled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:  # FK id attribute + reverse relation, type checker only
        conversation_id: uuid.UUID
        attachments: "RelatedManager[Attachment]"

    class Meta:
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "seq"], name="uniq_conversation_seq"
            ),
            models.UniqueConstraint(
                fields=["conversation", "client_msg_id"],
                condition=~models.Q(client_msg_id=""),
                name="uniq_conversation_client_msg_id",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "archived", "seq"]),
        ]

    def __str__(self) -> str:
        return f"{self.role}#{self.seq} {self.status}"


class Attachment(models.Model):
    KIND_CHOICES = [("upload", "upload"), ("generated", "generated")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="attachments"
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    if TYPE_CHECKING:
        message_id: uuid.UUID
    # Historical: a separate vision model used to describe every upload and the
    # text went to the lead model in place of the picture. The lead models read
    # well enough now that the picture goes as a picture, so nothing writes
    # this any more — the column stays because old turns still hold theirs.
    description = models.TextField(blank=True, default="")
    file = models.FileField(upload_to=attachment_upload_path, max_length=400)
    # A small JPEG of the same picture, for the places that show it at 200-410px
    # and were being sent 1.3 MB of PNG to do it. Written the first time it is
    # asked for rather than at generation time, so pictures that already exist
    # get one too, and a turn never waits on one.
    #
    # It is a file and not a column because it is an image, and images live in
    # MEDIA_ROOT. The database holds text.
    thumb = models.FileField(
        upload_to=attachment_thumb_path, max_length=400, blank=True, default=""
    )
    mime = models.CharField(max_length=64, default="image/png")
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    seed = models.BigIntegerField(null=True, blank=True)
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "created_at"]

    def __str__(self) -> str:
        return f"{self.kind} {self.file.name}"


class QuickPhrase(models.Model):
    """Global quick phrases: click in the UI to append `content` to the composer."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=100)
    content = models.TextField()
    #: Hand-ordered, the way folders are. They are buttons someone reaches for
    #: without looking, so the order is muscle memory and belongs to the person
    #: rather than to when they happened to write each one.
    position = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # created_at breaks ties, so phrases made before there were positions
        # keep the order they already had
        ordering = ["position", "created_at"]

    def __str__(self) -> str:
        return self.title


class LibraryProposal(models.Model):
    """
    An edit to the library that the lead model wants to make.

    Nothing is written until the user says so: the proposal sits next to the
    message that raised it until it is approved or dismissed, and it survives
    the user carrying on with the conversation.
    """

    STATUS_CHOICES = [
        ("pending", "pending"),
        ("applied", "applied"),
        ("rejected", "rejected"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="proposals"
    )
    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name="proposals"
    )
    # the folder the edits would land in (the conversation's folder at the time)
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.CASCADE)
    # {"settings": [{key, category?, note, positive, negative, prose}],
    #  "characters": [{key, note, positive, negative, prose,
    #   variants: [{key, note, positive, negative, prose}]}]}
    # keys carry the project prefix "@"; an item without the tag fields leaves
    # them to the patrol (proposals carried over from before entries, 0031)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    applied_summary = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        conversation_id: uuid.UUID
        message_id: uuid.UUID
        folder_id: uuid.UUID | None

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"proposal {self.status} for {self.conversation_id}"


class DailySpend(models.Model):
    """Local accounting of API usage, one row per day."""

    day = models.DateField(unique=True)
    # Cost in "usd ticks" (1 tick = 1e-10 USD), the unit xAI reports. Providers
    # that do not price their responses leave this at zero and still count the
    # request — their spend is read from the account balance instead.
    llm_ticks = models.BigIntegerField(default=0)
    llm_requests = models.IntegerField(default=0)
    nai_images = models.IntegerField(default=0)

    class Meta:
        ordering = ["-day"]

    def __str__(self) -> str:
        return f"{self.day}: llm={self.llm_requests}req nai={self.nai_images}img"


class LoginAttempt(models.Model):
    """DB-backed login throttle (works across gunicorn workers)."""

    key = models.CharField(max_length=80, unique=True)  # "ip:1.2.3.4"
    fails = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.key} fails={self.fails}"
