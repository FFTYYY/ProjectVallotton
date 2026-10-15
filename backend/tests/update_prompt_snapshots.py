"""
Regenerate tests/fixtures/prompt_*.txt: the assembled system prompts with
the ORIGINAL 「使用场景」 texts (tests_prompt_scenario.ORIGINAL_*). Run after editing lead.txt,
drawing.txt or tag_lines.txt on purpose:

    cd backend && python tests/update_prompt_snapshots.py
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vallotton.settings")
os.environ.setdefault("DJANGO_DB", "sqlite")

import django  # noqa: E402

django.setup()

from django.test import override_settings  # noqa: E402

from core import prompts  # noqa: E402
from tests.tests_prompt_scenario import ORIGINAL_DRAW, ORIGINAL_LEAD  # noqa: E402

with override_settings(LEAD_USAGE_SCENARIO=ORIGINAL_LEAD, DRAW_USAGE_SCENARIO=ORIGINAL_DRAW):
    for name, text in (("lead", prompts.lead()), ("drawing", prompts.drawing()),
                       ("drawing_edit", prompts.drawing() + "\n\n" + prompts.drawing_edit())):
        (HERE / "fixtures" / f"prompt_{name}.txt").write_text(text, encoding="utf-8", newline="\n")
        print(f"prompt_{name}.txt: {len(text)} chars")
