"""
quickstart.py speaks the language --lang chose (Chinese unless told), and the
Chinese it speaks is the Chinese it spoke before there was a choice.

tests/fixtures/quickstart_zh.json is every Chinese-bearing string constant of
quickstart.py (f-string parts included) as it stood the day the English table
was added (2026-10-16). Each must survive whole: in a value of the Chinese
table, or in the code around the table (the two log files keep their Chinese
names on disk, and the docstrings are documentation, not output). Change a
Chinese sentence on purpose and change the fixture with it. The English table
is held to the Chinese one by key and by {field}, and may not contain Chinese.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import string
import sys
from pathlib import Path
from types import ModuleType

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SOURCE = ROOT / "quickstart.py"

#: CJK ideographs, CJK punctuation (「」、。) and full-width forms
CJK = re.compile(r"[⺀-鿿豈-﫿＀-￯　-〿]")

#: the only Chinese allowed outside the table: names of files on disk
LOG_NAMES = ["quickstart-前端.log", "quickstart-后端.log"]


def load_quickstart() -> ModuleType:
    """quickstart.py is a script at the repo root, not a package: import it
    from there, fresh, so LANG starts at its default."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.modules.pop("quickstart", None)
    return importlib.import_module("quickstart")


def fields(template: str) -> list[str]:
    """The {names} a template takes, format specs stripped. Sorted: they are
    filled by keyword, so a sentence may put them in any order."""
    return sorted(f for _, f, _, _ in string.Formatter().parse(template) if f is not None)


def table_assignment(tree: ast.Module) -> ast.stmt:
    """The statement that defines TEXT at module level."""
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == "TEXT" for t in targets):
            return node
    raise AssertionError("quickstart.py has no module-level TEXT")


def docstring_nodes(tree: ast.Module) -> set[int]:
    """The ids of the string constants that are docstrings (module, class, function)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def chinese_strings(tree: ast.AST) -> set[str]:
    """Every string constant under `tree` that carries CJK; f-string parts are
    constants of their own, so they are found too."""
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and CJK.search(node.value)}


class QuickstartLanguageTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.qs = load_quickstart()
        cls.src = SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)
        cls.table = table_assignment(cls.tree)

    def setUp(self) -> None:
        # setattr: a module's attributes are not declared, so pyright refuses
        # the plain assignment
        setattr(self.qs, "LANG", "zh")

    # ---- the tables

    def test_the_table_has_every_language_and_every_language_has_every_key(self):
        self.assertEqual(sorted(self.qs.TEXT), sorted(self.qs.LANGS))
        self.assertEqual(self.qs.LANGS[0], "zh")
        for code, table in self.qs.TEXT.items():
            self.assertEqual(sorted(table), sorted(self.qs.TEXT["zh"]), code)

    def test_every_key_takes_the_same_fields_in_every_language(self):
        zh = self.qs.TEXT["zh"]
        for code, table in self.qs.TEXT.items():
            for key, template in table.items():
                self.assertIsInstance(template, str, f"{code}.{key}")
                self.assertEqual(fields(template), fields(zh[key]), f"{code}.{key}")

    def test_english_has_no_chinese_and_is_not_the_chinese(self):
        zh = self.qs.TEXT["zh"]
        for key, value in self.qs.TEXT["en"].items():
            self.assertIsNone(CJK.search(value), f"{key}: {value!r}")
            if CJK.search(zh[key]):
                self.assertNotEqual(value, zh[key], key)

    # ---- the Chinese is what it was

    def test_every_chinese_string_of_before_survives_whole(self):
        pinned = json.loads((FIXTURES / "quickstart_zh.json").read_text(encoding="utf-8"))
        self.assertEqual(pinned, sorted(set(pinned)), "the fixture is sorted and has no duplicates")
        self.assertTrue(pinned)
        zh_values = list(self.qs.TEXT["zh"].values())
        # the code around the table: the source with the table's lines cut out
        start, end = self.table.lineno, self.table.end_lineno or self.table.lineno
        lines = self.src.splitlines(keepends=True)
        remaining = "".join(lines[:start - 1] + lines[end:])
        lost = [s for s in pinned if not any(s in v for v in zh_values) and s not in remaining]
        self.assertEqual(lost, [])

    def test_no_chinese_remains_outside_the_table_but_the_log_names(self):
        inside = chinese_strings(self.table)
        skip = docstring_nodes(self.tree)
        outside = sorted({node.value for node in ast.walk(self.tree)
                          if isinstance(node, ast.Constant) and isinstance(node.value, str)
                          and CJK.search(node.value) and id(node) not in skip
                          and node.value not in inside})
        self.assertEqual(outside, sorted(LOG_NAMES))
        self.assertTrue(inside)

    def test_the_log_files_keep_their_names_in_every_language(self):
        for name in LOG_NAMES:
            self.assertIn(f'"{name}"', self.src)
        for table in self.qs.TEXT.values():
            for value in table.values():
                for name in LOG_NAMES:
                    self.assertNotIn(name, value)

    # ---- choosing the language

    def test_the_language_is_read_from_the_command_line_in_each_form(self):
        read = self.qs.lang_from_argv
        self.assertEqual(read(["--lang", "en"]), "en")
        self.assertEqual(read(["--lang=en"]), "en")
        self.assertEqual(read(["--plain", "--lang", "zh", "--port", "1"]), "zh")
        self.assertEqual(read([]), "zh")
        self.assertEqual(read(["--plain"]), "zh")
        self.assertEqual(read(["--lang"]), "zh")
        self.assertEqual(read(["--lang", "klingon"]), "zh")
        self.assertEqual(read(["--lang=klingon"]), "zh")
        self.assertEqual(read(["--language", "en"]), "zh")
        self.assertEqual(read(["--lang", "--plain"]), "zh")

    def test_t_speaks_the_current_language_and_fills_the_fields(self):
        qs = self.qs
        self.assertEqual(qs.LANG, "zh")
        self.assertEqual(qs.T("env_written", file="x"), "已写入 x")
        self.assertEqual(qs.T("ask_with_default", prompt="p", default="d"), "  p（默认 d）：")
        self.assertEqual(qs.T("db_existing", convs="", mb=12.44), "已有 · 12.4 MB")
        setattr(qs, "LANG", "en")
        self.assertEqual(qs.T("env_written", file="x"), "written to x")
        self.assertEqual(qs.T("today_usd", usd=1.5), " · today $1.50")
        self.assertEqual(qs.T("env_template", deepseek="a", novelai="b", username="c", password="d",
                              admin_password="e", secret="f").count("\n"), qs.TEXT["zh"]["env_template"].count("\n"))

    def test_the_env_file_it_writes_has_the_same_keys_in_every_language(self):
        keys = {}
        for code, table in self.qs.TEXT.items():
            text = table["env_template"].format(deepseek="a", novelai="b", username="c", password="d",
                                                admin_password="e", secret="f")
            keys[code] = [line.partition("=")[0] for line in text.splitlines() if "=" in line and not line.startswith("#")]
        self.assertEqual(keys["en"], keys["zh"])
        self.assertIn("DJANGO_SECRET_KEY", keys["zh"])
