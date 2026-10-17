"""
Projects: folders, their library (设定 / 角色 + 变体), what a conversation
selects from it, and the edit proposals the lead model raises.

Ordering model
--------------
Everything on the projects page is hand-ordered by `position` within one
parent, and folders always sort ahead of conversations. New items land where
the brief says: a new folder at the very top, a new conversation after the
last folder, an item dropped into a folder at the end of its kind.
"""

from __future__ import annotations

import json
import uuid

from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core.http import (
    api_login_required,
    conversation_to_dict,
    folder_breadcrumbs,
    json_err,
    json_ok,
)
from core.services import library, characters as characters_service, proposals
from core.models import (
    Character,
    CharacterVariant,
    Conversation,
    Folder,
    LibraryProposal,
    Setting,
)
from core import ui_text

#: what a PATCH of an entry may carry
ENTRY_KEYS = {"title", "name", "content", "description", "category", "positive", "negative", "prose"}
MAX_NAME = 120
MAX_DEPTH = 8


# ---------------------------------------------------------------- serializers


def folder_to_dict(f: Folder, *, with_path: bool = False) -> dict:
    """`with_path` walks the ancestor chain, so only use it for single items."""
    data = {
        "id": str(f.id),
        "name": f.name,
        "parent_id": str(f.parent_id) if f.parent_id else None,
        "position": f.position,
        "is_archived": f.is_archived,
    }
    if with_path:
        data["path"] = f.breadcrumb()
    return data


def _entry_fields(obj) -> dict:
    """The fields every entry has: the tags, the prose, where it came from,
    and whether a model still has to fill it from the note."""
    return {
        "positive": library.split_tags(obj.positive),
        "negative": library.split_tags(obj.negative),
        "prose": obj.prose or "",
        "origin_seq": obj.origin_seq,
        # the note was edited and the tags have not been rewritten from it
        # yet (services/library_lines.py does that on the sweeper's clock)
        "lines_stale": obj.lines_stale,
    }


def variant_to_dict(v: CharacterVariant) -> dict:
    return {
        "id": str(v.id),
        "title": v.title,
        "content": v.content,
        **_entry_fields(v),
        "position": v.position,
    }


def character_to_dict(c: Character) -> dict:
    return {
        "id": str(c.id),
        "folder_id": str(c.folder_id) if c.folder_id else None,
        # set on a conversation-layer entry: it belongs to that conversation alone
        "conversation_id": str(c.conversation_id) if c.conversation_id else None,
        "name": c.name,
        "description": c.description,
        **_entry_fields(c),
        "position": c.position,
        "variants": [variant_to_dict(v) for v in c.variants.all()],
    }


def setting_to_dict(s: Setting) -> dict:
    return {
        "id": str(s.id),
        "folder_id": str(s.folder_id) if s.folder_id else None,
        "conversation_id": str(s.conversation_id) if s.conversation_id else None,
        "title": s.title,
        "content": s.content,
        "category": s.category or "",
        **_entry_fields(s),
        "position": s.position,
    }


def proposal_to_dict(p: LibraryProposal) -> dict:
    return {
        "id": str(p.id),
        "message_id": str(p.message_id),
        "status": p.status,
        "payload": p.payload,
        "applied_summary": p.applied_summary,
        # where it WOULD land — the conversation's folder when it was raised.
        # null means there is nowhere to put it, and the card says so rather
        # than letting the button be pressed and fail.
        "folder_id": str(p.folder_id) if p.folder_id else None,
        "created_at": p.created_at.isoformat(),
    }


# ---------------------------------------------------------------- helpers


def _body(request) -> dict | None:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return None


def _folder_depth(folder: Folder | None) -> int:
    return len(folder.ancestors()) if folder else 0


def _descendant_ids(folder: Folder) -> set[uuid.UUID]:
    """Every folder at or below `folder` — used to refuse cyclic moves."""
    out: set[uuid.UUID] = {folder.id}
    frontier = [folder.id]
    while frontier:
        kids = list(
            Folder.objects.filter(parent_id__in=frontier).values_list("id", flat=True)
        )
        kids = [k for k in kids if k not in out]
        out.update(kids)
        frontier = kids
    return out


def inherited_library(folder: Folder | None) -> tuple[list[Setting], list[Character]]:
    """
    The library a conversation in `folder` may draw on: this folder's entries
    plus every ancestor's, nearest folder last so the most specific entries
    read at the bottom of a list.
    """
    if folder is None:
        return [], []
    chain = folder.ancestors()  # root-first
    ids = [f.id for f in chain]
    settings = list(Setting.objects.filter(folder_id__in=ids, conversation__isnull=True))
    # a character with no variants can be seen and never switched on — see
    # core.services.characters. Repaired on the way past rather than guarded
    # at each entrance, so no caller has to know the rule.
    characters_service.repair_in(ids)
    characters = list(
        Character.objects.filter(folder_id__in=ids, conversation__isnull=True)
        .prefetch_related("variants")
    )
    order: dict[uuid.UUID | None, int] = {fid: i for i, fid in enumerate(ids)}
    settings.sort(key=lambda s: (order.get(s.folder_id, 0), s.position, s.created_at))
    characters.sort(key=lambda c: (order.get(c.folder_id, 0), c.position, c.created_at))
    return settings, characters


# ---------------------------------------------------------------- folders


@api_login_required
@require_http_methods(["GET", "POST"])
def folders(request):
    if request.method == "GET":
        items = list(Folder.objects.all())
        return json_ok({"folders": [folder_to_dict(f, with_path=True) for f in items]})

    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    name = str(body.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        return json_err(ui_text.NAME_REQUIRED.format(max=MAX_NAME))

    parent = None
    if body.get("parent_id"):
        try:
            parent = Folder.objects.filter(id=uuid.UUID(str(body["parent_id"]))).first()
        except ValueError:
            return json_err(ui_text.BAD_PARENT_ID)
        if not parent:
            return json_err(ui_text.NO_PARENT_FOLDER, status=404)
        if _folder_depth(parent) >= MAX_DEPTH:
            return json_err(ui_text.FOLDER_TOO_DEEP.format(max=MAX_DEPTH))

    with transaction.atomic():
        # a new folder goes to the very top of its level
        top = Folder.objects.filter(parent=parent).aggregate(m=Min("position"))["m"]
        folder = Folder.objects.create(
            name=name, parent=parent, position=(top or 0) - 1
        )
    return json_ok(folder_to_dict(folder, with_path=True), status=201)


@api_login_required
@require_http_methods(["PATCH"])
def folder_detail(request, folder_id: uuid.UUID):
    """
    Rename a folder, or move it.

    No DELETE, for the same reason conversations have none: folders are
    archived, never deleted. The endpoint that used to be here took the whole
    library with it, without a confirmation and without a trace.
    """
    folder = Folder.objects.filter(id=folder_id).first()
    if not folder:
        return json_err(ui_text.NO_FOLDER, status=404)

    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    name = str(body.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        return json_err(ui_text.NAME_REQUIRED.format(max=MAX_NAME))
    folder.name = name
    folder.save(update_fields=["name", "updated_at"])
    return json_ok(folder_to_dict(folder, with_path=True))


def _group_roots(folders: list[Folder]) -> list[Folder]:
    """
    The archived folders worth listing: the ones you actually archived.

    Archiving is recursive, so a folder's children carry the flag too. Listing
    every one of them would bury the thing you put away under its own
    contents, so only the top of each archived group is shown — and that is
    also the one to bring back.
    """
    archived = {f.id for f in folders if f.is_archived}
    return [
        f for f in folders if f.is_archived and f.parent_id not in archived
    ]


@api_login_required
@require_GET
def project_tree(request):
    """
    Everything the projects page draws: folders and their conversations.

    `scope=archived` lists the archived folders on their own, so they can be
    found and brought back.
    """
    all_folders = list(Folder.objects.all())

    if request.GET.get("scope") == "archived":
        return json_ok(
            {
                "folders": [
                    folder_to_dict(f, with_path=True) for f in _group_roots(all_folders)
                ],
                "conversations": [],
            }
        )

    folders_qs = [f for f in all_folders if not f.is_archived]
    archived_ids = {f.id for f in all_folders if f.is_archived}
    convs = list(
        Conversation.objects.filter(is_archived=False)
        .exclude(folder_id__in=archived_ids)
        .only("id", "title", "source", "folder", "position", "updated_at", "created_at")
    )
    convs.sort(key=lambda c: (c.position, c.created_at))
    return json_ok(
        {
            "folders": [folder_to_dict(f) for f in folders_qs],
            "conversations": [
                {
                    "id": str(c.id),
                    "title": c.title,
                    "source": c.source,
                    "folder_id": str(c.folder_id) if c.folder_id else None,
                    "position": c.position,
                }
                for c in convs
            ],
        }
    )


@api_login_required
@require_POST
def move_item(request):
    """
    Place one dragged item. The client sends where it landed; the server
    re-numbers that destination so positions stay dense and folders keep
    sorting ahead of conversations.

    body: {kind: "folder"|"conversation", id, parent_id|null, index}
    """
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    kind = body.get("kind")
    if kind not in ("folder", "conversation"):
        return json_err(ui_text.KIND_NOT_FOLDER_OR_CONVERSATION)

    try:
        item_id = uuid.UUID(str(body.get("id")))
        parent_id = (
            uuid.UUID(str(body["parent_id"])) if body.get("parent_id") else None
        )
    except (ValueError, TypeError):
        return json_err(ui_text.BAD_ID_OR_PARENT_ID)

    parent = Folder.objects.filter(id=parent_id).first() if parent_id else None
    if parent_id and not parent:
        return json_err(ui_text.NO_TARGET_FOLDER, status=404)

    try:
        index = int(body.get("index", 0))
    except (TypeError, ValueError):
        index = 0

    with transaction.atomic():
        if kind == "folder":
            folder = Folder.objects.select_for_update().filter(id=item_id).first()
            if not folder:
                return json_err(ui_text.NO_FOLDER, status=404)
            if parent and parent.id in _descendant_ids(folder):
                return json_err(ui_text.FOLDER_INTO_ITSELF)
            if parent and _folder_depth(parent) >= MAX_DEPTH:
                return json_err(ui_text.FOLDER_TOO_DEEP.format(max=MAX_DEPTH))
            folder.parent = parent
            folder.save(update_fields=["parent", "updated_at"])
            siblings = [
                f for f in Folder.objects.filter(parent=parent) if f.id != folder.id
            ]
            index = max(0, min(index, len(siblings)))
            siblings.insert(index, folder)
            for i, f in enumerate(siblings):
                Folder.objects.filter(id=f.id).update(position=i)
        else:
            conv = Conversation.objects.select_for_update().filter(id=item_id).first()
            if not conv:
                return json_err(ui_text.NO_CONVERSATION, status=404)
            conv.folder = parent
            conv.save(update_fields=["folder", "updated_at"])
            # the same move as set_conversation_folder: entries of the old
            # project are out of reach and their selections go
            library.rehome(conv)
            siblings = [
                c
                for c in Conversation.objects.filter(folder=parent, is_archived=False)
                if c.id != conv.id
            ]
            siblings.sort(key=lambda c: (c.position, c.created_at))
            index = max(0, min(index, len(siblings)))
            siblings.insert(index, conv)
            for i, c in enumerate(siblings):
                Conversation.objects.filter(id=c.id).update(position=i)

    return json_ok({"ok": True})


# ---------------------------------------------------------------- library


@api_login_required
@require_POST
def archive_folder(request, folder_id: uuid.UUID):
    """
    Put a folder away, or bring it back — with everything inside it.

    Archiving reaches all the way down: every folder below it and every
    conversation in any of them gets the flag too. Half-archiving a tree
    would leave children stranded at the root, which is not what putting
    something away means.
    """
    folder = Folder.objects.filter(id=folder_id).first()
    if not folder:
        return json_err(ui_text.NO_FOLDER, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    archived = bool(body.get("archived", True))

    with transaction.atomic():
        ids = list(_descendant_ids(folder))
        Folder.objects.filter(id__in=ids).update(
            is_archived=archived, updated_at=timezone.now()
        )
        Conversation.objects.filter(folder_id__in=ids).update(
            is_archived=archived, updated_at=timezone.now()
        )
        folder.refresh_from_db()
        if not archived and folder.parent_id:
            # coming back under a parent that is still put away would leave it
            # invisible; move it to the root, where it can be found
            parent = Folder.objects.filter(id=folder.parent_id).first()
            if parent is None or parent.is_archived:
                folder.parent = None
                folder.save(update_fields=["parent", "updated_at"])

    folder.refresh_from_db()
    return json_ok(folder_to_dict(folder, with_path=True))


@api_login_required
@require_GET
def archive(request):
    """
    Everything that has been put away, folders and conversations together,
    newest first. Nothing here is ordered by hand, so nothing here drags.
    """
    # only the tops of archived groups, and only conversations archived in
    # their own right — the rest came along for the ride and go back the same way
    roots = _group_roots(list(Folder.objects.all()))
    roots.sort(key=lambda f: f.updated_at, reverse=True)
    archived_folder_ids = {f.id for f in Folder.objects.filter(is_archived=True)}
    convs = [
        c
        for c in Conversation.objects.filter(is_archived=True).order_by("-updated_at")
        if c.folder_id not in archived_folder_ids
    ]
    # every path built once, rather than one ancestor chain per row
    crumbs = folder_breadcrumbs()
    return json_ok(
        {
            "folders": [
                {**folder_to_dict(f, with_path=True), "updated_at": f.updated_at.isoformat()}
                for f in roots
            ],
            # with the paths built once, not one ancestor chain per row
            "conversations": [
                conversation_to_dict(c, crumbs=crumbs) for c in convs
            ],
        }
    )


@api_login_required
@require_GET
def folder_library(request, folder_id: uuid.UUID):
    """This folder's own entries, plus what it inherits from its ancestors."""
    folder = Folder.objects.filter(id=folder_id).first()
    if not folder:
        return json_err(ui_text.NO_FOLDER, status=404)
    # the project's own entries: a conversation's own entry carries the folder
    # it was written in, and is not the project's to show or edit
    own_settings = list(folder.settings.filter(conversation__isnull=True))
    characters_service.repair_in([folder.id])
    own_characters = list(folder.characters.filter(conversation__isnull=True).prefetch_related("variants"))
    inh_settings, inh_characters = inherited_library(folder.parent)
    return json_ok(
        {
            "folder": folder_to_dict(folder, with_path=True),
            "settings": [setting_to_dict(s) for s in own_settings],
            "characters": [character_to_dict(c) for c in own_characters],
            "inherited": {
                "settings": [setting_to_dict(s) for s in inh_settings],
                "characters": [character_to_dict(c) for c in inh_characters],
            },
        }
    )


@api_login_required
@require_http_methods(["POST"])
def create_setting(request, folder_id: uuid.UUID):
    folder = Folder.objects.filter(id=folder_id).first()
    if not folder:
        return json_err(ui_text.NO_FOLDER, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    title = str(body.get("title") or "").strip()
    if not title or len(title) > MAX_NAME:
        return json_err(ui_text.TITLE_REQUIRED.format(max=MAX_NAME))
    if Setting.objects.filter(folder=folder, conversation__isnull=True, title=title).exists():
        return json_err(ui_text.TITLE_TAKEN)
    pos = (folder.settings.aggregate(m=Max("position"))["m"] or 0) + 1
    content = str(body.get("content") or "")
    s = Setting.objects.create(
        folder=folder, title=title, content=content, position=pos,
        text_edited_at=timezone.now() if content.strip() else None,
    )
    return json_ok(setting_to_dict(s), status=201)


@api_login_required
@require_http_methods(["PATCH", "DELETE"])
def setting_detail(request, setting_id: uuid.UUID):
    s = Setting.objects.filter(id=setting_id).first()
    if not s:
        return json_err(ui_text.NO_SETTING, status=404)
    if request.method == "DELETE":
        s.delete()
        return json_ok({"ok": True})
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    fields = []
    if "title" in body:
        title = str(body.get("title") or "").strip()
        if not title or len(title) > MAX_NAME:
            return json_err(ui_text.TITLE_REQUIRED.format(max=MAX_NAME))
        if _layer_filter(Setting, s).filter(title=title).exclude(id=s.id).exists():
            return json_err(ui_text.TITLE_TAKEN)
        s.title = title
        fields.append("title")
    if "content" in body and library.set_text(s, str(body.get("content") or "")):
        fields += ["content", "text_edited_at"]
    obj = s
    if "category" in body and hasattr(obj, "category"):
        setattr(obj, "category", str(body.get("category") or "").strip()[:40])
        fields.append("category")
    for key in ("positive", "negative"):
        if key in body:
            setattr(obj, key, library.split_tags(body.get(key)))
            fields.append(key)
    if "prose" in body:
        obj.prose = str(body.get("prose") or "").strip()
        fields.append("prose")
    if not fields:
        # nothing changed, but nothing was wrong either: text sent back as it
        # was is not an edit, and not an error
        if not body.keys() & ENTRY_KEYS:
            return json_err(ui_text.NOTHING_TO_UPDATE)
        return json_ok(setting_to_dict(s))
    s.save(update_fields=fields + ["updated_at"])
    return json_ok(setting_to_dict(s))


@api_login_required
@require_POST
def create_character(request, folder_id: uuid.UUID):
    folder = Folder.objects.filter(id=folder_id).first()
    if not folder:
        return json_err(ui_text.NO_FOLDER, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    name = str(body.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        return json_err(ui_text.NAME_REQUIRED.format(max=MAX_NAME))
    if Character.objects.filter(folder=folder, conversation__isnull=True, name=name).exists():
        return json_err(ui_text.NAME_TAKEN)
    pos = (folder.characters.aggregate(m=Max("position"))["m"] or 0) + 1
    with transaction.atomic():
        description = str(body.get("description") or "")
        c = Character.objects.create(
            folder=folder,
            name=name,
            description=description,
            position=pos,
            text_edited_at=timezone.now() if description.strip() else None,
        )
        # a character is only usable through a variant, so it never starts empty
        v_content = str(body.get("variant_content") or "")
        CharacterVariant.objects.create(
            character=c,
            title=str(body.get("variant_title") or ui_text.DEFAULT_VARIANT_TITLE),
            content=v_content,
            position=1,
            text_edited_at=timezone.now() if v_content.strip() else None,
        )
    c.refresh_from_db()
    return json_ok(character_to_dict(c), status=201)


@api_login_required
@require_http_methods(["PATCH", "DELETE"])
def character_detail(request, character_id: uuid.UUID):
    c = Character.objects.filter(id=character_id).prefetch_related("variants").first()
    if not c:
        return json_err(ui_text.NO_CHARACTER, status=404)
    if request.method == "DELETE":
        c.delete()
        return json_ok({"ok": True})
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    fields = []
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name or len(name) > MAX_NAME:
            return json_err(ui_text.NAME_REQUIRED.format(max=MAX_NAME))
        if _layer_filter(Character, c).filter(name=name).exclude(id=c.id).exists():
            return json_err(ui_text.NAME_TAKEN)
        c.name = name
        fields.append("name")
    if "description" in body and library.set_text(c, str(body.get("description") or "")):
        fields += ["description", "text_edited_at"]
    obj = c
    if "category" in body and hasattr(obj, "category"):
        setattr(obj, "category", str(body.get("category") or "").strip()[:40])
        fields.append("category")
    for key in ("positive", "negative"):
        if key in body:
            setattr(obj, key, library.split_tags(body.get(key)))
            fields.append(key)
    if "prose" in body:
        obj.prose = str(body.get("prose") or "").strip()
        fields.append("prose")
    if not fields:
        # nothing changed, but nothing was wrong either: text sent back as it
        # was is not an edit, and not an error
        if not body.keys() & ENTRY_KEYS:
            return json_err(ui_text.NOTHING_TO_UPDATE)
        return json_ok(character_to_dict(c))
    c.save(update_fields=fields + ["updated_at"])
    c.refresh_from_db()
    return json_ok(character_to_dict(c))


@api_login_required
@require_POST
def create_variant(request, character_id: uuid.UUID):
    c = Character.objects.filter(id=character_id).first()
    if not c:
        return json_err(ui_text.NO_CHARACTER, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    title = str(body.get("title") or "").strip()
    if not title or len(title) > MAX_NAME:
        return json_err(ui_text.TITLE_REQUIRED.format(max=MAX_NAME))
    if CharacterVariant.objects.filter(character=c, title=title).exists():
        return json_err(ui_text.VARIANT_TITLE_TAKEN)
    pos = (c.variants.aggregate(m=Max("position"))["m"] or 0) + 1
    content = str(body.get("content") or "")
    v = CharacterVariant.objects.create(
        character=c, title=title, content=content, position=pos,
        text_edited_at=timezone.now() if content.strip() else None,
    )
    return json_ok(variant_to_dict(v), status=201)


@api_login_required
@require_http_methods(["PATCH", "DELETE"])
def variant_detail(request, variant_id: uuid.UUID):
    """
    Edit or remove one costume.

    "Variants are only ever edited, never deleted" is a rule about the MODEL:
    proposals.apply adds and updates, and has no way to take anything out of
    the library. That rule had been written here as though it bound the user
    too, which left a variant removable only by deleting the character it
    belonged to.

    Deleting one is safe for the same reason deleting a setting is: what a past
    turn was drawn from lives in that message's own selection_snapshot, which
    is a copy. The only live reference is a conversation's current selection,
    and dropping out of that is what deleting is supposed to mean.
    """
    v = CharacterVariant.objects.filter(id=variant_id).first()
    if not v:
        return json_err(ui_text.NO_VARIANT, status=404)
    if request.method == "DELETE":
        character_id_of = v.character_id
        v.delete()
        # not a second rule: the same repair the reads do, run now so the
        # reply already shows the 默认 that took its place instead of it
        # turning up a moment later
        characters_service.repair(Character.objects.filter(id=character_id_of))
        return json_ok({"ok": True})
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    fields = []
    if "title" in body:
        title = str(body.get("title") or "").strip()
        if not title or len(title) > MAX_NAME:
            return json_err(ui_text.TITLE_REQUIRED.format(max=MAX_NAME))
        if CharacterVariant.objects.filter(character_id=v.character_id, title=title).exclude(id=v.id).exists():
            return json_err(ui_text.VARIANT_TITLE_TAKEN)
        v.title = title
        fields.append("title")
    if "content" in body and library.set_text(v, str(body.get("content") or "")):
        fields += ["content", "text_edited_at"]
    obj = v
    if "category" in body and hasattr(obj, "category"):
        setattr(obj, "category", str(body.get("category") or "").strip()[:40])
        fields.append("category")
    for key in ("positive", "negative"):
        if key in body:
            setattr(obj, key, library.split_tags(body.get(key)))
            fields.append(key)
    if "prose" in body:
        obj.prose = str(body.get("prose") or "").strip()
        fields.append("prose")
    if not fields:
        # nothing changed, but nothing was wrong either: text sent back as it
        # was is not an edit, and not an error
        if not body.keys() & ENTRY_KEYS:
            return json_err(ui_text.NOTHING_TO_UPDATE)
        return json_ok(variant_to_dict(v))
    v.save(update_fields=fields + ["updated_at"])
    return json_ok(variant_to_dict(v))


# ---------------------------------------------------------------- the conversation layer


def _layer_filter(model, entry):
    """The rows an entry's name must be unique among: its own layer."""
    if entry.conversation_id:
        return model.objects.filter(conversation_id=entry.conversation_id)
    return model.objects.filter(folder_id=entry.folder_id, conversation__isnull=True)


@api_login_required
@require_POST
def create_conversation_setting(request, conv_id: uuid.UUID):
    """A setting that belongs to this conversation alone."""
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    title = str(body.get("title") or "").strip()
    if not title or len(title) > MAX_NAME:
        return json_err(ui_text.TITLE_REQUIRED.format(max=MAX_NAME))
    if Setting.objects.filter(conversation=conv, title=title).exists():
        return json_err(ui_text.TITLE_TAKEN)
    pos = (conv.own_settings.aggregate(m=Max("position"))["m"] or 0) + 1
    content = str(body.get("content") or "")
    s = Setting.objects.create(
        folder=None, conversation=conv, title=title, content=content,
        category=str(body.get("category") or "").strip()[:40],
        positive=library.split_tags(body.get("positive")), negative=library.split_tags(body.get("negative")),
        prose=str(body.get("prose") or "").strip(),
        position=pos, text_edited_at=timezone.now() if content.strip() else None,
    )
    conv.selected_settings.add(s)
    return json_ok(setting_to_dict(s), status=201)


@api_login_required
@require_POST
def create_conversation_character(request, conv_id: uuid.UUID):
    """A character that belongs to this conversation alone."""
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    name = str(body.get("name") or "").strip()
    if not name or len(name) > MAX_NAME:
        return json_err(ui_text.NAME_REQUIRED.format(max=MAX_NAME))
    if Character.objects.filter(conversation=conv, name=name).exists():
        return json_err(ui_text.NAME_TAKEN)
    pos = (conv.own_characters.aggregate(m=Max("position"))["m"] or 0) + 1
    with transaction.atomic():
        description = str(body.get("description") or "")
        c = Character.objects.create(
            folder=None, conversation=conv, name=name, description=description,
            positive=library.split_tags(body.get("positive")), negative=library.split_tags(body.get("negative")),
            prose=str(body.get("prose") or "").strip(), position=pos,
            text_edited_at=timezone.now() if description.strip() else None,
        )
        v_content = str(body.get("variant_content") or "")
        v = CharacterVariant.objects.create(
            character=c,
            title=str(body.get("variant_title") or ui_text.DEFAULT_VARIANT_TITLE),
            content=v_content, position=1,
            positive=library.split_tags(body.get("variant_positive")),
            negative=library.split_tags(body.get("variant_negative")),
            prose=str(body.get("variant_prose") or "").strip(),
            text_edited_at=timezone.now() if v_content.strip() else None,
        )
        conv.selected_variants.add(v)
    c.refresh_from_db()
    return json_ok(character_to_dict(c), status=201)


def _promote(entry, model, **lookup):
    """
    Move a conversation-layer entry up into the project.

    One row changes two foreign keys; nothing is copied, so every picture that
    refers to its lines still points at the same lines. Refused when the
    conversation is in no project, or the project already has that name — the
    second case is the merge the user should do by hand.
    """
    conv = entry.conversation
    if conv is None:
        return json_err(ui_text.ALREADY_IN_PROJECT)
    if conv.folder is None:
        return json_err(ui_text.NO_PROJECT_TO_SAVE_TO)
    if model.objects.filter(folder=conv.folder, conversation__isnull=True, **lookup).exists():
        return json_err(ui_text.PROJECT_HAS_SAME_NAME)
    entry.conversation = None
    entry.folder = conv.folder
    entry.save(update_fields=["conversation", "folder", "updated_at"])
    return None


@api_login_required
@require_POST
def promote_setting(request, setting_id: uuid.UUID):
    s = Setting.objects.filter(id=setting_id).select_related("conversation").first()
    if not s:
        return json_err(ui_text.NO_SETTING, status=404)
    refused = _promote(s, Setting, title=s.title)
    if refused is not None:
        return refused
    return json_ok(setting_to_dict(s))


@api_login_required
@require_POST
def promote_character(request, character_id: uuid.UUID):
    c = Character.objects.filter(id=character_id).select_related("conversation").first()
    if not c:
        return json_err(ui_text.NO_CHARACTER, status=404)
    refused = _promote(c, Character, name=c.name)
    if refused is not None:
        return refused
    c.refresh_from_db()
    return json_ok(character_to_dict(c))


# ---------------------------------------------------------------- selection


def selection_payload(conv: Conversation) -> dict:
    """What this conversation can draw on, and what of it is switched on."""
    # Both layers, same-name entries included: the lead is listed both too
    # (the project one under its @ prefix, library.selection_for), and the
    # person has to be able to see and switch what the lead sees.
    settings, characters = library.visible_entries(conv)
    chosen_settings = set(conv.selected_settings.values_list("id", flat=True))
    chosen_variants = set(conv.selected_variants.values_list("id", flat=True))
    return {
        "settings": [
            {**setting_to_dict(s), "selected": s.id in chosen_settings} for s in settings
        ],
        "characters": [
            {
                **character_to_dict(c),
                "variants": [
                    {**variant_to_dict(v), "selected": v.id in chosen_variants}
                    for v in c.variants.all()
                ],
                "selected": any(v.id in chosen_variants for v in c.variants.all()),
            }
            for c in characters
        ],
    }


@api_login_required
@require_http_methods(["GET", "POST"])
def conversation_selection(request, conv_id: uuid.UUID):
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)

    if request.method == "GET":
        return json_ok(selection_payload(conv))

    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)

    available_settings, available_characters = library.visible_entries(conv)
    allowed_setting_ids = {s.id for s in available_settings}
    allowed_variant_ids = {
        v.id for c in available_characters for v in c.variants.all()
    }

    def parse(ids, allowed):
        out = []
        for raw in ids or []:
            try:
                val = uuid.UUID(str(raw))
            except ValueError:
                continue
            if val in allowed:   # ignore anything outside this conversation's reach
                out.append(val)
        return out

    setting_ids = parse(body.get("setting_ids"), allowed_setting_ids)
    variant_ids = parse(body.get("variant_ids"), allowed_variant_ids)

    with transaction.atomic():
        conv.selected_settings.set(setting_ids)
        conv.selected_variants.set(variant_ids)
    return json_ok(selection_payload(conv))


@api_login_required
@require_POST
def toggle_conversation_selection(request, conv_id: uuid.UUID):
    """
    Switch ONE entry on or off: {"kind": "setting"|"variant"|"character",
    "id": ..., "on": bool}. The whole-set write above is what a chip used to
    do, from a cached list -- and a cached list silently switched off what
    the server had switched on during the turn in between.
    """
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    try:
        target = uuid.UUID(str(body.get("id")))
    except ValueError:
        return json_err(ui_text.BAD_JSON)
    on = bool(body.get("on"))
    kind = str(body.get("kind") or "")
    available_settings, available_characters = library.visible_entries(conv)
    with transaction.atomic():
        if kind == "setting":
            hit = next((s for s in available_settings if s.id == target), None)
            if hit is None:
                return json_err(ui_text.BAD_JSON)
            (conv.selected_settings.add if on else conv.selected_settings.remove)(hit)
        elif kind == "variant":
            hit = next((v for c in available_characters for v in c.variants.all() if v.id == target), None)
            if hit is None:
                return json_err(ui_text.BAD_JSON)
            (conv.selected_variants.add if on else conv.selected_variants.remove)(hit)
        elif kind == "character":
            hit = next((c for c in available_characters if c.id == target), None)
            if hit is None:
                return json_err(ui_text.BAD_JSON)
            variants = list(hit.variants.all())
            if on:
                # on means the first costume, unless some already are
                if not conv.selected_variants.filter(character=hit).exists() and variants:
                    conv.selected_variants.add(variants[0])
            else:
                conv.selected_variants.remove(*variants)
        else:
            return json_err(ui_text.BAD_JSON)
    return json_ok(selection_payload(conv))


# ---------------------------------------------------------------- proposals


@api_login_required
@require_POST
def resolve_proposal(request, proposal_id: uuid.UUID):
    """
    Approve (write it) or dismiss one library proposal.

    The writing itself lives in services.proposals, which is also what the
    automatic path calls — so a proposal approved here and one approved on the
    user's behalf are the same row settled the same way, and nothing further
    down can tell them apart.
    """
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    approve = bool(body.get("approve"))

    p = LibraryProposal.objects.filter(id=proposal_id).first()
    if not p:
        return json_err(ui_text.NO_PROPOSAL, status=404)
    if approve and p.status == "pending" and p.folder is None:
        return json_err(ui_text.NO_FOLDER_FOR_LIBRARY)

    # Switched on in the conversation that proposed it, as the automatic path
    # does: the lead wrote the entry down to draw from it here, and the next
    # picture reads the truth table, not the approval. Approved-but-off meant a
    # character the user had just approved was drawn from a passer-by's look.
    try:
        done = proposals.resolve(proposal_id, approve=approve, select_new=True)
    except proposals.NameTaken as exc:
        # nothing written, still pending: tidy the project and press again
        return json_err(ui_text.PROPOSAL_NAME_TAKEN.format(name=exc.name), status=409)
    if done is None:
        return json_err(ui_text.NO_PROPOSAL, status=404)
    return json_ok(proposal_to_dict(done))


@api_login_required
@require_GET
def conversation_proposals(request, conv_id: uuid.UUID):
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)
    items = LibraryProposal.objects.filter(conversation=conv)
    return json_ok({"proposals": [proposal_to_dict(p) for p in items]})


@api_login_required
@require_POST
def set_conversation_folder(request, conv_id: uuid.UUID):
    """Move one conversation into a folder (or out to the root)."""
    conv = Conversation.objects.filter(id=conv_id).first()
    if not conv:
        return json_err(ui_text.NO_CONVERSATION, status=404)
    body = _body(request)
    if body is None:
        return json_err(ui_text.BAD_JSON)
    folder = None
    if body.get("folder_id"):
        try:
            folder = Folder.objects.filter(id=uuid.UUID(str(body["folder_id"]))).first()
        except ValueError:
            return json_err(ui_text.BAD_FOLDER_ID)
        if not folder:
            return json_err(ui_text.NO_FOLDER, status=404)
    conv.folder = folder
    conv.save(update_fields=["folder", "updated_at"])
    library.rehome(conv)
    return json_ok(conversation_to_dict(conv))
