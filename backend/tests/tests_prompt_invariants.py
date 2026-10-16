"""
The prompt NovelAI receives, held to the invariants written at the top of
core/services/prompt.py: built by render, checked by lint. The audit of a
real run (tmp/proto/audit_real.py) runs the same lint over every turn.
"""

from django.test import SimpleTestCase

from core import i18n
from core.services import llm, prompt as prompt_slots


def _picture(slots: dict, characters: list[dict], text: str = "", negative: str = "") -> dict:
    full: dict = {name: {"tags": "", "prose": ""} for name in prompt_slots.FIELD_SLOTS}
    for name, half in slots.items():
        full[name] = {"tags": half.get("tags", ""), "prose": half.get("prose", "")}
    full[prompt_slots.TEXT_SLOT] = text
    chars = []
    for i, ch in enumerate(characters, 1):
        chars.append({
            "key": ch.get("key") or f"主体{i}", "from": ch.get("from", ""), "use": ch.get("use", []),
            "patches": ch.get("patches", []),
            "look": {"tags": ch.get("look_tags", ""), "prose": ch.get("look_prose", "")},
            "moment": {"tags": ch.get("moment_tags", ""), "prose": ch.get("moment_prose", "")},
            "center_x": None, "center_y": None,
        })
    return prompt_slots.upgrade({"size": "portrait", "slots": full, "settings": [],
                                 "characters": chars, "negative": negative})


LINES = {"林": {"k1": "1girl", "k2": "black hair, long hair", "v1": "white shirt, long sleeves"}}


class RenderedPromptInvariantsTests(SimpleTestCase):
    def setUp(self):
        # the notes lint writes come from ui_text: in Chinese, without a database
        self.enterContext(i18n.use("zh"))

    def test_sentences_and_glue(self):
        self.assertEqual(prompt_slots.sentences(["She stands still.", "the wind lifts her hair", "", " , "]),
                         "She stands still. the wind lifts her hair.")
        self.assertEqual(prompt_slots.glue(["1girl, solo", "She stands still.", "Midnight Pages"]),
                         "1girl, solo, She stands still. Midnight Pages")
        self.assertEqual(prompt_slots.glue(["", "1girl", ""]), "1girl")
        self.assertEqual(prompt_slots.glue(["", "She waits."]), "She waits.")

    def test_shape_order_and_separators(self):
        picture = _picture(
            {"count": {"tags": "1girl, solo", "prose": "She is alone."},
             "camera": {"tags": "full body", "prose": ""},
             "scene": {"tags": "street, night", "prose": "A row of trees lines the street"},
             "light": {"tags": "", "prose": "Light comes from the left"}},
            [{"from": "林", "use": ["k1", "k2", "v1"], "patches": [{"line": "v1", "add": "sleeves rolled up"}],
              "look_tags": "glasses", "look_prose": "Her hair is tied back.",
              "moment_tags": "standing", "moment_prose": "She looks at the sky"}],
            text="Midnight Pages", negative="lowres, text")
        out = prompt_slots.render(picture, LINES)
        # 1 and 2: tags in slot order, then sentences in slot order, then the text
        self.assertEqual(out["base_prompt"],
                         "1girl, solo, full body, street, night, "
                         "She is alone. A row of trees lines the street. Light comes from the left. Midnight Pages")
        # lines verbatim and patched, in use order, then look, then moment, then prose
        self.assertEqual(out["character_prompts"][0]["prompt"],
                         "1girl, black hair, long hair, white shirt, long sleeves, sleeves rolled up, glasses, standing, "
                         "Her hair is tied back. She looks at the sky.")
        self.assertEqual(prompt_slots.lint(out, picture), [])

    def test_a_line_not_in_use_does_not_go_out_and_a_repeat_goes_out_once(self):
        picture = _picture({"count": {"tags": "1girl, 1girl, Solo"}},
                           [{"from": "林", "use": ["k1", "v1"], "look_tags": "white shirt, hat"}])
        out = prompt_slots.render(picture, LINES)
        self.assertEqual(out["base_prompt"], "1girl, Solo")
        self.assertEqual(out["character_prompts"][0]["prompt"], "1girl, white shirt, long sleeves, hat")
        self.assertNotIn("black hair", out["character_prompts"][0]["prompt"])
        self.assertEqual(prompt_slots.lint(out), [])

    def test_prose_written_without_a_stop_or_with_stray_commas_still_glues_clean(self):
        picture = _picture({"scene": {"tags": "street", "prose": " she walks on,  "},
                            "other": {"tags": "", "prose": "the wind is strong."}},
                           [{"look_tags": "black hair", "moment_prose": "She turns her head, slowly"}])
        out = prompt_slots.render(picture, {})
        self.assertEqual(out["base_prompt"], "street, she walks on. the wind is strong.")
        self.assertEqual(out["character_prompts"][0]["prompt"], "black hair, She turns her head, slowly.")
        self.assertEqual(prompt_slots.lint(out), [])

    def test_a_sentence_inside_a_tag_line_goes_out_without_its_stop(self):
        lines = {"林": {"k1": "1girl", "v4": "Her robe is coarse wool and a rope is knotted at her waist."}}
        picture = _picture({"count": {"tags": "1girl"}},
                           [{"from": "林", "use": ["k1", "v4"], "moment_tags": "standing"}])
        out = prompt_slots.render(picture, lines)
        self.assertEqual(out["character_prompts"][0]["prompt"],
                         "1girl, Her robe is coarse wool and a rope is knotted at her waist, standing")
        self.assertEqual(prompt_slots.lint(out), [])

    def test_a_tag_in_different_dressing_and_a_sentence_said_twice_go_out_once(self):
        from core.services import novelai
        picture = _picture({"scene": {"tags": "rim light", "prose": "The cat sleeps on the table."},
                            "light": {"tags": "{{rim light}}, rim_light, 1.2::Rim Light::, [rim light]"},
                            "other": {"tags": "", "prose": "the cat sleeps on the table"}},
                           [{"look_tags": "black cat, {black cat}", "look_prose": "It sleeps on the table.",
                             "moment_prose": "It sleeps on the table. Its tail hangs down."}])
        out = prompt_slots.render(picture, {})
        # the first spelling wins; emphasis, underscores and case are not a difference
        self.assertEqual(out["base_prompt"], "rim light, The cat sleeps on the table.")
        self.assertEqual(out["character_prompts"][0]["prompt"], "black cat, It sleeps on the table. Its tail hangs down.")
        self.assertEqual(prompt_slots.lint(out, picture), [])
        # lint judges a repeat by the same rule
        self.assertIn("base: repeated tag 「{{rim light}}」",
                      prompt_slots.lint({"base_prompt": "rim light, {{rim light}}", "character_prompts": [],
                                         "negative_prompt": ""}))
        # and so does the negative, where the default negative sits behind the picture's own
        merged = novelai.merge_negative("Text, {{text}}, lowres, LOWRES")
        self.assertEqual(merged.lower().count("text"), 1)
        self.assertEqual(merged.lower().count("lowres"), 1)
        # a piece that is only a weight is not a tag: it is neither deduped nor dropped
        self.assertEqual(prompt_slots.join(["1.5::", "rain", "night ::", "1.5::"]), "1.5::, rain, night ::, 1.5::")

    def test_a_character_with_nothing_to_say_is_left_out(self):
        picture = _picture({"count": {"tags": "1girl"}}, [{"look_tags": "red dress"}, {}])
        out = prompt_slots.render(picture, {})
        self.assertEqual([c["prompt"] for c in out["character_prompts"]], ["red dress"])

    def test_a_sentence_that_opens_with_a_tag_word_is_not_a_repeat(self):
        picture = _picture({"count": {"tags": "1girl"},
                            "scene": {"tags": "bread, table",
                                      "prose": "Bread, wine and plates lie along the table."}},
                           [{"look_tags": "red dress", "moment_prose": "Red dress, she waits."}])
        out = prompt_slots.render(picture, {})
        self.assertEqual(out["base_prompt"], "1girl, bread, table, Bread, wine and plates lie along the table.")
        self.assertEqual(prompt_slots.lint(out, picture), [])
        # judged from the string alone the opening piece does look like a repeat
        self.assertIn("base: repeated tag 「Bread」", prompt_slots.lint(out))
        self.assertIn("character 主体1: repeated tag 「Red dress」", prompt_slots.lint(out))

    def test_a_complete_prompt_carries_novelais_own_words_in_their_place(self):
        from core.services import novelai
        picture = _picture({"count": {"tags": "1girl"},
                            "scene": {"tags": "street", "prose": "A sign hangs over the door."}},
                           [{"look_tags": "black hair"}], text="Text: Midnight Pages", negative="lowres, blurry")
        out = prompt_slots.render(picture, {}, complete=True)
        # quality words close the tag list; the sentence and the text follow; no 「no text」 beside a text
        self.assertEqual(out["base_prompt"],
                         "1girl, street, very aesthetic, masterpiece, A sign hangs over the door. Text: Midnight Pages")
        self.assertEqual(prompt_slots.lint(out, picture), [])
        # the default negative sits behind the picture's own, each word once
        self.assertTrue(out["negative_prompt"].startswith("lowres, blurry, "))
        self.assertEqual(out["negative_prompt"].lower().count("lowres"), 1)
        # with no text, 「no text」 is sent
        plain = _picture({"count": {"tags": "1girl"}}, [{"look_tags": "black hair"}])
        self.assertEqual(prompt_slots.render(plain, {}, complete=True)["base_prompt"],
                         "1girl, very aesthetic, masterpiece, no text")
        # the payload builder adds nothing to a complete prompt, so what the
        # row stores is what NovelAI receives
        payload = novelai._build_payload(out["base_prompt"], out["character_prompts"], out["negative_prompt"],
                                         "portrait", seed=1)
        self.assertEqual(payload["input"], out["base_prompt"])
        self.assertEqual(payload["parameters"]["negative_prompt"], out["negative_prompt"])
        # the old way -- appended after the text -- is what lint now catches
        old = {**out, "base_prompt": out["base_prompt"] + ", very aesthetic, masterpiece, no text"}
        faults = prompt_slots.lint(old, picture)
        self.assertIn("base: 「no text」 beside a text", faults)
        self.assertIn("base: words after the text", faults)

    def test_a_tag_that_is_only_punctuation_keeps_it(self):
        self.assertEqual(prompt_slots.join(["!", "!?", "...", "surprised.", "1girl"]), "!, !?, ..., surprised, 1girl")

    def test_a_legacy_note_joins_the_prose_as_a_sentence(self):
        old = {"size": "portrait", "slots": {"count": "1girl", "other": "", "note": "She waits by the door.",
                                              "text": ""}, "characters": [], "negative": ""}
        up = prompt_slots.upgrade(old)
        self.assertEqual(up["slots"]["other"]["prose"], "She waits by the door.")
        old["slots"]["other"] = {"tags": "", "prose": "The street is empty."}
        up = prompt_slots.upgrade(old)
        self.assertEqual(up["slots"]["other"]["prose"], "She waits by the door. The street is empty.")
        self.assertNotIn(".,", prompt_slots.render(up, {})["base_prompt"])

    def test_lint_names_a_character_by_its_key_not_its_place(self):
        # the first subject has nothing to say and is left out of the prompt;
        # the second one's sentence is glued wrong -- lint must blame the second
        picture = _picture({"count": {"tags": "2girls"}},
                           [{"key": "空"}, {"key": "林", "look_tags": "black hair", "moment_prose": "She waits"}])
        out = prompt_slots.render(picture, {})
        self.assertEqual([c["key"] for c in out["character_prompts"]], ["林"])
        self.assertEqual(prompt_slots.lint(out, picture), [])
        broken = {**out, "character_prompts": [{**out["character_prompts"][0], "prompt": "black hair, She waits., x"}]}
        self.assertIn("character 林: a comma after a stop", prompt_slots.lint(broken, picture))

    def test_a_chinese_library_line_is_named_in_the_check(self):
        picture = _picture({"count": {"tags": "1girl"}}, [{"from": "林", "use": ["k1"]}])
        notes = prompt_slots.check(picture, {"林": {"k1": "1girl, 黑发"}}, None,
                                   {"海边": {"s1": "painterly, 明亮的夏日海边日光"}})
        self.assertIn("「林」的行k1里有中文：行只写英文tag，中文描述给绘图AI的brief", notes)
        self.assertIn("「海边」的行s1里有中文：行只写英文tag，中文描述给绘图AI的brief", notes)

    def test_used_entries_is_the_pictures_own_account(self):
        snap = {"settings": [{"title": "黄昏", "tags": [{"id": "s1", "part": "画风", "tags": "painterly"},
                                                       {"id": "s2", "part": "天色", "tags": "sunset"}]},
                             {"title": "海边", "tags": [{"id": "s1", "part": "沙滩", "tags": "beach"}]}],
                "characters": [{"name": "林", "tags": [{"id": "k1", "part": "身份", "tags": "1girl"}],
                                "variants": [{"title": "校服", "tags": [{"id": "v1", "part": "衬衫", "tags": "white shirt"}]}]},
                               {"name": "阿岚", "tags": [{"id": "k1", "part": "身份", "tags": "1boy"}], "variants": []}]}
        picture = _picture({"count": {"tags": "1girl"}}, [{"from": "林", "use": ["k1", "v1"], "patches": [{"line": "v1", "add": "wet"}]}])
        picture["settings"] = [{"from": "黄昏", "use": ["s2"], "patches": []}]
        used = prompt_slots.used_entries(picture, snap)
        # the entry that is on but not in the picture (阿岚, 海边) is not "used"
        self.assertEqual(used, [
            {"kind": "character", "name": "林", "lines": ["身份", "校服·衬衫"], "patches": 1},
            {"kind": "setting", "name": "黄昏", "lines": ["画风", "天色"], "patches": 0},
        ])

    def test_prose_changes_by_whole_sentences(self):
        picture = _picture({"scene": {"tags": "street", "prose": "The street is empty. A lamp glows."}},
                           [{"look_tags": "black hair", "moment_prose": "She waits."}])
        out = prompt_slots.apply(picture, {
            "scene": {"prose": {"remove": "the street is empty", "add": "One cloud hangs in the sky."}},
            "characters": [{"from": "主体1", "moment": {"prose": {"add": "She looks up."}}}],
        })
        self.assertEqual(out["slots"]["scene"]["prose"], "A lamp glows. One cloud hangs in the sky.")
        self.assertEqual(out["characters"][0]["moment"]["prose"], "She waits. She looks up.")
        self.assertEqual(prompt_slots.lint(prompt_slots.render(out, {}), out), [])
        with self.assertRaises(prompt_slots.PatchRejected):
            prompt_slots.apply(picture, {"scene": {"prose": {"replace": "x"}}})

    def test_two_subjects_without_coordinates_are_noted(self):
        picture = _picture({"count": {"tags": "2girls"}}, [{"key": "林", "look_tags": "black hair"},
                                                           {"key": "阿岚", "look_tags": "red hair"}])
        notes = prompt_slots.check(picture, {})
        self.assertTrue(any("没给center坐标" in n and "林" in n and "阿岚" in n for n in notes), notes)
        picture["characters"][0]["center_x"] = picture["characters"][0]["center_y"] = 0.3
        picture["characters"][1]["center_x"] = picture["characters"][1]["center_y"] = 0.7
        self.assertFalse(any("center" in n for n in prompt_slots.check(picture, {})))

    def test_lint_names_each_fault(self):
        faults = prompt_slots.lint({
            "base_prompt": "1girl, solo, She waits., Seen from above, 1girl",
            "character_prompts": [{"prompt": ""}, {"prompt": "black hair,, standing ,"}],
            "negative_prompt": "lowres,  text",
        })
        self.assertIn("base: a comma after a stop", faults)
        self.assertIn("base: repeated tag 「1girl」", faults)
        self.assertIn("character 1: empty", faults)
        self.assertIn("character 2: an empty item between commas", faults)
        self.assertIn("character 2: a separator at an end", faults)
        self.assertIn("character 2: a space before a comma", faults)
        self.assertIn("negative: a doubled space", faults)

    def test_chinese_is_a_bad_answer_except_as_sign_text(self):
        picture = _picture({"scene": {"tags": "street", "prose": "她站在街上"}},
                           [{"look_tags": "black hair", "moment_prose": "她背对镜头"}], text="深夜书店")
        self.assertEqual(prompt_slots.foreign(picture), ["scene.prose", "主体1.moment.prose"])
        with self.assertRaises(llm.ModelError):
            llm._reject_foreign(picture)
        picture["slots"]["scene"]["prose"] = "She stands in the street"
        picture["characters"][0]["moment"]["prose"] = "Her back is to the camera"
        self.assertEqual(prompt_slots.foreign(picture), [])
        llm._reject_foreign(picture)
        out = prompt_slots.render(picture, {})
        self.assertTrue(out["base_prompt"].endswith("深夜书店"))
        self.assertEqual(prompt_slots.lint(out, picture), [])
        self.assertIn("base: not English", prompt_slots.lint(out))
