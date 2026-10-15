from django.urls import path

from core.views import (
    appsettings, auth, chat, internal, media, phrases, projects, quota,
)

urlpatterns = [
    # auth
    path("csrf", auth.csrf),
    path("auth/login", auth.login),
    path("auth/logout", auth.logout),
    path("auth/me", auth.me),
    # chat
    path("conversations", chat.conversations),
    path("conversations/<uuid:conv_id>", chat.conversation_detail),
    path("conversations/<uuid:conv_id>/archive", chat.archive_conversation),
    path("conversations/<uuid:conv_id>/messages", chat.messages),
    path("conversations/<uuid:conv_id>/rerun", chat.rerun_conversation),
    path("messages/<uuid:msg_id>/raw", chat.message_raw),
    path("messages/<uuid:msg_id>/cancel", chat.cancel_message),
    path("messages/<uuid:msg_id>/redraw", chat.redraw_message),
    path("messages/<uuid:msg_id>/main", chat.set_main_image),
    path("messages/<uuid:msg_id>/branch", chat.branch_conversation),
    # projects: folders and placement
    path("projects/tree", projects.project_tree),
    path("archive", projects.archive),
    path("projects/move", projects.move_item),
    path("folders", projects.folders),
    path("folders/<uuid:folder_id>", projects.folder_detail),
    path("folders/<uuid:folder_id>/archive", projects.archive_folder),
    path("conversations/<uuid:conv_id>/folder", projects.set_conversation_folder),
    # projects: the library
    path("folders/<uuid:folder_id>/library", projects.folder_library),
    path("folders/<uuid:folder_id>/settings", projects.create_setting),
    path("settings/<uuid:setting_id>", projects.setting_detail),
    path("folders/<uuid:folder_id>/characters", projects.create_character),
    path("characters/<uuid:character_id>", projects.character_detail),
    path("characters/<uuid:character_id>/variants", projects.create_variant),
    path("variants/<uuid:variant_id>", projects.variant_detail),
    # projects: per-conversation selection + the model's edit proposals
    path("conversations/<uuid:conv_id>/selection", projects.conversation_selection),
    path("conversations/<uuid:conv_id>/selection/toggle", projects.toggle_conversation_selection),
    # the conversation layer of the library: entries that belong to one
    # conversation and shadow the project's
    path("conversations/<uuid:conv_id>/settings", projects.create_conversation_setting),
    path("conversations/<uuid:conv_id>/characters", projects.create_conversation_character),
    path("settings/<uuid:setting_id>/promote", projects.promote_setting),
    path("characters/<uuid:character_id>/promote", projects.promote_character),
    path("conversations/<uuid:conv_id>/proposals", projects.conversation_proposals),
    path("proposals/<uuid:proposal_id>/resolve", projects.resolve_proposal),
    # quick phrases
    path("phrases", phrases.phrases),
    path("phrases/reorder", phrases.reorder),
    path("phrases/<uuid:phrase_id>", phrases.phrase_detail),
    # which way the backend is reaching the model, and saying one is out
    path("roads/<str:name>", appsettings.road),
    # media & misc
    path("media/<uuid:att_id>/thumb", media.media_thumb),
    path("media/<uuid:att_id>/<str:filename>", media.media),
    path("quota", quota.quota),
    path("app-settings", appsettings.app_settings),
    # internal (MCP)
    path("internal/chat", internal.chat),
    path("internal/generate", internal.generate),
    path("internal/quota", internal.quota),
]
