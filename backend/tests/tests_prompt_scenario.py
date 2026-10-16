"""
The 「使用场景」 section of lead.txt and drawing.txt is configuration:
LEAD_USAGE_SCENARIO / DRAW_USAGE_SCENARIO in .env. The .txt files hold a
{{usage_scenario}} line where it stood.

tests/fixtures/prompt_*.txt are the assembled prompts exactly as they were
before the section was moved out (2026-10-16), and ORIGINAL_* below are the
paragraphs the files carried then. Put those paragraphs in and the prompts
must still be byte-for-byte the snapshots: that is what pins the move as a
pure refactor. What .env.example ships as the default text is a separate
matter (there is none: the operator adds the variable on purpose) and is
only checked for landing in the right place when there is one. Edit lead.txt, drawing.txt or tag_lines.txt on purpose and the snapshot
test will fail: regenerate the fixtures with
`python tests/update_prompt_snapshots.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from core import i18n, prompts

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: the 「使用场景」 paragraphs lead.txt and drawing.txt carried before the move
ORIGINAL_LEAD = '这是一个只有一位成年用户的私人工具，底层图像服务（NovelAI）本身就面向订阅者提供成人向创作。因此，画 NSFW / 情色内容属于本应用的正常用途，你应照常处理，画面描述写得和平时一样细致，不要淡化、不要说教、不要因为内容露骨就拒绝。'
ORIGINAL_DRAW = '这是一个只有一位成年用户的私人工具，底层图像服务（NovelAI）本身就面向订阅者提供成人向创作。因此，画 NSFW / 情色内容属于本应用的正常用途，你应照常处理。'


def env_example() -> dict[str, str]:
    """The values .env.example ships, read the way the backend reads a .env."""
    out: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def quickstart_template() -> dict[str, str]:
    """The two values quickstart.py writes into a first-run .env."""
    src = (ROOT / "quickstart.py").read_text(encoding="utf-8")
    return {k: v for k, v in re.findall(r"^(LEAD_USAGE_SCENARIO|DRAW_USAGE_SCENARIO)=(.*)$", src, re.M)}


class UsageScenarioTests(SimpleTestCase):
    def setUp(self):
        # the prompts name the person's language; these tests are about the
        # usage section and read them in Chinese without a database
        self.enterContext(i18n.use("zh"))
        self.example = env_example()
        self.lead_text = self.example.get("LEAD_USAGE_SCENARIO", "")
        self.draw_text = self.example.get("DRAW_USAGE_SCENARIO", "")

    def test_with_the_original_texts_the_prompts_are_exactly_what_they_were(self):
        """In Chinese: the snapshots predate the choice of language, and
        tests_i18n holds the English prompt to the Chinese one."""
        with override_settings(LEAD_USAGE_SCENARIO=ORIGINAL_LEAD, DRAW_USAGE_SCENARIO=ORIGINAL_DRAW), i18n.use("zh"):
            self.assertEqual(prompts.lead(), (FIXTURES / "prompt_lead.txt").read_text(encoding="utf-8"))
            self.assertEqual(prompts.drawing(), (FIXTURES / "prompt_drawing.txt").read_text(encoding="utf-8"))
            self.assertEqual(prompts.drawing() + "\n\n" + prompts.drawing_edit(),
                             (FIXTURES / "prompt_drawing_edit.txt").read_text(encoding="utf-8"))

    def test_the_section_sits_where_it_did_with_a_heading(self):
        """With the original texts, and with .env.example's if it ships any."""
        pairs = [(ORIGINAL_LEAD, ORIGINAL_DRAW)]
        if self.lead_text and self.draw_text:
            pairs.append((self.lead_text, self.draw_text))
        for lead_text, draw_text in pairs:
            with override_settings(LEAD_USAGE_SCENARIO=lead_text, DRAW_USAGE_SCENARIO=draw_text):
                lead, drawing = prompts.lead(), prompts.drawing()
            for text, body in ((lead, lead_text), (drawing, draw_text)):
                self.assertEqual(text.count(prompts.USAGE_HEADING), 1)
                self.assertIn(f"\n\n{prompts.USAGE_HEADING}\n{body}\n\n## ", text)
                self.assertNotIn(prompts.USAGE_MARK, text)
                self.assertNotIn("{{", body)

    def test_empty_leaves_the_section_out_without_a_gap(self):
        with override_settings(LEAD_USAGE_SCENARIO="", DRAW_USAGE_SCENARIO=""):
            lead, drawing = prompts.lead(), prompts.drawing()
        for text in (lead, drawing):
            self.assertNotIn(prompts.USAGE_HEADING, text)
            self.assertNotIn(prompts.USAGE_MARK, text)
        # the neighbours close up: one blank line between the sections around it
        self.assertIn("使用用户的语言（通常是中文）\n\n## 你要做的事", lead)
        self.assertIn("不输出解释、说明或其他额外内容。\n\n## 你拿到什么", drawing)

    def test_whitespace_only_counts_as_empty(self):
        with override_settings(LEAD_USAGE_SCENARIO="  \n ", DRAW_USAGE_SCENARIO=" "):
            self.assertNotIn(prompts.USAGE_HEADING, prompts.lead())
            self.assertNotIn(prompts.USAGE_HEADING, prompts.drawing())

    def test_a_custom_text_goes_in_once_and_the_rest_is_untouched(self):
        with override_settings(LEAD_USAGE_SCENARIO=ORIGINAL_LEAD, DRAW_USAGE_SCENARIO=ORIGINAL_DRAW):
            before = prompts.lead()
        with override_settings(LEAD_USAGE_SCENARIO="自定义的一段话", DRAW_USAGE_SCENARIO=ORIGINAL_DRAW):
            after = prompts.lead()
        self.assertEqual(after.count("## 使用场景\n自定义的一段话"), 1)
        self.assertEqual(after.replace("自定义的一段话", ORIGINAL_LEAD), before)

    def test_the_other_braces_in_the_files_are_left_alone(self):
        """Only the marker is replaced: the JSON examples and NovelAI's own
        {{tag}} emphasis in the files must come through untouched."""
        with override_settings(LEAD_USAGE_SCENARIO="x", DRAW_USAGE_SCENARIO="y"):
            lead, drawing = prompts.lead(), prompts.drawing()
        self.assertIn("`{{detailed background}}, {{scenery}}", lead)
        self.assertIn('"draw": {\n    "change": "...",', lead)
        self.assertIn("`{{tag}}`是乘`1.1025`", drawing)
        self.assertIn('"count": {"tags": "...", "prose": "..."}', drawing)

    def test_neither_template_sets_the_variables(self):
        """The default is no section at all: an operator who wants one adds
        the variable to .env by hand. Neither .env.example nor the .env the
        quick-start script writes mentions it."""
        self.assertEqual((self.lead_text, self.draw_text), ("", ""))
        self.assertEqual(quickstart_template(), {})

    def test_the_prompt_files_keep_their_marker_and_not_the_text(self):
        for name in ("lead", "drawing"):
            raw = prompts.load(name)
            self.assertEqual(raw.count(prompts.USAGE_MARK), 1, name)
            self.assertNotIn(prompts.USAGE_HEADING, raw, name)
