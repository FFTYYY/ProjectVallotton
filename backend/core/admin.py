from django.contrib import admin
from django.utils.html import format_html

from core.models import (
    Attachment,
    Conversation,
    DailySpend,
    LoginAttempt,
    Message,
    QuickPhrase,
)
from core import ui_text_zh as ui_text  # the admin is served in Chinese, like Django's own

admin.site.site_header = ui_text.ADMIN_SITE_HEADER
admin.site.site_title = ui_text.APP_NAME
admin.site.index_title = ui_text.ADMIN_INDEX_TITLE


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0
    fields = ("preview", "kind", "mime", "width", "height", "size_bytes", "seed",
              "description")
    readonly_fields = fields

    def preview(self, obj):
        if not obj.file:
            return "-"
        name = obj.file.name.rsplit("/", 1)[-1]
        return format_html(
            '<a href="/api/media/{}/{}" target="_blank">'
            '<img src="/api/media/{}/{}" style="max-height:96px;border-radius:6px" /></a>',
            obj.id, name, obj.id, name,
        )


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("title", "source", "updated_at", "created_at", "id")
    list_filter = ("source",)
    search_fields = ("title", "id")


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "conversation", "seq", "role", "status", "kind", "archived", "created_at",
    )
    list_filter = ("role", "status", "kind", "archived")
    search_fields = ("content", "id", "conversation__id")
    readonly_fields = ("id", "conversation", "seq", "created_at", "heartbeat_at")
    inlines = [AttachmentInline]


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "mime", "width", "height", "size_bytes", "created_at")
    list_filter = ("kind",)


@admin.register(DailySpend)
class DailySpendAdmin(admin.ModelAdmin):
    list_display = ("day", "llm_requests", "llm_usd", "nai_images")

    @admin.display(description=ui_text.ADMIN_LLM_USD)
    def llm_usd(self, obj):
        return f"${obj.llm_ticks * 1e-10:.4f}"


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ("key", "fails", "locked_until", "updated_at")


@admin.register(QuickPhrase)
class QuickPhraseAdmin(admin.ModelAdmin):
    list_display = ("title", "content", "created_at")
    search_fields = ("title", "content")
