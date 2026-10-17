"""
The app speaks the language the account chose (core.i18n), and the Chinese
it speaks is the Chinese it spoke before there was a choice.

tests/fixtures/ui_text_zh.json is every name of core.ui_text_zh as it stood
the day the English table was added (2026-10-16); the first test holds the
module to it. Change a Chinese sentence on purpose and change the fixture with
it. The other tables are held to the Chinese one by name, by shape and by
{field}, and the prompts are held to what they were: in Chinese nothing moved,
in English only the lines that name the language.
"""

from __future__ import annotations

import json
import re
import string
import uuid
from pathlib import Path
from unittest import mock

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

from core import i18n, prompts, ui_text, ui_text_en, ui_text_zh
from core.models import AppSettings, Conversation, Message
from core.prompts import wording
from core.http import message_to_dict
from core.services import characters as characters_service
from core.services import continuous, notices, pipeline
from tests.tests_prompt_scenario import ORIGINAL_DRAW, ORIGINAL_LEAD

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: CJK ideographs, CJK punctuation (「」、。) and full-width forms
CJK = re.compile(r"[⺀-鿿豈-﫿＀-￯　-〿]")


def names(module) -> list[str]:
    return sorted(n for n in dir(module) if n.isupper() and not n.startswith("_"))


def fields(template: str) -> list[str]:
    """The {names} a template takes, format specs stripped. Sorted: they are
    filled by keyword, so a sentence may put them in any order."""
    return sorted(f for _, f, _, _ in string.Formatter().parse(template) if f is not None)


def strings_in(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_in(v)]
    return []


class TablesTests(SimpleTestCase):
    def test_the_chinese_table_is_what_it_was(self):
        pinned = json.loads((FIXTURES / "ui_text_zh.json").read_text(encoding="utf-8"))
        now = {n: getattr(ui_text_zh, n) for n in names(ui_text_zh)}
        self.assertEqual(json.loads(json.dumps(now)), pinned)   # tuples read back as lists

    def test_every_language_has_every_name(self):
        for lang in i18n.LANGUAGES:
            self.assertEqual(names(lang.texts), names(ui_text_zh), lang.code)

    def test_every_language_has_the_same_shapes_and_fields(self):
        for lang in i18n.LANGUAGES:
            for name in names(ui_text_zh):
                zh, other = getattr(ui_text_zh, name), getattr(lang.texts, name)
                self.assertIs(type(other), type(zh), f"{lang.code}.{name}")
                if isinstance(zh, dict):
                    self.assertEqual(sorted(other), sorted(zh), f"{lang.code}.{name}")
                    for key in zh:
                        self.assertEqual(fields(other[key]), fields(zh[key]), f"{lang.code}.{name}[{key}]")
                elif isinstance(zh, str):
                    self.assertEqual(fields(other), fields(zh), f"{lang.code}.{name}")

    def test_english_has_no_chinese_and_is_not_the_chinese(self):
        for name in names(ui_text_en):
            zh, en = getattr(ui_text_zh, name), getattr(ui_text_en, name)
            for s in strings_in(en):
                self.assertIsNone(CJK.search(s), f"{name}: {s!r}")
            if any(CJK.search(s) for s in strings_in(zh)):
                self.assertNotEqual(en, zh, name)

    def test_the_wording_tables_keyed_by_language_cover_every_language(self):
        """Any dict in wording.py keyed by a language code is keyed by all of
        them: a new language that forgot one would splice a KeyError into
        the prompt."""
        found = 0
        for name in dir(wording):
            value = getattr(wording, name)
            if isinstance(value, dict) and set(value) & set(i18n.CODES):
                found += 1
                self.assertEqual(sorted(value), sorted(i18n.CODES), name)
                for code, phrase in value.items():
                    words = list(phrase.values()) if isinstance(phrase, dict) else [phrase]
                    for word in words:
                        self.assertIsInstance(word, str, f"{name}[{code}]")
                        self.assertNotIn("{{", word, f"{name}[{code}]")
                    if isinstance(phrase, dict):
                        self.assertEqual(sorted(phrase), sorted(value["zh"]), f"{name}[{code}]")
        self.assertEqual(found, 3)   # REPLY_LANGUAGE, PROSE_LANGUAGE, TITLE_LANGUAGE

    def test_the_registry_is_consistent(self):
        self.assertEqual(i18n.CODES, tuple(i18n.BY_CODE))
        self.assertIn(i18n.DEFAULT, i18n.CODES)
        self.assertEqual(len(set(i18n.CODES)), len(i18n.CODES))
        for lang in i18n.LANGUAGES:
            self.assertTrue(lang.label)
            self.assertTrue(i18n.is_known(lang.code))
        self.assertFalse(i18n.is_known("xx"))
        self.assertFalse(i18n.is_known(None))
        self.assertEqual(i18n.normalise("klingon"), i18n.DEFAULT)
        self.assertEqual(i18n.normalise("en"), "en")


class DispatchTests(TestCase):
    def test_before_anything_is_saved_it_speaks_chinese(self):
        self.assertFalse(AppSettings.objects.exists())
        self.assertEqual(i18n.current(), "zh")
        self.assertEqual(ui_text.NO_CONVERSATION, ui_text_zh.NO_CONVERSATION)
        self.assertEqual(ui_text.ROUTE_LABELS, ui_text_zh.ROUTE_LABELS)

    def test_a_stored_choice_is_what_is_read(self):
        i18n.set_language("en")
        self.assertEqual(AppSettings.get().language, "en")
        self.assertEqual(i18n.current(), "en")
        self.assertEqual(ui_text.NO_CONVERSATION, ui_text_en.NO_CONVERSATION)
        self.assertEqual(ui_text.CONTINUOUS_HEADS["done"], ui_text_en.CONTINUOUS_HEADS["done"])
        i18n.set_language("zh")
        self.assertEqual(ui_text.NO_CONVERSATION, ui_text_zh.NO_CONVERSATION)

    def test_an_unknown_code_is_refused_and_an_unknown_row_reads_as_the_default(self):
        with self.assertRaises(ValueError):
            i18n.set_language("xx")
        self.assertFalse(AppSettings.objects.exists())
        AppSettings.get()
        AppSettings.objects.filter(id=1).update(language="klingon")
        self.assertEqual(i18n.current(), i18n.DEFAULT)

    def test_use_pins_a_language_whatever_is_stored(self):
        i18n.set_language("en")
        with i18n.use("zh"):
            self.assertEqual(i18n.current(), "zh")
            self.assertEqual(ui_text.NO_FOLDER, ui_text_zh.NO_FOLDER)
            i18n.set_language("en")           # does not move a pinned scope
            self.assertEqual(i18n.current(), "zh")
        self.assertEqual(i18n.current(), "en")
        with self.assertRaises(ValueError):
            with i18n.use("xx"):
                pass

    def test_a_scope_reads_the_choice_once_and_follows_its_own_change(self):
        with i18n.scope():
            self.assertEqual(i18n.current(), "zh")
            AppSettings.get()
            AppSettings.objects.filter(id=1).update(language="en")
            self.assertEqual(i18n.current(), "zh", "a scope does not re-read")
            i18n.set_language("en")
            self.assertEqual(i18n.current(), "en", "but it follows a change made through it")
        self.assertEqual(i18n.current(), "en")

    def test_in_every_language(self):
        self.assertEqual(ui_text.in_every_language("NEW_CONVERSATION_TITLE"),
                         tuple(lang.texts.NEW_CONVERSATION_TITLE for lang in i18n.LANGUAGES))
        self.assertIn("新对话", ui_text.in_every_language("NEW_CONVERSATION_TITLE"))

    def test_the_module_looks_like_a_table(self):
        self.assertIn("NO_CONVERSATION", dir(ui_text))
        with self.assertRaises(AttributeError):
            ui_text.NO_SUCH_NAME
        with self.assertRaises(AttributeError):
            ui_text._private

    def test_the_admin_stays_chinese(self):
        with i18n.use("en"):
            self.assertEqual(admin.site.site_header, ui_text_zh.ADMIN_SITE_HEADER)

    def test_a_new_conversation_is_titled_in_the_current_language(self):
        with i18n.use("en"):
            self.assertEqual(Conversation.objects.create().title, ui_text_en.NEW_CONVERSATION_TITLE)
        with i18n.use("zh"):
            self.assertEqual(Conversation.objects.create().title, "新对话")

    def test_auto_title_still_recognises_a_conversation_left_untitled_in_the_other_language(self):
        conv = Conversation.objects.create(title="新对话")
        Message.objects.create(conversation=conv, seq=1, role="user", content="hi")
        reply = Message.objects.create(conversation=conv, seq=2, role="assistant", content="hello", status="done")
        with i18n.use("en"), mock.patch.object(pipeline.llm, "make_title", return_value="Greetings"):
            pipeline._maybe_title(conv, reply)
        conv.refresh_from_db()
        self.assertEqual(conv.title, "Greetings")

    def test_a_titled_conversation_is_left_alone(self):
        conv = Conversation.objects.create(title="my title")
        Message.objects.create(conversation=conv, seq=1, role="user", content="hi")
        reply = Message.objects.create(conversation=conv, seq=2, role="assistant", content="hello", status="done")
        with i18n.use("en"), mock.patch.object(pipeline.llm, "make_title", return_value="Greetings") as mt:
            pipeline._maybe_title(conv, reply)
        conv.refresh_from_db()
        self.assertEqual(conv.title, "my title")
        mt.assert_not_called()

    def test_the_default_variant_is_titled_in_the_current_language(self):
        with i18n.use("en"):
            self.assertEqual(characters_service.default_title(), ui_text_en.DEFAULT_VARIANT_TITLE)
        with i18n.use("zh"):
            self.assertEqual(characters_service.default_title(), "默认")

    def test_token_counts_are_shortened_the_way_each_language_counts(self):
        with i18n.use("zh"):
            self.assertEqual(pipeline._fmt_tokens(123456), "12.3 万")
            self.assertEqual(pipeline._fmt_tokens(9999), "9999")
        with i18n.use("en"):
            self.assertEqual(pipeline._fmt_tokens(123456), "123.5k")
            self.assertEqual(pipeline._fmt_tokens(1_000_000), "1.0M")
            self.assertEqual(pipeline._fmt_tokens(999), "999")


class NoticeTests(TestCase):
    """The app's own rows in a transcript are read in the reader's language."""

    def test_a_note_is_read_in_the_language_of_the_reader(self):
        conv = Conversation.objects.create(title="c")
        with i18n.use("zh"):
            continuous._note(conv, "continuous", {"key": "CONTINUOUS_SUMMARY", "how": "stopped",
                                                  "of": 8, "done": 6, "failed": 0})
        row = conv.messages.get(role="system")
        self.assertEqual(row.content, "连续生成已停下 · 共8轮 · 成功6轮 · 失败0轮")
        with i18n.use("en"):
            self.assertEqual(message_to_dict(row)["content"],
                             "Continuous generation stopped · 8 rounds · 6 succeeded · 0 failed")
        with i18n.use("zh"):
            self.assertEqual(message_to_dict(row)["content"], row.content)

    def test_a_row_from_before_notes_were_structured_keeps_its_words(self):
        conv = Conversation.objects.create(title="c")
        row = Message.objects.create(conversation=conv, seq=1, role="system", status="done",
                                     notice_kind="continuous", content="旧的一行")
        with i18n.use("en"):
            self.assertEqual(message_to_dict(row)["content"], "旧的一行")

    def test_a_note_it_does_not_know_falls_back_to_the_words(self):
        conv = Conversation.objects.create(title="c")
        row = Message.objects.create(conversation=conv, seq=1, role="system", status="done",
                                     notice_kind="x", notice={"key": "FROM_THE_FUTURE"}, content="原话")
        self.assertEqual(message_to_dict(row)["content"], "原话")
        self.assertEqual(notices.render(None), "")
        self.assertEqual(notices.render({"key": "CONTINUOUS_START"}), "")   # a number missing

    def test_context_notes_carry_the_counts_and_say_them_per_language(self):
        note = {"key": "CONTEXT_NEAR_LIMIT", "used": 123456, "limit": 1000000}
        with i18n.use("zh"):
            self.assertEqual(notices.render(note), "这个对话已经用掉约 12.3 万 token，接近模型上限（100.0 万）。超出后最早的消息会被丢弃。")
        with i18n.use("en"):
            self.assertIn("about 123.5k tokens", notices.render(note))
            self.assertIn("(1.0M)", notices.render(note))
            self.assertEqual(notices.render({"key": "LONG_CONTEXT_PRICED", "threshold": 200000}),
                             "This conversation's context is over 200.0k tokens.")

    def test_the_pipeline_writes_the_counts_not_the_words(self):
        conv = Conversation.objects.create(title="c")
        Message.objects.create(conversation=conv, seq=1, role="user", content="hi")
        limit = pipeline.providers.active().context_limit
        with i18n.use("zh"):
            pipeline.maybe_context_notice(conv, int(limit * 0.9))
        row = conv.messages.get(role="system")
        self.assertEqual(row.notice, {"key": "CONTEXT_NEAR_LIMIT", "used": int(limit * 0.9), "limit": limit})
        self.assertEqual(row.content, notices.render(row.notice))
        with i18n.use("en"):
            self.assertTrue(message_to_dict(row)["content"].startswith("This conversation has used about"))

    def test_a_user_row_is_its_content_whatever_the_language(self):
        conv = Conversation.objects.create(title="c")
        row = Message.objects.create(conversation=conv, seq=1, role="user", content="你好")
        with i18n.use("en"):
            self.assertEqual(message_to_dict(row)["content"], "你好")


class ApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_user(username="a", password="x"))

    def _patch(self, body: dict):
        return self.client.patch("/api/app-settings", json.dumps(body), content_type="application/json")

    def test_the_settings_say_the_language_and_take_a_new_one(self):
        self.assertEqual(self.client.get("/api/app-settings").json()["language"], "zh")
        r = self._patch({"language": "en"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["language"], "en")
        self.assertEqual(self.client.get("/api/app-settings").json()["language"], "en")
        self.assertEqual(AppSettings.get().language, "en")

    def test_an_unknown_language_is_refused_and_nothing_is_stored(self):
        r = self._patch({"language": "xx"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], ui_text_zh.LANGUAGE_UNKNOWN.format(codes="zh, en"))
        self.assertEqual(self.client.get("/api/app-settings").json()["language"], "zh")
        r = self._patch({"language": 3})
        self.assertEqual(r.status_code, 400)

    def test_language_and_defaults_can_go_in_one_request(self):
        r = self._patch({"language": "en", "gen_defaults": {"continuous_rounds": 5}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["language"], "en")
        self.assertEqual(r.json()["gen_defaults"]["continuous_rounds"], 5)

    def test_an_empty_patch_is_refused(self):
        r = self._patch({})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], ui_text_zh.NOTHING_TO_UPDATE)

    def test_bad_defaults_are_refused_before_the_language_is_stored(self):
        r = self._patch({"gen_defaults": "everything", "language": "en"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], ui_text_zh.GEN_DEFAULTS_NOT_OBJECT)
        self.assertEqual(i18n.current(), "zh")

    def test_after_the_switch_the_api_answers_in_english(self):
        missing = f"/api/conversations/{uuid.uuid4()}/messages"
        self.assertEqual(self.client.get(missing).json()["error"], ui_text_zh.NO_CONVERSATION)
        self._patch({"language": "en"})
        r = self.client.get(missing)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], ui_text_en.NO_CONVERSATION)
        # and a conversation made now is titled in English
        made = self.client.post("/api/conversations", json.dumps({}), content_type="application/json").json()
        self.assertEqual(made["title"], ui_text_en.NEW_CONVERSATION_TITLE)

    def test_me_says_the_language_before_login_too(self):
        anonymous = Client()
        self.assertEqual(anonymous.get("/api/auth/me").json(), {"authenticated": False, "language": "zh"})
        self._patch({"language": "en"})
        self.assertEqual(anonymous.get("/api/auth/me").json(), {"authenticated": False, "language": "en"})
        me = self.client.get("/api/auth/me").json()
        self.assertEqual((me["authenticated"], me["username"], me["language"]), (True, "a", "en"))

    def test_a_request_reads_the_choice_once(self):
        with mock.patch.object(i18n, "stored", wraps=i18n.stored) as stored:
            self.client.get("/api/app-settings")
        self.assertEqual(stored.call_count, 1)


class PromptTests(SimpleTestCase):
    """What the models are told. In Chinese: exactly what it was. In English:
    the same prompts, with only the lines that name the person's language
    changed -- the lead's reply and the note of an entry. The drawing model,
    the compressor and the entry rules name no language at all. The
    usage-scenario texts are the originals so the fixtures of
    tests_prompt_scenario apply."""

    SCENARIO = dict(LEAD_USAGE_SCENARIO=ORIGINAL_LEAD, DRAW_USAGE_SCENARIO=ORIGINAL_DRAW)

    #: the one sentence of lead.txt and the one of patrol.txt that carry
    #: PROSE_LANGUAGE_MARK, with the Chinese word in place
    LEAD_NOTE_ZH = "注释用" + wording.PROSE_LANGUAGE["zh"] + "说明这是什么样的画风"
    PATROL_NOTE_ZH = "条目的" + wording.PROSE_LANGUAGE["zh"] + "注释是人写的"

    @staticmethod
    def _in(sentence: str, code: str) -> str:
        return sentence.replace(wording.PROSE_LANGUAGE["zh"], wording.PROSE_LANGUAGE[code], 1)

    def test_in_chinese_the_prompts_are_what_they_were(self):
        with override_settings(**self.SCENARIO), i18n.use("zh"):
            self.assertEqual(prompts.lead(), (FIXTURES / "prompt_lead.txt").read_text(encoding="utf-8"))
            self.assertEqual(prompts.drawing(), (FIXTURES / "prompt_drawing.txt").read_text(encoding="utf-8"))
            self.assertIn("通常不要用 emoji。" + wording.REPLY_LANGUAGE["zh"] + "\n", prompts.lead())
            self.assertIn(self.LEAD_NOTE_ZH, prompts.lead())
            self.assertIn(self.PATROL_NOTE_ZH, prompts.patrol())

    def test_in_english_only_the_language_lines_change(self):
        with override_settings(**self.SCENARIO):
            with i18n.use("zh"):
                zh_lead, zh_patrol = prompts.lead(), prompts.patrol()
            with i18n.use("en"):
                en_lead, en_patrol = prompts.lead(), prompts.patrol()
        self.assertNotEqual(en_lead, zh_lead)
        self.assertIn("通常不要用 emoji。" + wording.REPLY_LANGUAGE["en"] + "\n", en_lead)
        self.assertIn(self._in(self.LEAD_NOTE_ZH, "en"), en_lead)
        back = (en_lead.replace(wording.REPLY_LANGUAGE["en"], wording.REPLY_LANGUAGE["zh"], 1)
                .replace(self._in(self.LEAD_NOTE_ZH, "en"), self.LEAD_NOTE_ZH, 1))
        self.assertEqual(back, zh_lead)
        self.assertNotEqual(en_patrol, zh_patrol)
        self.assertIn(self._in(self.PATROL_NOTE_ZH, "en"), en_patrol)
        self.assertEqual(en_patrol.replace(self._in(self.PATROL_NOTE_ZH, "en"), self.PATROL_NOTE_ZH, 1), zh_patrol)

    def test_the_prompts_that_name_no_language_read_the_same_in_every_language(self):
        seen = {}
        for code in i18n.CODES:
            with override_settings(**self.SCENARIO), i18n.use(code):
                seen[code] = (prompts.drawing(), prompts.compressor(), prompts.entries())
        for code in i18n.CODES:
            self.assertEqual(seen[code], seen["zh"], code)

    def test_the_entry_rules_are_one_text_read_by_the_lead_and_the_patrol(self):
        """One copy of how an entry is written: a rule the patrol followed and
        the lead did not would be two kinds of entry for the same note."""
        for code in i18n.CODES:
            with override_settings(**self.SCENARIO), i18n.use(code):
                rules = prompts.entries()
                self.assertEqual(rules, prompts.load("entries"))
                self.assertEqual(prompts.lead().count(rules), 1, code)
                self.assertTrue(prompts.patrol().endswith("\n\n" + rules), code)
                self.assertNotIn(prompts.ENTRIES_MARK, prompts.lead(), code)

    def test_no_marker_survives_and_the_other_braces_do(self):
        marks = (prompts.REPLY_LANGUAGE_MARK, prompts.PROSE_LANGUAGE_MARK, prompts.ENTRIES_MARK, prompts.USAGE_MARK)
        for code in i18n.CODES:
            with override_settings(**self.SCENARIO), i18n.use(code):
                for text in (prompts.lead(), prompts.patrol(), prompts.drawing(), prompts.compressor(),
                             prompts.entries()):
                    for mark in marks:
                        self.assertNotIn(mark, text, code)
                self.assertIn("`{{tag}}`是乘`1.1025`", prompts.drawing())
                self.assertIn('"expose": ["画风", "苏棠/通勤装", "@天台", "-旧条目"]', prompts.lead())

    def test_the_files_carry_each_marker_once_and_not_the_words(self):
        lead, patrol = prompts.load("lead"), prompts.load("patrol")
        others = (prompts.load("drawing"), prompts.load("compressor"), prompts.load("entries"))
        self.assertEqual(lead.count(prompts.REPLY_LANGUAGE_MARK), 1)
        self.assertEqual(lead.count(prompts.PROSE_LANGUAGE_MARK), 1)
        self.assertEqual(lead.count(prompts.ENTRIES_MARK), 1)
        self.assertEqual(patrol.count(prompts.PROSE_LANGUAGE_MARK), 1)
        self.assertNotIn(prompts.REPLY_LANGUAGE_MARK, patrol)
        self.assertNotIn(prompts.ENTRIES_MARK, patrol)
        for code in i18n.CODES:
            self.assertNotIn(wording.REPLY_LANGUAGE[code], lead, code)
            self.assertNotIn(self._in(self.LEAD_NOTE_ZH, code), lead, code)
            self.assertNotIn(self._in(self.PATROL_NOTE_ZH, code), patrol, code)
        for raw in others:
            for mark in (prompts.REPLY_LANGUAGE_MARK, prompts.PROSE_LANGUAGE_MARK, prompts.ENTRIES_MARK):
                self.assertNotIn(mark, raw)
